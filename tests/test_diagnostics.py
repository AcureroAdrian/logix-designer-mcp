from pathlib import Path

from logix_mcp.diagnostics import run_diagnostics
from logix_mcp.workspace import ingest_l5x


DIAG_L5X = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="34.03" TargetName="Demo" TargetType="Controller">
  <Controller Use="Target" Name="Demo" ProcessorType="1756-L85E" MajorRev="34" MinorRev="11">
    <DataTypes>
      <DataType Name="USED_UDT" Family="NoFamily" Class="User">
        <Members><Member Name="Flag" DataType="BOOL" Dimension="0"/></Members>
      </DataType>
      <DataType Name="UNUSED_UDT" Family="NoFamily" Class="User">
        <Members><Member Name="Flag" DataType="BOOL" Dimension="0"/></Members>
      </DataType>
    </DataTypes>
    <AddOnInstructionDefinitions>
      <AddOnInstructionDefinition Name="USED_AOI" Revision="1.0">
        <Parameters><Parameter Name="Inp" TagType="Base" DataType="BOOL" Usage="Input"/></Parameters>
        <Routines/>
      </AddOnInstructionDefinition>
      <AddOnInstructionDefinition Name="UNUSED_AOI" Revision="1.0">
        <Parameters><Parameter Name="Inp" TagType="Base" DataType="BOOL" Usage="Input"/></Parameters>
        <Routines/>
      </AddOnInstructionDefinition>
    </AddOnInstructionDefinitions>
    <Tags>
      <Tag Name="Coil_Conflict" TagType="Base" DataType="BOOL"/>
      <Tag Name="Dead_Out" TagType="Base" DataType="BOOL"/>
      <Tag Name="Ghost_In" TagType="Base" DataType="BOOL"/>
      <Tag Name="Start" TagType="Base" DataType="BOOL"/>
      <Tag Name="Used_Struct" TagType="Base" DataType="USED_UDT"/>
      <Tag Name="Inst_Used" TagType="Base" DataType="USED_AOI"/>
      <Tag Name="Bad_Alias" TagType="Alias" DataType="BOOL" AliasFor="Does_Not_Exist"/>
      <Tag Name="Good_Alias" TagType="Alias" DataType="BOOL" AliasFor="Coil_Conflict"/>
    </Tags>
    <Programs>
      <Program Name="Main" MainRoutineName="R_A">
        <Routines>
          <Routine Name="R_A" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N"><Text><![CDATA[XIC(Ghost_In)OTE(Coil_Conflict);]]></Text></Rung>
              <Rung Number="1" Type="N"><Text><![CDATA[XIC(Start)OTE(Dead_Out);]]></Text></Rung>
            </RLLContent>
          </Routine>
          <Routine Name="R_B" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N"><Text><![CDATA[XIC(Start)OTE(Coil_Conflict);]]></Text></Rung>
            </RLLContent>
          </Routine>
        </Routines>
      </Program>
      <Program Name="Orphan">
        <Routines>
          <Routine Name="R_Idle" Type="RLL">
            <RLLContent><Rung Number="0" Type="N"><Text><![CDATA[NOP();]]></Text></Rung></RLLContent>
          </Routine>
        </Routines>
      </Program>
    </Programs>
    <Modules>
      <Module Name="Local" CatalogNumber="1756-L85E" ParentModule="Local">
        <Ports><Port Id="1" Address="0" Type="ICP"/></Ports>
      </Module>
      <Module Name="InhibMod" CatalogNumber="1756-IB16" ParentModule="Local" Inhibited="true">
        <Ports><Port Id="1" Address="2" Type="ICP"/></Ports>
      </Module>
    </Modules>
    <Tasks>
      <Task Name="MainTask" Type="CONTINUOUS">
        <ScheduledPrograms><ScheduledProgram Name="Main"/></ScheduledPrograms>
      </Task>
    </Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _entities_by_rule(result: dict) -> dict[str, set]:
    by_rule: dict[str, set] = {}
    for finding in result["findings"]:
        by_rule.setdefault(finding["rule"], set()).add(finding["entity"])
    return by_rule


def _run(tmp_path: Path) -> dict:
    source = tmp_path / "diag.L5X"
    out = tmp_path / "diag.logix"
    source.write_text(DIAG_L5X, encoding="utf-8")
    ingest_l5x(source, out)
    return run_diagnostics(out)


def test_multiple_output_flags_coil_written_by_two_routines(tmp_path: Path):
    result = _run(tmp_path)
    multi = [f for f in result["findings"] if f["rule"] == "multiple_output" and f["entity"] == "Coil_Conflict"]
    assert multi, "Coil_Conflict should be flagged as multiple-output"
    assert multi[0]["severity"] == "warning"  # BOOL coil
    assert multi[0]["writer_count"] == 2


def test_written_never_read_and_read_never_written(tmp_path: Path):
    by_rule = _entities_by_rule(_run(tmp_path))
    assert "Dead_Out" in by_rule.get("written_never_read", set())
    assert "Ghost_In" in by_rule.get("read_never_written", set())


def test_broken_alias_is_an_error_and_valid_alias_is_not(tmp_path: Path):
    result = _run(tmp_path)
    broken = [f for f in result["findings"] if f["rule"] == "broken_alias"]
    entities = {f["entity"] for f in broken}
    assert "Bad_Alias" in entities
    assert "Good_Alias" not in entities
    assert all(f["severity"] == "error" for f in broken)


def test_unscheduled_program_and_inhibited_module(tmp_path: Path):
    by_rule = _entities_by_rule(_run(tmp_path))
    assert "Orphan" in by_rule.get("unscheduled_program", set())
    assert "InhibMod" in by_rule.get("inhibited_or_faulted_module", set())


def test_unused_aoi_and_udt_detected_but_used_ones_are_not(tmp_path: Path):
    by_rule = _entities_by_rule(_run(tmp_path))
    assert "UNUSED_AOI" in by_rule.get("aoi_never_instantiated", set())
    assert "USED_AOI" not in by_rule.get("aoi_never_instantiated", set())
    assert "UNUSED_UDT" in by_rule.get("udt_never_used", set())
    assert "USED_UDT" not in by_rule.get("udt_never_used", set())


def test_summary_counts_are_consistent(tmp_path: Path):
    result = _run(tmp_path)
    summary = result["summary"]
    assert summary["total"] == len(result["findings"])
    assert sum(summary["by_severity"].values()) == summary["total"]
    assert sum(summary["by_rule"].values()) == summary["total"]
