import hashlib
from pathlib import Path

from logix_mcp.parser import parse_l5x
from logix_mcp.workspace import ingest_l5x, inspect_workspace, read_jsonl


SIMPLE_L5X = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="34.03" TargetName="Demo" TargetType="Controller">
  <Controller Use="Target" Name="Demo" ProcessorType="1756-L85E" MajorRev="34" MinorRev="11" LastModifiedDate="Today">
    <DataTypes>
      <DataType Name="MOTOR_UDT" Family="NoFamily" Class="User">
        <Members>
          <Member Name="Run" DataType="BOOL" Dimension="0" Radix="Decimal" ExternalAccess="Read/Write"/>
        </Members>
      </DataType>
    </DataTypes>
    <AddOnInstructionDefinitions>
      <AddOnInstructionDefinition Name="MOTOR_AOI" Revision="1.0">
        <Parameters>
          <Parameter Name="Enable" TagType="Base" DataType="BOOL" Usage="Input" Required="true"/>
        </Parameters>
        <Routines>
          <Routine Name="Logic" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N">
                <Text><![CDATA[XIC(Enable)OTE(Done);]]></Text>
              </Rung>
            </RLLContent>
          </Routine>
        </Routines>
      </AddOnInstructionDefinition>
    </AddOnInstructionDefinitions>
    <Tags>
      <Tag Name="StartPB" TagType="Base" DataType="BOOL" Radix="Decimal" ExternalAccess="Read/Write"/>
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine">
        <Tags>
          <Tag Name="MotorRun" TagType="Base" DataType="BOOL" Radix="Decimal" ExternalAccess="Read/Write"/>
        </Tags>
        <Routines>
          <Routine Name="MainRoutine" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N">
                <Text><![CDATA[XIC(StartPB)OTE(MotorRun);]]></Text>
              </Rung>
            </RLLContent>
          </Routine>
          <Routine Name="Calc" Type="ST">
            <STContent>
              <Line Number="0"><![CDATA[MotorRun := StartPB;]]></Line>
            </STContent>
          </Routine>
        </Routines>
      </Program>
    </Programs>
    <Modules>
      <Module Name="Local" CatalogNumber="1756-L85E" ParentModule="Local">
        <Ports><Port Id="1" Address="192.168.1.10" Type="Ethernet"/></Ports>
      </Module>
    </Modules>
    <Tasks>
      <Task Name="MainTask" Type="CONTINUOUS">
        <ScheduledPrograms><ScheduledProgram Name="MainProgram"/></ScheduledPrograms>
      </Task>
    </Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _source_fragment_l5x() -> str:
    fixture = SIMPLE_L5X.replace(
        "    <Tags>\n      <Tag Name=\"StartPB\"",
        """    <RedundancyInfo Enabled=\"true\"/>
    <Trends/>
    <Tags>
      <Tag Name=\"MSG1\" TagType=\"Base\" DataType=\"MESSAGE\" ExternalAccess=\"Read/Write\">
        <MessageParameters MessageType=\"CIP Generic\" ServiceCode=\"16#4C\" ObjectType=\"CIP\" Class=\"4\" Instance=\"101\" Attribute=\"3\" ConnectionPath=\"2,192.168.1.20\" DestinationTag=\"MSG_REPLY\"/>
      </Tag>
      <Tag Name=\"StartPB\"""",
        1,
    )
    fixture = fixture.replace(
        "      <Program Name=\"MainProgram\" MainRoutineName=\"MainRoutine\">\n        <Tags>",
        """      <Program Name=\"MainProgram\" MainRoutineName=\"MainRoutine\">
        <ChildPrograms><ChildProgram Name=\"ChildProgramA\"/></ChildPrograms>
        <Tags>""",
        1,
    )
    return fixture.replace(
        "        <Ports><Port Id=\"1\" Address=\"192.168.1.10\" Type=\"Ethernet\"/></Ports>",
        """        <Ports><Port Id=\"1\" Address=\"192.168.1.10\" Type=\"Ethernet\"/></Ports>
        <ConfigTag>
          <EngineeringUnits><EngineeringUnit Operand=\"[0]\">PSI</EngineeringUnit></EngineeringUnits>
        </ConfigTag>
        <ExtendedProperties>
          <public><Vendor>ProSoft</Vendor><CatNum>MVI56E-MNETC</CatNum></public>
          <PL>ProfileLevelText<Version Name=\"1.2\"/><Connection Name=\"Input\" Format=\"DINT\"/></PL>
          <DataTypeFormats><DataTypeFormat Type=\"Input\" InstanceApplicationPath=\"Local:1:I\" Format=\"DINT\"/></DataTypeFormats>
        </ExtendedProperties>""",
        1,
    )


def test_parse_l5x_extracts_project_shapes(tmp_path: Path):
    source = tmp_path / "demo.L5X"
    source.write_text(SIMPLE_L5X, encoding="utf-8")

    project = parse_l5x(source)

    assert project["controller"]["name"] == "Demo"
    assert project["counts"]["data_types"] == 1
    assert project["counts"]["aois"] == 1
    assert project["counts"]["programs"] == 1
    assert project["counts"]["routines"] == 3
    assert project["counts"]["modules"] == 1
    assert any(ref["symbol"] == "MotorRun" and ref["access"] == "write" for ref in project["xrefs"])


