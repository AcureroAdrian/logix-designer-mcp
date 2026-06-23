from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

import pytest

from logix_mcp import runtime_store


FIXED_START = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


def _sample(tag: str, value, *, cycle: int, error: str | None = None, data_type: str = "DINT") -> dict:
    return {
        "ts_utc": f"2026-06-23T12:00:{cycle:02d}Z",
        "ts_mono": float(cycle),
        "cycle": cycle,
        "tag": tag,
        "value": value,
        "type": data_type,
        "error": error,
    }


def test_create_session_writes_only_runtime_evidence_files(tmp_path: Path):
    manifest = runtime_store.create_session(
        tmp_path,
        session_id="session_001",
        created_at=FIXED_START,
        workspace_fingerprint="workspace-fp",
        controller_identity={"name": "DemoPLC", "serial": "1234"},
        comm_path_fingerprint="comm-fp",
        mode="ONLINE",
        source="pycomm3",
        interval_ms=100,
        requested_tags=["Timer.ACC", "Timer.ACC", "Motor_Run"],
        limits={"max_tags": 32},
        permission={"approved_by": "Adrian Acurero"},
        retention_policy={"ttl_seconds": 300},
        pid=1234,
    )

    assert manifest["schema_version"] == 1
    assert manifest["session_id"] == "session_001"
    assert manifest["requested_tags"] == ["Timer.ACC", "Motor_Run"]

    root = tmp_path / "runtime_evidence" / "sessions"
    assert (root / "session_001.manifest.json").exists()
    assert (root / "session_001.samples.jsonl").exists()
    assert (root / "session_001.state.json").exists()
    assert not (tmp_path / "ir").exists()
    assert not (tmp_path / "source").exists()
    assert not (tmp_path / "index").exists()
    assert not (tmp_path / "ai").exists()

    assert runtime_store.read_manifest(tmp_path, "session_001")["comm_path_fingerprint"] == "comm-fp"
    state = runtime_store.read_state(tmp_path, "session_001")
    assert state["status"] == "running"
    assert state["pid"] == 1234

    with pytest.raises(ValueError):
        runtime_store.create_session(tmp_path, session_id="../bad")


def test_append_samples_and_session_summary_are_compact(tmp_path: Path):
    runtime_store.create_session(tmp_path, session_id="summary", created_at=FIXED_START, requested_tags=["Timer.ACC"])
    giant = {"blob": "X" * 5_000, "items": list(range(50))}
    runtime_store.append_samples(
        tmp_path,
        "summary",
        [
            _sample("Timer.ACC", 10, cycle=0),
            _sample("Timer.ACC", 10, cycle=1),
            _sample("Timer.ACC", 12, cycle=2),
            _sample("Timer.ACC", 8, cycle=3),
            _sample("Timer.ACC", 8, cycle=4, error="timeout"),
            _sample("Big.UDT", giant, cycle=5, data_type="UDT"),
        ],
    )

    raw_lines = (tmp_path / "runtime_evidence" / "sessions" / "summary.samples.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 6
    assert set(json.loads(raw_lines[0])) == {"ts_utc", "ts_mono", "cycle", "tag", "value", "type", "error"}

    state = runtime_store.read_state(tmp_path, "summary")
    assert state["counts"]["samples"] == 6
    assert state["counts"]["errors"] == 1

    summary = runtime_store.session_summary(tmp_path, "summary")
    timer = next(item for item in summary["tags"] if item["tag"] == "Timer.ACC")
    assert timer["sample_count"] == 5
    assert timer["min"] == 8
    assert timer["max"] == 12
    assert timer["first_value"] == 10
    assert timer["last_value"] == 8
    assert timer["n_changes"] == 3
    assert timer["first_change_at"] == "2026-06-23T12:00:02Z"
    assert timer["errors"] == 1
    assert summary["sample_count"] == 6
    assert summary["error_count"] == 1

    serialized = json.dumps(summary)
    assert "X" * 1_000 not in serialized
    assert len(serialized) < 5_000


def test_list_sessions_uses_envelope_and_compact_manifest_preview(tmp_path: Path):
    many_tags = [f"Tag_{index}" for index in range(25)]
    runtime_store.create_session(tmp_path, session_id="older", created_at="2026-06-23T12:00:00Z", requested_tags=["A"])
    runtime_store.create_session(tmp_path, session_id="newer", created_at="2026-06-23T12:01:00Z", requested_tags=many_tags)
    runtime_store.write_state(tmp_path, "older", {"status": "completed", "started_at": "2026-06-23T12:00:00Z"})

    result = runtime_store.list_sessions(tmp_path, limit=1)

    assert set(result) == {"items", "total", "offset", "limit", "has_more", "truncated"}
    assert result["total"] == 2
    assert result["limit"] == 1
    assert result["has_more"] is True
    assert result["truncated"] == 1
    assert result["items"][0]["session_id"] == "newer"
    assert result["items"][0]["requested_tags_count"] == 25
    assert len(result["items"][0]["requested_tags_preview"]) == runtime_store.MAX_COMPACT_COLLECTION_ITEMS
    assert result["items"][0]["requested_tags_truncated"] == 5

    completed = runtime_store.list_sessions(tmp_path, status="completed")
    assert completed["total"] == 1
    assert completed["items"][0]["session_id"] == "older"


def test_read_stream_slice_downsamples_and_preserves_endpoints(tmp_path: Path):
    runtime_store.create_session(tmp_path, session_id="slice", created_at=FIXED_START, requested_tags=["Ramp"])
    runtime_store.append_samples(tmp_path, "slice", [_sample("Ramp", index, cycle=index) for index in range(20)])

    result = runtime_store.read_stream_slice(tmp_path, "slice", tag="Ramp", max_points=5)

    assert result["total"] == 20
    assert result["limit"] == 5
    assert result["has_more"] is True
    assert result["truncated"] == 15
    assert result["downsampled"] is True
    assert len(result["items"]) == 5
    assert result["items"][0]["value"] == 0
    assert result["items"][-1]["value"] == 19

    filtered = runtime_store.read_stream_slice(
        tmp_path,
        "slice",
        tag="Ramp",
        start_ts_utc="2026-06-23T12:00:05Z",
        end_ts_utc="2026-06-23T12:00:07Z",
        max_points=10,
    )
    assert [item["value"] for item in filtered["items"]] == [5, 6, 7]
    assert filtered["has_more"] is False


def test_runtime_change_points_only_returns_value_or_error_transitions(tmp_path: Path):
    runtime_store.create_session(tmp_path, session_id="changes", created_at=FIXED_START, requested_tags=["Mode"])
    runtime_store.append_samples(
        tmp_path,
        "changes",
        [
            _sample("Mode", 0, cycle=0),
            _sample("Mode", 0, cycle=1),
            _sample("Mode", 1, cycle=2),
            _sample("Mode", 1, cycle=3, error="bad quality"),
            _sample("Mode", 1, cycle=4, error="bad quality"),
            _sample("Mode", 2, cycle=5),
        ],
    )

    result = runtime_store.runtime_change_points(tmp_path, "changes", tag="Mode", max_points=10)

    assert result["total"] == 3
    assert result["has_more"] is False
    assert [item["cycle"] for item in result["items"]] == [2, 3, 5]
    assert result["items"][0]["previous_value"] == 0
    assert result["items"][0]["value_changed"] is True
    assert result["items"][1]["error_changed"] is True
    assert result["items"][2]["previous_error"] == "bad quality"
