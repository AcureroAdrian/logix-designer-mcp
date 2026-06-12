"""Context-budget tests: no default tool call may flood the model's context.

Before these guards a default ``list_routines()`` against the Arnold workspace
serialized 2.8M characters (~700K tokens).
"""

import asyncio
import json
from pathlib import Path

import pytest

from logix_mcp.server import create_server, envelope, probe_envelope, summarize_row
from logix_mcp.workspace import ingest_l5x

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
    ("run_diagnostics", {}),
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARNOLD_WORKSPACE = PROJECT_ROOT / "Arnold_0058_020_060926.logix"


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
    ]:
        size = _payload_size(_call(server, name, arguments))
        if size >= BUDGET_CHARS:
            oversized.append(f"{name}: {size} chars")
    assert not oversized, "Tools over budget: " + ", ".join(oversized)


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