def test_ingest_l5x_materializes_workspace(tmp_path: Path):
    source = tmp_path / "demo.L5X"
    out = tmp_path / "demo.logix"
    source.write_text(SIMPLE_L5X, encoding="utf-8")

    result = ingest_l5x(source, out)

    assert result["project"]["controller"]["name"] == "Demo"
    assert (out / "ai" / "overview.md").exists()
    assert (out / "ir" / "symbols.jsonl").exists()
    assert (out / "index" / "logix.sqlite").exists()
    symbols = read_jsonl(out, "symbols.jsonl")
    assert any(row["name"] == "StartPB" for row in symbols)


def test_inspect_workspace_exposes_short_source_fingerprint(tmp_path: Path):
    source = tmp_path / "demo.L5X"
    out = tmp_path / "demo.logix"
    source.write_text(SIMPLE_L5X, encoding="utf-8")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()[:12]

    result = ingest_l5x(source, out)
    inspected = inspect_workspace(out)

    assert result["project"]["source_fingerprint"] == expected
    assert result["project"]["identity"]["fingerprint"] == expected
    assert inspected["source_fingerprint"] == expected
    assert inspected["identity"]["export_date"] == result["project"]["root"].get("ExportDate")


def test_coverage_gate_passes_for_fully_handled_l5x(tmp_path: Path):
    source = tmp_path / "demo.L5X"
    source.write_text(SIMPLE_L5X, encoding="utf-8")

    coverage = parse_l5x(source)["coverage"]

    assert coverage["surfaces"]["unknown_elements"]["missing_count"] == 0
    assert coverage["surfaces"]["unextracted_elements"]["missing_count"] == 0
    assert coverage["missing"]["P0"] == []


def test_coverage_gate_flags_unknown_and_unextracted_elements(tmp_path: Path):
    # An element the pipeline has never heard of must turn the P0 semaphore red.
    # Known P1 elements are no longer treated as lost when source fragments
    # preserve them, even if some are only raw-preserved.
    fixture = SIMPLE_L5X.replace(
        "<Tags>\n      <Tag Name=\"StartPB\"",
        "<RedundancyInfo Enabled=\"true\"/>\n    <Trends/>\n    <FutureWidget Mode=\"x\"/>\n    <Tags>\n      <Tag Name=\"StartPB\"",
        1,
    )
    source = tmp_path / "demo.L5X"
    source.write_text(fixture, encoding="utf-8")

    coverage = parse_l5x(source)["coverage"]
    unknown = coverage["surfaces"]["unknown_elements"]
    unextracted = coverage["surfaces"]["unextracted_elements"]

    assert unknown["missing"] == [{"element": "FutureWidget", "count": 1}]
    assert "unknown_elements" in coverage["missing"]["P0"]
    assert unextracted["missing_count"] == 0
    assert any(
        entry["element"] == "RedundancyInfo" and entry["semantic_covered_count"] == 1
        for entry in unextracted["elements"]
    )
    assert any(
        entry["element"] == "Trends" and entry["raw_preserved_count"] == 1
        for entry in unextracted["elements"]
    )
    assert "unextracted_elements" not in coverage["missing"]["P1"]


def test_known_source_fragments_are_normalized_or_raw_preserved(tmp_path: Path):
    source = tmp_path / "demo.L5X"
    source.write_text(_source_fragment_l5x(), encoding="utf-8")

    project = parse_l5x(source)
    coverage = project["coverage"]["surfaces"]["unextracted_elements"]

    assert coverage["missing_count"] == 0
    assert coverage["semantic_covered_count"] >= 7
    assert coverage["raw_preserved_count"] >= 2
    assert project["controller_metadata"][0]["element"] == "RedundancyInfo"
    assert project["message_parameters"][0]["tag_name"] == "MSG1"
    assert project["message_parameters"][0]["connection_path"] == "2,192.168.1.20"
    assert project["engineering_units"][0]["module"] == "Local"
    assert project["engineering_units"][0]["engineering_unit"] == "PSI"
    assert any(row["element"] == "PL" and row.get("version_name") == "1.2" for row in project["module_profile_fragments"])
    assert any(row["element"] == "DataTypeFormat" and row.get("instance_application_path") == "Local:1:I" for row in project["module_profile_fragments"])
    assert project["program_children"][0]["parent_program"] == "MainProgram"
    assert project["program_children"][0]["child_program"] == "ChildProgramA"
    assert any(row["element"] == "Trends" and row["coverage_mode"] == "raw_preserved" for row in project["source_fragments"])


def test_ingest_materializes_source_fragment_datasets(tmp_path: Path):
    source = tmp_path / "demo.L5X"
    out = tmp_path / "demo.logix"
    source.write_text(_source_fragment_l5x(), encoding="utf-8")

    ingest_l5x(source, out)

    assert read_jsonl(out, "source_fragments.jsonl")
    assert read_jsonl(out, "controller_metadata.jsonl")[0]["element"] == "RedundancyInfo"
    assert read_jsonl(out, "message_parameters.jsonl")[0]["tag_name"] == "MSG1"
    assert read_jsonl(out, "engineering_units.jsonl")[0]["engineering_unit"] == "PSI"
    assert read_jsonl(out, "module_profile_fragments.jsonl")
    assert read_jsonl(out, "program_children.jsonl")[0]["child_program"] == "ChildProgramA"
