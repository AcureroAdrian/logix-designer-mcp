"""Session-oriented runtime evidence storage for live PLC reads.

The store is intentionally local and boring: one manifest, one JSONL stream,
and one mutable state file per capture session. It never writes under the
canonical offline workspace directories (``ir/``, ``source/``, ``index/``,
or ``ai/``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
import hashlib
import json
import os
import re
import time
import uuid


JsonDict = dict[str, Any]

RUNTIME_EVIDENCE_SCHEMA_VERSION = 1
RUNTIME_EVIDENCE_DIR_NAME = "runtime_evidence"
RUNTIME_SESSIONS_DIR_NAME = "sessions"
MANIFEST_SUFFIX = ".manifest.json"
SAMPLES_SUFFIX = ".samples.jsonl"
STATE_SUFFIX = ".state.json"
STOP_SUFFIX = ".stop"

DEFAULT_LIST_LIMIT = 50
DEFAULT_STREAM_POINTS = 200
MAX_QUERY_POINTS = 1_000
MAX_COMPACT_STRING_CHARS = 300
MAX_COMPACT_COLLECTION_ITEMS = 20

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
STATE_STATUSES = frozenset({"running", "completed", "stopped", "failed"})


class RuntimeStoreError(RuntimeError):
    """A runtime evidence operation could not be completed safely."""


def create_session(
    workspace: str | Path,
    *,
    session_id: str | None = None,
    created_at: str | datetime | None = None,
    workspace_fingerprint: str | None = None,
    controller_identity: Mapping[str, Any] | None = None,
    comm_path_fingerprint: str | None = None,
    mode: str = "ONLINE",
    source: str = "pycomm3",
    interval_ms: int | None = None,
    requested_tags: Iterable[str] | None = None,
    limits: Mapping[str, Any] | None = None,
    permission: Mapping[str, Any] | None = None,
    retention_policy: Mapping[str, Any] | None = None,
    pid: int | None = None,
    status: str = "running",
) -> JsonDict:
    """Create a runtime capture session and return its manifest."""

    created = format_utc(created_at)
    safe_id = validate_session_id(session_id or _new_session_id(created))
    if status not in STATE_STATUSES:
        raise ValueError(f"Unsupported runtime session status: {status}")

    manifest_path = _session_file(workspace, safe_id, MANIFEST_SUFFIX, create=True)
    samples_path = _session_file(workspace, safe_id, SAMPLES_SUFFIX, create=True)
    state_path = _session_file(workspace, safe_id, STATE_SUFFIX, create=True)
    for path in (manifest_path, samples_path, state_path):
        if path.exists():
            raise FileExistsError(f"Runtime session already exists: {safe_id}")

    manifest: JsonDict = {
        "schema_version": RUNTIME_EVIDENCE_SCHEMA_VERSION,
        "session_id": safe_id,
        "created_at": created,
        "workspace_fingerprint": workspace_fingerprint or fingerprint_text(str(Path(workspace).resolve())),
        "controller_identity": _json_ready(dict(controller_identity or {})),
        "comm_path_fingerprint": str(comm_path_fingerprint or ""),
        "mode": str(mode),
        "source": str(source),
        "interval_ms": int(interval_ms or 0),
        "requested_tags": _normalize_tags(requested_tags or []),
        "limits": _json_ready(dict(limits or {})),
        "permission": _json_ready(dict(permission or {})),
        "retention_policy": _json_ready(dict(retention_policy or {})),
    }
    _write_json(manifest_path, manifest)
    samples_path.touch()

    state: JsonDict = {
        "session_id": safe_id,
        "status": status,
        "started_at": created,
        "updated_at": created,
        "counts": {"samples": 0, "errors": 0},
    }
    if pid is not None:
        state["pid"] = int(pid)
    _write_json(state_path, state)
    return manifest


def append_sample(workspace: str | Path, session_id: str, sample: Mapping[str, Any]) -> JsonDict:
    """Append one sample row to a session JSONL stream."""

    return append_samples(workspace, session_id, [sample])


def append_samples(workspace: str | Path, session_id: str, samples: Iterable[Mapping[str, Any]]) -> JsonDict:
    """Append sample rows to a session JSONL stream."""

    safe_id = validate_session_id(session_id)
    read_manifest(workspace, safe_id)
    path = _session_file(workspace, safe_id, SAMPLES_SUFFIX)
    normalized = [_normalize_sample(sample) for sample in samples]
    if not normalized:
        return {"ok": True, "session_id": safe_id, "written": 0, "path": str(path)}

    with path.open("a", encoding="utf-8") as handle:
        for sample in normalized:
            handle.write(json.dumps(sample, ensure_ascii=True, separators=(",", ":")) + "\n")

    state = read_state(workspace, safe_id, default_status="running")
    counts = dict(state.get("counts") or {})
    counts["samples"] = int(counts.get("samples") or 0) + len(normalized)
    counts["errors"] = int(counts.get("errors") or 0) + sum(1 for sample in normalized if _has_error(sample))
    last_cycle = next((sample.get("cycle") for sample in reversed(normalized) if sample.get("cycle") is not None), None)
    if last_cycle is not None:
        counts["last_cycle"] = last_cycle
    state["counts"] = counts
    state["updated_at"] = normalized[-1]["ts_utc"]
    write_state(workspace, safe_id, state, preserve_updated_at=True)
    return {"ok": True, "session_id": safe_id, "written": len(normalized), "path": str(path)}


def read_manifest(workspace: str | Path, session_id: str) -> JsonDict:
    """Read a session manifest."""

    path = _session_file(workspace, session_id, MANIFEST_SUFFIX)
    manifest = _read_json(path)
    if int(manifest.get("schema_version", 0)) != RUNTIME_EVIDENCE_SCHEMA_VERSION:
        raise RuntimeStoreError(f"Unsupported runtime evidence schema: {manifest.get('schema_version')}")
    if manifest.get("session_id") != validate_session_id(session_id):
        raise RuntimeStoreError(f"Manifest/session mismatch: {session_id}")
    return manifest


def read_state(workspace: str | Path, session_id: str, *, default_status: str | None = None) -> JsonDict:
    """Read mutable session state."""

    safe_id = validate_session_id(session_id)
    path = _session_file(workspace, safe_id, STATE_SUFFIX)
    if not path.exists() and default_status:
        manifest = read_manifest(workspace, safe_id)
        return {
            "session_id": safe_id,
            "status": default_status,
            "started_at": manifest.get("created_at"),
            "updated_at": manifest.get("created_at"),
        }
    state = _read_json(path)
    if state.get("session_id") != safe_id:
        raise RuntimeStoreError(f"State/session mismatch: {safe_id}")
    return state


def write_state(
    workspace: str | Path,
    session_id: str,
    state: Mapping[str, Any],
    *,
    updated_at: str | datetime | None = None,
    preserve_updated_at: bool = False,
) -> JsonDict:
    """Write mutable session state after validating the status and filename."""

    safe_id = validate_session_id(session_id)
    record = _json_ready(dict(state))
    record["session_id"] = safe_id
    status = str(record.get("status") or "running")
    if status not in STATE_STATUSES:
        raise ValueError(f"Unsupported runtime session status: {status}")
    record["status"] = status
    if record.get("started_at"):
        record["started_at"] = format_utc(record["started_at"])
    if record.get("ended_at"):
        record["ended_at"] = format_utc(record["ended_at"])
    if updated_at is not None:
        record["updated_at"] = format_utc(updated_at)
    elif preserve_updated_at and record.get("updated_at"):
        record["updated_at"] = format_utc(record["updated_at"])
    elif record.get("updated_at"):
        record["updated_at"] = format_utc(record["updated_at"])
    else:
        record["updated_at"] = format_utc()
    path = _session_file(workspace, safe_id, STATE_SUFFIX, create=True)
    _write_json(path, record)
    return record


def list_sessions(
    workspace: str | Path,
    *,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> JsonDict:
    """List compact runtime sessions in a standard envelope."""

    rows: list[JsonDict] = []
    root = runtime_sessions_dir(workspace)
    if root.exists():
        for path in sorted(root.glob(f"*{MANIFEST_SUFFIX}")):
            session_id = _session_id_from_path(path, MANIFEST_SUFFIX)
            try:
                manifest = read_manifest(workspace, session_id)
                state = read_state(workspace, session_id, default_status="running")
            except (OSError, ValueError, json.JSONDecodeError, RuntimeStoreError):
                continue
            if status and state.get("status") != status:
                continue
            rows.append(_session_row(manifest, state))

    rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("session_id") or "")), reverse=True)
    return _page_envelope(rows, limit=limit, offset=offset)


def session_summary(workspace: str | Path, session_id: str) -> JsonDict:
    """Summarize a runtime session without returning the raw JSONL payload."""

    safe_id = validate_session_id(session_id)
    manifest = read_manifest(workspace, safe_id)
    state = read_state(workspace, safe_id, default_status="running")
    stats_by_tag: dict[str, JsonDict] = {}
    sample_count = 0
    error_count = 0
    first_ts = None
    last_ts = None

    for sample in _iter_samples(workspace, safe_id):
        sample_count += 1
        if first_ts is None:
            first_ts = sample.get("ts_utc")
        last_ts = sample.get("ts_utc")
        if _has_error(sample):
            error_count += 1
        tag = str(sample.get("tag") or "")
        if not tag:
            continue
        _update_tag_stats(stats_by_tag.setdefault(tag, _new_tag_stats(tag)), sample)

    tags = [_finalize_tag_stats(stats) for stats in stats_by_tag.values()]
    tags.sort(key=lambda item: str(item.get("tag") or ""))
    return {
        "session_id": safe_id,
        "manifest": _manifest_summary(manifest),
        "state": _state_summary(state),
        "sample_count": sample_count,
        "error_count": error_count,
        "first_ts_utc": first_ts,
        "last_ts_utc": last_ts,
        "tag_count": len(tags),
        "tags": tags,
    }


def read_stream_slice(
    workspace: str | Path,
    session_id: str,
    *,
    tag: str | None = None,
    start_ts_utc: str | None = None,
    end_ts_utc: str | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
    max_points: int = DEFAULT_STREAM_POINTS,
    limit: int | None = None,
    offset: int = 0,
) -> JsonDict:
    """Read a compact, optionally downsampled stream slice."""

    safe_id = validate_session_id(session_id)
    read_manifest(workspace, safe_id)
    point_limit = _bounded_int(limit if limit is not None else max_points, minimum=0, maximum=MAX_QUERY_POINTS)
    start = start_ts_utc or start_ts
    end = end_ts_utc or end_ts
    rows = [
        _sample_preview(sample)
        for sample in _iter_samples(workspace, safe_id)
        if _sample_matches(sample, tag=tag, start=start, end=end)
    ]
    result = _downsample_envelope(rows, limit=point_limit, offset=offset)
    result.update(
        {
            "session_id": safe_id,
            "tag": tag,
            "downsampled": result["truncated"] > 0,
            "source_total": len(rows),
        }
    )
    return result


def runtime_change_points(
    workspace: str | Path,
    session_id: str,
    *,
    tag: str | None = None,
    max_points: int = DEFAULT_STREAM_POINTS,
    limit: int | None = None,
    offset: int = 0,
) -> JsonDict:
    """Return compact points where a tag value or error changed."""

    safe_id = validate_session_id(session_id)
    read_manifest(workspace, safe_id)
    point_limit = _bounded_int(limit if limit is not None else max_points, minimum=0, maximum=MAX_QUERY_POINTS)
    previous_by_tag: dict[str, tuple[str, str, Mapping[str, Any]]] = {}
    changes: list[JsonDict] = []
    for sample in _iter_samples(workspace, safe_id):
        if tag and sample.get("tag") != tag:
            continue
        sample_tag = str(sample.get("tag") or "")
        if not sample_tag:
            continue
        value_key = _value_key(sample.get("value"))
        error_key = str(sample.get("error") or "")
        previous = previous_by_tag.get(sample_tag)
        if previous and (value_key != previous[0] or error_key != previous[1]):
            previous_sample = previous[2]
            row = _sample_preview(sample)
            row["previous_ts_utc"] = previous_sample.get("ts_utc")
            row["previous_value"] = compact_json_value(previous_sample.get("value"))
            row["previous_error"] = previous_sample.get("error")
            row["value_changed"] = value_key != previous[0]
            row["error_changed"] = error_key != previous[1]
            changes.append(row)
        previous_by_tag[sample_tag] = (value_key, error_key, sample)

    result = _downsample_envelope(changes, limit=point_limit, offset=offset)
    result.update(
        {
            "session_id": safe_id,
            "tag": tag,
            "downsampled": result["truncated"] > 0,
            "source_total": len(changes),
        }
    )
    return result


def normalize_tags(tags: Iterable[str]) -> list[str]:
    """Public tag normalizer used by runtime readers."""

    normalized = _normalize_tags(tags)
    if not normalized:
        raise RuntimeStoreError("At least one runtime tag is required")
    return normalized


def new_session_id(prefix: str = "rt") -> str:
    """Return a safe session id with an optional prefix."""

    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", prefix).strip("_") or "rt"
    return f"{safe_prefix}_{uuid.uuid4().hex[:16]}"


def update_state(workspace: str | Path, session_id: str, **updates: Any) -> JsonDict:
    """Read-modify-write a session state file."""

    state = read_state(workspace, session_id, default_status="running")
    state.update(_json_ready(updates))
    if state.get("status") in {"completed", "stopped", "failed"} and not state.get("ended_at"):
        state["ended_at"] = format_utc()
    return write_state(workspace, session_id, state)


def request_stop(workspace: str | Path, session_id: str) -> JsonDict:
    """Create a stop-file that a capture subprocess checks between cycles."""

    safe_id = validate_session_id(session_id)
    stop_path = _session_file(workspace, safe_id, STOP_SUFFIX, create=True)
    stop_path.write_text(format_utc() + "\n", encoding="utf-8")
    return update_state(workspace, safe_id, stop_requested=True)


def stop_requested(workspace: str | Path, session_id: str) -> bool:
    """Return whether stop was requested for a session."""

    return _session_file(workspace, session_id, STOP_SUFFIX).exists()


def session_status(workspace: str | Path, session_id: str) -> JsonDict:
    """Return one compact session status row."""

    safe_id = validate_session_id(session_id)
    paths = {
        "manifest": _session_file(workspace, safe_id, MANIFEST_SUFFIX),
        "state": _session_file(workspace, safe_id, STATE_SUFFIX),
    }
    if not paths["manifest"].exists():
        return {
            "found": False,
            "session_id": safe_id,
            "status": "starting",
            "stop_requested": stop_requested(workspace, safe_id),
        }
    manifest = read_manifest(workspace, safe_id)
    state = read_state(workspace, safe_id, default_status="running")
    row = _session_row(manifest, state)
    row["found"] = True
    row["stop_requested"] = stop_requested(workspace, safe_id)
    return row


def read_samples(workspace: str | Path, session_id: str) -> list[JsonDict]:
    """Return raw session samples for local tests and aggregation helpers."""

    return list(_iter_samples(workspace, validate_session_id(session_id)))


def runtime_summary(workspace: str | Path, *, limit: int = DEFAULT_LIST_LIMIT) -> JsonDict:
    """Summarize all runtime sessions with latest values per tag."""

    sessions = list_sessions(workspace, limit=limit, offset=0)
    latest: dict[str, JsonDict] = {}
    for session in sessions["items"]:
        session_id = session.get("session_id")
        if not session_id:
            continue
        for sample in read_samples(workspace, str(session_id)):
            tag = str(sample.get("tag") or "")
            if not tag:
                continue
            current = latest.get(tag)
            if current is None or str(sample.get("ts_utc") or "") >= str(current.get("ts_utc") or ""):
                latest[tag] = _sample_preview(sample) | {"session_id": session_id}
    return {
        "storage": str(runtime_sessions_dir(workspace)),
        "sessions": sessions,
        "latest": sorted(latest.values(), key=lambda item: str(item.get("tag") or "")),
    }


def runtime_evidence_dir(workspace: str | Path) -> Path:
    """Return the volatile runtime-evidence directory for a workspace."""

    return (Path(workspace).resolve() / RUNTIME_EVIDENCE_DIR_NAME).resolve()


def runtime_sessions_dir(workspace: str | Path, *, create: bool = False) -> Path:
    """Return the directory that holds per-session runtime evidence files."""

    root = runtime_evidence_dir(workspace)
    path = (root / RUNTIME_SESSIONS_DIR_NAME).resolve()
    if not path.is_relative_to(root):
        raise RuntimeStoreError(f"Runtime session path escaped evidence root: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def validate_session_id(session_id: str) -> str:
    """Validate that a session id is safe to use as a filename stem."""

    value = str(session_id or "")
    if not SESSION_ID_RE.fullmatch(value):
        raise ValueError(f"Unsafe runtime session id: {session_id!r}")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: str | datetime | None = None) -> str:
    if value is None:
        value = utc_now()
    if isinstance(value, str):
        return value
    return _as_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fingerprint_text(value: str, *, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def compact_json_value(value: Any) -> Any:
    """Return a JSON-safe value small enough for default tool responses."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_COMPACT_STRING_CHARS else value[:MAX_COMPACT_STRING_CHARS] + "..."
    if isinstance(value, Mapping):
        compact: JsonDict = {}
        items = list(value.items())
        for key, item in items[:MAX_COMPACT_COLLECTION_ITEMS]:
            compact[str(key)] = compact_json_value(item)
        if len(items) > MAX_COMPACT_COLLECTION_ITEMS:
            compact["truncated_items"] = len(items) - MAX_COMPACT_COLLECTION_ITEMS
        return compact
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        compact_items = [compact_json_value(item) for item in items[:MAX_COMPACT_COLLECTION_ITEMS]]
        if len(items) > MAX_COMPACT_COLLECTION_ITEMS:
            compact_items.append({"truncated_items": len(items) - MAX_COMPACT_COLLECTION_ITEMS})
        return compact_items
    return compact_json_value(str(value))


