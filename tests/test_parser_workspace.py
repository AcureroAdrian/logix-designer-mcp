from pathlib import Path

from logix_mcp.parser import parse_l5x
from logix_mcp.workspace import ingest_l5x, read_jsonl


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
