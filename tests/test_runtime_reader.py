import importlib
import sys
from pathlib import Path

from logix_mcp import runtime_reader, runtime_store


def test_runtime_reader_does_not_import_pycomm3_at_module_import_time():
    sys.modules.pop("pycomm3", None)

    importlib.reload(runtime_reader)

    assert "pycomm3" not in sys.modules


def test_fake_read_tags_now_returns_compact_values():
    result = runtime_reader.read_tags_now("FAKE/PATH", ["Timer.ACC", "Timer.DN"], source="fake")

    assert result["ok"] is True
    assert result["operation"] == "read_tags_now"
    assert result["source"] == "fake"
    assert result["mode"] == "OFFLINE"
    assert result["comm_path_fingerprint"] == runtime_store.fingerprint_text("FAKE/PATH")
    assert [item["tag"] for item in result["results"]] == ["Timer.ACC", "Timer.DN"]
    assert "FAKE/PATH" not in str(result)


def test_fake_capture_writes_session_manifest_and_samples(tmp_path: Path):
    workspace = tmp_path / "demo.logix"

    result = runtime_reader.run_capture(
        runtime_reader.RuntimeCaptureRequest(
            workspace=workspace,
            path="FAKE",
            tags=("Timer.ACC",),
            interval_ms=100,
            duration_seconds=0.035,
            session_id="capture-unit",
            source="fake",
        )
    )

    assert result["status"] == "completed"
    summary = runtime_store.session_summary(workspace, "capture-unit")
    assert summary["sample_count"] >= 1
    assert summary["manifest"]["source"] == "fake"
    assert summary["tags"][0]["n_changes"] >= 0


def test_capture_honors_stop_file(tmp_path: Path):
    workspace = tmp_path / "demo.logix"
    session_id = "stop-unit"
    stop_file = tmp_path / "stop.capture"

    class StoppingReader(runtime_reader.FakeTagReader):
        def read_tags(self, tags):
            values = super().read_tags(tags)
            stop_file.write_text("stop", encoding="utf-8")
            return values

    result = runtime_reader.run_capture(
        runtime_reader.RuntimeCaptureRequest(
            workspace=workspace,
            path="FAKE",
            tags=("Timer.ACC",),
            interval_ms=100,
            duration_seconds=1,
            session_id=session_id,
            stop_file=stop_file,
            source="fake",
        ),
        reader=StoppingReader(),
    )

    assert result["status"] == "stopped"
    assert stop_file.exists()


def test_runtime_reader_source_exposes_no_controller_mutation_calls():
    source = Path(runtime_reader.__file__).read_text(encoding="utf-8")

    assert ".write(" not in source
    assert ".generic_message(" not in source
    assert "change_controller_mode" not in source


# Forbidden controller-mutation tokens. These must never appear in any runtime
# module so the pycomm3 read path can never write/download/change the PLC.
_FORBIDDEN_PLC_MUTATORS = (
    ".generic_message(",
    "set_tag",
    ".download(",
    "change_controller_mode",
    ".save(",
    "partial_import",
)


def test_runtime_modules_never_reference_plc_mutators():
    for module in (runtime_reader, runtime_store):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for token in _FORBIDDEN_PLC_MUTATORS:
            assert token not in source, f"{module.__name__} must not reference {token!r}"


def test_runtime_reader_has_no_pycomm3_write_call():
    # runtime_store legitimately uses file-handle ``.write(``; the reader (which
    # holds the LogixDriver) must never call ``.write(`` on anything.
    source = Path(runtime_reader.__file__).read_text(encoding="utf-8")
    assert ".write(" not in source
