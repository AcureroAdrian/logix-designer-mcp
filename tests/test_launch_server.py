import importlib.util
import json
from pathlib import Path


def _load_launcher():
    path = Path(__file__).resolve().parents[1] / "logix-plugin" / "scripts" / "launch_server.py"
    spec = importlib.util.spec_from_file_location("launch_server_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_workspace(root: Path, name: str, export_date: str, source_text: str) -> Path:
    source = root / name.replace(".logix", ".L5X")
    source.write_text(source_text, encoding="utf-8")
    workspace = root / name
    (workspace / "ir").mkdir(parents=True)
    project = {
        "source_path": str(source),
        "workspace": str(workspace),
        "root": {
            "TargetName": "Arnold",
            "ExportDate": export_date,
        },
        "controller": {
            "name": "Arnold",
            "processor_type": "1756-L85E",
            "project_sn": "16#014d_cbd5",
        },
        "counts": {},
        "coverage": {},
        "warnings": [],
    }
    (workspace / "ir" / "project.json").write_text(json.dumps(project), encoding="utf-8")
    return workspace


def test_mcp_json_override_warns_when_newer_workspace_exists(tmp_path: Path, monkeypatch, capsys):
    launcher = _load_launcher()
    old = _write_workspace(
        tmp_path,
        "Arnold_0058_029_061826.logix",
        "Thu Jun 18 12:00:00 2026",
        "old source",
    )
    newer = _write_workspace(
        tmp_path,
        "Arnold_0058_029_062226.logix",
        "Mon Jun 22 12:43:23 2026",
        "new source",
    )
    config = {
        "mcpServers": {
            "logix-mcp": {
                "command": "python",
                "args": ["-m", "logix_mcp", "serve", old.name],
            }
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.delenv("LOGIX_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)

    selected = launcher.find_workspace()

    assert selected == old.resolve()
    stderr = capsys.readouterr().err
    assert "without auto-switching" in stderr
    assert old.name in stderr
    assert newer.name in stderr
    assert "fingerprint=" in stderr
