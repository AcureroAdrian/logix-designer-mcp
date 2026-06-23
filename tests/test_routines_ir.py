import json
import xml.etree.ElementTree as ET

from logix_mcp.routines import (
    extract_fbd_units,
    extract_rll_units,
    extract_sfc_units,
    extract_st_units,
    routine_ir_from_element,
)


def _xml(text: str) -> ET.Element:
    return ET.fromstring(text)


def test_extract_rll_units_include_rungs_instructions_xrefs_and_calls():
    routine = _xml(
        """
        <Routine Name="MainRoutine" Type="RLL">
          <RLLContent>
            <Rung Number="0" Type="N">
              <Comment><![CDATA[Start the motor]]></Comment>
              <Text><![CDATA[XIC(StartPB)OTE(MotorRun);JSR(CheckPump,InputTag,OutputTag);]]></Text>
            </Rung>
          </RLLContent>
        </Routine>
        """
    )

    units = extract_rll_units(routine, routine_id="Program:Main.Routine:MainRoutine")

    assert units[0]["comment"] == "Start the motor"
    assert units[0]["instructions"][0]["instruction"] == "XIC"
    assert ("StartPB", "read") in {(ref["symbol"], ref["access"]) for ref in units[0]["xrefs"]}
    assert ("MotorRun", "write") in {(ref["symbol"], ref["access"]) for ref in units[0]["xrefs"]}
    assert ("routine", "CheckPump") in {(call["call_type"], call["callee"]) for call in units[0]["calls"]}
    json.dumps(units)


def test_extract_st_units_include_lines_numbers_xrefs_and_calls():
    routine = _xml(
        """
        <Routine Name="Calc" Type="ST">
          <STContent>
            <Line Number="10"><![CDATA[SpeedCommand := Scale(InputRaw);]]></Line>
            <Line Number="11"><![CDATA[IF Faulted THEN]]></Line>
          </STContent>
        </Routine>
        """
    )

    units = extract_st_units(routine, routine_id="Program:Main.Routine:Calc")

    assert units[0]["number"] == "10"
    assert units[0]["text"] == "SpeedCommand := Scale(InputRaw);"
    assert ("SpeedCommand", "write") in {(ref["symbol"], ref["access"]) for ref in units[0]["xrefs"]}
    assert ("InputRaw", "read") in {(ref["symbol"], ref["access"]) for ref in units[0]["xrefs"]}
    assert ("Faulted", "read") in {(ref["symbol"], ref["access"]) for ref in units[1]["xrefs"]}
    assert "Scale" in {call["callee"] for call in units[0]["calls"]}
    json.dumps(units)


def test_extract_fbd_units_include_sheets_nodes_wires_textboxes_aoi_calls_and_xrefs():
    routine = _xml(
        """
        <Routine Name="FbdLogic" Type="FBD">
          <FBDContent SheetSize="B - 11 x 17 in" SheetOrientation="Landscape">
            <Sheet Number="1">
              <IRef ID="0" X="100" Y="120" Operand="StartPB" HideDesc="false"/>
              <ORef ID="1" X="620" Y="120" Operand="MotorRun" HideDesc="false"/>
              <Block Type="MAVE" ID="2" X="300" Y="80" Operand="MAVE_01" VisiblePins="In Out">
                <Array Name="StorageArray" Operand="Storage_Array"/>
              </Block>
              <AddOnInstruction Name="Motor_AOI" ID="3" X="420" Y="160" Operand="Motor_AOI_01" VisiblePins="Cmd Sts">
                <InOutParameter Name="Sts" Argument="Motor.Status"/>
              </AddOnInstruction>
              <Wire FromID="0" ToID="3" ToParam="Cmd"/>
              <Wire FromID="3" FromParam="Out" ToID="1"/>
              <TextBox ID="4" X="20" Y="20" Width="0"><Text><![CDATA[Motor note]]></Text></TextBox>
            </Sheet>
          </FBDContent>
        </Routine>
        """
    )

    units = extract_fbd_units(routine, routine_id="Program:Main.Routine:FbdLogic")
    sheet = units[0]

    assert sheet["number"] == "1"
    assert {node["node_type"] for node in sheet["nodes"]} == {"IRef", "ORef", "Block", "AddOnInstruction"}
    assert len(sheet["wires"]) == 2
    assert sheet["textboxes"][0]["text"] == "Motor note"
    assert ("Motor_AOI", "aoi") in {(call["callee"], call["call_type"]) for call in sheet["calls"]}
    assert ("MAVE", "block") in {(call["callee"], call["call_type"]) for call in sheet["calls"]}
    assert ("StartPB", "read") in {(ref["symbol"], ref["access"]) for ref in sheet["xrefs"]}
    assert ("MotorRun", "write") in {(ref["symbol"], ref["access"]) for ref in sheet["xrefs"]}
    assert ("Motor.Status", "read_write") in {(ref["symbol"], ref["access"]) for ref in sheet["xrefs"]}
    json.dumps(routine_ir_from_element(routine, routine_id="Program:Main.Routine:FbdLogic"))


