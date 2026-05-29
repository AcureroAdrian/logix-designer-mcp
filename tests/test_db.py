import sqlite3
from pathlib import Path

from logix_mcp import db
from logix_mcp.workspace import ingest_l5x


SIMPLE_L5X = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="34.03" TargetName="Demo" TargetType="Controller">
  <Controller Use="Target" Name="Demo" ProcessorType="1756-L85E" MajorRev="34" MinorRev="11">
    <Tags>
      <Tag Name="Start_PB" TagType="Base" DataType="BOOL"/>
      <Tag Name="Motor_Run" TagType="Base" DataType="BOOL"/>
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine">
        <Routines>
          <Routine Name="MainRoutine" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N">
                <Text><![CDATA[XIC(Start_PB)OTE(Motor_Run);]]></Text>
              </Rung>
            </RLLContent>
          </Routine>
        </Routines>
      </Program>
    </Programs>
    <Tasks>
      <Task Name="MainTask" Type="CONTINUOUS">
        <ScheduledPrograms><ScheduledProgram Name="MainProgram"/></ScheduledPrograms>
      </Task>
    </Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _workspace(tmp_path: Path) -> Path:
    source = tmp_path / "demo.L5X"
    out = tmp_path / "demo.logix"
    source.write_text(SIMPLE_L5X, encoding="utf-8")
    ingest_l5x(source, out)
    return out


def test_index_built_with_base_symbol_column_and_indexes(tmp_path: Path):
    workspace = _workspace(tmp_path)
    assert db.has_index(workspace)

    with db.connect(workspace) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(xrefs)")}
        assert "base_symbol" in columns
        index_names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_xrefs_base" in index_names
        assert "idx_xrefs_routine" in index_names
        assert "idx_edges_source" in index_names


def test_find_references_underscore_tag_is_literal(tmp_path: Path):
    workspace = _workspace(tmp_path)

    refs = db.find_references(workspace, "Motor_Run")
    assert refs, "expected references for Motor_Run"
    assert all(ref["symbol"].startswith("Motor_Run") for ref in refs)
    # The underscore must be treated literally, not as a LIKE wildcard.
    assert any(ref["symbol"] == "Motor_Run" and ref["access"] == "write" for ref in refs)


def test_routine_context_returns_units_and_xrefs(tmp_path: Path):
    workspace = _workspace(tmp_path)

    context = db.routine_context(workspace, program="MainProgram", routine="MainRoutine")
    assert context is not None
    assert context["routine"]["name"] == "MainRoutine"
    assert context["units"], "expected RLL rung units"
    symbols = {(ref["symbol"], ref["access"]) for ref in context["xrefs"]}
    assert ("Motor_Run", "write") in symbols
    assert ("Start_PB", "read") in symbols


def test_routine_context_missing_returns_none(tmp_path: Path):
    workspace = _workspace(tmp_path)
    assert db.routine_context(workspace, program="Nope", routine="Nope") is None


def test_get_entity_round_trips_routine(tmp_path: Path):
    workspace = _workspace(tmp_path)
    entity = db.get_entity(workspace, "Program:MainProgram.Routine:MainRoutine")
    assert entity is not None
    assert entity.get("id") == "Program:MainProgram.Routine:MainRoutine"


def test_find_references_matches_jsonl_fallback(tmp_path: Path):
    workspace = _workspace(tmp_path)

    db_refs = db.find_references(workspace, "Motor_Run")
    # Reproduce the historical JSONL scan and compare the matched symbol set.
    from logix_mcp.workspace import read_jsonl

    target = "motor_run"
    jsonl_refs = [
        row
        for row in read_jsonl(workspace, "xrefs.jsonl")
        if row.get("symbol", "").lower() == target
        or row.get("symbol", "").lower().startswith(target + ".")
        or row.get("symbol", "").lower().startswith(target + "[")
    ]
    assert {r["symbol"] for r in db_refs} == {r["symbol"] for r in jsonl_refs}
