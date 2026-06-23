"""Context-budget tests: no default tool call may flood the model's context.

Before these guards a default ``list_routines()`` against the Arnold workspace
serialized 2.8M characters (~700K tokens).
"""

import asyncio
import json
from pathlib import Path

import pytest

from logix_mcp.intelligence import coverage_limits, get_routine_slice
from logix_mcp.server import create_server, envelope, module_context_view, probe_envelope, resolve_alias, summarize_row
from logix_mcp.workspace import ingest_l5x, read_jsonl

from test_parser_workspace import SIMPLE_L5X


BUDGET_CHARS = 50_000

# Tool name -> default arguments. Every call here must stay under BUDGET_CHARS.
DEFAULT_CALLS = [
    ("project_summary", {}),
    ("coverage_report", {}),
    ("list_tags", {}),
    ("list_udts", {}),
    ("list_programs", {}),
    ("list_routines", {}),
    ("list_aois", {}),
    ("list_modules", {}),
    ("list_entities", {}),
    ("get_tag_context", {"name": "StartPB"}),
    ("get_aoi_context", {"name": "MOTOR_AOI"}),
    ("get_module_context", {"module": "Local"}),
    ("aoi_instance_bindings", {"instance": "Motor_AOI_01"}),
    ("run_diagnostics", {}),
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARNOLD_WORKSPACE = PROJECT_ROOT / "Arnold_0058_029_062226.logix"


@pytest.fixture(scope="module")
def fixture_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source = tmp_path_factory.mktemp("budget") / "demo.L5X"
    source.write_text(SIMPLE_L5X, encoding="utf-8")
    out = source.parent / "demo.logix"
    ingest_l5x(source, out)
    return out


def _payload_size(payload: object) -> int:
    def fallback(obj: object) -> object:
        dump = getattr(obj, "model_dump", None)
        return dump() if callable(dump) else str(obj)

    return len(json.dumps(payload, default=fallback))


def _call(server, name: str, arguments: dict) -> object:
    return asyncio.run(server.call_tool(name, arguments))


def _json_call(server, name: str, arguments: dict) -> dict:
    result = _call(server, name, arguments)
    if isinstance(result, tuple):
        result = result[0]
    assert result and hasattr(result[0], "text")
    return json.loads(result[0].text)


def _arnold_deep_calls(workspace: Path) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    tag_counts: dict[str, int] = {}
    for row in read_jsonl(workspace, "xrefs.jsonl"):
        symbol = row.get("base_symbol") or row.get("symbol")
        if symbol:
            tag_counts[str(symbol)] = tag_counts.get(str(symbol), 0) + 1
    if tag_counts:
        tag = max(tag_counts, key=tag_counts.get)
        calls.append(("get_tag_context", {"name": tag}))

    aois = read_jsonl(workspace, "aoi_definitions.jsonl")
    if aois:
        calls.append(("get_aoi_context", {"name": aois[0]["name"]}))

    point_counts: dict[str, int] = {}
    for row in read_jsonl(workspace, "module_io_points.jsonl"):
        module = row.get("module")
        if module:
            point_counts[str(module)] = point_counts.get(str(module), 0) + 1
    modules = read_jsonl(workspace, "modules.jsonl")
    if point_counts:
        calls.append(("get_module_context", {"module": max(point_counts, key=point_counts.get)}))
    elif modules:
        calls.append(("get_module_context", {"module": modules[0]["name"]}))

    for row in read_jsonl(workspace, "fbd_nodes.jsonl"):
        if row.get("node_type") == "AddOnInstruction" and row.get("operand"):
            calls.append(("aoi_instance_bindings", {"instance": row["operand"]}))
            break
    return calls


def _sfc_fixture() -> str:
    sfc_routine = """
          <Routine Name="Seq" Type="SFC">
            <SFCContent>
              <Step ID="0" X="100" Y="120" Operand="Step_000" InitialStep="true"/>
              <Transition ID="1" X="100" Y="240" Operand="Tran_000">
                <Condition>
                  <STContent>
                    <Line Number="0"><![CDATA[StartPB]]></Line>
                  </STContent>
                </Condition>
              </Transition>
              <DirectedLink FromID="0" ToID="1" Show="true"/>
            </SFCContent>
          </Routine>
"""
    return SIMPLE_L5X.replace('          <Routine Name="Calc" Type="ST">', sfc_routine + '          <Routine Name="Calc" Type="ST">', 1)


def _force_sfc_coverage_gap(workspace: Path) -> None:
    path = workspace / "ir" / "coverage.json"
    coverage = json.loads(path.read_text(encoding="utf-8"))
    surface = coverage["surfaces"]["sfc_nodes"]
    surface["source_count"] = max(int(surface.get("source_count") or 0), 2)
    surface["covered_count"] = 1
    surface["missing_count"] = 1
    coverage["missing"]["P0"] = sorted(set((coverage["missing"].get("P0") or []) + ["sfc_nodes"]))
    path.write_text(json.dumps(coverage, indent=2) + "\n", encoding="utf-8")


def test_default_tool_calls_stay_within_budget_on_fixture(fixture_workspace: Path):
    server = create_server(fixture_workspace)
    oversized = []
    for name, arguments in DEFAULT_CALLS:
        size = _payload_size(_call(server, name, arguments))
        if size >= BUDGET_CHARS:
            oversized.append(f"{name}: {size} chars")
    assert not oversized, "Tools over budget: " + ", ".join(oversized)


def test_default_tool_calls_stay_within_budget_on_arnold():
    if not (ARNOLD_WORKSPACE / "index" / "logix.sqlite").exists():
        pytest.skip("Arnold workspace not present")

    from logix_mcp import db

    try:
        with db.connect(ARNOLD_WORKSPACE):
            pass
    except db.WorkspaceSchemaError:
        pytest.skip("Arnold workspace was built with an older schema; re-ingest first")

    server = create_server(ARNOLD_WORKSPACE)
    oversized = []
    for name, arguments in DEFAULT_CALLS:
        size = _payload_size(_call(server, name, arguments))
        if size >= BUDGET_CHARS:
            oversized.append(f"{name}: {size} chars")
    # The historical worst offenders, with explicit defaults, must stay bounded.
    for name, arguments in [
        ("get_routine_context", {"program": "Drv_Cooling", "routine": "main"}),
        ("get_aoi", {"name": "BREAKER"}),
        ("get_fbd_sheet", {"program": "Drv_Cooling", "routine": "main", "sheet": "1"}),
        *_arnold_deep_calls(ARNOLD_WORKSPACE),
    ]:
        size = _payload_size(_call(server, name, arguments))
        if size >= BUDGET_CHARS:
            oversized.append(f"{name}: {size} chars")
    assert not oversized, "Tools over budget: " + ", ".join(oversized)


def test_deep_tool_defaults_report_size_and_do_not_spill(fixture_workspace: Path):
    server = create_server(fixture_workspace)
    for name, arguments in DEFAULT_CALLS:
        if name not in {"get_tag_context", "get_aoi_context", "get_module_context", "aoi_instance_bindings"}:
            continue
        result = _json_call(server, name, arguments)
        assert result["detail"] == "summary"
        assert isinstance(result["result_size"], int)
        assert result["result_size"] < BUDGET_CHARS
        assert result["truncated"] in {False, True}
        assert result.get("spilled") is None
    assert not (fixture_workspace.parent / ".tmp" / "logix_mcp_spill").exists()


def test_module_summary_keeps_io_tags_in_one_place():
    result, truncated = module_context_view(
        {
            "module": {"kind": "module", "name": "Slot1", "io_tags": [{"role": "Input"}]},
            "ports": [],
            "connections": [{"name": "Input", "io_tags": [{"role": "Input"}]}],
            "io_tags": [{"role": "Input", "direction": "input", "data_type": "BOOL"}],
            "io_points": [],
        },
        "summary",
    )

    assert truncated is False
    assert "io_tags" not in result["module"]
    assert "io_tags" not in result["connections"][0]
    assert result["io_tags"] == [{"role": "Input", "direction": "input", "data_type": "BOOL"}]


def test_alias_conflicts_fail_clearly(fixture_workspace: Path):
    assert resolve_alias("symbol", "Motor_A", name="Motor_A") == "Motor_A"
    conflict = _json_call(
        create_server(fixture_workspace),
        "cross_reference",
        {"symbol": "StartPB", "name": "MotorRun"},
    )

    assert conflict["error"] == "Conflicting aliases"
    assert conflict["values"] == {"symbol": "StartPB", "name": "MotorRun"}


def test_sfc_coverage_limits_are_targeted(tmp_path: Path, fixture_workspace: Path):
    source = tmp_path / "sfc_demo.L5X"
    source.write_text(_sfc_fixture(), encoding="utf-8")
    workspace = tmp_path / "sfc_demo.logix"
    ingest_l5x(source, workspace)
    _force_sfc_coverage_gap(workspace)

    assert any("sfc_nodes" in limit for limit in coverage_limits(workspace, "sfc"))
    sfc_slice = get_routine_slice(workspace, program="MainProgram", routine="Seq")
    rll_slice = get_routine_slice(fixture_workspace, program="MainProgram", routine="MainRoutine")
    sfc_context = _json_call(create_server(workspace), "get_routine_context", {"program": "MainProgram", "routine": "Seq"})

    assert any("sfc_nodes" in limit for limit in sfc_slice["limits"])
    assert any("sfc_nodes" in limit for limit in sfc_context["limits"])
    assert "limits" not in rll_slice


def test_envelope_reports_totals_and_truncation():
    rows = [{"name": f"Tag_{i}", "members": [1, 2, 3]} for i in range(10)]

    result = envelope(rows, limit=3, offset=8)

    assert result["total"] == 10
    assert [item["name"] for item in result["items"]] == ["Tag_8", "Tag_9"]
    assert result["has_more"] is False
    assert result["truncated"] == 0

    first_page = envelope(rows, limit=3)
    assert first_page["has_more"] is True
    assert first_page["truncated"] == 7
    assert first_page["items"][0]["members_count"] == 3
    assert "members" not in first_page["items"][0]


def test_probe_envelope_never_lies_about_has_more():
    # Helpers that fetch offset+limit+1 rows cannot know the true total; the
    # envelope must say has_more=True from the probe row and total=None.
    rows = [{"name": f"Tag_{i}"} for i in range(6)]

    probed = probe_envelope(rows, limit=5, offset=0)
    assert probed["has_more"] is True
    assert probed["total"] is None
    assert len(probed["items"]) == 5

    exact = probe_envelope(rows[:4], limit=5, offset=0)
    assert exact["has_more"] is False
    assert len(exact["items"]) == 4


def test_summarize_row_drops_bulky_fields_and_clips_text():
    row = {
        "name": "R10",
        "body": "X" * 10_000,
        "attributes": {"a": 1},
        "st_lines": ["line"] * 500,
        "comment": "Y" * 500,
        "unit_count": 12,
    }

    summary = summarize_row(row)

    assert "body" not in summary
    assert "attributes" not in summary
    assert summary["st_lines_count"] == 500
    assert len(summary["comment"]) <= 203
    assert summary["unit_count"] == 12
