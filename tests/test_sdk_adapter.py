import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import pytest

from logix_mcp import sdk_adapter
from logix_mcp.server import create_server
from logix_mcp.workspace import ingest_l5x

from test_parser_workspace import SIMPLE_L5X


FIXED_NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


def test_sdk_registry_is_named_and_rejects_dangerous_methods():
    result = sdk_adapter.validate_sdk_registry()

    assert result["ok"] is True
    assert "sdk_export_l5x" in sdk_adapter.allowed_capability_names()
    assert "sdk_read_tag_value_dint" in sdk_adapter.allowed_capability_names()
    assert "sdk_upload_to_new_project" not in sdk_adapter.allowed_capability_names()
    assert "upload_to_new_project" not in sdk_adapter.allowed_sdk_method_names()
    assert "upload_to_new_project" in sdk_adapter.ADMIN_MANUAL_ONLY_SDK_METHOD_NAMES
    assert "upload_to_new_project" in sdk_adapter.PUBLIC_DENIED_NAMES
    assert not set(sdk_adapter.allowed_sdk_method_names()) & sdk_adapter.DENIED_SDK_METHOD_NAMES
    assert all("*" not in name for name in sdk_adapter.allowed_capability_names())
    assert all("*" not in name for name in sdk_adapter.allowed_sdk_method_names())

    assert sdk_adapter.is_capability_allowed("sdk_read_tag_value_dint")
    assert not sdk_adapter.is_capability_allowed("sdk_read_tag_value_dint_extra")

    with pytest.raises(sdk_adapter.SdkSecurityError):
        sdk_adapter.capability_spec("sdk_read_tag_value_")
    with pytest.raises(sdk_adapter.SdkSecurityError):
        sdk_adapter.validate_public_surface(["download"])
    with pytest.raises(sdk_adapter.SdkSecurityError):
        sdk_adapter.validate_public_surface(["sdk_upload_to_new_project"])


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


def test_runtime_permission_requires_exact_capability_and_online_grant():
    permission = sdk_adapter.RuntimeReadPermission(
        capability="sdk_read_tag_value_dint",
        reason="unit test",
        approved_by="Adrian Acurero",
        max_tags=1,
        expires_at=sdk_adapter.format_utc(FIXED_NOW + timedelta(minutes=5)),
    )

    assert sdk_adapter.validate_runtime_permission(
        "sdk_read_tag_value_dint",
        permission,
        mode="OFFLINE",
        tag_count=1,
        now=FIXED_NOW,
    )["ok"]

    with pytest.raises(sdk_adapter.SdkPermissionError):
        sdk_adapter.validate_runtime_permission(
            "sdk_read_tag_value_real",
            permission,
            mode="OFFLINE",
            tag_count=1,
            now=FIXED_NOW,
        )
    with pytest.raises(sdk_adapter.SdkPermissionError):
        sdk_adapter.validate_runtime_permission(
            "sdk_read_tag_value_dint",
            permission,
            mode="ONLINE",
            tag_count=1,
            now=FIXED_NOW,
        )
    with pytest.raises(sdk_adapter.SdkPermissionError):
        sdk_adapter.validate_runtime_permission(
            "sdk_read_tag_value_dint",
            permission,
            mode="OFFLINE",
            tag_count=2,
            now=FIXED_NOW,
        )
    with pytest.raises(sdk_adapter.SdkPermissionError):
        sdk_adapter.validate_runtime_permission(
            "sdk_read_tag_value_dint",
            permission,
            mode="OFFLINE",
            tag_count=1,
            now=FIXED_NOW + timedelta(minutes=6),
        )

    online_permission = sdk_adapter.RuntimeReadPermission(
        capability="sdk_read_controller_mode",
        reason="confirmed runtime check",
        approved_by="Adrian Acurero",
        allow_online=True,
        comm_path="secret/plant/path",
        max_tags=1,
        expires_at=sdk_adapter.format_utc(FIXED_NOW + timedelta(minutes=5)),
    )
    assert sdk_adapter.validate_runtime_permission(
        "sdk_read_controller_mode",
        online_permission,
        mode="ONLINE",
        tag_count=1,
        now=FIXED_NOW,
    )["ok"]


