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
    # An element the pipeline has never heard of must turn the P0 semaphore
    # red; a known-but-unextracted element must show up as a documented P1 gap.
    fixture = SIMPLE_L5X.replace(
        "<Tags>\n      <Tag Name=\"StartPB\"",
        "<RedundancyInfo Enabled=\"true\"/>\n    <FutureWidget Mode=\"x\"/>\n    <Tags>\n      <Tag Name=\"StartPB\"",
        1,
    )
    source = tmp_path / "demo.L5X"
    source.write_text(fixture, encoding="utf-8")

    coverage = parse_l5x(source)["coverage"]
    unknown = coverage["surfaces"]["unknown_elements"]
    unextracted = coverage["surfaces"]["unextracted_elements"]

    assert unknown["missing"] == [{"element": "FutureWidget", "count": 1}]
    assert "unknown_elements" in coverage["missing"]["P0"]
    assert any(entry["element"] == "RedundancyInfo" and entry["note"] for entry in unextracted["missing"])
    assert "unextracted_elements" in coverage["missing"]["P1"]