def test_extract_fbd_units_model_jsr_as_first_class_node():
    # Golden case from Arnold Drv_Cooling/main: a dispatch-only FBD sheet whose
    # entire body is JSR nodes used to look empty with no warning.
    routine = _xml(
        """
        <Routine Name="main" Type="FBD">
          <FBDContent>
            <Sheet Number="1">
              <JSR ID="0" X="660" Y="100" Routine="PID"/>
              <JSR ID="1" X="660" Y="200" Routine="Scale_Valve"/>
            </Sheet>
          </FBDContent>
        </Routine>
        """
    )

    units = extract_fbd_units(routine, routine_id="Program:Drv_Cooling.Routine:main")
    sheet = units[0]
    jsr_nodes = [node for node in sheet["nodes"] if node["node_type"] == "JSR"]

    assert [node["callee"] for node in jsr_nodes] == ["PID", "Scale_Valve"]
    assert all(node["instruction"] == "JSR" for node in jsr_nodes)
    assert ("PID", "routine") in {(call["callee"], call["call_type"]) for call in sheet["calls"]}
    assert ("Scale_Valve", "routine") in {(call["callee"], call["call_type"]) for call in sheet["calls"]}
    assert ("PID", "call", "FBD_JSR") in {(ref["symbol"], ref["access"], ref["instruction"]) for ref in sheet["xrefs"]}
    json.dumps(units)


def test_extract_fbd_units_include_named_connectors():
    routine = _xml(
        """
        <Routine Name="FbdLogic" Type="FBD">
          <FBDContent>
            <Sheet Number="1">
              <IRef ID="0" X="100" Y="120" Operand="StartPB"/>
              <OCon ID="10" X="220" Y="120" Name="StartNet"/>
              <ICon ID="11" X="320" Y="120" Name="StartNet"/>
              <ORef ID="1" X="620" Y="120" Operand="MotorRun"/>
              <Wire FromID="0" ToID="10"/>
              <Wire FromID="11" ToID="1"/>
            </Sheet>
          </FBDContent>
        </Routine>
        """
    )

    units = extract_fbd_units(routine, routine_id="Program:Main.Routine:FbdLogic")
    nodes = {node["node_type"]: node for node in units[0]["nodes"]}

    assert nodes["OCon"]["connector_name"] == "StartNet"
    assert nodes["ICon"]["connector_name"] == "StartNet"
    assert any(node["instruction"] == "StartNet" for node in units[0]["nodes"] if node["node_type"] in {"ICon", "OCon"})


