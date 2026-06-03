from pathlib import Path

from logix_mcp.cli import main
from logix_mcp.intelligence import (
    cross_reference,
    exists,
    get_operand_context,
    get_routine_slice,
    search_project,
    trace_signal,
    triage_issue,
)
from logix_mcp.workspace import ingest_l5x


INTELLIGENCE_L5X = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="34.03" TargetName="Demo" TargetType="Controller">
  <Controller Use="Target" Name="Demo" ProcessorType="1756-L85E" MajorRev="34" MinorRev="11">
    <DataTypes>
      <DataType Name="PUMP_UDT" Family="NoFamily" Class="User">
        <Members>
          <Member Name="Status" DataType="BOOL" Dimension="0" Radix="Decimal" ExternalAccess="Read/Write"/>
        </Members>
      </DataType>
    </DataTypes>
    <Tags>
      <Tag Name="Start_PB" TagType="Base" DataType="BOOL"/>
      <Tag Name="Motor_Run" TagType="Base" DataType="BOOL"/>
      <Tag Name="Pump" TagType="Base" DataType="PUMP_UDT"/>
    </Tags>
    <Programs>
      <Program Name="Main" MainRoutineName="R_Main">
        <Routines>
          <Routine Name="R_Main" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N"><Text><![CDATA[XIC(Pump.Status)OTE(Motor_Run);]]></Text></Rung>
            </RLLContent>
          </Routine>
          <Routine Name="FbdLogic" Type="FBD">
            <FBDContent>
              <Sheet Number="1">
                <IRef ID="0" X="100" Y="120" Operand="Start_PB"/>
                <OCon ID="10" X="220" Y="120" Name="StartNet"/>
                <ICon ID="11" X="320" Y="120" Name="StartNet"/>
                <ORef ID="1" X="620" Y="120" Operand="Motor_Run"/>
                <Wire FromID="0" ToID="10"/>
                <Wire FromID="11" ToID="1"/>
              </Sheet>
            </FBDContent>
          </Routine>
        </Routines>
      </Program>
    </Programs>
    <Tasks>
      <Task Name="MainTask" Type="CONTINUOUS">
        <ScheduledPrograms><ScheduledProgram Name="Main"/></ScheduledPrograms>
      </Task>
    </Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _workspace(tmp_path: Path) -> Path:
    source = tmp_path / "intelligence.L5X"
    out = tmp_path / "intelligence.logix"
    source.write_text(INTELLIGENCE_L5X, encoding="utf-8")
    ingest_l5x(source, out)
    return out


def test_search_project_and_exists_are_compact(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = search_project(workspace, "Motor_Run", limit=5)

    assert result["total"] >= 1
    assert result["items"]
    assert "snippet" in result["items"][0]
    assert exists(workspace, "Motor_Run")["exists"] is True
    assert exists(workspace, "DefinitelyMissingTag")["exists"] is False


def test_cross_reference_modes_and_destructive_filter(tmp_path: Path):
    workspace = _workspace(tmp_path)

    exact = cross_reference(workspace, "Pump.Status", mode="exact")
    base = cross_reference(workspace, "Pump", mode="base")
    destructive = cross_reference(workspace, "Motor_Run", mode="exact", destructive=True)

    assert exact["total"] == 1
    assert exact["rows"][0]["symbol"] == "Pump.Status"
    assert base["total"] >= exact["total"]
    assert destructive["rows"][0]["destructive"] is True
    assert destructive["rows"][0]["snippet"]


def test_operand_context_and_routine_slice(tmp_path: Path):
    workspace = _workspace(tmp_path)

    context = get_operand_context(workspace, "Pump.Status")
    fbd_slice = get_routine_slice(workspace, program="Main", routine="FbdLogic", sheet=1)
    query_slice = get_routine_slice(workspace, program="Main", routine="R_Main", query="Pump.Status")

    assert context["found"] is True
    assert context["base_symbol"] == "Pump"
    assert fbd_slice["items"][0]["nodes"]
    assert any(node.get("connector_name") == "StartNet" for node in fbd_slice["items"][0]["nodes"])
    assert query_slice["items"][0]["text"] == "XIC(Pump.Status)OTE(Motor_Run);"


def test_trace_signal_follows_fbd_named_connector(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = trace_signal(workspace, "Motor_Run")

    assert result["status"] == "ok"
    fbd_paths = [path for path in result["paths"] if path["type"] == "fbd"]
    assert fbd_paths
    text = str(fbd_paths[0]["path"])
    assert "StartNet" in text
    assert "Start_PB" in text


def test_triage_issue_returns_plc_first_bundle(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = triage_issue(workspace, "HMI shows Motor_Run red on MCC screen")

    assert "needs_hmi_export_or_runtime" in result["limits"]
    assert "Motor_Run" in result["likely_tags"]


def test_cli_compact_commands_smoke(tmp_path: Path, capsys):
    workspace = _workspace(tmp_path)

    assert main(["search", str(workspace), "Motor_Run", "--limit", "2"]) == 0
    assert '"items"' in capsys.readouterr().out
    assert main(["xref", str(workspace), "Motor_Run", "--destructive"]) == 0
    assert '"destructive": true' in capsys.readouterr().out
    assert main(["trace", str(workspace), "Motor_Run"]) == 0
    assert '"paths"' in capsys.readouterr().out
