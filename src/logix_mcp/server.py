"""FastMCP server exposing an ingested Logix workspace."""

from __future__ import annotations

from pathlib import Path
import hashlib
import sys
import json

from .diagnostics import run_diagnostics as run_diagnostics_impl
from .graph import (
    call_graph as graph_call_graph,
    impact_of as graph_impact_of,
    io_trace as graph_io_trace,
    tag_producers_consumers as graph_tag_producers_consumers,
)
from .intelligence import (
    aoi_instance_bindings as analysis_aoi_instance_bindings,
    cross_reference as analysis_cross_reference,
    decode_summary as analysis_decode_summary,
    exists as analysis_exists,
    get_fbd_sheet as analysis_get_fbd_sheet,
    coverage_limits as analysis_coverage_limits,
    get_operand_context as analysis_get_operand_context,
    get_routine_slice as analysis_get_routine_slice,
    resolve_alarm as analysis_resolve_alarm,
    search_project as analysis_search_project,
    scope_metadata as analysis_scope_metadata,
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
    inspect_workspace,
    query_entities,
    query_symbols,
    read_jsonl,
    search_entities as workspace_search_entities,
    search_logic as search_logic_rows,
    workspace_identity,
)


SUMMARY_TEXT_LIMIT = 200
RESULT_SPILL_THRESHOLD = 50_000
_BULKY_FIELDS = {"body", "attributes"}

# Per-kind whitelists keep listing rows lean enough that a default page of 200
# stays within the context budget; kinds without a whitelist fall back to the
# generic scalar projection.
_KIND_SUMMARY_KEYS = {
    "tag": ("id", "kind", "name", "scope", "data_type", "tag_type", "alias_for", "comment_count"),
    "udt": ("id", "kind", "name", "family", "class", "members_count"),
    "aoi": ("id", "kind", "name", "revision", "parameters_count", "local_tags_count", "routines_count"),
    "module": ("id", "kind", "name", "catalog_number", "parent_module", "slot", "address", "inhibited", "major_fault"),
    "program": ("id", "kind", "name", "main_routine"),
    "routine": ("id", "kind", "name", "program", "owner", "language", "unit_count", "fbd_node_count", "sfc_node_count"),
}


def summarize_row(row: dict) -> dict:
    """Project a row to its scalar fields; lists collapse to ``<key>_count``.

    Raw IR rows can embed full routine bodies, member trees, or node lists; a
    default listing must never serialize those (a default ``list_routines()``
    used to return 2.8M characters).
    """

    summary: dict = {}
    for key, value in row.items():
        if key in _BULKY_FIELDS:
            continue
        if isinstance(value, str):
            summary[key] = value if len(value) <= SUMMARY_TEXT_LIMIT else value[:SUMMARY_TEXT_LIMIT] + "..."
        elif isinstance(value, (int, float, bool)):
            summary[key] = value
        elif isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    keys = _KIND_SUMMARY_KEYS.get(str(row.get("kind") or ""))
    if keys:
        return {key: summary[key] for key in keys if key in summary}
    return summary


def envelope(rows: list[dict], limit: int, offset: int = 0, detail: str = "summary") -> dict:
    """Uniform collection envelope: items, total, has_more, truncated."""

    total = len(rows)
    offset = max(int(offset or 0), 0)
    limit = max(int(limit or 0), 0)
    page = rows[offset : offset + limit]
    items = list(page) if detail == "full" else [summarize_row(row) for row in page]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
        "truncated": max(0, total - offset - len(page)),
    }


def probe_envelope(rows: list[dict], limit: int, offset: int = 0, detail: str = "summary") -> dict:
    """Envelope for helpers that only fetched ``offset + limit + 1`` rows.

    The extra probed row makes ``has_more`` reliable, but the true total is
    unknown, so it is reported as ``None`` instead of a number that would lie.
    """

    offset = max(int(offset or 0), 0)
    limit = max(int(limit or 0), 0)
    has_more = len(rows) > offset + limit
    result = envelope(rows[: offset + limit], limit, offset, detail)
    result["has_more"] = has_more
    result["total"] = None
    result["truncated"] = None
    return result