def test_extract_sfc_units_include_steps_actions_transitions_branches_links_and_internal_st():
    routine = _xml(
        """
        <Routine Name="Seq" Type="SFC">
          <SFCContent SheetSize="Letter - 8.5 x 11 in" SheetOrientation="Landscape">
            <Step ID="0" X="100" Y="120" Operand="Step_000" InitialStep="true" ShowActions="true">
              <Action ID="1" Operand="Action_000" Qualifier="NonStored" IsBoolean="false">
                <Body>
                  <STContent>
                    <Line Number="0"><![CDATA[MotorRun := StartPB;]]></Line>
                  </STContent>
                </Body>
              </Action>
            </Step>
            <Transition ID="2" X="100" Y="240" Operand="Tran_000">
              <Condition>
                <STContent>
                  <Line Number="0"><![CDATA[Faulted OR Done]]></Line>
                </STContent>
              </Condition>
            </Transition>
            <Branch ID="3" Y="300" BranchType="Selection" BranchFlow="Diverge" Priority="Default">
              <Leg ID="4"/>
              <Leg ID="5"/>
            </Branch>
            <DirectedLink FromID="0" ToID="2" Show="true"/>
          </SFCContent>
        </Routine>
        """
    )

    units = extract_sfc_units(routine, routine_id="Program:Main.Routine:Seq")
    step = units[0]
    transition = units[1]

    assert step["initial_step"] is True
    assert step["actions"][0]["lines"][0]["text"] == "MotorRun := StartPB;"
    assert ("MotorRun", "write") in {
        (ref["symbol"], ref["access"]) for ref in step["actions"][0]["lines"][0]["xrefs"]
    }
    assert ("StartPB", "read") in {
        (ref["symbol"], ref["access"]) for ref in step["actions"][0]["lines"][0]["xrefs"]
    }
    assert ("Faulted", "read") in {
        (ref["symbol"], ref["access"]) for ref in transition["condition_lines"][0]["xrefs"]
    }
    assert units[2]["kind"] == "branch"
    assert [leg["id"] for leg in units[2]["legs"]] == ["4", "5"]
    assert units[3]["kind"] == "directed_link"
    assert units[3]["show"] is True
    json.dumps(routine_ir_from_element(routine, routine_id="Program:Main.Routine:Seq"))


def test_sfc_ir_preserves_multiple_content_variants_and_nested_branch_context():
    routine = _xml(
        """
        <Routine Name="Seq" Type="SFC">
          <SFCContent OnlineEditType="Original">
            <Step ID="0" X="100" Y="120" Operand="Step_000" InitialStep="true"/>
            <Branch ID="10" Y="300" BranchType="Selection" BranchFlow="Diverge">
              <Leg ID="11">
                <Step ID="12" X="200" Y="360" Operand="NestedStep"/>
              </Leg>
            </Branch>
            <DirectedLink FromID="0" ToID="12"/>
          </SFCContent>
          <SFCContent OnlineEditType="Pending">
            <Step ID="100" X="100" Y="120" Operand="PendingStep">
              <Action ID="101" Operand="PendingAction" Qualifier="NonStored">
                <Body>
                  <STContent>
                    <Line Number="0"><![CDATA[OutTag := InTag;]]></Line>
                  </STContent>
                </Body>
              </Action>
            </Step>
            <Transition ID="102" X="100" Y="240" Operand="PendingTransition">
              <Condition>
                <STContent>
                  <Line Number="0"><![CDATA[ReadyTag]]></Line>
                </STContent>
              </Condition>
            </Transition>
            <DirectedLink FromID="100" ToID="102"/>
          </SFCContent>
        </Routine>
        """
    )

    compat_units = extract_sfc_units(routine, routine_id="Program:Main.Routine:Seq")
    pending_step = next(unit for unit in compat_units if unit.get("operand") == "PendingStep")
    assert pending_step["content_index"] == 1
    assert pending_step["online_edit_type"] == "Pending"

    bundle = routine_ir_from_element(
        routine,
        routine_id="Program:Main.Routine:Seq",
        owner="Program:Main",
        program="Main",
    )
    charts = bundle["sfc_charts"]
    assert [chart["online_edit_type"] for chart in charts] == ["Original", "Pending"]
    assert [chart["node_count"] for chart in charts] == [3, 3]
    assert [chart["link_count"] for chart in charts] == [1, 1]
    assert charts[0]["branch_count"] == 1
    assert charts[0]["leg_count"] == 1

    nested_step = next(node for node in bundle["sfc_nodes"] if node.get("operand") == "NestedStep")
    assert nested_step["chart_id"] == charts[0]["chart_id"]
    assert nested_step["parent_branch_id"] == "10"
    assert nested_step["parent_leg_id"] == "11"
    assert bundle["sfc_branches"][0]["branch_id"] == "10"
    assert bundle["sfc_legs"][0]["leg_id"] == "11"

    pending_chart_id = charts[1]["chart_id"]
    pending_symbols = {
        (ref["symbol"], ref["access"], ref.get("chart_id"))
        for ref in bundle["xrefs"]
        if ref.get("chart_id") == pending_chart_id
    }
    assert ("OutTag", "write", pending_chart_id) in pending_symbols
    assert ("InTag", "read", pending_chart_id) in pending_symbols
    assert ("ReadyTag", "read", pending_chart_id) in pending_symbols
