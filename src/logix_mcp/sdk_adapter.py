"""Fail-closed scaffolding for optional Logix Designer SDK integration.

This module defines the SDK boundary, local compact logging, simulation harness,
and runtime-evidence helpers without opening projects, connecting to a
controller, or importing the optional SDK at module import time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Iterable, Mapping
import hashlib
import json
import math
import re
import uuid


JsonDict = dict[str, Any]

SDK_PACKAGE_NAME = "logix_designer_sdk"
RUNTIME_EVIDENCE_SCHEMA_VERSION = 1
DEFAULT_RUNTIME_TTL_SECONDS = 300
RUNTIME_EVIDENCE_DIR_NAME = "runtime_evidence"
SDK_LOG_DIR = Path(".tmp") / "sdk_logs"
SDK_EXPORT_DIR = Path(".tmp") / "sdk_exports"
MAX_COMPACT_STRING_CHARS = 300
MAX_COMPACT_COLLECTION_ITEMS = 20
MAX_RUNTIME_POLL_TAGS = 32
MAX_RUNTIME_POLL_SAMPLES = 5_000
MAX_RUNTIME_QUERY_LIMIT = 1_000
DEFAULT_SIMULATED_INTERVAL_SECONDS = 1.0
SIMULATED_RUNTIME_SOURCE = "simulated_sdk_runtime"

SDK_SURFACE_EXPORT_OFFLINE = "export_offline_scratch"
SDK_SURFACE_RUNTIME_READ = "runtime_read"

SDK_RUNTIME_ONLINE = "ONLINE"
SDK_RUNTIME_OFFLINE = "OFFLINE"
SDK_RUNTIME_MODES = frozenset({SDK_RUNTIME_ONLINE, SDK_RUNTIME_OFFLINE})


class SdkSecurityError(RuntimeError):
    """An SDK request crossed the declared read-only boundary."""


class SdkUnavailableError(RuntimeError):
    """The optional SDK package is not installed or not loaded."""


class SdkPermissionError(RuntimeError):
    """A runtime SDK read was attempted without an explicit matching grant."""


@dataclass(frozen=True)
class SdkCapability:
    """One named adapter operation and the exact SDK method names it may use."""

    public_name: str
    sdk_methods: tuple[str, ...]
    surface: str
    requires_explicit_permission: bool
    requires_online_permission: bool
    writes_controller: bool = False
    writes_original_project: bool = False


@dataclass(frozen=True)
class RuntimeReadPermission:
    """Explicit grant for one runtime read request."""

    capability: str
    reason: str
    approved_by: str
    allow_online: bool = False
    comm_path: str | None = None
    max_tags: int = 1
    expires_at: str | None = None

    def to_record(self) -> JsonDict:
        record = asdict(self)
        if self.comm_path:
            record["comm_path_fingerprint"] = fingerprint_text(self.comm_path)
        record.pop("comm_path", None)
        return compact_json_value(record)


ALLOWED_SDK_CAPABILITIES: tuple[SdkCapability, ...] = (
    SdkCapability(
        public_name="sdk_export_l5x",
        sdk_methods=("open_logix_project", "save_as"),
        surface=SDK_SURFACE_EXPORT_OFFLINE,
        requires_explicit_permission=False,
        requires_online_permission=False,
    ),
    SdkCapability(
        public_name="sdk_partial_export",
        sdk_methods=("open_logix_project", "partial_export_to_xml_file"),
        surface=SDK_SURFACE_EXPORT_OFFLINE,
        requires_explicit_permission=False,
        requires_online_permission=False,
    ),
    SdkCapability(
        public_name="sdk_read_controller_mode",
        sdk_methods=("read_controller_mode",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_connected_state",
        sdk_methods=("read_connected_state",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value",
        sdk_methods=("get_tag_value",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_bool",
        sdk_methods=("get_tag_value_bool",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_sint",
        sdk_methods=("get_tag_value_sint",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_int",
        sdk_methods=("get_tag_value_int",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_dint",
        sdk_methods=("get_tag_value_dint",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_lint",
        sdk_methods=("get_tag_value_lint",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_usint",
        sdk_methods=("get_tag_value_usint",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_uint",
        sdk_methods=("get_tag_value_uint",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_udint",
        sdk_methods=("get_tag_value_udint",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_ulint",
        sdk_methods=("get_tag_value_ulint",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_string",
        sdk_methods=("get_tag_value_string",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_real",
        sdk_methods=("get_tag_value_real",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
    SdkCapability(
        public_name="sdk_read_tag_value_lreal",
        sdk_methods=("get_tag_value_lreal",),
        surface=SDK_SURFACE_RUNTIME_READ,
        requires_explicit_permission=True,
        requires_online_permission=True,
    ),
)


DENIED_SDK_METHOD_NAMES = frozenset(
    {
        "build",
        "change_controller_mode",
        "delete_safety_signature",
        "download",
        "generate_safety_signature",
        "load_image_from_sd_card",
        "lock",
        "partial_import",
        "partial_import_from_xml_file",
        "protect",
        "safety_lock",
        "safety_unlock",
        "save",
        "set_communications_path",
        "set_safety_network_number",
        "set_tag_value",
        "set_tag_value_bool",
        "set_tag_value_dint",
        "set_tag_value_int",
        "set_tag_value_lint",
        "set_tag_value_lreal",
        "set_tag_value_real",
        "set_tag_value_sint",
        "set_tag_value_string",
        "set_tag_value_udint",
        "set_tag_value_uint",
        "set_tag_value_ulint",
        "set_tag_value_usint",
        "store_image_on_sd_card",
        "unlock",
        "unprotect",
        "upload",
    }
)

ADMIN_MANUAL_ONLY_SDK_METHOD_NAMES = frozenset({"upload_to_new_project"})

PUBLIC_DENIED_NAMES = DENIED_SDK_METHOD_NAMES | ADMIN_MANUAL_ONLY_SDK_METHOD_NAMES | {
    "sdk_upload_to_new_project",
    "upload_to_new_project",
}

_CAPABILITIES_BY_NAME = {capability.public_name: capability for capability in ALLOWED_SDK_CAPABILITIES}
_LOG_ALLOWED_KEYS = frozenset(
    {
        "capability",
        "connected",
        "data_type",
        "duration_s",
        "error_code",
        "message",
        "mode",
        "ok",
        "operation",
        "sdk_method",
        "tag",
    }
)
_SENSITIVE_LOG_KEYS = frozenset(
    {
        "comm_path",
        "event_log",
        "exception",
        "log",
        "logs",
        "operation_events",
        "out_path",
        "project_path",
        "raw_xml",
        "stderr",
        "stdout",
        "xml",
    }
)


def sdk_status(package_name: str = SDK_PACKAGE_NAME) -> JsonDict:
    """Return optional SDK availability without importing or initializing it."""

    available = find_spec(package_name) is not None
    return {"available": available, "package": package_name, "mode": "optional_fail_closed"}


def require_sdk_available(package_name: str = SDK_PACKAGE_NAME) -> None:
    """Fail closed when future SDK-backed code tries to run without the SDK."""

    if not sdk_status(package_name)["available"]:
        raise SdkUnavailableError(f"Optional SDK package is unavailable: {package_name}")


def allowed_capabilities(surface: str | None = None) -> tuple[SdkCapability, ...]:
    """Return the exact adapter capability registry."""

    if surface is None:
        return ALLOWED_SDK_CAPABILITIES
    return tuple(capability for capability in ALLOWED_SDK_CAPABILITIES if capability.surface == surface)


def allowed_capability_names(surface: str | None = None) -> tuple[str, ...]:
    return tuple(capability.public_name for capability in allowed_capabilities(surface))


def allowed_sdk_method_names() -> tuple[str, ...]:
    names: list[str] = []
    for capability in ALLOWED_SDK_CAPABILITIES:
        names.extend(capability.sdk_methods)
    return tuple(sorted(set(names)))


def capability_spec(public_name: str) -> SdkCapability:
    """Exact-name capability lookup; callers must not do dynamic SDK dispatch."""

    try:
        return _CAPABILITIES_BY_NAME[public_name]
    except KeyError as exc:
        raise SdkSecurityError(f"SDK capability is not allowlisted: {public_name}") from exc


def is_capability_allowed(public_name: str) -> bool:
    return public_name in _CAPABILITIES_BY_NAME


def validate_sdk_registry() -> JsonDict:
    """Assert that the static registry has no dangerous or ambiguous entries."""

    public_names = set(allowed_capability_names())
    sdk_methods = set(allowed_sdk_method_names())
    denied_public = sorted(public_names & PUBLIC_DENIED_NAMES)
    denied_methods = sorted(sdk_methods & DENIED_SDK_METHOD_NAMES)
    ambiguous = sorted(name for name in public_names if "*" in name)
    ambiguous.extend(sorted(method for method in sdk_methods if "*" in method))
    if denied_public or denied_methods or ambiguous:
        raise SdkSecurityError(
            "Unsafe SDK registry entries: "
            f"denied_public={denied_public}, denied_methods={denied_methods}, ambiguous={ambiguous}"
        )
    return {
        "ok": True,
        "capability_count": len(public_names),
        "sdk_method_count": len(sdk_methods),
        "surfaces": sorted({capability.surface for capability in ALLOWED_SDK_CAPABILITIES}),
    }


def validate_public_surface(public_names: Iterable[str]) -> JsonDict:
    """Reject dangerous names before any future MCP/admin surface is exposed."""

    names = set(public_names)
    denied = sorted(names & PUBLIC_DENIED_NAMES)
    if denied:
        raise SdkSecurityError(f"Blocked dangerous SDK public names: {denied}")
    unknown = sorted(name for name in names if name not in _CAPABILITIES_BY_NAME)
    return {"ok": True, "checked": len(names), "unknown": unknown}


def validate_runtime_permission(
    capability: str,
    permission: RuntimeReadPermission,
    *,
    mode: str,
    tag_count: int = 1,
    now: datetime | None = None,
) -> JsonDict:
    """Validate an explicit runtime grant before future online reads."""

    spec = capability_spec(capability)
    if spec.surface != SDK_SURFACE_RUNTIME_READ:
        raise SdkPermissionError(f"Capability is not a runtime read: {capability}")
    if permission.capability != capability:
        raise SdkPermissionError(f"Permission is for {permission.capability}, not {capability}")
    normalized_mode = normalize_runtime_mode(mode)
    if tag_count < 1:
        raise SdkPermissionError("Runtime reads must request at least one tag or value")
    if tag_count > max(int(permission.max_tags or 0), 0):
        raise SdkPermissionError(f"Runtime read requested {tag_count} values, limit is {permission.max_tags}")
    if permission.expires_at and freshness_for(permission.expires_at, now=now) == "stale":
        raise SdkPermissionError("Runtime permission is stale")
    if normalized_mode == SDK_RUNTIME_ONLINE:
        if not permission.allow_online:
            raise SdkPermissionError("ONLINE SDK runtime read requires allow_online=True")
        if not permission.comm_path:
            raise SdkPermissionError("ONLINE SDK runtime read requires a confirmed comm_path")
    return {"ok": True, "capability": capability, "mode": normalized_mode, "tag_count": tag_count}


def validate_scratch_output_path(
    workspace: str | Path,
    out_path: str | Path,
    *,
    suffixes: tuple[str, ...] = (".L5X",),
    allow_overwrite: bool = False,
) -> Path:
    """Require SDK exports to land under ``<workspace>/.tmp/sdk_exports``."""

    workspace_path = Path(workspace).resolve()
    scratch_root = (workspace_path / SDK_EXPORT_DIR).resolve()
    target = Path(out_path)
    if not target.is_absolute():
        target = scratch_root / target
    resolved = target.resolve()
    if not resolved.is_relative_to(scratch_root):
        raise SdkSecurityError(f"SDK export path must be under {scratch_root}")
    if suffixes and resolved.suffix not in suffixes:
        raise SdkSecurityError(f"SDK export path must use one of {suffixes}")
    if resolved.exists() and not allow_overwrite:
        raise SdkSecurityError(f"SDK export path already exists: {resolved}")
    return resolved


def compact_sdk_log_event(event: Mapping[str, Any], *, now: datetime | None = None) -> JsonDict:
    """Project a verbose SDK event into a small local JSON record."""

    compact: JsonDict = {
        "schema_version": 1,
        "logged_at": format_utc(now or utc_now()),
    }
    redacted_keys: list[str] = []
    for key, value in event.items():
        if key in _LOG_ALLOWED_KEYS:
            compact[key] = compact_json_value(value)
        else:
            redacted_keys.append(str(key))
    sensitive = sorted(set(redacted_keys) & _SENSITIVE_LOG_KEYS)
    if redacted_keys:
        compact["redacted_keys_count"] = len(redacted_keys)
    if sensitive:
        compact["sensitive_keys_redacted"] = sensitive[:MAX_COMPACT_COLLECTION_ITEMS]
    return compact


def write_compact_sdk_log(
    workspace: str | Path,
    event: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> JsonDict:
    """Write one compact SDK audit event under ignored local scratch space."""

    logged_at = now or utc_now()
    root = (Path(workspace).resolve() / SDK_LOG_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = f"{_timestamp_for_filename(logged_at)}_{uuid.uuid4().hex[:12]}.jsonl"
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise SdkSecurityError(f"SDK log path escaped scratch root: {path}")
    compact = compact_sdk_log_event(event, now=logged_at)
    line = json.dumps(compact, ensure_ascii=True, separators=(",", ":")) + "\n"
    path.write_text(line, encoding="utf-8")
    return {"ok": True, "log_handle": str(path), "bytes": len(line), "event": compact}


def runtime_evidence_dir(workspace: str | Path) -> Path:
    """Return the volatile runtime-evidence directory, outside canonical IR."""

    workspace_path = Path(workspace).resolve()
    path = (workspace_path / RUNTIME_EVIDENCE_DIR_NAME).resolve()
    reserved = [
        (workspace_path / "ir").resolve(),
        (workspace_path / "ai").resolve(),
        (workspace_path / "index").resolve(),
        (workspace_path / "source").resolve(),
        (workspace_path / "bundles").resolve(),
    ]
    if any(path == item or path.is_relative_to(item) for item in reserved):
        raise SdkSecurityError("runtime_evidence must stay separate from IR/source/index/AI data")
    return path


def build_runtime_evidence(
    *,
    evidence_type: str,
    source_fingerprint: str,
    mode: str,
    controller_identity: Mapping[str, Any],
    permission: Mapping[str, Any] | RuntimeReadPermission,
    retention_policy: Mapping[str, Any],
    ttl_seconds: int = DEFAULT_RUNTIME_TTL_SECONDS,
    scope: str | None = None,
    tag: str | None = None,
    comm_path: str | None = None,
    value: Any = None,
    observed_at: datetime | None = None,
) -> JsonDict:
    """Create a runtime-evidence record with TTL and explicit freshness."""

    if ttl_seconds < 0:
        raise ValueError("ttl_seconds must be >= 0")
    observed = observed_at or utc_now()
    expires = observed + timedelta(seconds=ttl_seconds)
    if isinstance(permission, RuntimeReadPermission):
        permission_record = permission.to_record()
    else:
        permission_record = compact_json_value(dict(permission))
    record: JsonDict = {
        "schema_version": RUNTIME_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": _safe_token(evidence_type),
        "observed_at": format_utc(observed),
        "expires_at": format_utc(expires),
        "freshness": freshness_for(expires, now=observed),
        "source_fingerprint": str(source_fingerprint),
        "mode": normalize_runtime_mode(mode),
        "controller_identity": compact_json_value(dict(controller_identity)),
        "permission": permission_record,
        "retention_policy": compact_json_value(dict(retention_policy)),
        "storage": "runtime_evidence",
    }
    if scope:
        record["scope"] = str(scope)
    if tag:
        record["tag"] = str(tag)
    if comm_path:
        record["comm_path_fingerprint"] = fingerprint_text(comm_path)
    if value is not None:
        record["value"] = compact_json_value(value)
    return record


def write_runtime_evidence(
    workspace: str | Path,
    record: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> JsonDict:
    """Persist one runtime evidence record outside ``ir/`` and SQLite."""

    root = runtime_evidence_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)
    refreshed = refresh_runtime_evidence(record, now=now)
    observed = parse_utc(refreshed["observed_at"])
    kind = _safe_token(str(refreshed.get("evidence_type") or "runtime"))
    path = (root / f"{_timestamp_for_filename(observed)}_{kind}_{uuid.uuid4().hex[:12]}.json").resolve()
    if not path.is_relative_to(root):
        raise SdkSecurityError(f"Runtime evidence path escaped storage root: {path}")
    text = json.dumps(refreshed, ensure_ascii=True, separators=(",", ":")) + "\n"
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(path), "bytes": len(text), "record": refreshed}


def simulate_runtime_tag_stream(
    workspace: str | Path | None,
    tags: Iterable[str],
    *,
    samples: int = 10,
    interval_seconds: float = DEFAULT_SIMULATED_INTERVAL_SECONDS,
    data_type: str = "REAL",
    signal: str = "sine",
    amplitude: float = 100.0,
    offset: float = 0.0,
    period_samples: int = 20,
    mode: str = SDK_RUNTIME_OFFLINE,
    scope: str | None = None,
    controller_identity: Mapping[str, Any] | None = None,
    permission: RuntimeReadPermission | None = None,
    retention_policy: Mapping[str, Any] | None = None,
    ttl_seconds: int = DEFAULT_RUNTIME_TTL_SECONDS,
    source_fingerprint: str | None = None,
    observed_start: datetime | None = None,
    session_id: str | None = None,
    persist: bool = True,
    preview_limit: int = 20,
) -> JsonDict:
    """Generate deterministic SDK-style runtime samples for harness testing.

    The simulator never imports the optional SDK or touches a controller. It
    exercises the same permission, compact-evidence, and freshness contracts that
    a future SDK read runner must use.
    """

    normalized_tags = _normalize_tags(tags)
    normalized_samples = _bounded_int(samples, minimum=1, maximum=MAX_RUNTIME_POLL_SAMPLES, name="samples")
    if len(normalized_tags) > MAX_RUNTIME_POLL_TAGS:
        raise SdkPermissionError(f"Runtime read requested {len(normalized_tags)} tags, limit is {MAX_RUNTIME_POLL_TAGS}")
    interval = float(interval_seconds)
    if interval < 0:
        raise ValueError("interval_seconds must be >= 0")
    normalized_type = str(data_type or "REAL").upper()
    capability = _capability_for_data_type(normalized_type)
    normalized_mode = normalize_runtime_mode(mode)
    observed = observed_start or utc_now()
    if permission is None:
        permission = RuntimeReadPermission(
            capability=capability,
            reason="simulated runtime read harness",
            approved_by="Adrian Acurero",
            allow_online=normalized_mode == SDK_RUNTIME_ONLINE,
            comm_path="SIMULATED" if normalized_mode == SDK_RUNTIME_ONLINE else None,
            max_tags=len(normalized_tags),
            expires_at=format_utc(observed + timedelta(seconds=max(ttl_seconds, DEFAULT_RUNTIME_TTL_SECONDS))),
        )
    validate_runtime_permission(capability, permission, mode=normalized_mode, tag_count=len(normalized_tags), now=observed)

    normalized_signal = _normalize_signal(signal)
    normalized_period = _bounded_int(period_samples, minimum=1, maximum=MAX_RUNTIME_POLL_SAMPLES, name="period_samples")
    runtime_session_id = session_id or f"sim-{uuid.uuid4().hex[:12]}"
    identity = dict(
        controller_identity
        or {
            "name": "Simulated Logix Runtime",
            "source": SIMULATED_RUNTIME_SOURCE,
            "simulated": True,
        }
    )
    retention = dict(
        retention_policy
        or {
            "ttl_seconds": ttl_seconds,
            "purge": "manual",
            "source": SIMULATED_RUNTIME_SOURCE,
        }
    )
    fingerprint = source_fingerprint or fingerprint_text(
        "|".join([SIMULATED_RUNTIME_SOURCE, ",".join(normalized_tags), normalized_type, normalized_signal, str(amplitude), str(offset)])
    )

    records: list[JsonDict] = []
    interval_us = int(round(interval * 1_000_000))
    for sample_index in range(normalized_samples):
        sample_time = observed + timedelta(microseconds=interval_us * sample_index)
        for tag_index, tag in enumerate(normalized_tags):
            raw_value = _simulated_signal_value(
                sample_index,
                tag_index=tag_index,
                tag_count=len(normalized_tags),
                signal=normalized_signal,
                amplitude=float(amplitude),
                offset=float(offset),
                period_samples=normalized_period,
            )
            value = _coerce_simulated_value(raw_value, normalized_type)
            record = build_runtime_evidence(
                evidence_type="tag_value_sample",
                source_fingerprint=fingerprint,
                mode=normalized_mode,
                controller_identity=identity,
                permission=permission,
                retention_policy=retention,
                ttl_seconds=ttl_seconds,
                scope=scope,
                tag=tag,
                value=value,
                observed_at=sample_time,
            )
            record.update(
                {
                    "source": SIMULATED_RUNTIME_SOURCE,
                    "session_id": runtime_session_id,
                    "sample_index": sample_index,
                    "sample_elapsed_seconds": round(interval * sample_index, 6),
                    "sample_interval_seconds": interval,
                    "data_type": normalized_type,
                    "signal": normalized_signal,
                    "quality": "simulated",
                }
            )
            records.append(record)

    written_paths: list[str] = []
    if persist:
        if workspace is None:
            raise SdkSecurityError("workspace is required when persist=True")
        for record in records:
            written = write_runtime_evidence(workspace, record, now=observed)
            if len(written_paths) < preview_limit:
                written_paths.append(str(written["path"]))

    preview = [_runtime_evidence_preview(record) for record in records[: max(int(preview_limit or 0), 0)]]
    first_observed = records[0]["observed_at"] if records else None
    last_observed = records[-1]["observed_at"] if records else None
    return {
        "ok": True,
        "operation": "simulate_runtime_tag_stream",
        "source": SIMULATED_RUNTIME_SOURCE,
        "persisted": bool(persist),
        "session_id": runtime_session_id,
        "mode": normalized_mode,
        "data_type": normalized_type,
        "signal": normalized_signal,
        "tag_count": len(normalized_tags),
        "sample_count": normalized_samples,
        "record_count": len(records),
        "first_observed_at": first_observed,
        "last_observed_at": last_observed,
        "records_preview": preview,
        "records_truncated": max(0, len(records) - len(preview)),
        "paths_preview": written_paths,
        "paths_truncated": max(0, len(records) - len(written_paths)) if persist else 0,
    }


def query_runtime_evidence(
    workspace: str | Path,
    *,
    tag: str | None = None,
    scope: str | None = None,
    freshness: str | None = None,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> JsonDict:
    """Return compact runtime evidence rows from the volatile evidence store."""

    root = runtime_evidence_dir(workspace)
    normalized_limit = _bounded_int(limit, minimum=0, maximum=MAX_RUNTIME_QUERY_LIMIT, name="limit")
    normalized_offset = max(int(offset or 0), 0)
    normalized_freshness = str(freshness).lower() if freshness else None
    if normalized_freshness and normalized_freshness not in {"fresh", "stale"}:
        raise SdkSecurityError(f"Unsupported freshness filter: {freshness}")

    rows: list[JsonDict] = []
    if root.exists():
        for path in sorted(root.glob("*.json")):
            record = read_runtime_evidence(path, now=now)
            if tag and record.get("tag") != tag:
                continue
            if scope and record.get("scope") != scope:
                continue
            if normalized_freshness and record.get("freshness") != normalized_freshness:
                continue
            row = _runtime_evidence_preview(record)
            row["path"] = str(path)
            rows.append(row)

    rows.sort(key=lambda row: (str(row.get("observed_at") or ""), int(row.get("sample_index") or 0), str(row.get("tag") or "")), reverse=True)
    page = rows[normalized_offset : normalized_offset + normalized_limit]
    return {
        "items": page,
        "total": len(rows),
        "offset": normalized_offset,
        "limit": normalized_limit,
        "has_more": normalized_offset + len(page) < len(rows),
        "truncated": max(0, len(rows) - normalized_offset - len(page)),
    }


def runtime_evidence_summary(workspace: str | Path, *, now: datetime | None = None) -> JsonDict:
    """Summarize runtime evidence without returning full records."""

    root = runtime_evidence_dir(workspace)
    by_freshness: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_tag: dict[str, JsonDict] = {}
    total = 0
    if root.exists():
        for path in sorted(root.glob("*.json")):
            record = read_runtime_evidence(path, now=now)
            total += 1
            fresh = str(record.get("freshness") or "unknown")
            source = str(record.get("source") or record.get("evidence_type") or "unknown")
            by_freshness[fresh] = by_freshness.get(fresh, 0) + 1
            by_source[source] = by_source.get(source, 0) + 1
            tag = str(record.get("tag") or "")
            if tag:
                current = by_tag.get(tag)
                observed_at = str(record.get("observed_at") or "")
                if current is None or observed_at >= str(current.get("observed_at") or ""):
                    by_tag[tag] = {
                        "tag": tag,
                        "scope": record.get("scope"),
                        "observed_at": observed_at,
                        "freshness": fresh,
                        "value": compact_json_value(record.get("value")),
                        "data_type": record.get("data_type"),
                        "source": source,
                        "session_id": record.get("session_id"),
                    }
    return {
        "total": total,
        "by_freshness": by_freshness,
        "by_source": by_source,
        "tags": sorted(by_tag.values(), key=lambda item: str(item.get("tag") or "")),
        "storage": str(root),
    }


def read_runtime_evidence(path: str | Path, *, now: datetime | None = None) -> JsonDict:
    """Read a runtime-evidence file and recompute fresh/stale in memory."""

    record = json.loads(Path(path).read_text(encoding="utf-8"))
    return refresh_runtime_evidence(record, now=now)


def list_runtime_evidence(workspace: str | Path, *, now: datetime | None = None) -> list[JsonDict]:
    """Read runtime-evidence records from the separate volatile directory."""

    root = runtime_evidence_dir(workspace)
    if not root.exists():
        return []
    records: list[JsonDict] = []
    for path in sorted(root.glob("*.json")):
        records.append(read_runtime_evidence(path, now=now))
    return records


def refresh_runtime_evidence(record: Mapping[str, Any], *, now: datetime | None = None) -> JsonDict:
    refreshed = dict(record)
    if int(refreshed.get("schema_version", 0)) != RUNTIME_EVIDENCE_SCHEMA_VERSION:
        raise SdkSecurityError(f"Unsupported runtime evidence schema: {refreshed.get('schema_version')}")
    refreshed["freshness"] = freshness_for(str(refreshed["expires_at"]), now=now)
    return refreshed


def freshness_for(expires_at: str | datetime, *, now: datetime | None = None) -> str:
    expires = parse_utc(expires_at) if isinstance(expires_at, str) else _as_utc(expires_at)
    current = now or utc_now()
    return "fresh" if _as_utc(current) <= expires else "stale"


def normalize_runtime_mode(mode: str) -> str:
    normalized = str(mode).upper()
    if normalized not in SDK_RUNTIME_MODES:
        raise SdkSecurityError(f"Unsupported SDK runtime mode: {mode}")
    return normalized


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    return _as_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return _as_utc(datetime.fromisoformat(text))


def fingerprint_text(value: str, *, length: int = 16) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:length]


def compact_json_value(value: Any) -> Any:
    """Keep returned SDK metadata small and JSON-safe."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_COMPACT_STRING_CHARS else value[:MAX_COMPACT_STRING_CHARS] + "..."
    if isinstance(value, Mapping):
        compact: JsonDict = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_COMPACT_COLLECTION_ITEMS:
                compact["truncated_items"] = len(value) - MAX_COMPACT_COLLECTION_ITEMS
                break
            compact[str(key)] = compact_json_value(item)
        return compact
    if isinstance(value, (list, tuple)):
        items = [compact_json_value(item) for item in value[:MAX_COMPACT_COLLECTION_ITEMS]]
        if len(value) > MAX_COMPACT_COLLECTION_ITEMS:
            items.append({"truncated_items": len(value) - MAX_COMPACT_COLLECTION_ITEMS})
        return items
    return compact_json_value(str(value))


