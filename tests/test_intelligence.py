from pathlib import Path

from logix_mcp.cli import main
from logix_mcp.intelligence import (
    aoi_instance_bindings,
    cross_reference,
    decode_summary,
    exists,
    get_fbd_sheet,
    get_operand_context,
    get_routine_slice,
    resolve_alarm,
    search_project,
    scope_metadata,
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
    <AddOnInstructionDefinitions>
      <AddOnInstructionDefinition Name="Motor_AOI" Revision="1.0">
        <Parameters>
          <Parameter Name="Cmd" TagType="Base" DataType="BOOL" Usage="Input" Required="true"/>
          <Parameter Name="Feedback" TagType="Base" DataType="BOOL" Usage="Input" Required="true"/>
          <Parameter Name="Out" TagType="Base" DataType="BOOL" Usage="Output" Required="true"/>
        </Parameters>
        <Routines>
          <Routine Name="Logic" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N"><Text><![CDATA[XIC(Cmd)OTE(Out);]]></Text></Rung>
            </RLLContent>
          </Routine>
        </Routines>
      </AddOnInstructionDefinition>
    </AddOnInstructionDefinitions>
    <Tags>
      <Tag Name="Start_PB" TagType="Base" DataType="BOOL"/>
      <Tag Name="Motor_Run" TagType="Base" DataType="BOOL"/>
      <Tag Name="Motor_AOI_01" TagType="Base" DataType="Motor_AOI"/>
      <Tag Name="Motor_AOI_Out" TagType="Base" DataType="BOOL"/>
      <Tag Name="Pump" TagType="Base" DataType="PUMP_UDT"/>
      <Tag Name="Alarm_A" TagType="Base" DataType="BOOL"/>
      <Tag Name="Alarm_B" TagType="Base" DataType="BOOL"/>
      <Tag Name="Summary_Alarm" TagType="Base" DataType="BOOL"/>
      <Tag Name="AlarmTag" TagType="Base" DataType="ALARM_DIGITAL" ExternalAccess="Read/Write">
        <Data Format="Alarm">
          <AlarmDigitalParameters Severity="500" AckRequired="true" AssocTag1="Alarm_A" AssocTag2="SPACE"/>
          <AlarmConfig>
            <Messages>
              <Message Type="AM">
                <Text Lang="en-US"><![CDATA[Alarm A active]]></Text>
              </Message>
            </Messages>
            <AlarmClass><![CDATA[MCC]]></AlarmClass>
          </AlarmConfig>
        </Data>
      </Tag>
    </Tags>
    <Programs>
      <Program Name="Main" MainRoutineName="R_Main">
        <Routines>
          <Routine Name="R_Main" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N"><Text><![CDATA[XIC(Pump.Status)OTE(Motor_Run);]]></Text></Rung>
              <Rung Number="1" Type="N"><Text><![CDATA[XIC(Alarm_A)XIC(Alarm_B)OTE(Summary_Alarm);]]></Text></Rung>
            </RLLContent>
          </Routine>
          <Routine Name="FbdLogic" Type="FBD">
            <FBDContent>
              <Sheet Number="1">
                <IRef ID="0" X="100" Y="120" Operand="Start_PB"/>
                <OCon ID="10" X="220" Y="120" Name="StartNet"/>
                <ICon ID="11" X="320" Y="120" Name="StartNet"/>
                <ORef ID="1" X="620" Y="120" Operand="Motor_Run"/>
                <AddOnInstruction Name="Motor_AOI" ID="20" X="420" Y="220" Operand="Motor_AOI_01">
                  <OutputParameter Name="Out" Argument="Motor_AOI_Out"/>
                </AddOnInstruction>
                <ORef ID="21" X="620" Y="220" Operand="Motor_AOI_Out"/>
                <Wire FromID="0" ToID="10"/>
                <Wire FromID="11" ToID="1"/>
                <Wire FromID="0" ToID="20" ToParam="Cmd"/>
                <Wire FromID="20" FromParam="Out" ToID="21"/>
              </Sheet>
            </FBDContent>
          </Routine>
          <Routine Name="Dispatch" Type="FBD">
            <FBDContent>
              <Sheet Number="1">
                <JSR ID="0" X="660" Y="100" Routine="Cooling_PID"/>
              </Sheet>
            </FBDContent>
          </Routine>
          <Routine Name="EmptyFbd" Type="FBD">
            <FBDContent>
              <Sheet Number="1"/>
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
    assert any("Motor_Run := net \"StartNet\"" in eq for eq in fbd_paths[0]["pseudo_equations"])
    assert any("net \"StartNet\" := Start_PB" in eq for eq in fbd_paths[0]["pseudo_equations"])


def test_get_fbd_sheet_returns_pseudo_equations_and_aoi_unwired(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = get_fbd_sheet(workspace, program="Main", routine="FbdLogic", sheet=1)

    assert result["found"] is True
    assert result["status"] == "ok"
    assert result["routine"]["id"] == "Program:Main.Routine:FbdLogic"
    assert result["sheet"]["number"] == "1"
    assert "Start_PB" in result["source_tags"]
    assert "Motor_Run" in result["output_tags"]
    assert any("net \"StartNet\" := Start_PB" in eq for eq in result["equations"])
    assert any("Motor_Run := net \"StartNet\"" in eq for eq in result["equations"])
    assert any("Motor_AOI_01.Cmd := Start_PB" in eq for eq in result["equations"])
    assert any("Motor_AOI_01.Feedback := UNWIRED" in eq for eq in result["equations"])
    assert any("Motor_AOI_Out := Motor_AOI_01.Out" in eq for eq in result["equations"])
    assert result["aoi_instances"][0]["summary"]["required_unwired"] == ["Feedback"]
    assert any(row["param"] == "Feedback" for row in result["unwired_pins"])


def test_get_fbd_sheet_renders_jsr_dispatch_as_call(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = get_fbd_sheet(workspace, program="Main", routine="Dispatch", sheet=1)

    assert result["status"] == "ok"
    assert result["summary"]["jsr_calls"] == 1
    assert "CALL Cooling_PID" in result["equations"]


def test_get_fbd_sheet_warns_instead_of_ok_on_empty_sheet(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = get_fbd_sheet(workspace, program="Main", routine="EmptyFbd", sheet=1)

    assert result["status"] == "empty_sheet"
    assert any("No FBD nodes were extracted" in str(limit) for limit in result["limits"])


def test_triage_issue_returns_plc_first_bundle(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = triage_issue(workspace, "HMI shows Motor_Run red on MCC screen")

    assert "needs_hmi_export_or_runtime" in result["limits"]
    assert "needs_hmi_export_or_runtime" in result["scope"]["limits"]
    assert "plc_l5x_logic" in result["scope"]["available"]
    assert "Motor_Run" in result["likely_tags"]


def test_scope_metadata_marks_hmi_and_runtime_limits(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = scope_metadata(workspace, "Plate cooler shows running on HMI with breaker off")

    assert result["controller"] == "Demo"
    assert "needs_hmi_export_or_runtime" in result["limits"]
    assert "needs_runtime_or_field_state" in result["limits"]
    assert result["coverage"]["p0_missing"] == []
    assert any(row["name"] == "fbd_graph" and row["available"] for row in result["available_evidence"])
    assert any(row["name"] == "factorytalk_hmi_export" and not row["available"] for row in result["unavailable_evidence"])


def test_resolve_alarm_returns_source_tags_and_messages(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = resolve_alarm(workspace, "AlarmTag")
    summary_result = resolve_alarm(workspace, "Summary_Alarm")

    assert result["alarms"][0]["alarm"]["tag_name"] == "AlarmTag"
    assert result["alarms"][0]["source_tags"] == ["Alarm_A"]
    assert result["alarms"][0]["messages"][0]["text"] == "Alarm A active"
    assert summary_result["summary_decode"]["status"] == "ok"


def test_decode_summary_expands_writer_members(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = decode_summary(workspace, "Summary_Alarm")
    symbols = {row["symbol"] for row in result["members"]}

    assert result["status"] == "ok"
    assert {"Alarm_A", "Alarm_B"} <= symbols
    assert any(row["alarms"] for row in result["members"] if row["symbol"] == "Alarm_A")


def test_aoi_instance_bindings_lists_unwired_required_pins(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = aoi_instance_bindings(workspace, "Motor_AOI_01")
    bindings = {row["param"]: row for row in result["instances"][0]["bindings"]}

    assert result["found"] is True
    assert bindings["Cmd"]["wired"] is True
    assert bindings["Cmd"]["sources"] == ["Start_PB"]
    assert bindings["Out"]["wired"] is True
    assert bindings["Out"]["argument"] == "Motor_AOI_Out"
    assert bindings["Feedback"]["wired"] is False
    assert result["instances"][0]["summary"]["required_unwired"] == ["Feedback"]


def test_cli_compact_commands_smoke(tmp_path: Path, capsys):
    workspace = _workspace(tmp_path)

    assert main(["search", str(workspace), "Motor_Run", "--limit", "2"]) == 0
    assert '"items"' in capsys.readouterr().out
    assert main(["xref", str(workspace), "Motor_Run", "--destructive"]) == 0
    assert '"destructive": true' in capsys.readouterr().out
    assert main(["trace", str(workspace), "Motor_Run"]) == 0
    assert '"pseudo_equations"' in capsys.readouterr().out
    assert main(["fbd-sheet", str(workspace), "--program", "Main", "--routine", "FbdLogic", "--sheet", "1"]) == 0
    assert '"equations"' in capsys.readouterr().out
    assert main(["scope", str(workspace), "HMI", "red"]) == 0
    assert '"needs_hmi_export_or_runtime"' in capsys.readouterr().out
    assert main(["resolve-alarm", str(workspace), "AlarmTag"]) == 0
    assert '"source_tags"' in capsys.readouterr().out
    assert main(["decode-summary", str(workspace), "Summary_Alarm"]) == 0
    assert '"members"' in capsys.readouterr().out
    assert main(["aoi-bindings", str(workspace), "Motor_AOI_01"]) == 0
    assert '"required_unwired"' in capsys.readouterr().out
    assert main(["sdk-status"]) == 0
    assert '"optional_fail_closed"' in capsys.readouterr().out
    assert main(["simulate-runtime", str(workspace), "--tag", "Motor_Run", "--samples", "2", "--signal", "square"]) == 0
    assert '"simulate_runtime_tag_stream"' in capsys.readouterr().out
    assert main(["runtime-summary", str(workspace)]) == 0
    assert '"simulated_sdk_runtime"' in capsys.readouterr().out
    assert main(["runtime-evidence", str(workspace), "--tag", "Motor_Run", "--limit", "1"]) == 0
    assert '"Motor_Run"' in capsys.readouterr().out
