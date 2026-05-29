"""Command line interface for Logix MCP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .workspace import ingest_l5x, inspect_workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="logix-mcp", description="Analyze Studio 5000 Logix Designer L5X exports.")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Parse an L5X file into a persistent analysis workspace.")
    ingest.add_argument("l5x", type=Path)
    ingest.add_argument("--out", type=Path, default=None, help="Output workspace directory. Defaults to <input>.logix.")
    ingest.add_argument("--no-copy-source", action="store_true", help="Do not copy the source L5X into source/original.")

    inspect = sub.add_parser("inspect", help="Print a summary of an ingested workspace.")
    inspect.add_argument("workspace", type=Path)

    serve = sub.add_parser("serve", help="Run the MCP server for an ingested workspace.")
    serve.add_argument("workspace", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "ingest":
        result = ingest_l5x(args.l5x, args.out, copy_source=not args.no_copy_source)
        print(json.dumps(result["project"], indent=2, ensure_ascii=False))
        return 0
    if args.command == "inspect":
        print(json.dumps(inspect_workspace(args.workspace), indent=2, ensure_ascii=False))
        return 0
    if args.command == "serve":
        from .server import run_server

        run_server(args.workspace)
        return 0
    parser.error("unknown command")
    return 2