def test_compact_sdk_log_redacts_raw_and_sensitive_fields(tmp_path: Path):
    result = sdk_adapter.write_compact_sdk_log(
        tmp_path,
        {
            "operation": "read_controller_mode",
            "ok": True,
            "mode": "RUN",
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
    assert payload["operation"] == "read_controller_mode"
    assert payload["mode"] == "RUN"
    assert payload["redacted_keys_count"] == 3
    assert "comm_path" in payload["sensitive_keys_redacted"]
    serialized = path.read_text(encoding="utf-8")
    assert "secret/plant/path" not in serialized
    assert "very verbose SDK output" not in serialized
    assert "<Controller>huge</Controller>" not in serialized


def test_runtime_evidence_stays_separate_from_ir_and_tracks_freshness(tmp_path: Path):
    permission = sdk_adapter.RuntimeReadPermission(
        capability="sdk_read_tag_value_dint",
        reason="field value check",
        approved_by="Adrian Acurero",
        allow_online=True,
        comm_path="secret/plant/path",
        max_tags=1,
    )
    record = sdk_adapter.build_runtime_evidence(
        evidence_type="tag_value",
        source_fingerprint="abc123",
        mode="ONLINE",
        controller_identity={"name": "Demo", "serial": "1234"},
        permission=permission,
        retention_policy={"ttl_seconds": 30, "purge": "manual"},
        ttl_seconds=30,
        scope="Controller",
        tag="ACCELERATION_RAMP_RATE",
        comm_path="secret/plant/path",
        value=36,
        observed_at=FIXED_NOW,
    )

    assert record["freshness"] == "fresh"
    assert record["comm_path_fingerprint"] == sdk_adapter.fingerprint_text("secret/plant/path")
    assert "comm_path" not in record["permission"]

    written = sdk_adapter.write_runtime_evidence(tmp_path, record, now=FIXED_NOW)
    path = Path(written["path"])
    assert path.is_relative_to(tmp_path / "runtime_evidence")
    assert not path.is_relative_to(tmp_path / "ir")

    fresh = sdk_adapter.read_runtime_evidence(path, now=FIXED_NOW + timedelta(seconds=29))
    stale = sdk_adapter.read_runtime_evidence(path, now=FIXED_NOW + timedelta(seconds=31))
    assert fresh["freshness"] == "fresh"
    assert stale["freshness"] == "stale"
    assert sdk_adapter.list_runtime_evidence(tmp_path, now=FIXED_NOW + timedelta(seconds=31))[0]["freshness"] == "stale"


def test_scratch_export_paths_are_confined_to_ignored_tmp(tmp_path: Path):
    target = sdk_adapter.validate_scratch_output_path(tmp_path, "snapshot.L5X")

    assert target == (tmp_path / ".tmp" / "sdk_exports" / "snapshot.L5X").resolve()
    with pytest.raises(sdk_adapter.SdkSecurityError):
        sdk_adapter.validate_scratch_output_path(tmp_path, tmp_path / "snapshot.L5X")
    with pytest.raises(sdk_adapter.SdkSecurityError):
        sdk_adapter.validate_scratch_output_path(tmp_path, "snapshot.ACD")


def test_sdk_upload_is_not_registered_as_a_normal_mcp_tool(tmp_path: Path):
    source = tmp_path / "demo.L5X"
    source.write_text(SIMPLE_L5X, encoding="utf-8")
    workspace = tmp_path / "demo.logix"
    ingest_l5x(source, workspace)

    server = create_server(workspace)
    tool_names = {tool.name for tool in asyncio.run(server.list_tools())}

    assert "upload_to_new_project" not in tool_names
    assert "sdk_upload_to_new_project" not in tool_names
    assert not tool_names & sdk_adapter.PUBLIC_DENIED_NAMES
