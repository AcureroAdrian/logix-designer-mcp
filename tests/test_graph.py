from pathlib import Path

from logix_mcp import graph
from logix_mcp.workspace import ingest_l5x


GRAPH_L5X = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="34.03" TargetName="Demo" TargetType="Controller">
  <Controller Use="Target" Name="Demo" ProcessorType="1756-L85E" MajorRev="34" MinorRev="11">
    <Tags>
      <Tag Name="Start_PB" TagType="Base" DataType="BOOL"/>
      <Tag Name="Motor_Cmd" TagType="Base" DataType="BOOL"/>
      <Tag Name="Motor_Run" TagType="Base" DataType="BOOL"/>
      <Tag Name="Speed_Ref" TagType="Base" DataType="REAL"/>
      <Tag Name="Speed_Disp" TagType="Base" DataType="REAL"/>
      <Tag Name="Speed_Disp2" TagType="Base" DataType="REAL"/>
      <Tag Name="Speed_Alias" TagType="Alias" DataType="REAL" AliasFor="Speed_Ref"/>
    </Tags>
    <Programs>
      <Program Name="Main" MainRoutineName="R_Main">
        <Routines>
          <Routine Name="R_Main" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N"><Text><![CDATA[XIC(Start_PB)OTE(Motor_Cmd);]]></Text></Rung>
              <Rung Number="1" Type="N"><Text><![CDATA[JSR(R_Calc);]]></Text></Rung>
            </RLLContent>
          </Routine>
          <Routine Name="R_Calc" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N"><Text><![CDATA[XIC(Motor_Cmd)OTE(Motor_Run);]]></Text></Rung>
              <Rung Number="1" Type="N"><Text><![CDATA[MOV(Speed_Ref,Speed_Disp);]]></Text></Rung>
              <Rung Number="2" Type="N"><Text><![CDATA[MOV(Speed_Alias,Speed_Disp2);]]></Text></Rung>
            </RLLContent>
          </Routine>
        </Routines>
      </Program>
      <Program Name="Spare">
        <Routines>
          <Routine Name="R_Idle" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N"><Text><![CDATA[NOP();]]></Text></Rung>
            </RLLContent>
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
    source = tmp_path / "graph.L5X"
    out = tmp_path / "graph.logix"
    source.write_text(GRAPH_L5X, encoding="utf-8")
    ingest_l5x(source, out)
    return out


def test_tag_producers_consumers(tmp_path: Path):
    workspace = _workspace(tmp_path)
    result = graph.tag_producers_consumers(workspace, "Motor_Cmd")

    producer_routines = {row["routine"] for row in result["producers"]}
    consumer_routines = {row["routine"] for row in result["consumers"]}
    assert producer_routines == {"R_Main"}
    assert consumer_routines == {"R_Calc"}


def test_impact_of_propagates_through_logic(tmp_path: Path):
    workspace = _workspace(tmp_path)
    result = graph.impact_of(workspace, "Start_PB", max_depth=3)

    routines = {row["routine"] for row in result["affected_routines"]}
    tags = {row["tag"] for row in result["affected_tags"]}
    assert {"R_Main", "R_Calc"} <= routines
    assert {"Motor_Cmd", "Motor_Run"} <= tags
    # Motor_Cmd is one hop out, Motor_Run two hops out.
    depth_by_tag = {row["tag"]: row["depth"] for row in result["affected_tags"]}
    assert depth_by_tag["Motor_Cmd"] == 1
    assert depth_by_tag["Motor_Run"] == 2


def test_impact_depth_limit_is_respected(tmp_path: Path):
    workspace = _workspace(tmp_path)
    shallow = graph.impact_of(workspace, "Start_PB", max_depth=1)
    tags = {row["tag"] for row in shallow["affected_tags"]}
    assert "Motor_Cmd" in tags
    assert "Motor_Run" not in tags  # two hops away, beyond depth 1


def test_io_trace_resolves_alias_chain(tmp_path: Path):
    workspace = _workspace(tmp_path)
    result = graph.io_trace(workspace, "Speed_Ref")

    assert set(result["alias_chain"]) == {"Speed_Ref", "Speed_Alias"}
    assert result["alias_root"] == "Speed_Ref"
    assert "R_Calc" in {row.get("routine") for row in result["routines"]}


def test_call_graph_routine_callers_and_callees(tmp_path: Path):
    workspace = _workspace(tmp_path)
    view = graph.call_graph(workspace, routine="R_Main")

    assert view["found"] is True
    assert {"type": "routine", "callee": "R_Calc"} in view["callees"]

    callee_view = graph.call_graph(workspace, routine="R_Calc")
    caller_routines = {row.get("routine") for row in callee_view["callers"]}
    assert "R_Main" in caller_routines


def test_call_graph_scheduling_tree_flags_unscheduled(tmp_path: Path):
    workspace = _workspace(tmp_path)
    tree = graph.call_graph(workspace)

    task_names = {row["task"] for row in tree["tasks"]}
    assert "MainTask" in task_names
    assert "Spare" in tree["unscheduled_programs"]
    assert "Main" not in tree["unscheduled_programs"]
