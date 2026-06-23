"""Launcher for the logix-mcp server bundled with the logix-tools plugin.

Resolves the workspace to serve in this order:
1. LOGIX_WORKSPACE env var (explicit path to a .logix workspace dir).
2. The workspace declared in the project's .mcp.json.
3. A single *.logix workspace in the project dir.

The logix_mcp package is imported from LOGIX_MCP_SRC (set in plugin.json)
so the plugin works without pip-installing the package.
"""
from __future__ import annotations

from datetime import datetime
import os
import sys
import json
from pathlib import Path

src = os.environ.get("LOGIX_MCP_SRC")
if src and src not in sys.path:
    sys.path.insert(0, src)

try:
    from logix_mcp.server import run_server
    from logix_mcp.workspace import workspace_identity as read_workspace_identity
except ImportError as exc:  # pragma: no cover
    sys.exit(
        "logix-tools plugin: cannot import logix_mcp. "
        f"Check LOGIX_MCP_SRC in plugin.json. Original error: {exc}"
    )


def workspace_from_mcp_json(cwd: Path) -> Path | None:
    """If the project's .mcp.json already declares a logix workspace, honor it."""
    cfg = cwd / ".mcp.json"
    if not cfg.exists():
        return None
    try:
        servers = json.loads(cfg.read_text(encoding="utf-8")).get("mcpServers", {})
    except Exception:
        return None
    for server in servers.values():
        for arg in server.get("args", []):
            if isinstance(arg, str) and arg.endswith(".logix"):
                ws = (cwd / arg).resolve()
                if ws.is_dir():
                    return ws
    return None


def workspace_info(workspace: Path) -> dict | None:
    try:
        identity = read_workspace_identity(workspace)
    except Exception:
        return None
    return {
        **identity,
        "path": Path(workspace).resolve(),
        "export_datetime": parse_export_date(identity.get("export_date")),
    }


def parse_export_date(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None


def latest_workspace_by_export_date(cwd: Path) -> dict | None:
    candidates = []
    for workspace in sorted(path for path in cwd.glob("*.logix") if path.is_dir()):
        info = workspace_info(workspace)
        if info is not None and info["export_datetime"] is not None:
            candidates.append(info)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["export_datetime"], item["fingerprint"], item["path"].name))


def format_identity(info: dict) -> str:
    return (
        f"{info['path'].name} "
        f"(source_path={info.get('source_path') or 'unknown'}, "
        f"ExportDate={info.get('export_date') or 'unknown'}, "
        f"fingerprint={info.get('fingerprint') or 'unknown'})"
    )


def warn_if_workspace_not_latest(cwd: Path, selected: Path) -> bool:
    selected_info = workspace_info(selected)
    latest_info = latest_workspace_by_export_date(cwd)
    if selected_info is None or latest_info is None:
        return False
    if selected_info["path"] == latest_info["path"]:
        return False
    selected_date = selected_info.get("export_datetime")
    latest_date = latest_info.get("export_datetime")
    if selected_date is not None and latest_date is not None and selected_date >= latest_date:
        return False
    print(
        "logix-tools plugin WARNING: .mcp.json selects an older Logix workspace; "
        "honoring it without auto-switching.\n"
        f"  selected: {format_identity(selected_info)}\n"
        f"  latest:   {format_identity(latest_info)}",
        file=sys.stderr,
    )
    return True


def emit_workspace_banner(workspace: Path) -> None:
    info = workspace_info(workspace)
    if info is None:
        print(f"logix-tools plugin: serving workspace {workspace}", file=sys.stderr)
        return
    print(f"logix-tools plugin: serving {format_identity(info)}", file=sys.stderr)


def find_workspace() -> Path:
    env = os.environ.get("LOGIX_WORKSPACE")
    if env:
        ws = Path(env)
        if not ws.exists():
            sys.exit(f"logix-tools plugin: LOGIX_WORKSPACE does not exist: {ws}")
        return ws
    cwd = Path.cwd()
    ws = workspace_from_mcp_json(cwd)
    if ws:
        warn_if_workspace_not_latest(cwd, ws)
        return ws
    matches = sorted(p for p in cwd.glob("*.logix") if p.is_dir())
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(
            f"logix-tools plugin: no *.logix workspace found in {cwd}. "
            "Run 'logix-mcp ingest <file.l5x>' first or set LOGIX_WORKSPACE."
        )
    sys.exit(
        "logix-tools plugin: multiple *.logix workspaces found: "
        + ", ".join(p.name for p in matches)
        + ". Set LOGIX_WORKSPACE or declare one in .mcp.json."
    )


if __name__ == "__main__":
    workspace = find_workspace()
    emit_workspace_banner(workspace)
    run_server(workspace)
