import json

from logix_mcp.xrefs import (
    aoi_signature_from_parameters,
    classify_ladder_instruction,
    detect_st_assignments,
    extract_operands_from_neutral_text,
    extract_references,
    parse_ladder_instructions,
    xrefs_from_ladder_neutral_text,
    xrefs_from_structured_text,
)


def test_extract_rll_references_classifies_common_instructions():
    text = "XIC(StartPB)OTE(MotorRun);MOV(SourceValue,DestValue);JSR(CheckPump,InputTag,OutputTag);"
    refs = extract_references(text, "RLL", "Program:Main.Routine:Run")
    by_symbol = {(ref["symbol"], ref["access"], ref["instruction"]) for ref in refs}

    assert ("StartPB", "read", "XIC") in by_symbol
    assert ("MotorRun", "write", "OTE") in by_symbol
    assert ("SourceValue", "read", "MOV") in by_symbol
    assert ("DestValue", "write", "MOV") in by_symbol
    assert ("CheckPump", "call", "JSR") in by_symbol
    assert ("InputTag", "read_write", "JSR") in by_symbol


def test_extract_st_references_detects_assignment_reads_and_writes():
    text = """
    MotorRun := StartPB AND NOT Faulted;
    SpeedCommand := Scale(InputRaw);
    """
    refs = extract_references(text, "ST", "Program:Main.Routine:Calc")
    by_symbol = {(ref["symbol"], ref["access"]) for ref in refs}

    assert ("MotorRun", "write") in by_symbol
    assert ("SpeedCommand", "write") in by_symbol
    assert ("StartPB", "read") in by_symbol
    assert ("Faulted", "read") in by_symbol
    assert ("InputRaw", "read") in by_symbol
    assert ("Scale", "read") not in by_symbol


def test_extract_operands_from_neutral_text_keeps_indexed_and_member_tags():
    text = "XIC(MotorStart)MOV(SourceTag, Pump.Speed)ADD(Pump.Speed, Array[IndexTag], Total);"

    assert extract_operands_from_neutral_text(text) == [
        "MotorStart",
        "SourceTag",
        "Pump.Speed",
        "Array[IndexTag]",
        "Total",
    ]


def test_parse_ladder_instructions_handles_nested_commas():
    text = "CPT(Result, (A + Lookup[IndexTag,OtherTag]) / Scale);"

    assert parse_ladder_instructions(text) == [
        {
            "instruction": "CPT",
            "args": ["Result", "(A + Lookup[IndexTag,OtherTag]) / Scale"],
            "span": [0, len(text) - 1],
        }
    ]


def test_ladder_json_records_classify_second_and_last_destinations():
    text = "COP(SourceArray, DestArray, LengthTag)ADD(A, B, Sum)TON(TimerTag, PresetTag, AccumTag);"

    refs = xrefs_from_ladder_neutral_text(text, routine="Program:Main.Routine:Run")
    by_symbol = {(ref["symbol"], ref["access"], ref["instruction"]) for ref in refs}

    assert ("SourceArray", "read", "COP") in by_symbol
    assert ("DestArray", "write", "COP") in by_symbol
    assert ("LengthTag", "read", "COP") in by_symbol
    assert ("A", "read", "ADD") in by_symbol
    assert ("B", "read", "ADD") in by_symbol
    assert ("Sum", "write", "ADD") in by_symbol
    assert ("TimerTag", "read_write", "TON") in by_symbol
    json.dumps(refs)


def test_detect_st_assignments_and_json_records():
    text = """
    // ignored assignment: Commented := Out;
    Pump.Cmd := StartPB AND NOT StopPB;
    Alarm := LIMIT(0, Total, 100) OR Faulted;
    """

    assignments = detect_st_assignments(text)

    assert assignments[0]["target"] == "Pump.Cmd"
    assert assignments[0]["reads"] == ["StartPB", "StopPB"]
    assert assignments[1]["target"] == "Alarm"
    assert assignments[1]["reads"] == ["Total", "Faulted"]

    refs = xrefs_from_structured_text(text, routine="Program:Main.Routine:Calc")
    assert ("Pump.Cmd", "write") in {(ref["symbol"], ref["access"]) for ref in refs}
    json.dumps(refs)


def test_instruction_signature_table_fixes_btd_cpt_and_mvm():
    text = (
        "BTD(SrcWord, 0, DestWord, 4, 8)"
        "CPT(Result, (A + B) * Scale)"
        "MVM(SourceWord, MaskWord, DestWord2);"
    )
    refs = extract_references(text, "RLL", "Program:Main.Routine:Run")
    by_symbol = {(ref["symbol"], ref["access"], ref["instruction"]) for ref in refs}

    # BTD: source reads, destination writes (previously classified "unknown").
    assert ("SrcWord", "read", "BTD") in by_symbol
    assert ("DestWord", "write", "BTD") in by_symbol
    # CPT destination is the FIRST operand, not the last.
    assert ("Result", "write", "CPT") in by_symbol
    assert ("A", "read", "CPT") in by_symbol
    # MVM destination is the THIRD operand.
    assert ("SourceWord", "read", "MVM") in by_symbol
    assert ("MaskWord", "read", "MVM") in by_symbol
    assert ("DestWord2", "write", "MVM") in by_symbol