def suggest_names(workspace: str | Path, name: str, kind: str | None = None, limit: int = 5) -> list[str]:
    from . import db

    try:
        if not db.has_index(workspace):
            return []
        with db.connect(workspace) as conn:
            params: list = [f"%{name.lower()}%"]
            sql = "SELECT DISTINCT name FROM symbols WHERE lower(name) LIKE ?"
            if kind:
                sql += " AND kind = ?"
                params.append(kind)
            sql += " ORDER BY length(name) LIMIT ?"
            params.append(limit)
            return [row["name"] for row in conn.execute(sql, params) if row["name"]]
    except Exception:
        return []


def not_found(workspace: str | Path, kind: str, name: str) -> dict:
    return {
        "found": False,
        "kind": kind,
        "name": name,
        "did_you_mean": suggest_names(workspace, name, kind=kind),
        "hint": "Try search_project(<name>) or exists(<name>); program scopes accept 'UWP' or 'Program:UWP'.",
    }


def resolve_alias(canonical_name: str, canonical_value: object | None = None, **aliases: object | None) -> str | dict:
    values = {
        name: str(value)
        for name, value in {canonical_name: canonical_value, **aliases}.items()
        if value not in (None, "")
    }
    if not values:
        return {"error": f"Missing required argument: {canonical_name}", "accepted_names": [canonical_name, *aliases.keys()]}
    unique = {value for value in values.values()}
    if len(unique) > 1:
        return {"error": "Conflicting aliases", "values": values}
    return next(iter(unique))


def validate_detail(detail: str) -> dict | None:
    if detail in {"summary", "detail", "full"}:
        return None
    return {"error": "Unsupported detail", "detail": detail, "accepted": ["summary", "detail", "full"]}


def json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def finish_result(result: dict, *, workspace: Path, tool: str, truncated: bool = False, spill: bool = False) -> dict:
    size = json_size(result)
    if spill and size > RESULT_SPILL_THRESHOLD:
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]
        spill_dir = workspace.parent / ".tmp" / "logix_mcp_spill"
        spill_dir.mkdir(parents=True, exist_ok=True)
        spill_path = spill_dir / f"{tool}-{digest}.json"
        spill_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        return {"tool": tool, "spilled": True, "spill_path": str(spill_path), "result_size": size, "truncated": True}
    result["result_size"] = size
    result["truncated"] = bool(truncated)
    return result


def compact_comment(row: dict) -> dict:
    return {
        key: value
        for key, value in {
            "target": row.get("target") or row.get("tag_name"),
            "text": _clip_text(row.get("text") or row.get("comment") or row.get("description")),
        }.items()
        if value not in (None, "")
    }


def compact_data(row: dict) -> dict:
    return {
        key: value
        for key, value in {
            "owner": row.get("owner") or row.get("owner_name"),
            "element": row.get("element"),
            "format": row.get("format"),
            "raw_text": _clip_text(row.get("raw_text"), 180),
        }.items()
        if value not in (None, "")
    }


def _clip_text(value: object, size: int = 240) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text if len(text) <= size else text[: size - 3] + "..."


def _project_rows(rows: list[dict], limit: int) -> list[dict]:
    return [summarize_row(row) for row in rows[:limit]]


