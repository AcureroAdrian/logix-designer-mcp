"""Compact PLC-first analysis helpers for the Logix MCP workspace.

These functions are intentionally small-output query surfaces. They sit above
the SQLite index and IR rows so agents can inspect projects without reading
large JSONL or Markdown files into context.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import json
import re
import sqlite3

from . import db


JsonDict = dict[str, Any]

MAX_SEARCH_LIMIT = 100
MAX_XREF_LIMIT = 200
MAX_TRACE_LIMIT = 100
MAX_FBD_BRANCHES = 3


def search_project(
    workspace: str | Path,
    query: str,
    kinds: str | list[str] | None = None,
    scope: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> JsonDict:
    """Search the workspace FTS index and return compact snippets."""

    limit = _bounded_int(limit, 20, MAX_SEARCH_LIMIT)
    offset = max(int(offset or 0), 0)
    kind_values = _csv(kinds)
    if not query.strip():
        return _page("search_project", query, limit, offset, [], 0)

    if not db.has_index(workspace):
        return _jsonl_search(workspace, query, kind_values, scope, limit, offset)

    with db.connect(workspace) as conn:
        rows, total = _fts_search(conn, query, kind_values, scope, limit, offset)
        if not rows:
            rows, total = _like_search(conn, query, kind_values, scope, limit, offset)
    rows = _dedupe_search_rows(rows)
    return _page("search_project", query, limit, offset, rows, total)


def exists(
    workspace: str | Path,
    query: str,
    kinds: str | list[str] | None = None,
    scope: str | None = None,
) -> JsonDict:
    """Cheap existence check over the same compact search surface."""

    result = search_project(workspace, query, kinds=kinds, scope=scope, limit=1, offset=0)
    first = result["items"][0] if result["items"] else None
    return {
        "query": query,
        "exists": result["total"] > 0,
        "count": result["total"],
        "first": first,
    }


def cross_reference(
    workspace: str | Path,
    symbol: str,
    mode: str = "exact",
    access: str | list[str] | None = None,
    destructive: bool | None = None,
    scope: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonDict:
    """Return Logix-style cross references with destructive classification."""

    if not db.has_index(workspace):
        rows = _fallback_xrefs(workspace, symbol, mode, access, destructive, scope)
        total = len(rows)
        page_rows = rows[offset : offset + _bounded_int(limit, 50, MAX_XREF_LIMIT)]
        return _xref_result(symbol, mode, access, destructive, scope, page_rows, total, limit, offset)

    limit = _bounded_int(limit, 50, MAX_XREF_LIMIT)
    offset = max(int(offset or 0), 0)
    where, params = _xref_where(symbol, mode)
    access_values = _csv(access)
    if access_values:
        where.append(f"access IN ({','.join('?' for _ in access_values)})")
        params.extend(access_values)
    if destructive is True:
        where.append("access IN ('write', 'read_write')")
    elif destructive is False:
        where.append("access NOT IN ('write', 'read_write')")
    if scope:
        where.append("json LIKE ?")
        params.append(f'%"program": "{scope}"%')

    clause = " AND ".join(where)
    with db.connect(workspace) as conn:
        total = conn.execute(f"SELECT count(*) FROM xrefs WHERE {clause}", params).fetchone()[0]
        cursor = conn.execute(
            f"SELECT json FROM xrefs WHERE {clause} ORDER BY routine, source, symbol LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = [_enrich_xref(conn, json.loads(row["json"])) for row in cursor]
        summary = _xref_summary(conn, clause, params)
    return _xref_result(symbol, mode, access, destructive, scope, rows, total, limit, offset, summary)


def get_operand_context(
    workspace: str | Path,
    operand: str,
    scope: str | None = None,
    detail: str = "summary",
) -> JsonDict:
    """Return compact context for a tag/member operand."""

    base = _base_symbol(operand)
    member = operand[len(base) :] if operand != base else ""
    symbol = db.find_symbol(workspace, operand, scope) if db.has_index(workspace) else None
    base_symbol = db.find_symbol(workspace, base, scope) if db.has_index(workspace) and base != operand else symbol
    refs = cross_reference(workspace, operand, mode="exact", limit=25)
    member_refs = cross_reference(workspace, base, mode="members", limit=25) if operand == base else refs
    comments = db.comments_for_target(workspace, operand)[:5] if db.has_index(workspace) else []
    tag_comments = db.tag_comments(workspace, base)[:10] if db.has_index(workspace) else []
    data = db.tag_data(workspace, base)[:10] if db.has_index(workspace) else []

    out: JsonDict = {
        "operand": operand,
        "base_symbol": base,
        "member": member or None,
        "scope": scope,
        "found": bool(symbol or base_symbol or refs["total"] or member_refs["total"]),
        "symbol": _compact_symbol(symbol),
        "base": _compact_symbol(base_symbol) if base_symbol is not symbol else None,
        "references": {
            "exact_total": refs["total"],
            "members_total": member_refs["total"],
            "summary": refs.get("summary") or {},
            "sample": refs["rows"][:10],
        },
        "comments": [_compact_comment(row) for row in comments],
        "tag_comments": [_compact_comment(row) for row in tag_comments],
        "data_preview": [_compact_data(row) for row in data],
        "next_calls": [
            f"cross_reference({operand!r}, mode='exact')",
            f"trace_signal({operand!r}, direction='upstream')",
        ],
    }
    if detail == "full":
        out["raw_data"] = data
    return out


def get_routine_slice(
    workspace: str | Path,
    program: str | None = None,
    routine: str | None = None,
    routine_id: str | None = None,
    sheet: str | int | None = None,
    unit_id: str | None = None,
    query: str | None = None,
    before: int = 1,
    after: int = 1,
) -> JsonDict:
    """Return a bounded, evidence-oriented routine slice."""

    context = _routine_context(workspace, program, routine, routine_id)
    if context is None:
        return {"found": False, "routine": routine, "program": program, "routine_id": routine_id, "items": []}

    units = list(context.get("units") or [])
    selected_indexes = _selected_unit_indexes(context, units, sheet, unit_id, query, before, after)
    if not selected_indexes and not (sheet or unit_id or query):
        selected_indexes = list(range(min(len(units), 20)))

    fbd_by_sheet = _fbd_by_sheet(context)
    items = [_compact_unit(units[index], fbd_by_sheet) for index in selected_indexes[:50]]
    return {
        "found": True,
        "routine": _compact_routine(context["routine"]),
        "selection": {
            "sheet": str(sheet) if sheet is not None else None,
            "unit_id": unit_id,
            "query": query,
            "before": before,
            "after": after,
        },
        "total_units": len(units),
        "returned": len(items),
        "truncated": len(selected_indexes) > len(items),
        "items": items,
    }


def trace_signal(
    workspace: str | Path,
    symbol: str,
    direction: str = "upstream",
    max_depth: int = 4,
    limit: int = 100,
) -> JsonDict:
    """Trace a signal through compact references and first-pass FBD flow."""

    limit = _bounded_int(limit, 50, MAX_TRACE_LIMIT)
    max_depth = _bounded_int(max_depth, 4, 12)
    if direction != "upstream":
        refs = cross_reference(workspace, symbol, mode="members", destructive=False, limit=limit)
        return {
            "symbol": symbol,
            "direction": direction,
            "status": "limited",
            "message": "V1 implements full tracing for upstream paths; downstream returns compact consumers.",
            "references": refs,
        }

    refs = cross_reference(workspace, symbol, mode="members", destructive=True, limit=limit)
    paths = []
    unresolved = []
    for ref in refs["rows"][:limit]:
        routine_id = ref.get("routine")
        context = _routine_context(workspace, None, None, routine_id)
        if not context:
            unresolved.append({"source": ref.get("source"), "reason": "routine_context_not_found"})
            continue
        language = str(context.get("routine", {}).get("language") or ref.get("language") or "").upper()
        if language == "FBD":
            fbd_path = _trace_fbd_upstream(context, symbol, ref, max_depth)
            paths.append({"type": "fbd", "reference": ref, "path": fbd_path})
            unresolved.extend(fbd_path.get("unresolved", []))
        else:
            unit = _source_unit(context, ref.get("source"))
            if unit:
                paths.append({"type": language.lower() or "logic", "reference": ref, "unit": _compact_unit(unit, {})})
            else:
                unresolved.append({"source": ref.get("source"), "reason": "source_unit_not_found"})

    return {
        "symbol": symbol,
        "direction": direction,
        "status": "ok" if paths else "unresolved",
        "writer_count": refs["total"],
        "paths": paths,
        "unresolved": _dedupe_dicts(unresolved),
        "limits": ["FBD trace is V1: it follows wires/connectors and reports unresolved gaps instead of inferring missing logic."],
        "next_calls": [f"cross_reference({symbol!r}, destructive=True)", f"get_operand_context({symbol!r})"],
    }


def triage_issue(workspace: str | Path, issue_text: str, limit: int = 5) -> JsonDict:
    """Create a compact PLC-first evidence bundle for a field issue."""

    limit = _bounded_int(limit, 5, 20)
    search = search_project(workspace, issue_text, limit=limit)
    if not search["items"]:
        for token in _issue_tokens(issue_text):
            search = search_project(workspace, token, limit=limit)
            if search["items"]:
                break

    likely_tags = _likely_names(search["items"], issue_text)[:limit]
    candidates = []
    evidence = []
    for name in likely_tags:
        operand = get_operand_context(workspace, name)
        trace = trace_signal(workspace, name, limit=10)
        symbol_row = operand.get("symbol") or {}
        base_row = operand.get("base") or {}
        candidates.append(
            {
                "name": name,
                "kind": symbol_row.get("kind") or base_row.get("kind") or "unknown",
                "reference_total": operand.get("references", {}).get("exact_total", 0),
                "trace_status": trace.get("status"),
                "first_writer": (trace.get("paths") or [{}])[0].get("reference"),
            }
        )
        evidence.extend(operand.get("references", {}).get("sample", [])[:3])

    limits = []
    if re.search(r"\b(hmi|screen|mcc|red|green|color|colour|display|slow|runtime)\b", issue_text, re.I):
        limits.append("needs_hmi_export_or_runtime")
    if not likely_tags:
        limits.append("no_likely_plc_tag_found")

    return {
        "issue": issue_text,
        "candidates": candidates,
        "likely_tags": likely_tags,
        "evidence": evidence[:20],
        "limits": limits,
        "next_calls": [
            "search_project(<specific equipment/tag text>)",
            "cross_reference(<tag>, mode='members')",
            "trace_signal(<tag>, direction='upstream')",
            "get_routine_slice(..., query=<tag>)",
        ],
    }


def _fts_search(
    conn: sqlite3.Connection,
    query: str,
    kinds: list[str],
    scope: str | None,
    limit: int,
    offset: int,
) -> tuple[list[JsonDict], int]:
    fts = _fts_query(query)
    where = ["search_index MATCH ?"]
    params: list[Any] = [fts]
    _add_search_filters(where, params, kinds, scope)
    clause = " AND ".join(where)
    try:
        total = conn.execute(f"SELECT count(*) FROM search_index WHERE {clause}", params).fetchone()[0]
        cursor = conn.execute(
            "SELECT kind, name, scope, snippet(search_index, 3, '[', ']', ' ... ', 32) AS snippet, "
            f"bm25(search_index) AS rank FROM search_index WHERE {clause} "
            "ORDER BY rank LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
    except sqlite3.Error:
        return [], 0
    return [_search_row(row) for row in cursor], total


def _like_search(
    conn: sqlite3.Connection,
    query: str,
    kinds: list[str],
    scope: str | None,
    limit: int,
    offset: int,
) -> tuple[list[JsonDict], int]:
    like = f"%{query}%"
    where = ["(text LIKE ? OR name LIKE ?)"]
    params: list[Any] = [like, like]
    _add_search_filters(where, params, kinds, scope)
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) FROM search_index WHERE {clause}", params).fetchone()[0]
    cursor = conn.execute(
        f"SELECT kind, name, scope, text FROM search_index WHERE {clause} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [
        {
            "kind": row["kind"],
            "name": row["name"],
            "scope": row["scope"],
            "snippet": _snippet(row["text"] or row["name"] or "", query),
            "evidence_ref": f"search_index:{row['kind']}:{row['scope']}:{row['name']}",
        }
        for row in cursor
    ], total


def _jsonl_search(workspace: str | Path, query: str, kinds: list[str], scope: str | None, limit: int, offset: int) -> JsonDict:
    rows = []
    needle = query.lower()
    for dataset in ["symbols", "entities", "routines", "routine_units", "comments", "alarms", "messages"]:
        for row in _read_jsonl(workspace, dataset):
            text = " ".join(str(value) for value in row.values() if isinstance(value, (str, int, float)))
            if needle not in text.lower():
                continue
            if kinds and str(row.get("kind") or dataset) not in kinds:
                continue
            if scope and row.get("scope") != scope and row.get("program") != scope and row.get("owner") != scope:
                continue
            rows.append(
                {
                    "kind": row.get("kind") or dataset,
                    "name": row.get("name") or row.get("routine") or row.get("tag_name"),
                    "scope": row.get("scope") or row.get("program") or row.get("owner"),
                    "snippet": _snippet(text, query),
                    "evidence_ref": row.get("id"),
                }
            )
    return _page("search_project", query, limit, offset, rows[offset : offset + limit], len(rows))


def _add_search_filters(where: list[str], params: list[Any], kinds: list[str], scope: str | None) -> None:
    if kinds:
        where.append(f"kind IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    if scope:
        where.append("scope = ?")
        params.append(scope)


def _search_row(row: sqlite3.Row) -> JsonDict:
    return {
        "kind": row["kind"],
        "name": row["name"],
        "scope": row["scope"],
        "snippet": row["snippet"],
        "rank": row["rank"],
        "evidence_ref": f"search_index:{row['kind']}:{row['scope']}:{row['name']}",
    }


def _xref_where(symbol: str, mode: str) -> tuple[list[str], list[Any]]:
    mode = (mode or "exact").lower()
    if mode not in {"exact", "members", "base"}:
        raise ValueError("mode must be one of: exact, members, base")
    pattern = _like_escape(symbol)
    if mode == "exact":
        return ["symbol = ? COLLATE NOCASE"], [symbol]
    if mode == "members":
        return [
            "(symbol = ? COLLATE NOCASE OR symbol LIKE ? ESCAPE '\\' OR symbol LIKE ? ESCAPE '\\')"
        ], [symbol, pattern + ".%", pattern + "[%"]
    return ["base_symbol = ? COLLATE NOCASE"], [_base_symbol(symbol)]


def _xref_summary(conn: sqlite3.Connection, clause: str, params: list[Any]) -> JsonDict:
    def grouped(column: str) -> list[JsonDict]:
        cursor = conn.execute(
            f"SELECT {column} AS key, count(*) AS count FROM xrefs WHERE {clause} "
            f"GROUP BY {column} ORDER BY count DESC, key LIMIT 20",
            params,
        )
        return [{"key": row["key"], "count": row["count"]} for row in cursor]

    return {
        "by_access": grouped("access"),
        "by_instruction": grouped("instruction"),
        "by_routine": grouped("routine"),
    }


def _xref_result(
    symbol: str,
    mode: str,
    access: str | list[str] | None,
    destructive: bool | None,
    scope: str | None,
    rows: list[JsonDict],
    total: int,
    limit: int,
    offset: int,
    summary: JsonDict | None = None,
) -> JsonDict:
    summary = summary or {
        "by_access": _counter_rows(row.get("access") for row in rows),
        "by_instruction": _counter_rows(row.get("instruction") for row in rows),
        "by_routine": _counter_rows(row.get("routine") for row in rows),
    }
    return {
        "symbol": symbol,
        "mode": mode,
        "filters": {"access": access, "destructive": destructive, "scope": scope},
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "summary": summary,
        "rows": rows,
    }


def _enrich_xref(conn: sqlite3.Connection, ref: JsonDict) -> JsonDict:
    source = ref.get("source")
    evidence = _evidence_for_source(conn, str(source or ""))
    return {
        "symbol": ref.get("symbol"),
        "base_symbol": ref.get("base_symbol"),
        "access": ref.get("access"),
        "destructive": ref.get("access") in {"write", "read_write"},
        "instruction": ref.get("instruction"),
        "confidence": ref.get("confidence"),
        "program": ref.get("program"),
        "routine": ref.get("routine"),
        "routine_name": ref.get("routine_name"),
        "owner": ref.get("owner"),
        "language": ref.get("language"),
        "location": ref.get("location"),
        "source": source,
        "operand": ref.get("operand"),
        "snippet": evidence.get("snippet"),
        "evidence_ref": evidence.get("evidence_ref") or source,
    }


def _evidence_for_source(conn: sqlite3.Connection, source: str) -> JsonDict:
    if not source:
        return {}
    for dataset in ["routine_units", "fbd_nodes", "sfc_nodes"]:
        row = conn.execute("SELECT json FROM ir_rows WHERE dataset = ? AND id = ? LIMIT 1", (dataset, source)).fetchone()
        if row:
            obj = json.loads(row["json"])
            return {"snippet": _record_snippet(obj), "evidence_ref": obj.get("id")}
    return {}


def _fallback_xrefs(
    workspace: str | Path,
    symbol: str,
    mode: str,
    access: str | list[str] | None,
    destructive: bool | None,
    scope: str | None,
) -> list[JsonDict]:
    access_values = set(_csv(access))
    rows = []
    for ref in _read_jsonl(workspace, "xrefs"):
        if not _xref_matches(ref, symbol, mode):
            continue
        if access_values and ref.get("access") not in access_values:
            continue
        is_destructive = ref.get("access") in {"write", "read_write"}
        if destructive is not None and destructive != is_destructive:
            continue
        if scope and ref.get("program") != scope and ref.get("owner") != scope:
            continue
        row = dict(ref)
        row["destructive"] = is_destructive
        row["snippet"] = None
        row["evidence_ref"] = ref.get("source")
        rows.append(row)
    return rows


def _xref_matches(ref: JsonDict, symbol: str, mode: str) -> bool:
    ref_symbol = str(ref.get("symbol") or "")
    ref_lower = ref_symbol.lower()
    target = symbol.lower()
    if mode == "exact":
        return ref_lower == target
    if mode == "members":
        return ref_lower == target or ref_lower.startswith(target + ".") or ref_lower.startswith(target + "[")
    return str(ref.get("base_symbol") or "").lower() == _base_symbol(symbol).lower()


def _routine_context(workspace: str | Path, program: str | None, routine: str | None, routine_id: str | None) -> JsonDict | None:
    if db.has_index(workspace):
        return db.routine_context(workspace, program, routine, routine_id)
    routines = _read_jsonl(workspace, "routines")
    selected = None
    for row in routines:
        if routine_id and row.get("id") == routine_id:
            selected = row
            break
        if routine and row.get("name") == routine and (program is None or row.get("program") == program):
            selected = row
            break
    if selected is None:
        return None
    rid = selected["id"]
    return {
        "routine": selected,
        "units": [row for row in _read_jsonl(workspace, "routine_units") if row.get("routine_id") == rid],
        "xrefs": [row for row in _read_jsonl(workspace, "xrefs") if row.get("routine") == rid],
        "fbd_nodes": [row for row in _read_jsonl(workspace, "fbd_nodes") if row.get("routine_id") == rid],
        "fbd_wires": [row for row in _read_jsonl(workspace, "fbd_wires") if row.get("routine_id") == rid],
        "sfc_nodes": [row for row in _read_jsonl(workspace, "sfc_nodes") if row.get("routine_id") == rid],
        "sfc_links": [row for row in _read_jsonl(workspace, "sfc_links") if row.get("routine_id") == rid],
    }


def _selected_unit_indexes(
    context: JsonDict,
    units: list[JsonDict],
    sheet: str | int | None,
    unit_id: str | None,
    query: str | None,
    before: int,
    after: int,
) -> list[int]:
    if unit_id:
        return [i for i, unit in enumerate(units) if unit.get("id") == unit_id]
    if sheet is not None:
        sheet_text = str(sheet)
        return [i for i, unit in enumerate(units) if str(unit.get("number")) == sheet_text or str(unit.get("sheet_number")) == sheet_text]
    if query:
        needle = query.lower()
        matches = []
        fbd_by_sheet = _fbd_by_sheet(context)
        for index, unit in enumerate(units):
            text = _unit_search_text(unit)
            if unit.get("kind") == "fbd_sheet":
                bundle = fbd_by_sheet.get(unit.get("id"), {})
                text += " " + json.dumps(bundle, ensure_ascii=False)
            if needle in text.lower():
                matches.append(index)
        selected: set[int] = set()
        for index in matches:
            for around in range(max(0, index - before), min(len(units), index + after + 1)):
                selected.add(around)
        return sorted(selected)
    return []


def _fbd_by_sheet(context: JsonDict) -> dict[object, JsonDict]:
    out: dict[object, JsonDict] = {}
    for node in context.get("fbd_nodes") or []:
        out.setdefault(node.get("sheet_id"), {"nodes": [], "wires": []})["nodes"].append(node)
    for wire in context.get("fbd_wires") or []:
        out.setdefault(wire.get("sheet_id"), {"nodes": [], "wires": []})["wires"].append(wire)
    return out


def _compact_unit(unit: JsonDict, fbd_by_sheet: dict[object, JsonDict]) -> JsonDict:
    row = {
        "id": unit.get("id"),
        "kind": unit.get("kind"),
        "routine": unit.get("routine"),
        "program": unit.get("program"),
        "owner": unit.get("owner"),
        "language": unit.get("language"),
        "number": unit.get("number"),
        "sequence": unit.get("sequence"),
        "comment": _clip(unit.get("comment")),
        "text": _clip(unit.get("text")),
        "instructions": [_compact_instruction(item) for item in unit.get("instructions", [])[:20]],
        "calls": unit.get("calls", [])[:20],
    }
    if unit.get("kind") == "fbd_sheet":
        bundle = fbd_by_sheet.get(unit.get("id"), {})
        nodes = bundle.get("nodes", [])
        wires = bundle.get("wires", [])
        row["nodes"] = [_compact_fbd_node(node) for node in nodes[:25]]
        row["wires"] = [_compact_wire(wire) for wire in wires[:40]]
        row["node_count"] = len(nodes)
        row["wire_count"] = len(wires)
        if len(nodes) > 25:
            row["truncated_nodes"] = len(nodes) - 25
        if len(wires) > 40:
            row["truncated_wires"] = len(wires) - 40
        row["text_boxes"] = unit.get("text_boxes", [])[:10]
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def _trace_fbd_upstream(context: JsonDict, symbol: str, ref: JsonDict, max_depth: int) -> JsonDict:
    nodes = context.get("fbd_nodes") or []
    wires = context.get("fbd_wires") or []
    by_full_id = {node.get("id"): node for node in nodes}
    by_key = {(node.get("sheet_id"), str(node.get("node_id"))): node for node in nodes}
    connector_sources: dict[str, list[JsonDict]] = {}
    for node in nodes:
        if node.get("node_type") == "OCon" and node.get("connector_name"):
            connector_sources.setdefault(str(node["connector_name"]), []).append(node)

    incoming: dict[tuple[object, str], list[JsonDict]] = {}
    for wire in wires:
        incoming.setdefault((wire.get("sheet_id"), str(wire.get("to_id"))), []).append(wire)

    start_nodes = []
    if ref.get("source") in by_full_id:
        start_nodes.append(by_full_id[ref.get("source")])
    start_nodes.extend(_find_fbd_target_nodes(nodes, symbol))
    start_nodes = _dedupe_nodes(start_nodes)
    unresolved: list[JsonDict] = []

    def trace_node(node: JsonDict, depth: int, seen: set[tuple[object, str]]) -> JsonDict:
        key = (node.get("sheet_id"), str(node.get("node_id")))
        compact = _compact_fbd_node(node)
        if key in seen:
            compact["unresolved"] = "cycle"
            unresolved.append({"node": node.get("id"), "reason": "cycle"})
            return compact
        if depth >= max_depth:
            compact["unresolved"] = "max_depth"
            unresolved.append({"node": node.get("id"), "reason": "max_depth"})
            return compact
        node_type = node.get("node_type")
        if node_type == "IRef":
            compact["leaf_operand"] = node.get("operand")
            return compact
        if node_type == "ICon":
            name = node.get("connector_name")
            producers = connector_sources.get(str(name), [])
            if not producers:
                compact["unresolved"] = "connector_source_not_found"
                unresolved.append({"node": node.get("id"), "connector": name, "reason": "connector_source_not_found"})
                return compact
            compact["upstream"] = [trace_node(producer, depth + 1, {*seen, key}) for producer in producers]
            return compact

        all_input_wires = incoming.get(key, [])
        input_wires = all_input_wires[:MAX_FBD_BRANCHES]
        upstream = []
        for wire in input_wires:
            source_node = by_key.get((wire.get("sheet_id"), str(wire.get("from_id"))))
            if source_node:
                upstream.append({"wire": _compact_wire(wire), "source": trace_node(source_node, depth + 1, {*seen, key})})
            else:
                unresolved.append({"wire": wire.get("id"), "from_id": wire.get("from_id"), "reason": "source_node_not_found"})
                upstream.append({"wire": _compact_wire(wire), "unresolved": "source_node_not_found"})
        input_args = [
            {"name": param.get("name"), "argument": param.get("argument")}
            for param in node.get("parameters", [])
            if param.get("kind") == "InputParameter" and param.get("argument")
        ][:MAX_FBD_BRANCHES]
        if input_args:
            compact["input_arguments"] = input_args
        if upstream:
            compact["upstream"] = upstream
            if len(all_input_wires) > len(input_wires):
                compact["truncated_upstream"] = len(all_input_wires) - len(input_wires)
        elif node_type in {"ORef", "OCon"}:
            compact["unresolved"] = "no_incoming_wire"
            unresolved.append({"node": node.get("id"), "reason": "no_incoming_wire"})
        return compact

    roots = [trace_node(node, 0, set()) for node in start_nodes]
    if not roots:
        unresolved.append({"symbol": symbol, "reason": "fbd_target_node_not_found"})
    return {"roots": roots, "unresolved": _dedupe_dicts(unresolved)}


def _find_fbd_target_nodes(nodes: list[JsonDict], symbol: str) -> list[JsonDict]:
    out = []
    for node in nodes:
        if node.get("operand") == symbol:
            out.append(node)
            continue
        for param in node.get("parameters", []):
            if param.get("argument") == symbol and param.get("kind") in {"OutputParameter", "InOutParameter"}:
                out.append(node)
                break
    return out


def _source_unit(context: JsonDict, source: object) -> JsonDict | None:
    for unit in context.get("units") or []:
        if unit.get("id") == source:
            return unit
    return None


def _page(tool: str, query: str, limit: int, offset: int, items: list[JsonDict], total: int) -> JsonDict:
    return {"tool": tool, "query": query, "total": total, "limit": limit, "offset": offset, "has_more": offset + len(items) < total, "items": items}


def _csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def _bounded_int(value: int | str | None, default: int, max_value: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, max_value))


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_.$\[\]-]+", query)
    if not tokens:
        return f'"{query.replace(chr(34), chr(34) + chr(34))}"'
    return " AND ".join(f'"{token.replace(chr(34), chr(34) + chr(34))}"' for token in tokens[:12])


def _snippet(text: str, query: str, size: int = 240) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= size:
        return clean
    index = clean.lower().find(query.lower())
    if index < 0:
        index = 0
    start = max(index - size // 3, 0)
    end = min(start + size, len(clean))
    return ("..." if start else "") + clean[start:end] + ("..." if end < len(clean) else "")


def _record_snippet(row: JsonDict) -> str:
    for key in ["text", "comment", "operand", "instruction", "description", "st_body", "condition_body"]:
        if row.get(key):
            return _clip(row.get(key), 260) or ""
    if row.get("parameters"):
        return _clip(json.dumps(row["parameters"], ensure_ascii=False), 260) or ""
    return _clip(json.dumps(row, ensure_ascii=False), 260) or ""


def _unit_search_text(unit: JsonDict) -> str:
    values = [unit.get("text"), unit.get("comment"), unit.get("id"), unit.get("routine")]
    values.extend(item.get("instruction") for item in unit.get("instructions", []))
    return " ".join(str(value) for value in values if value)


def _compact_symbol(row: JsonDict | None) -> JsonDict | None:
    if not row:
        return None
    return {
        key: row.get(key)
        for key in ["id", "kind", "name", "scope", "data_type", "tag_type", "alias_for", "description"]
        if row.get(key) not in (None, "")
    }


def _compact_routine(row: JsonDict) -> JsonDict:
    return {
        key: row.get(key)
        for key in ["id", "name", "owner", "program", "language", "unit_count", "fbd_node_count", "fbd_wire_count"]
        if row.get(key) not in (None, "")
    }


def _compact_fbd_node(node: JsonDict) -> JsonDict:
    row = {
        "id": node.get("id"),
        "node_id": node.get("node_id"),
        "sheet": node.get("sheet_number"),
        "type": node.get("node_type"),
        "instruction": node.get("instruction"),
        "operand": node.get("operand"),
        "connector_name": node.get("connector_name"),
        "parameters": [
            {"kind": param.get("kind"), "name": param.get("name"), "argument": param.get("argument")}
            for param in node.get("parameters", [])[:12]
        ],
    }
    return {key: value for key, value in row.items() if value not in (None, "", [])}


def _compact_wire(wire: JsonDict) -> JsonDict:
    return {
        key: wire.get(key)
        for key in ["id", "sheet_number", "from_id", "from_param", "to_id", "to_param"]
        if wire.get(key) not in (None, "")
    }


def _compact_instruction(item: JsonDict) -> JsonDict:
    return {key: item.get(key) for key in ["instruction", "args"] if item.get(key) not in (None, "")}


def _compact_comment(row: JsonDict) -> JsonDict:
    return {
        "target": row.get("target") or row.get("tag_name"),
        "text": _clip(row.get("text") or row.get("comment") or row.get("description")),
    }


def _compact_data(row: JsonDict) -> JsonDict:
    return {
        "owner": row.get("owner") or row.get("owner_name"),
        "element": row.get("element"),
        "format": row.get("format"),
        "raw_text": _clip(row.get("raw_text"), 180),
    }


def _counter_rows(values: Any) -> list[JsonDict]:
    counts = Counter(value for value in values if value is not None)
    return [{"key": key, "count": count} for key, count in counts.most_common(20)]


def _issue_tokens(text: str) -> list[str]:
    stop = {"the", "and", "will", "not", "run", "auto", "shows", "with", "even", "hmi", "mcc", "alarm"}
    tokens = [token for token in re.findall(r"[A-Za-z0-9_#.-]+", text) if len(token) > 2 and token.lower() not in stop]
    return tokens[:12]


def _likely_names(items: list[JsonDict], issue_text: str) -> list[str]:
    names = []
    for item in items:
        name = item.get("name")
        if name and re.match(r"^[A-Za-z_][A-Za-z0-9_.\[\]-]*$", str(name)):
            names.append(str(name))
    if not names:
        names.extend(
            name
            for name in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+|\[[A-Za-z0-9_]+\])*\b", issue_text)
            if any(marker in name for marker in "_.[")
        )
    seen = set()
    out = []
    for name in names:
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(name)
    return out


def _read_jsonl(workspace: str | Path, name: str) -> list[JsonDict]:
    path = Path(workspace) / "ir" / (name if name.endswith(".jsonl") else f"{name}.jsonl")
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _base_symbol(symbol: str) -> str:
    return symbol.split(".", 1)[0].split("[", 1)[0]


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clip(value: object, size: int = 240) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) <= size:
        return text
    return text[: size - 3] + "..."


def _dedupe_nodes(nodes: list[JsonDict]) -> list[JsonDict]:
    out = []
    seen = set()
    for node in nodes:
        key = node.get("id")
        if key in seen:
            continue
        seen.add(key)
        out.append(node)
    return out


def _dedupe_search_rows(rows: list[JsonDict]) -> list[JsonDict]:
    out = []
    seen = set()
    for row in rows:
        key = (row.get("kind"), row.get("name"), row.get("scope"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_dicts(rows: list[JsonDict]) -> list[JsonDict]:
    out = []
    seen = set()
    for row in rows:
        key = json.dumps(row, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