def test_typed_instructions_report_confidence():
    classified = classify_ladder_instruction("MOV", ["Src", "Dest"])
    assert classified[0]["confidence"] == "typed"
    assert classified[1] == {"operand": "Dest", "access": "write", "confidence": "typed"}

    # An instruction not in the table stays heuristic / unknown.
    unknown = classify_ladder_instruction("ZZZ", ["A", "B"])
    assert unknown[0]["access"] == "unknown"
    assert unknown[0]["confidence"] == "heuristic"


def test_almd_classifies_alarm_tag_and_derives_inalarm_write():
    # Golden case from Arnold AUX_EQUIP/R06_ALARM Rung 0 (1756-RM003 semantics).
    text = "XIO(SWGR480.Power)ALMD(SWGR480_ALM_PowerFail,ALARMS_ACK_ALL_PB,0,0,0);"
    refs = extract_references(text, "RLL", "Program:AUX_EQUIP.Routine:R06_ALARM")
    by_symbol = {(ref["symbol"], ref["access"], ref["instruction"]) for ref in refs}

    assert ("SWGR480.Power", "read", "XIO") in by_symbol
    assert ("SWGR480_ALM_PowerFail", "read_write", "ALMD") in by_symbol
    assert ("ALARMS_ACK_ALL_PB", "read", "ALMD") in by_symbol
    # Derived member write so traces find a producer for the alarm status.
    assert ("SWGR480_ALM_PowerFail.InAlarm", "write", "ALMD") in by_symbol
    assert all(ref["access"] != "unknown" for ref in refs)


def test_alma_classifies_analog_input_as_read():
    # Golden case from Arnold AUX_EQUIP/R06_ALARM Rung 134.
    text = "CPT(G1_WINDING_TEMP_COMPT,G1_WINDING_TEMP_RAW*0.1)ALMA(G1_WINDING_TEMP,G1_WINDING_TEMP_COMPT,ALARMS_ACK_ALL_PB,0,0);"
    refs = extract_references(text, "RLL", "Program:AUX_EQUIP.Routine:R06_ALARM")
    by_symbol = {(ref["symbol"], ref["access"], ref["instruction"]) for ref in refs}

    assert ("G1_WINDING_TEMP", "read_write", "ALMA") in by_symbol
    # The analog In operand is the causal read that links CPT -> ALMA -> alarm.
    assert ("G1_WINDING_TEMP_COMPT", "read", "ALMA") in by_symbol
    assert ("G1_WINDING_TEMP_COMPT", "write", "CPT") in by_symbol
    assert ("G1_WINDING_TEMP.InAlarm", "write", "ALMA") in by_symbol
    assert all(ref["access"] != "unknown" for ref in refs)


def test_jmp_lbl_labels_produce_no_xrefs_and_size_sfr_are_typed():
    text = "JMP(END_LBL)LBL(END_LBL)SIZE(DataArray,0,ArrayLen)SFR(SFC_Main,Step_010);"
    refs = extract_references(text, "RLL", "Program:Main.Routine:Run")
    by_symbol = {(ref["symbol"], ref["access"], ref["instruction"]) for ref in refs}

    # Rung labels are jump targets, not tags.
    assert not any(ref["symbol"] == "END_LBL" for ref in refs)
    assert ("DataArray", "read", "SIZE") in by_symbol
    assert ("ArrayLen", "write", "SIZE") in by_symbol
    assert ("SFC_Main", "call", "SFR") in by_symbol
    assert ("Step_010", "read", "SFR") in by_symbol
    assert all(ref["access"] != "unknown" for ref in refs)


def test_aoi_signature_from_parameters_skips_enable_bits_and_maps_usage():
    params = [
        {"name": "EnableIn", "usage": "Input"},
        {"name": "EnableOut", "usage": "Output"},
        {"name": "Cmd", "usage": "Input"},
        {"name": "Sts", "usage": "Output"},
        {"name": "Ref", "usage": "InOut"},
    ]
    assert aoi_signature_from_parameters(params) == ["read", "write", "read_write"]


def test_aoi_call_operands_classified_from_parameter_usage():
    signatures = {"MOTOR_AOI": ["read", "write", "read_write"]}
    text = "XIC(Run)MOTOR_AOI(Motor_Inst, CmdIn, StsOut, RefTag);"
    refs = extract_references(text, "RLL", "Program:Main.Routine:Run", signatures)
    by_symbol = {(ref["symbol"], ref["access"], ref["confidence"]) for ref in refs}

    assert ("Motor_Inst", "read_write", "typed") in by_symbol  # backing/instance tag
    assert ("CmdIn", "read", "typed") in by_symbol
    assert ("StsOut", "write", "typed") in by_symbol
    assert ("RefTag", "read_write", "typed") in by_symbol
