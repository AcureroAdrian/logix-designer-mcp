"""FastMCP server exposing an ingested Logix workspace."""

from __future__ import annotations

from pathlib import Path

from .diagnostics import run_diagnostics as run_diagnostics_impl
from .graph import (
    call_graph as graph_call_graph,
    impact_of as graph_impact_of,
    io_trace as graph_io_trace,
    tag_producers_consumers as graph_tag_producers_consumers,
)
from .intelligence import (
    cross_reference as analysis_cross_reference,
    exists as analysis_exists,
    get_operand_context as analysis_get_operand_context,
    get_routine_slice as analysis_get_routine_slice,
    search_project as analysis_search_project,
    trace_signal as analysis_trace_signal,
    triage_issue as analysis_triage_issue,
)
from .workspace import (
    get_aoi_bundle,
    get_entity as workspace_get_entity,
    get_module_bundle,
    get_routine_context as workspace_get_routine_context,
    get_tag_bundle,
    find_references as workspace_find_references,
    find_routine,
    find_symbol,
    ingest_l5x,
    inspect_workspace,
    query_entities,
    query_symbols,
    read_jsonl,
    search_entities as workspace_search_entities,
    search_logic as search_logic_rows,
)


def create_server(workspace: str | Path):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("The MCP server requires the 'mcp' package. Install with: pip install -e .") from exc

    workspace_path = Path(workspace).resolve()
    mcp = FastMCP("logix-mcp")

    @mcp.tool()
    def load_project(path: str, out: str | None = None) -> dict:
        """Ingest an L5X file and make the generated workspace current."""

        nonlocal workspace_path
        result = ingest_l5x(path, out)
        workspace_path = Path(result["workspace"]).resolve()
        return result["project"]

    @mcp.tool()
    def project_summary() -> dict:
        return inspect_workspace(workspace_path)

    @mcp.tool()
    def coverage_report() -> dict:
        """Return extraction coverage counts and missing P0/P1 surfaces."""

        coverage_path = workspace_path / "ir" / "coverage.json"
        if not coverage_path.exists():
            return {"error": f"coverage.json not found in {workspace_path}"}
        import json

        return json.loads(coverage_path.read_text(encoding="utf-8"))

    @mcp.tool()
    def list_tags(scope: str | None = None, data_type: str | None = None, limit: int = 200) -> list[dict]:
        rows = [row for row in read_jsonl(workspace_path, "symbols.jsonl") if row.get("kind") == "tag"]
        if scope:
            rows = [row for row in rows if row.get("scope") == scope]
        if data_type:
            rows = [row for row in rows if row.get("data_type") == data_type]
        return rows[:limit]

    @mcp.tool()
    def get_tag(name: str, scope: str | None = None) -> dict | None:
        row = find_symbol(workspace_path, name, scope)
        return row if row and row.get("kind") == "tag" else None

    @mcp.tool()
    def get_tag_context(name: str, scope: str | None = None) -> dict | None:
        """Return a tag with descriptions/comments, data/defaults, and references."""

        return get_tag_bundle(workspace_path, name, scope)

    @mcp.tool()
    def list_udts(limit: int = 200) -> list[dict]:
        return query_symbols(workspace_path, kind="udt", limit=limit)

    @mcp.tool()
    def get_udt(name: str) -> dict | None:
        row = find_symbol(workspace_path, name)
        return row if row and row.get("kind") == "udt" else None

    @mcp.tool()
    def list_programs() -> list[dict]:
        return query_symbols(workspace_path, kind="program", limit=1000)

    @mcp.tool()
    def get_program(name: str) -> dict | None:
        row = find_symbol(workspace_path, name)
        return row if row and row.get("kind") == "program" else None

    @mcp.tool()
    def list_routines(program: str | None = None, limit: int = 200) -> list[dict]:
        rows = read_jsonl(workspace_path, "routines.jsonl")
        if program:
            rows = [row for row in rows if row.get("program") == program]
        return rows[:limit]

    @mcp.tool()
    def get_routine(program: str, routine: str) -> dict | None:
        return find_routine(workspace_path, program, routine)

    @mcp.tool()
    def get_routine_context(program: str | None = None, routine: str | None = None, routine_id: str | None = None) -> dict | None:
        """Return a routine with rung/ST/FBD/SFC units, graph rows, and xrefs."""

        return workspace_get_routine_context(workspace_path, program, routine, routine_id)

    @mcp.tool()
    def list_aois(limit: int = 200) -> list[dict]:
        return query_symbols(workspace_path, kind="aoi", limit=limit)

    @mcp.tool()
    def get_aoi(name: str) -> dict | None:
        row = find_symbol(workspace_path, name)
        return row if row and row.get("kind") == "aoi" else None

    @mcp.tool()
    def get_aoi_context(name: str) -> dict | None:
        """Return an AOI definition, parameters, local tags, and routines."""

        return get_aoi_bundle(workspace_path, name)

    @mcp.tool()
    def list_modules(limit: int = 200) -> list[dict]:
        return query_symbols(workspace_path, kind="module", limit=limit)

    @mcp.tool()
    def get_module_context(module: str) -> dict | None:
        """Return a module with ports, connections, I/O tags, and point comments."""

        return get_module_bundle(workspace_path, module)

    @mcp.tool()
    def list_entities(kind: str | None = None, limit: int = 200) -> list[dict]:
        return query_entities(workspace_path, kind=kind, limit=limit)

    @mcp.tool()
    def get_entity(entity_id: str) -> dict | None:
        return workspace_get_entity(workspace_path, entity_id)

    @mcp.tool()
    def search_entities(pattern: str, limit: int = 50) -> list[dict]:
        return workspace_search_entities(workspace_path, pattern, limit)

    @mcp.tool()
    def search_logic(pattern: str, limit: int = 50) -> list[dict]:
        return search_logic_rows(workspace_path, pattern, limit)

    @mcp.tool()
    def search_project(query: str, kinds: str | None = None, scope: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        """Compact FTS-backed project search with bounded snippets."""

        return analysis_search_project(workspace_path, query, kinds=kinds, scope=scope, limit=limit, offset=offset)

    @mcp.tool()
    def exists(query: str, kinds: str | None = None, scope: str | None = None) -> dict:
        """Cheap existence check over project search."""

        return analysis_exists(workspace_path, query, kinds=kinds, scope=scope)

    @mcp.tool()
    def get_operand_context(operand: str, scope: str | None = None, detail: str = "summary") -> dict:
        """Compact context for a tag/member operand, references, comments, and data preview."""

        return analysis_get_operand_context(workspace_path, operand, scope=scope, detail=detail)

    @mcp.tool()
    def get_routine_slice(
        program: str | None = None,
        routine: str | None = None,
        routine_id: str | None = None,
        sheet: str | None = None,
        unit_id: str | None = None,
        query: str | None = None,
        before: int = 1,
        after: int = 1,
    ) -> dict:
        """Return a bounded routine slice by sheet/unit/query."""

        return analysis_get_routine_slice(workspace_path, program, routine, routine_id, sheet, unit_id, query, before, after)

    @mcp.tool()
    def cross_reference(
        symbol: str,
        mode: str = "exact",
        access: str | None = None,
        destructive: bool | None = None,
        scope: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Logix-style cross reference with destructive classification and snippets."""

        return analysis_cross_reference(workspace_path, symbol, mode=mode, access=access, destructive=destructive, scope=scope, limit=limit, offset=offset)

    @mcp.tool()
    def find_references(symbol: str, limit: int = 200) -> list[dict]:
        return workspace_find_references(workspace_path, symbol, limit)

    @mcp.tool()
    def trace_signal(symbol: str, direction: str = "upstream", max_depth: int = 4, limit: int = 100) -> dict:
        """Trace a signal through compact writers/readers and first-pass FBD flow."""

        return analysis_trace_signal(workspace_path, symbol, direction=direction, max_depth=max_depth, limit=limit)

    @mcp.tool()
    def triage_issue(issue_text: str, limit: int = 5) -> dict:
        """PLC-first evidence bundle for a field issue description."""

        return analysis_triage_issue(workspace_path, issue_text, limit=limit)

    @mcp.tool()
    def tag_producers_consumers(name: str) -> dict:
        """List routines that write a tag (producers) vs read it (consumers)."""

        return graph_tag_producers_consumers(workspace_path, name)

    @mcp.tool()
    def impact_of(name: str, max_depth: int = 3, limit: int = 300) -> dict:
        """Transitive change-propagation analysis from a tag through the logic."""

        return graph_impact_of(workspace_path, name, max_depth=max_depth, limit=limit)

    @mcp.tool()
    def io_trace(name: str) -> dict:
        """Resolve a tag's alias chain to physical I/O points, logic, and alarms."""

        return graph_io_trace(workspace_path, name)

    @mcp.tool()
    def call_graph(routine: str | None = None, program: str | None = None) -> dict:
        """Callers/callees of a routine, or the task/program scheduling tree."""

        return graph_call_graph(workspace_path, routine, program)

    @mcp.tool()
    def run_diagnostics() -> dict:
        """Run static-analysis rules and return prioritized findings.

        Covers multiple-output writers, dead/uninitialized tags, broken aliases,
        unscheduled programs, inhibited/faulted modules, and unused AOIs/UDTs.
        """

        return run_diagnostics_impl(workspace_path)

    return mcp


def run_server(workspace: str | Path) -> None:
    create_server(workspace).run()
