from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from logix_mcp.cli import main
from logix_mcp.server import create_server
from logix_mcp.workspace import ingest_l5x

from test_parser_workspace import SIMPLE_L5X


def _workspace(tmp_path: Path) -> Path:
    source = tmp_path / "runtime_cli.L5X"
    workspace = tmp_path / "runtime_cli.logix"
    source.write_text(SIMPLE_L5X, encoding="utf-8")
    ingest_l5x(source, workspace)
    return workspace


def _json_from_stdout(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _call(server, name: str, arguments: dict) -> dict:
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[0]
    assert result and hasattr(result[0], "text")
    return json.loads(result[0].text)


def test_runtime_cli_fake_session_smoke(tmp_path: Path, capsys):
    workspace = _workspace(tmp_path)
    session_id = "cli-session"

    assert main(["runtime-read-now", str(workspace), "--path", "fake/path", "--tag", "Timer.ACC", "--source", "fake"]) == 0
    snapshot = _json_from_stdout(capsys)
    assert snapshot["operation"] == "read_tags_now"
    assert snapshot["source"] == "fake"
    assert snapshot["results"][0]["tag"] == "Timer.ACC"
    assert "fake/path" not in json.dumps(snapshot)

    assert (
        main(
            [
                "runtime-capture-start",
                str(workspace),
                "--path",
                "fake/path",
                "--tag",
                "Timer.ACC",
                "--tag",
                "Timer.DN",
                "--interval-ms",
                "100",
                "--duration-seconds",
                "0.22",
                "--source",
                "fake",
                "--session-id",
                session_id,
            ]
        )
        == 0
    )
    capture = _json_from_stdout(capsys)
    assert capture["ok"] is True
    assert capture["session_id"] == session_id
    assert capture["subprocess_mode"] is True

    status = {}
    for _ in range(30):
        assert main(["runtime-capture-status", str(workspace), "--session-id", session_id]) == 0
        status = _json_from_stdout(capsys)
        if status.get("status") == "completed":
            break
        time.sleep(0.1)
    assert status["session_id"] == session_id
    assert status["status"] == "completed"
    assert status["sample_count"] >= 2

    assert main(["runtime-sessions", str(workspace), "--limit", "1"]) == 0
    sessions = _json_from_stdout(capsys)
    assert sessions["items"][0]["session_id"] == session_id

    assert main(["runtime-summary", str(workspace), "--session-id", session_id]) == 0
    summary = _json_from_stdout(capsys)
    timer = next(item for item in summary["tags"] if item["tag"] == "Timer.ACC")
    assert timer["n_changes"] >= 1

    assert main(["runtime-slice", str(workspace), "--session-id", session_id, "--tag", "Timer.ACC", "--max-points", "2"]) == 0
    sliced = _json_from_stdout(capsys)
    assert sliced["limit"] == 2
    assert all(item["tag"] == "Timer.ACC" for item in sliced["items"])

    assert main(["runtime-change-points", str(workspace), "--session-id", session_id, "--tag", "Timer.ACC", "--limit", "5"]) == 0
    changes = _json_from_stdout(capsys)
    assert changes["total"] >= 2


def test_runtime_mcp_annotations_and_subprocess_start(tmp_path: Path):
    workspace = _workspace(tmp_path)
    server = create_server(workspace)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert tools["read_tags_now"].annotations.readOnlyHint is True
    assert tools["runtime_capture_status"].annotations.readOnlyHint is True
    assert tools["list_runtime_sessions"].annotations.readOnlyHint is True
    assert tools["start_runtime_capture"].annotations.readOnlyHint is False
    assert tools["start_runtime_capture"].annotations.destructiveHint is False
    assert tools["start_runtime_capture"].annotations.idempotentHint is False
    assert tools["stop_runtime_capture"].annotations.readOnlyHint is False
    assert tools["stop_runtime_capture"].annotations.destructiveHint is False
    assert tools["stop_runtime_capture"].annotations.idempotentHint is False

    snapshot = _call(server, "read_tags_now", {"path": "fake/path", "tags": "Timer.ACC", "source": "fake"})
    assert snapshot["ok"] is True
    assert snapshot["results"][0]["tag"] == "Timer.ACC"

    started = _call(
        server,
        "start_runtime_capture",
        {
            "path": "fake/path",
            "tags": "Timer.ACC",
            "interval_ms": 100,
            "duration_seconds": 0.22,
            "source": "fake",
        },
    )
    assert started["subprocess_mode"] is True
    assert isinstance(started["pid"], int)
    session_id = started["session_id"]

    status = {}
    for _ in range(30):
        status = _call(server, "runtime_capture_status", {"session_id": session_id})
        if status.get("status") == "completed":
            break
        time.sleep(0.1)
    assert status["session_id"] == session_id
    assert status["status"] == "completed"

    summary = _call(server, "runtime_evidence_summary", {"session_id": session_id})
    assert summary["session_id"] == session_id
    assert len(json.dumps(summary)) < 50_000

    sliced = _call(server, "read_runtime_stream_slice", {"session_id": session_id, "tag": "Timer.ACC", "max_points": 2})
    assert sliced["limit"] == 2
    assert len(json.dumps(sliced)) < 50_000
