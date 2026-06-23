"""Command line interface for Logix MCP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .intelligence import (
    aoi_instance_bindings,
    cross_reference,
    decode_summary,
    exists,
    get_fbd_sheet,
    get_operand_context,
    get_routine_slice,
    resolve_alarm,
    search_project,
    scope_metadata,
    trace_signal,
    triage_issue,
)
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

    search = sub.add_parser("search", help="Compact FTS-backed search over an ingested workspace.")
    search.add_argument("workspace", type=Path)
    search.add_argument("query")
    search.add_argument("--kinds", default=None, help="Comma-separated kind filter.")
    search.add_argument("--scope", default=None)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)

    exists_cmd = sub.add_parser("exists", help="Cheap existence check over compact project search.")
    exists_cmd.add_argument("workspace", type=Path)
    exists_cmd.add_argument("query")
    exists_cmd.add_argument("--kinds", default=None)
    exists_cmd.add_argument("--scope", default=None)

    operand = sub.add_parser("operand", help="Compact context for a tag/member operand.")
    operand.add_argument("workspace", type=Path)
    operand.add_argument("operand")
    operand.add_argument("--scope", default=None)
    operand.add_argument("--detail", choices=["summary", "full"], default="summary")

    routine_slice = sub.add_parser("routine-slice", help="Bounded routine slice by sheet, unit, or query.")
    routine_slice.add_argument("workspace", type=Path)
    routine_slice.add_argument("--program", default=None)
    routine_slice.add_argument("--routine", default=None)
    routine_slice.add_argument("--routine-id", default=None)
    routine_slice.add_argument("--sheet", default=None)
    routine_slice.add_argument("--unit-id", default=None)
    routine_slice.add_argument("--query", default=None)
    routine_slice.add_argument("--before", type=int, default=1)
    routine_slice.add_argument("--after", type=int, default=1)

    fbd_sheet = sub.add_parser("fbd-sheet", help="Compact pseudo-equation view of one FBD sheet.")
    fbd_sheet.add_argument("workspace", type=Path)
    fbd_sheet.add_argument("--program", default=None)
    fbd_sheet.add_argument("--routine", default=None)
    fbd_sheet.add_argument("--routine-id", default=None)
    fbd_sheet.add_argument("--sheet", default=None)
    fbd_sheet.add_argument("--form", choices=["pseudo", "summary"], default="pseudo")
    fbd_sheet.add_argument("--limit", type=int, default=100)

    xref = sub.add_parser("xref", help="Logix-style compact cross reference.")
    xref.add_argument("workspace", type=Path)
    xref.add_argument("symbol")
    xref.add_argument("--mode", choices=["exact", "members", "base"], default="exact")
    xref.add_argument("--access", default=None)
    xref.add_argument("--destructive", action="store_true", default=None)
    xref.add_argument("--scope", default=None)
    xref.add_argument("--limit", type=int, default=50)
    xref.add_argument("--offset", type=int, default=0)

    trace = sub.add_parser("trace", help="Trace a signal upstream through compact evidence.")
    trace.add_argument("workspace", type=Path)
    trace.add_argument("symbol")
    trace.add_argument("--direction", default="upstream")
    trace.add_argument("--max-depth", type=int, default=4)
    trace.add_argument("--limit", type=int, default=100)

    triage = sub.add_parser("triage", help="PLC-first evidence bundle for a field issue.")
    triage.add_argument("workspace", type=Path)
    triage.add_argument("issue_text", nargs="+")
    triage.add_argument("--limit", type=int, default=5)

    scope = sub.add_parser("scope", help="Describe in-scope/offline evidence and likely limits.")
    scope.add_argument("workspace", type=Path)
    scope.add_argument("issue_text", nargs="*")

    resolve = sub.add_parser("resolve-alarm", help="Resolve alarm records to source tags and PLC evidence.")
    resolve.add_argument("workspace", type=Path)
    resolve.add_argument("name_or_class")
    resolve.add_argument("--limit", type=int, default=10)

    summary = sub.add_parser("decode-summary", help="Expand a summary coil/tag into member bits and alarms.")
    summary.add_argument("workspace", type=Path)
    summary.add_argument("tag")
    summary.add_argument("--limit", type=int, default=50)

    bindings = sub.add_parser("aoi-bindings", help="Return FBD AOI instance pin bindings.")
    bindings.add_argument("workspace", type=Path)
    bindings.add_argument("instance")
    bindings.add_argument("--limit", type=int, default=10)

    sdk_status_cmd = sub.add_parser("sdk-status", help="Show optional SDK allowlist/fail-closed status.")

    simulate = sub.add_parser("simulate-runtime", help="Generate simulated SDK-style runtime tag samples.")
    simulate.add_argument("workspace", type=Path)
    simulate.add_argument("--tag", action="append", required=True, help="Tag path to sample; repeat for multiple tags.")
    simulate.add_argument("--samples", type=int, default=10)
    simulate.add_argument("--interval-seconds", type=float, default=1.0)
    simulate.add_argument("--data-type", default="REAL")
    simulate.add_argument("--signal", choices=["sine", "sawtooth", "triangle", "square"], default="sine")
    simulate.add_argument("--amplitude", type=float, default=100.0)
    simulate.add_argument("--offset", type=float, default=0.0)
    simulate.add_argument("--period-samples", type=int, default=20)
    simulate.add_argument("--scope", default=None)
    simulate.add_argument("--ttl-seconds", type=int, default=300)
    simulate.add_argument("--preview-only", action="store_true", help="Return samples without writing runtime_evidence files.")

    runtime_summary = sub.add_parser("runtime-summary", help="Summarize volatile runtime evidence for a workspace.")
    runtime_summary.add_argument("workspace", type=Path)

    runtime_evidence = sub.add_parser("runtime-evidence", help="List compact runtime evidence rows.")
    runtime_evidence.add_argument("workspace", type=Path)
    runtime_evidence.add_argument("--tag", default=None)
    runtime_evidence.add_argument("--scope", default=None)
    runtime_evidence.add_argument("--freshness", choices=["fresh", "stale"], default=None)
    runtime_evidence.add_argument("--limit", type=int, default=50)
    runtime_evidence.add_argument("--offset", type=int, default=0)

    return parser


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "ingest":
        result = ingest_l5x(args.l5x, args.out, copy_source=not args.no_copy_source)
        _print(result["project"])
        return 0
    if args.command == "inspect":
        _print(inspect_workspace(args.workspace))
        return 0
    if args.command == "serve":
        from .server import run_server

        run_server(args.workspace)
        return 0
    if args.command == "search":
        _print(search_project(args.workspace, args.query, kinds=args.kinds, scope=args.scope, limit=args.limit, offset=args.offset))
        return 0
    if args.command == "exists":
        _print(exists(args.workspace, args.query, kinds=args.kinds, scope=args.scope))
        return 0
    if args.command == "operand":
        _print(get_operand_context(args.workspace, args.operand, scope=args.scope, detail=args.detail))
        return 0
    if args.command == "routine-slice":
        _print(
            get_routine_slice(
                args.workspace,
                program=args.program,
                routine=args.routine,
                routine_id=args.routine_id,
                sheet=args.sheet,
                unit_id=args.unit_id,
                query=args.query,
                before=args.before,
                after=args.after,
            )
        )
        return 0
    if args.command == "fbd-sheet":
        _print(
            get_fbd_sheet(
                args.workspace,
                program=args.program,
                routine=args.routine,
                routine_id=args.routine_id,
                sheet=args.sheet,
                form=args.form,
                limit=args.limit,
            )
        )
        return 0
    if args.command == "xref":
        _print(
            cross_reference(
                args.workspace,
                args.symbol,
                mode=args.mode,
                access=args.access,
                destructive=args.destructive,
                scope=args.scope,
                limit=args.limit,
                offset=args.offset,
            )
        )
        return 0
    if args.command == "trace":
        _print(trace_signal(args.workspace, args.symbol, direction=args.direction, max_depth=args.max_depth, limit=args.limit))
        return 0
    if args.command == "triage":
        _print(triage_issue(args.workspace, " ".join(args.issue_text), limit=args.limit))
        return 0
    if args.command == "scope":
        _print(scope_metadata(args.workspace, " ".join(args.issue_text) if args.issue_text else None))
        return 0
    if args.command == "resolve-alarm":
        _print(resolve_alarm(args.workspace, args.name_or_class, limit=args.limit))
        return 0
    if args.command == "decode-summary":
        _print(decode_summary(args.workspace, args.tag, limit=args.limit))
        return 0
    if args.command == "aoi-bindings":
        _print(aoi_instance_bindings(args.workspace, args.instance, limit=args.limit))
        return 0
    if args.command == "sdk-status":
        from . import sdk_adapter

        _print({"status": sdk_adapter.sdk_status(), "registry": sdk_adapter.validate_sdk_registry()})
        return 0
    if args.command == "simulate-runtime":
        from . import sdk_adapter

        _print(
            sdk_adapter.simulate_runtime_tag_stream(
                args.workspace,
                args.tag,
                samples=args.samples,
                interval_seconds=args.interval_seconds,
                data_type=args.data_type,
                signal=args.signal,
                amplitude=args.amplitude,
                offset=args.offset,
                period_samples=args.period_samples,
                scope=args.scope,
                ttl_seconds=args.ttl_seconds,
                persist=not args.preview_only,
            )
        )
        return 0
    if args.command == "runtime-summary":
        from . import sdk_adapter

        _print(sdk_adapter.runtime_evidence_summary(args.workspace))
        return 0
    if args.command == "runtime-evidence":
        from . import sdk_adapter

        _print(
            sdk_adapter.query_runtime_evidence(
                args.workspace,
                tag=args.tag,
                scope=args.scope,
                freshness=args.freshness,
                limit=args.limit,
                offset=args.offset,
            )
        )
        return 0
    parser.error("unknown command")
    return 2
