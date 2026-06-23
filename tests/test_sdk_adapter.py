import asyncio
from datetime import datetime, timezone
from pathlib import Path
import json

import pytest

from logix_mcp import sdk_adapter
from logix_mcp.server import create_server
from logix_mcp.workspace import ingest_l5x

from test_parser_workspace import SIMPLE_L5X


FIXED_NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


def test_sdk_registry_is_export_only_and_rejects_dangerous_methods():
    result = sdk_adapter.validate_sdk_registry()

    assert result["ok"] is True
    assert set(sdk_adapter.allowed_capability_names()) == {"sdk_export_l5x", "sdk_partial_export"}
    assert "sdk_upload_to_new_project" not in sdk_adapter.allowed_capability_names()
    assert "upload_to_new_project" not in sdk_adapter.allowed_sdk_method_names()
    assert "upload_to_new_project" in sdk_adapter.ADMIN_MANUAL_ONLY_SDK_METHOD_NAMES
    assert "upload_to_new_project" in sdk_adapter.PUBLIC_DENIED_NAMES
    assert not set(sdk_adapter.allowed_sdk_method_names()) & sdk_adapter.DENIED_SDK_METHOD_NAMES
    assert all("*" not in name for name in sdk_adapter.allowed_capability_names())
    assert all("*" not in name for name in sdk_adapter.allowed_sdk_method_names())

    with pytest.raises(sdk_adapter.SdkSecurityError):
        sdk_adapter.validate_public_surface(["download"])
    with pytest.raises(sdk_adapter.SdkSecurityError):
        sdk_adapter.validate_public_surface(["sdk_upload_to_new_project"])


def test_sdk_runtime_read_capabilities_are_retired():
    # Runtime reads moved to pycomm3 (debate row 20); the SDK must not expose
    # any tag/mode/state read capability.
    names = sdk_adapter.allowed_capability_names()
    assert not any(name.startswith("sdk_read_") for name in names)
    for retired in ("sdk_read_tag_value_dint", "sdk_read_controller_mode", "sdk_read_connected_state"):
        assert not sdk_adapter.is_capability_allowed(retired)
        with pytest.raises(sdk_adapter.SdkSecurityError):
            sdk_adapter.capability_spec(retired)
    assert not hasattr(sdk_adapter, "simulate_runtime_tag_stream")
    assert not hasattr(sdk_adapter, "RuntimeReadPermission")
    assert not hasattr(sdk_adapter, "build_runtime_evidence")


def test_sdk_adapter_source_has_no_generic_dispatch():
    source = Path(sdk_adapter.__file__).read_text(encoding="utf-8")

    assert "getattr(" not in source
    assert "def invoke" not in source


def test_missing_sdk_fails_closed_without_import_side_effects():
    package_name = "__missing_logix_sdk_for_unit_test__"

    assert sdk_adapter.sdk_status(package_name) == {
        "available": False,
        "package": package_name,
        "mode": "optional_fail_closed",
    }
    with pytest.raises(sdk_adapter.SdkUnavailableError):
        sdk_adapter.require_sdk_available(package_name)


def test_compact_sdk_log_redacts_raw_and_sensitive_fields(tmp_path: Path):
    result = sdk_adapter.write_compact_sdk_log(
        tmp_path,
        {
            "operation": "export_l5x",
            "ok": True,
            "mode": "OFFLINE",
            "duration_s": 1.25,
            "comm_path": "secret/plant/path",
            "stdout": "very verbose SDK output",
            "raw_xml": "<Controller>huge</Controller>",
        },
        now=FIXED_NOW,
    )

    path = Path(result["log_handle"])
    assert path.is_relative_to(tmp_path / ".tmp" / "sdk_logs")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["operation"] == "export_l5x"
    assert payload["mode"] == "OFFLINE"
    assert payload["redacted_keys_count"] == 3
    assert "comm_path" in payload["sensitive_keys_redacted"]
    serialized = path.read_text(encoding="utf-8")
    assert "secret/plant/path" not in serialized
    assert "very verbose SDK output" not in serialized
    assert "<Controller>huge</Controller>" not in serialized


def test_scratch_export_paths_are_confined_to_ignored_tmp(tmp_path: Path):
    target = sdk_adapter.validate_scratch_output_path(tmp_path, "snapshot.L5X")

    assert target == (tmp_path / ".tmp" / "sdk_exports" / "snapshot.L5X").resolve()
    with pytest.raises(sdk_adapter.SdkSecurityError):
        sdk_adapter.validate_scratch_output_path(tmp_path, tmp_path / "snapshot.L5X")
    with pytest.raises(sdk_adapter.SdkSecurityError):
        sdk_adapter.validate_scratch_output_path(tmp_path, "snapshot.ACD")


def test_sdk_upload_is_not_registered_and_no_legacy_runtime_tools(tmp_path: Path):
    source = tmp_path / "demo.L5X"
    source.write_text(SIMPLE_L5X, encoding="utf-8")
    workspace = tmp_path / "demo.logix"
    ingest_l5x(source, workspace)

    server = create_server(workspace)
    tool_names = {tool.name for tool in asyncio.run(server.list_tools())}

    assert "upload_to_new_project" not in tool_names
    assert "sdk_upload_to_new_project" not in tool_names
    assert not tool_names & sdk_adapter.PUBLIC_DENIED_NAMES
    assert "sdk_status" in tool_names
    # Legacy SDK runtime/evidence tools are gone; pycomm3 session tools replace them.
    assert "simulate_runtime_read_preview" not in tool_names
    assert "list_runtime_evidence" not in tool_names
    assert "runtime_evidence_summary" in tool_names
    assert {"read_tags_now", "start_runtime_capture", "list_runtime_sessions"} <= tool_names