def _new_session_id(created_at: str) -> str:
    stamp = re.sub(r"[^0-9A-Za-z]", "", created_at.replace("Z", ""))
    return f"rt_{stamp}_{uuid.uuid4().hex[:12]}"


def _session_file(workspace: str | Path, session_id: str, suffix: str, *, create: bool = False) -> Path:
    safe_id = validate_session_id(session_id)
    root = runtime_sessions_dir(workspace, create=create)
    path = (root / f"{safe_id}{suffix}").resolve()
    if not path.is_relative_to(root):
        raise RuntimeStoreError(f"Runtime session file escaped evidence root: {path}")
    return path


def _session_id_from_path(path: Path, suffix: str) -> str:
    name = path.name
    if not name.endswith(suffix):
        raise ValueError(f"Unexpected runtime session filename: {name}")
    return validate_session_id(name[: -len(suffix)])


def _read_json(path: Path) -> JsonDict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    # Atomic write: a capture subprocess rewrites state ~10x/s while the MCP
    # process reads it. Write to a temp file and os.replace so a concurrent
    # reader always sees a complete old or new file, never a torn one.
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _iter_samples(workspace: str | Path, session_id: str) -> Iterable[JsonDict]:
    path = _session_file(workspace, session_id, SAMPLES_SUFFIX)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Tolerate a partially flushed trailing line written by a live
                # capture subprocess; skip it rather than failing the query.
                continue