def tag_context_view(bundle: dict, detail: str) -> tuple[dict, bool]:
    comments = list(bundle.get("comments") or [])
    tag_comments = list(bundle.get("tag_comments") or [])
    data = list(bundle.get("data") or [])
    references = list(bundle.get("references") or [])
    limits = {"summary": (5, 10, 10, 10), "detail": (25, 50, 50, 50)}[detail]
    comment_limit, tag_comment_limit, data_limit, ref_limit = limits
    truncated = any(
        [
            len(comments) > comment_limit,
            len(tag_comments) > tag_comment_limit,
            len(data) > data_limit,
            len(references) > ref_limit,
        ]
    )
    return (
        {
            "detail": detail,
            "tag": summarize_row(bundle.get("tag") or {}),
            "counts": {
                "comments": len(comments),
                "tag_comments": len(tag_comments),
                "data": len(data),
                "references": len(references),
            },
            "comments": [compact_comment(row) for row in comments[:comment_limit]],
            "tag_comments": [compact_comment(row) for row in tag_comments[:tag_comment_limit]],
            "data_preview": [compact_data(row) for row in data[:data_limit]],
            "references": _project_rows(references, ref_limit),
            "next_calls": [
                "get_operand_context(<tag>, detail='summary')",
                "cross_reference(<tag>, mode='members')",
                "get_tag_context(<tag>, detail='full') for the legacy raw bundle",
            ],
        },
        truncated,
    )


def aoi_context_view(bundle: dict, detail: str) -> tuple[dict, bool]:
    parameters = list(bundle.get("parameters") or [])
    local_tags = list(bundle.get("local_tags") or [])
    routines = list(bundle.get("routines") or [])
    limits = {"summary": (20, 20, 10), "detail": (100, 100, 50)}[detail]
    param_limit, local_limit, routine_limit = limits
    truncated = len(parameters) > param_limit or len(local_tags) > local_limit or len(routines) > routine_limit
    return (
        {
            "detail": detail,
            "definition": summarize_row(bundle.get("definition") or {}),
            "counts": {"parameters": len(parameters), "local_tags": len(local_tags), "routines": len(routines)},
            "parameters": _project_rows(parameters, param_limit),
            "local_tags": _project_rows(local_tags, local_limit),
            "routines": _project_rows(routines, routine_limit),
            "next_calls": ["get_aoi_context(<name>, detail='full') for the legacy raw bundle"],
        },
        truncated,
    )


def module_context_view(bundle: dict, detail: str) -> tuple[dict, bool]:
    ports = list(bundle.get("ports") or [])
    connections = [{key: value for key, value in row.items() if key != "io_tags"} for row in list(bundle.get("connections") or [])]
    io_tags = list(bundle.get("io_tags") or [])
    io_points = list(bundle.get("io_points") or [])
    limits = {"summary": (10, 10, 20, 50), "detail": (50, 50, 100, 250)}[detail]
    port_limit, conn_limit, tag_limit, point_limit = limits
    truncated = len(ports) > port_limit or len(connections) > conn_limit or len(io_tags) > tag_limit or len(io_points) > point_limit
    return (
        {
            "detail": detail,
            "module": summarize_row(bundle.get("module") or {}),
            "counts": {"ports": len(ports), "connections": len(connections), "io_tags": len(io_tags), "io_points": len(io_points)},
            "ports": _project_rows(ports, port_limit),
            "connections": _project_rows(connections, conn_limit),
            "io_tags": _project_rows(io_tags, tag_limit),
            "io_points": _project_rows(io_points, point_limit),
            "next_calls": ["get_module_context(<module>, detail='full') for embedded connection io_tags"],
        },
        truncated,
    )


def aoi_bindings_view(result: dict, detail: str) -> tuple[dict, bool]:
    instances = list(result.get("instances") or [])
    out_instances = []
    truncated = False
    binding_limit = 0 if detail == "summary" else 25
    for instance in instances:
        row = {key: instance.get(key) for key in ("instance", "aoi", "routine", "program", "owner", "sheet", "node_id", "evidence_ref", "summary") if instance.get(key) not in (None, "")}
        bindings = list(instance.get("bindings") or [])
        if detail == "detail":
            row["bindings"] = bindings[:binding_limit]
            if len(bindings) > binding_limit:
                row["bindings_truncated"] = len(bindings) - binding_limit
                truncated = True
        elif bindings:
            truncated = True
        out_instances.append(row)
    return (
        {
            "detail": detail,
            "query": result.get("query"),
            "found": result.get("found"),
            "instance_count": len(instances),
            "instances": out_instances,
            "limits": result.get("limits") or [],
            "next_calls": result.get("next_calls") or [],
        },
        truncated,
    )


