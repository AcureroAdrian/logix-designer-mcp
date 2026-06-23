"""Fail-closed scaffolding for the optional Logix Designer SDK boundary.

This module defines the SDK capability allowlist, compact local logging, and
scratch-export path validation. It never opens projects, connects to a
controller, or imports the optional SDK at import time.

Runtime tag reads are intentionally NOT part of the SDK surface. Live values are
read through ``pycomm3`` (see ``runtime_reader`` / ``runtime_store``). The SDK is
limited to offline export (``save_as`` / ``partial_export_to_xml_file``) plus the
manual-only, denylisted ``upload_to_new_project`` acquisition path. This keeps a
single runtime path and honors "SDK fuera del polling".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Iterable, Mapping
import hashlib
import json
import uuid


JsonDict = dict[str, Any]

SDK_PACKAGE_NAME = "logix_designer_sdk"
SDK_LOG_DIR = Path(".tmp") / "sdk_logs"
SDK_EXPORT_DIR = Path(".tmp") / "sdk_exports"
MAX_COMPACT_STRING_CHARS = 300
MAX_COMPACT_COLLECTION_ITEMS = 20

SDK_SURFACE_EXPORT_OFFLINE = "export_offline_scratch"


class SdkSecurityError(RuntimeError):
    """An SDK request crossed the declared read-only boundary."""


class SdkUnavailableError(RuntimeError):
    """The optional SDK package is not installed or not loaded."""


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


# Offline export/admin only. Runtime reads were removed: pycomm3 is the runtime
# path, so the SDK must not expose tag/mode/state reads (debate row 20).
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


def fingerprint_text(value: str, *, length: int = 16) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:length]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    return _as_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp_for_filename(value: datetime) -> str:
    return format_utc(value).replace(":", "").replace("-", "").replace("Z", "Z")
