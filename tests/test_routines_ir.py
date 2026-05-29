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