def routine_touches_sfc(result: dict) -> bool:
    routine = result.get("routine") or {}
    if str(routine.get("language") or "").upper() == "SFC":
        return True
    return bool(result.get("sfc_nodes") or result.get("sfc_links"))


def create_server(workspace: str | Path):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("The MCP server requires the 'mcp' package. Install with: pip install -e .") from exc

    from mcp.types import ToolAnnotations

    workspace_path = Path(workspace).resolve()
    mcp = FastMCP("logix-mcp")

    # Every tool is a read-only query over the ingested workspace; ingestion is
    # CLI-only (`python -m logix_mcp ingest`) so the MCP surface cannot write.
    def _tool():
        return mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))

    @_tool()
    def project_summary() -> dict:
        return inspect_workspace(workspace_path)

    @_tool()
    def coverage_report() -> dict:
        """Return extraction coverage counts and missing P0/P1 surfaces."""

        coverage_path = workspace_path / "ir" / "coverage.json"
        if not coverage_path.exists():
            return {"error": f"coverage.json not found in {workspace_path}"}
        import json

        return json.loads(coverage_path.read_text(encoding="utf-8"))

    @_tool()
    def list_tags(scope: str | None = None, data_type: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        """List tags as summary rows: {items, total, has_more, truncated}."""

        rows = [row for row in read_jsonl(workspace_path, "symbols.jsonl") if row.get("kind") == "tag"]
        if scope:
            rows = [row for row in rows if row.get("scope") == scope]
        if data_type:
            rows = [row for row in rows if row.get("data_type") == data_type]
        return envelope(rows, limit, offset)

    @_tool()
    def get_tag(name: str, scope: str | None = None) -> dict:
        row = find_symbol(workspace_path, name, scope)
        if row and row.get("kind") == "tag":
            return row
        return not_found(workspace_path, "tag", name)

    @_tool()
    def get_tag_context(name: str, scope: str | None = None, detail: str = "summary", spill: bool = False) -> dict:
        """Return a tag with descriptions/comments, data/defaults, and references."""

        detail_error = validate_detail(detail)
        if detail_error:
            return detail_error
        bundle = get_tag_bundle(workspace_path, name, scope)
        if not bundle:
            return not_found(workspace_path, "tag", name)
        if detail == "full":
            return bundle
        result, truncated = tag_context_view(bundle, detail)
        return finish_result(result, workspace=workspace_path, tool="get_tag_context", truncated=truncated, spill=spill)

    @_tool()
    def list_udts(limit: int = 200, offset: int = 0) -> dict:
        """List UDTs as summary rows; use get_udt(name) for members."""

        rows = query_symbols(workspace_path, kind="udt", limit=1_000_000)
        return envelope(rows, limit, offset)

    @_tool()
    def get_udt(name: str, detail: str = "summary") -> dict:
        """One UDT with projected members; detail='full' returns the raw row."""

        row = find_symbol(workspace_path, name)
        if not row or row.get("kind") != "udt":
            return not_found(workspace_path, "udt", name)
        if detail == "full":
            return row
        summary = summarize_row(row)
        summary["members"] = [
            {key: member.get(key) for key in ("name", "data_type", "dimension", "radix", "description") if member.get(key) not in (None, "")}
            for member in (row.get("members") or [])[:250]
        ]
        return summary

    @_tool()
    def list_programs(limit: int = 200, offset: int = 0) -> dict:
        rows = query_symbols(workspace_path, kind="program", limit=1_000_000)
        return envelope(rows, limit, offset)

    @_tool()
    def get_program(name: str) -> dict:
        row = find_symbol(workspace_path, name)
        if row and row.get("kind") == "program":
            return row
        return not_found(workspace_path, "program", name)

    @_tool()
    def list_routines(program: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        """List routines as summary rows (no bodies); use get_routine_slice for logic."""

        rows = read_jsonl(workspace_path, "routines.jsonl")
        if program:
            rows = [row for row in rows if row.get("program") == program]
        return envelope(rows, limit, offset)

    @_tool()
    def get_routine(program: str, routine: str) -> dict:
        row = find_routine(workspace_path, program, routine)
        return row if row else not_found(workspace_path, "routine", routine)

    @_tool()
    def get_routine_context(
        program: str | None = None,
        routine: str | None = None,
        routine_id: str | None = None,
        detail: str = "summary",
        unit_limit: int = 100,
    ) -> dict:
        """Routine overview with bounded unit list.

        detail='summary' (default) returns the routine row, dataset counts, and
        clipped unit texts; detail='full' returns raw units/fbd/sfc rows and can
        be very large for FBD routines - prefer get_fbd_sheet/get_routine_slice.
        """

        result = workspace_get_routine_context(workspace_path, program, routine, routine_id)
        if result is None:
            return not_found(workspace_path, "routine", routine or routine_id or program or "")
        if detail == "full":
            return result
        units = result.get("units") or []
        unit_limit = max(int(unit_limit or 0), 1)
        unit_rows = []
        for unit in units[:unit_limit]:
            row = {key: unit.get(key) for key in ("id", "kind", "number", "sequence") if unit.get(key) is not None}
            for text_key in ("comment", "text"):
                value = unit.get(text_key)
                if isinstance(value, str) and value:
                    row[text_key] = value if len(value) <= 300 else value[:300] + "..."
            unit_rows.append(row)
        summary = {
            "routine": summarize_row(result.get("routine") or {}),
            "counts": {key: len(result.get(key) or []) for key in ("units", "xrefs", "fbd_nodes", "fbd_wires", "sfc_nodes", "sfc_links")},
            "units": unit_rows,
            "units_truncated": max(0, len(units) - unit_limit),
            "detail": "summary",
            "next_calls": [
                "get_routine_slice(..., query=<tag>) for bounded logic",
                "get_fbd_sheet(..., sheet=<n>) for FBD pseudo-equations",
                "get_routine_context(..., detail='full') for raw rows",
            ],
        }
        if routine_touches_sfc(result):
            limits = analysis_coverage_limits(workspace_path, "sfc")
            if limits:
                summary["limits"] = limits
        return summary

    @_tool()
    def list_aois(limit: int = 200, offset: int = 0) -> dict:
        """List AOI definitions as summary rows; use get_aoi(name) for parameters."""

        rows = query_symbols(workspace_path, kind="aoi", limit=1_000_000)
        return envelope(rows, limit, offset)

    @_tool()
    def get_aoi(name: str, detail: str = "summary") -> dict:
        """One AOI with projected parameters; detail='full' returns the raw row."""

        row = find_symbol(workspace_path, name)
        if not row or row.get("kind") != "aoi":
            return not_found(workspace_path, "aoi", name)
        if detail == "full":
            return row
        summary = summarize_row(row)
        summary["parameters"] = [
            {key: param.get(key) for key in ("name", "usage", "data_type", "required", "visible") if param.get(key) not in (None, "")}
            for param in (row.get("parameters") or [])[:100]
        ]
        summary["routine_names"] = [str(item.get("name")) for item in (row.get("routines") or [])[:25]]
        return summary

    @_tool()
    def get_aoi_context(name: str, detail: str = "summary", spill: bool = False) -> dict | None:
        """Return an AOI definition, parameters, local tags, and routines."""

        detail_error = validate_detail(detail)
        if detail_error:
            return detail_error
        bundle = get_aoi_bundle(workspace_path, name)
        if bundle is None:
            return not_found(workspace_path, "aoi", name)
        if detail == "full":
            return bundle
        result, truncated = aoi_context_view(bundle, detail)
        return finish_result(result, workspace=workspace_path, tool="get_aoi_context", truncated=truncated, spill=spill)

    @_tool()
    def list_modules(limit: int = 100, offset: int = 0) -> dict:
        """List modules as summary rows; use get_module_context for ports/IO."""

        rows = query_symbols(workspace_path, kind="module", limit=1_000_000)
        return envelope(rows, limit, offset)

    @_tool()
    def get_module_context(module: str | None = None, name: str | None = None, detail: str = "summary", spill: bool = False) -> dict | None:
        """Return a module with ports, connections, I/O tags, and point comments."""

        resolved = resolve_alias("module", module, name=name)
        if isinstance(resolved, dict):
            return resolved
        detail_error = validate_detail(detail)
        if detail_error:
            return detail_error
        bundle = get_module_bundle(workspace_path, resolved)
        if bundle is None:
            return not_found(workspace_path, "module", resolved)
        if detail == "full":
            return bundle
        result, truncated = module_context_view(bundle, detail)
        return finish_result(result, workspace=workspace_path, tool="get_module_context", truncated=truncated, spill=spill)

    @_tool()
    def list_entities(kind: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        rows = query_entities(workspace_path, kind=kind, limit=1_000_000)
        return envelope(rows, limit, offset)

    @_tool()
    def get_entity(entity_id: str) -> dict:
        row = workspace_get_entity(workspace_path, entity_id)
        return row if row else not_found(workspace_path, "entity", entity_id)

    @_tool()
    def search_entities(pattern: str | None = None, query: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        resolved = resolve_alias("pattern", pattern, query=query)
        if isinstance(resolved, dict):
            return resolved
        rows = workspace_search_entities(workspace_path, resolved, offset + limit + 1)
        return probe_envelope(rows, limit, offset)

    @_tool()
    def search_logic(pattern: str | None = None, query: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        resolved = resolve_alias("pattern", pattern, query=query)
        if isinstance(resolved, dict):
            return resolved
        rows = search_logic_rows(workspace_path, resolved, offset + limit + 1)
        return probe_envelope(rows, limit, offset)

    @_tool()
    def search_project(query: str, kinds: str | None = None, scope: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        """Compact FTS-backed project search with bounded snippets."""

        return analysis_search_project(workspace_path, query, kinds=kinds, scope=scope, limit=limit, offset=offset)

    @_tool()
    def exists(query: str, kinds: str | None = None, scope: str | None = None) -> dict:
        """Cheap existence check over project search."""

        return analysis_exists(workspace_path, query, kinds=kinds, scope=scope)

    @_tool()
    def get_operand_context(operand: str, scope: str | None = None, detail: str = "summary") -> dict:
        """Compact context for a tag/member operand, references, comments, and data preview."""

        return analysis_get_operand_context(workspace_path, operand, scope=scope, detail=detail)

    @_tool()
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

    @_tool()
    def get_fbd_sheet(
        program: str | None = None,
        routine: str | None = None,
        routine_id: str | None = None,
        sheet: str | None = None,
        form: str = "pseudo",
        limit: int = 100,
    ) -> dict:
        """Return compact pseudo-equations for one FBD sheet."""

        return analysis_get_fbd_sheet(workspace_path, program=program, routine=routine, routine_id=routine_id, sheet=sheet, form=form, limit=limit)

    @_tool()
    def cross_reference(
        symbol: str | None = None,
        name: str | None = None,
        operand: str | None = None,
        mode: str = "exact",
        access: str | None = None,
        destructive: bool | None = None,
        scope: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Logix-style cross reference with destructive classification and snippets."""

        resolved = resolve_alias("symbol", symbol, name=name, operand=operand)
        if isinstance(resolved, dict):
            return resolved
        return analysis_cross_reference(workspace_path, resolved, mode=mode, access=access, destructive=destructive, scope=scope, limit=limit, offset=offset)

    @_tool()
    def find_references(symbol: str, limit: int = 200, offset: int = 0) -> dict:
        rows = workspace_find_references(workspace_path, symbol, offset + limit + 1)
        return probe_envelope(rows, limit, offset, detail="full")

    @_tool()
    def trace_signal(symbol: str, direction: str = "upstream", max_depth: int = 4, limit: int = 100) -> dict:
        """Trace a signal through compact writers/readers and first-pass FBD flow."""

        return analysis_trace_signal(workspace_path, symbol, direction=direction, max_depth=max_depth, limit=limit)

    @_tool()
    def triage_issue(issue_text: str, limit: int = 5) -> dict:
        """PLC-first evidence bundle for a field issue description."""

        return analysis_triage_issue(workspace_path, issue_text, limit=limit)

    @_tool()
    def scope_metadata(issue_text: str | None = None) -> dict:
        """Describe in-scope offline PLC evidence and out-of-scope HMI/runtime limits."""

        return analysis_scope_metadata(workspace_path, issue_text)

    @_tool()
    def resolve_alarm(name_or_class: str, limit: int = 10) -> dict:
        """Resolve alarm records to source tags, messages, and PLC evidence."""

        return analysis_resolve_alarm(workspace_path, name_or_class, limit=limit)

    @_tool()
    def decode_summary(tag: str, limit: int = 50) -> dict:
        """Expand a summary coil/tag into candidate member bits and alarms."""

        return analysis_decode_summary(workspace_path, tag, limit=limit)

    @_tool()
    def aoi_instance_bindings(instance: str | None = None, name: str | None = None, detail: str = "summary", limit: int = 10, spill: bool = False) -> dict:
        """Return FBD AOI instance pin bindings, including unwired parameters."""

        resolved = resolve_alias("instance", instance, name=name)
        if isinstance(resolved, dict):
            return resolved
        detail_error = validate_detail(detail)
        if detail_error:
            return detail_error
        result = analysis_aoi_instance_bindings(workspace_path, resolved, limit=limit)
        if detail == "full":
            return result
        view, truncated = aoi_bindings_view(result, detail)
        return finish_result(view, workspace=workspace_path, tool="aoi_instance_bindings", truncated=truncated, spill=spill)

    @_tool()
    def tag_producers_consumers(name: str) -> dict:
        """List routines that write a tag (producers) vs read it (consumers)."""

        return graph_tag_producers_consumers(workspace_path, name)

    @_tool()
    def impact_of(name: str, max_depth: int = 3, limit: int = 300) -> dict:
        """Transitive change-propagation analysis from a tag through the logic."""

        return graph_impact_of(workspace_path, name, max_depth=max_depth, limit=limit)

    @_tool()
    def io_trace(name: str | None = None, symbol: str | None = None) -> dict:
        """Resolve a tag's alias chain to physical I/O points, logic, and alarms."""

        resolved = resolve_alias("name", name, symbol=symbol)
        if isinstance(resolved, dict):
            return resolved
        return graph_io_trace(workspace_path, resolved)

    @_tool()
    def call_graph(routine: str | None = None, program: str | None = None) -> dict:
        """Callers/callees of a routine, or the task/program scheduling tree."""

        return graph_call_graph(workspace_path, routine, program)

    @_tool()
    def run_diagnostics(rules: str | None = None, severity: str | None = None, limit: int = 50) -> dict:
        """Run static-analysis rules and return prioritized findings.

        Covers multiple-output writers, dead/uninitialized tags, broken aliases,
        unscheduled programs, inhibited/faulted modules, and unused AOIs/UDTs.
        Filter with rules (comma-separated rule names) and severity; limit caps
        the returned findings while summary keeps the uncapped totals.
        """

        return run_diagnostics_impl(workspace_path, rules=rules, severity=severity, limit=limit)

    return mcp


def run_server(workspace: str | Path) -> None:
    workspace_path = Path(workspace).resolve()
    try:
        identity = workspace_identity(workspace_path)
        print(
            "logix-mcp serving "
            f"{workspace_path.name} "
            f"(source={identity.get('source_path') or 'unknown'}, "
            f"export={identity.get('export_date') or 'unknown'}, "
            f"fingerprint={identity.get('fingerprint') or 'unknown'})",
            file=sys.stderr,
        )
    except Exception as exc:  # pragma: no cover - startup should still surface the real server error
        print(f"logix-mcp serving {workspace_path.name} (identity unavailable: {exc})", file=sys.stderr)
    create_server(workspace_path).run()