def _normalize_tags(tags: Iterable[str]) -> list[str]:
    normalized = [str(tag).strip() for tag in tags if str(tag).strip()]
    if not normalized:
        raise SdkPermissionError("Runtime reads must request at least one tag")
    return normalized


def _bounded_int(value: int | str | float, *, minimum: int, maximum: int, name: str) -> int:
    number = int(value)
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if number > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return number


def _capability_for_data_type(data_type: str) -> str:
    normalized = str(data_type or "").upper()
    mapping = {
        "BOOL": "sdk_read_tag_value_bool",
        "SINT": "sdk_read_tag_value_sint",
        "INT": "sdk_read_tag_value_int",
        "DINT": "sdk_read_tag_value_dint",
        "LINT": "sdk_read_tag_value_lint",
        "USINT": "sdk_read_tag_value_usint",
        "UINT": "sdk_read_tag_value_uint",
        "UDINT": "sdk_read_tag_value_udint",
        "ULINT": "sdk_read_tag_value_ulint",
        "STRING": "sdk_read_tag_value_string",
        "REAL": "sdk_read_tag_value_real",
        "LREAL": "sdk_read_tag_value_lreal",
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise SdkSecurityError(f"Unsupported simulated tag data_type: {data_type}") from exc


def _normalize_signal(signal: str) -> str:
    normalized = str(signal or "sine").lower()
    if normalized not in {"sine", "sawtooth", "triangle", "square"}:
        raise SdkSecurityError(f"Unsupported simulated signal: {signal}")
    return normalized


def _simulated_signal_value(
    sample_index: int,
    *,
    tag_index: int,
    tag_count: int,
    signal: str,
    amplitude: float,
    offset: float,
    period_samples: int,
) -> float:
    tag_phase = tag_index / max(tag_count, 1)
    phase = ((sample_index / max(period_samples, 1)) + tag_phase) % 1.0
    if signal == "sine":
        return offset + amplitude * math.sin(2.0 * math.pi * phase)
    if signal == "sawtooth":
        return offset + amplitude * ((2.0 * phase) - 1.0)
    if signal == "triangle":
        return offset + amplitude * (1.0 - (4.0 * abs(phase - 0.5)))
    if signal == "square":
        return offset + (amplitude if phase < 0.5 else -amplitude)
    raise SdkSecurityError(f"Unsupported simulated signal: {signal}")


def _coerce_simulated_value(value: float, data_type: str) -> Any:
    if data_type == "BOOL":
        return bool(value >= 0)
    if data_type == "STRING":
        return str(round(value, 6))
    if data_type in {"REAL", "LREAL"}:
        return round(float(value), 6)
    return int(round(value))


def _runtime_evidence_preview(record: Mapping[str, Any]) -> JsonDict:
    controller = record.get("controller_identity")
    controller_name = controller.get("name") if isinstance(controller, Mapping) else None
    return {
        key: value
        for key, value in {
            "evidence_type": record.get("evidence_type"),
            "observed_at": record.get("observed_at"),
            "expires_at": record.get("expires_at"),
            "freshness": record.get("freshness"),
            "mode": record.get("mode"),
            "scope": record.get("scope"),
            "tag": record.get("tag"),
            "value": compact_json_value(record.get("value")),
            "data_type": record.get("data_type"),
            "source": record.get("source"),
            "quality": record.get("quality"),
            "session_id": record.get("session_id"),
            "sample_index": record.get("sample_index"),
            "sample_elapsed_seconds": record.get("sample_elapsed_seconds"),
            "controller": controller_name,
        }.items()
        if value not in (None, "")
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp_for_filename(value: datetime) -> str:
    return format_utc(value).replace(":", "").replace("-", "").replace("Z", "Z")


def _safe_token(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return safe[:80] or "runtime"