def _normalize_sample(sample: Mapping[str, Any]) -> JsonDict:
    tag = str(sample.get("tag") or "")
    if not tag:
        raise ValueError("Runtime sample requires a tag")
    ts_utc = format_utc(sample.get("ts_utc")) if sample.get("ts_utc") is not None else format_utc()
    ts_mono = sample.get("ts_mono")
    if ts_mono is None:
        ts_mono = time.monotonic()
    return {
        "ts_utc": ts_utc,
        "ts_mono": float(ts_mono),
        "cycle": sample.get("cycle"),
        "tag": tag,
        "value": _json_ready(sample.get("value")),
        "type": sample.get("type"),
        "error": sample.get("error"),
    }


def _normalize_tags(tags: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for tag in tags:
        value = str(tag)
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _json_ready(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return format_utc(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    return str(value)


def _session_row(manifest: Mapping[str, Any], state: Mapping[str, Any]) -> JsonDict:
    row = _manifest_summary(manifest)
    row.update(_state_summary(state))
    counts = dict(state.get("counts") or {})
    row["sample_count"] = int(counts.get("samples") or 0)
    row["error_count"] = int(counts.get("errors") or 0)
    return row


def _manifest_summary(manifest: Mapping[str, Any]) -> JsonDict:
    requested_tags = list(manifest.get("requested_tags") or [])
    return {
        "schema_version": manifest.get("schema_version"),
        "session_id": manifest.get("session_id"),
        "created_at": manifest.get("created_at"),
        "workspace_fingerprint": manifest.get("workspace_fingerprint"),
        "controller_identity": compact_json_value(manifest.get("controller_identity") or {}),
        "comm_path_fingerprint": manifest.get("comm_path_fingerprint"),
        "mode": manifest.get("mode"),
        "source": manifest.get("source"),
        "interval_ms": manifest.get("interval_ms"),
        "requested_tags_count": len(requested_tags),
        "requested_tags_preview": [compact_json_value(tag) for tag in requested_tags[:MAX_COMPACT_COLLECTION_ITEMS]],
        "requested_tags_truncated": max(0, len(requested_tags) - MAX_COMPACT_COLLECTION_ITEMS),
        "limits": compact_json_value(manifest.get("limits") or {}),
        "permission": compact_json_value(manifest.get("permission") or {}),
        "retention_policy": compact_json_value(manifest.get("retention_policy") or {}),
    }


def _state_summary(state: Mapping[str, Any]) -> JsonDict:
    return {
        key: compact_json_value(value)
        for key, value in {
            "status": state.get("status"),
            "pid": state.get("pid"),
            "started_at": state.get("started_at"),
            "updated_at": state.get("updated_at"),
            "ended_at": state.get("ended_at"),
            "counts": state.get("counts") or {},
            "errors": state.get("errors"),
        }.items()
        if value not in (None, "")
    }


def _new_tag_stats(tag: str) -> JsonDict:
    return {
        "tag": tag,
        "sample_count": 0,
        "errors": 0,
        "n_changes": 0,
        "first_ts_utc": None,
        "last_ts_utc": None,
        "first_value": None,
        "last_value": None,
        "first_change_at": None,
        "last_error": None,
        "_previous_key": None,
        "_min": None,
        "_max": None,
        "_minmax_kind": None,
        "_type": None,
    }


def _update_tag_stats(stats: JsonDict, sample: Mapping[str, Any]) -> None:
    value = sample.get("value")
    error = sample.get("error")
    sample_key = f"{_value_key(value)}|{error or ''}"
    if stats["sample_count"] == 0:
        stats["first_ts_utc"] = sample.get("ts_utc")
        stats["first_value"] = compact_json_value(value)
        stats["_previous_key"] = sample_key
    elif sample_key != stats["_previous_key"]:
        stats["n_changes"] += 1
        if not stats["first_change_at"]:
            stats["first_change_at"] = sample.get("ts_utc")
        stats["_previous_key"] = sample_key
    stats["sample_count"] += 1
    stats["last_ts_utc"] = sample.get("ts_utc")
    stats["last_value"] = compact_json_value(value)
    if sample.get("type"):
        stats["_type"] = sample.get("type")
    if _has_error(sample):
        stats["errors"] += 1
        stats["last_error"] = error
    else:
        _update_minmax(stats, value)


def _update_minmax(stats: JsonDict, value: Any) -> None:
    kind = _minmax_kind(value)
    if kind is None:
        return
    if stats["_minmax_kind"] is None:
        stats["_minmax_kind"] = kind
        stats["_min"] = value
        stats["_max"] = value
        return
    if stats["_minmax_kind"] != kind:
        return
    if value < stats["_min"]:
        stats["_min"] = value
    if value > stats["_max"]:
        stats["_max"] = value


def _minmax_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    return None


def _finalize_tag_stats(stats: Mapping[str, Any]) -> JsonDict:
    result = {
        key: value
        for key, value in stats.items()
        if not key.startswith("_") and value not in (None, "")
    }
    if stats.get("_type"):
        result["type"] = stats["_type"]
    if stats.get("_minmax_kind"):
        result["min"] = compact_json_value(stats.get("_min"))
        result["max"] = compact_json_value(stats.get("_max"))
    return result


def _sample_matches(sample: Mapping[str, Any], *, tag: str | None, start: str | None, end: str | None) -> bool:
    if tag and sample.get("tag") != tag:
        return False
    ts_utc = str(sample.get("ts_utc") or "")
    if start and ts_utc < start:
        return False
    if end and ts_utc > end:
        return False
    return True


def _sample_preview(sample: Mapping[str, Any]) -> JsonDict:
    return {
        "ts_utc": sample.get("ts_utc"),
        "ts_mono": sample.get("ts_mono"),
        "cycle": sample.get("cycle"),
        "tag": sample.get("tag"),
        "value": compact_json_value(sample.get("value")),
        "type": sample.get("type"),
        "error": sample.get("error"),
    }


def _has_error(sample: Mapping[str, Any]) -> bool:
    return sample.get("error") not in (None, "")


def _value_key(value: Any) -> str:
    return json.dumps(_json_ready(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _page_envelope(rows: list[JsonDict], *, limit: int, offset: int = 0) -> JsonDict:
    total = len(rows)
    normalized_offset = max(int(offset or 0), 0)
    normalized_limit = _bounded_int(limit, minimum=0, maximum=MAX_QUERY_POINTS)
    page = rows[normalized_offset : normalized_offset + normalized_limit]
    return {
        "items": page,
        "total": total,
        "offset": normalized_offset,
        "limit": normalized_limit,
        "has_more": normalized_offset + len(page) < total,
        "truncated": max(0, total - normalized_offset - len(page)),
    }


def _downsample_envelope(rows: list[JsonDict], *, limit: int, offset: int = 0) -> JsonDict:
    total = len(rows)
    normalized_offset = max(int(offset or 0), 0)
    normalized_limit = _bounded_int(limit, minimum=0, maximum=MAX_QUERY_POINTS)
    source = rows[normalized_offset:]
    page = _downsample(source, normalized_limit)
    return {
        "items": page,
        "total": total,
        "offset": normalized_offset,
        "limit": normalized_limit,
        "has_more": len(page) < len(source),
        "truncated": max(0, len(source) - len(page)),
    }


def _downsample(rows: list[JsonDict], max_points: int) -> list[JsonDict]:
    if max_points <= 0:
        return []
    if len(rows) <= max_points:
        return list(rows)
    if max_points == 1:
        return [rows[0]]
    last_index = len(rows) - 1
    indexes = [round(index * last_index / (max_points - 1)) for index in range(max_points)]
    return [rows[index] for index in indexes]


def _bounded_int(value: int | None, *, minimum: int, maximum: int) -> int:
    number = int(value or 0)
    if number < minimum:
        return minimum
    if number > maximum:
        return maximum
    return number


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
