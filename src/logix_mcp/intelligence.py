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
MAX_FBD_EQUATION_LIMIT = 300


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
        where.append("program = ?")
        params.append(_normalize_scope(scope))

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


def _normalize_scope(scope: str) -> str:
    """Accept both ``UWP`` and ``Program:UWP`` scope spellings."""

    if scope.startswith("Program:"):
        return scope.split(":", 1)[1]
    return scope


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
    chart_id: str | None = None,
    query: str | None = None,
    before: int = 1,
    after: int = 1,
) -> JsonDict:
    """Return a bounded, evidence-oriented routine slice."""

    context = _routine_context(workspace, program, routine, routine_id)
    if context is None:
        return {"found": False, "routine": routine, "program": program, "routine_id": routine_id, "items": []}

    units = list(context.get("units") or [])
    selected_indexes = _selected_unit_indexes(context, units, sheet, unit_id, chart_id, query, before, after)
    if not selected_indexes and not (sheet or unit_id or chart_id or query):
        selected_indexes = list(range(min(len(units), 20)))

    fbd_by_sheet = _fbd_by_sheet(context)
    items = [_compact_unit(units[index], fbd_by_sheet) for index in selected_indexes[:50]]
    result = {
        "found": True,
        "routine": _compact_routine(context["routine"]),
        "selection": {
            "sheet": str(sheet) if sheet is not None else None,
            "unit_id": unit_id,
            "chart_id": chart_id,
            "query": query,
            "before": before,
            "after": after,
        },
        "total_units": len(units),
        "returned": len(items),
        "truncated": len(selected_indexes) > len(items),
        "items": items,
    }
    if _routine_context_touches_sfc(context, items):
        limits = coverage_limits(workspace, "sfc")
        if limits:
            result["limits"] = limits
    return result


def coverage_limits(workspace: str | Path, area: str | None = None) -> list[str]:
    """Return compact extraction-coverage caveats for a specific analysis area."""

    coverage = _read_json_file(Path(workspace) / "ir" / "coverage.json") or {}
    surfaces = coverage.get("surfaces") if isinstance(coverage, dict) else {}
    missing = coverage.get("missing") if isinstance(coverage, dict) else {}
    if not isinstance(surfaces, dict):
        return []

    area_key = (area or "").lower()
    limits: list[str] = []
    if area_key in {"sfc", "routine", "routine_sfc"}:
        for name in ("sfc_nodes", "sfc_links"):
            surface = surfaces.get(name) or {}
            missing_count = int(surface.get("missing_count") or 0)
            if missing_count > 0 or name in (missing.get("P0") or []) or name in (missing.get("P1") or []):
                limits.append(
                    f"coverage_gap:{name}:{surface.get('covered_count', 0)}/{surface.get('source_count', 0)} "
                    f"covered, missing {missing_count}, priority {surface.get('priority') or 'unknown'}"
                )
    if area_key in {"sfc", "routine", "routine_sfc", "project"}:
        surface = surfaces.get("unextracted_elements") or {}
        missing_count = int(surface.get("missing_count") or 0)
        if missing_count > 0:
            limits.append(
                f"coverage_gap:unextracted_elements:{surface.get('covered_count', 0)}/{surface.get('source_count', 0)} "
                f"covered, missing {missing_count}, priority {surface.get('priority') or 'unknown'}"
            )
    return limits


def _routine_context_touches_sfc(context: JsonDict, items: list[JsonDict] | None = None) -> bool:
    routine = context.get("routine") or {}
    if str(routine.get("language") or "").upper() == "SFC":
        return True
    if context.get("sfc_nodes") or context.get("sfc_links"):
        return True
    for item in items or []:
        if str(item.get("kind") or "").lower().startswith("sfc") or str(item.get("language") or "").upper() == "SFC":
            return True
    return False


def get_fbd_sheet(
    workspace: str | Path,
    program: str | None = None,
    routine: str | None = None,
    routine_id: str | None = None,
    sheet: str | int | None = None,
    form: str = "pseudo",
    limit: int = 100,
) -> JsonDict:
    """Return a compact pseudo-equation view of one FBD sheet."""

    limit = _bounded_int(limit, 100, MAX_FBD_EQUATION_LIMIT)
    context = _routine_context(workspace, program, routine, routine_id)
    if context is None:
        return {"found": False, "program": program, "routine": routine, "routine_id": routine_id, "sheets": []}

    routine_row = context.get("routine") or {}
    if str(routine_row.get("language") or "").upper() != "FBD":
        return {
            "found": False,
            "routine": _compact_routine(routine_row),
            "status": "not_fbd",
            "message": "get_fbd_sheet only builds pseudo-equations for FBD routines.",
        }

    units = [unit for unit in context.get("units") or [] if unit.get("kind") == "fbd_sheet" or unit.get("language") == "FBD"]
    selected = _select_fbd_sheet(units, sheet)
    if selected is None:
        return {
            "found": True,
            "status": "needs_sheet" if units else "no_fbd_sheets",
            "routine": _compact_routine(routine_row),
            "sheets": _fbd_sheet_index(context, units),
            "limits": ["Pass sheet=<number> to return pseudo-equations."] if units else ["No FBD sheet units were extracted for this routine."],
        }

    if form not in {"pseudo", "summary"}:
        return {
            "found": False,
            "routine": _compact_routine(routine_row),
            "sheet": _compact_fbd_sheet_unit(selected),
            "status": "unsupported_form",
            "limits": ["Supported forms: pseudo, summary."],
        }

    result = _build_fbd_sheet_pseudo(workspace, context, selected, limit)
    if form == "summary":
        result.pop("pseudo_equations", None)
        result.pop("equations", None)
    return result


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
            pseudo = _trace_fbd_pseudo_sheet(workspace, context, symbol, ref, limit=min(max(limit, 40), 80))
            path_row = {"type": "fbd", "reference": ref, "path": fbd_path}
            if pseudo.get("found"):
                path_row["pseudo_sheet"] = pseudo.get("sheet")
                path_row["pseudo_equations"] = pseudo.get("equations", [])[:80]
                path_row["unwired_pins"] = pseudo.get("unwired_pins", [])[:30]
                path_row["pseudo_summary"] = pseudo.get("summary")
            paths.append(path_row)
            unresolved.extend(fbd_path.get("unresolved", []))
            unresolved.extend(pseudo.get("unresolved_edges", []) if pseudo.get("found") else [])
        else:
            unit = _source_unit(context, ref.get("source"))
            if unit:
                # The unit text already carries the rung; drop the reference
                # snippet and the parsed instruction list so the same rung is
                # not serialized three times per path.
                unit_row = _compact_unit(unit, {})
                unit_row.pop("instructions", None)
                ref_row = {key: value for key, value in ref.items() if key != "snippet"}
                paths.append({"type": language.lower() or "logic", "reference": ref_row, "unit": unit_row})
            else:
                unresolved.append({"source": ref.get("source"), "reason": "source_unit_not_found"})

    return {
        "symbol": symbol,
        "direction": direction,
        "status": "ok" if paths else "unresolved",
        "writer_count": refs["total"],
        "paths": paths,
        "unresolved": _dedupe_dicts(unresolved),
        "limits": [
            "FBD trace is V1: it follows wires/connectors and includes sheet pseudo-equations; unresolved gaps are reported instead of inferred."
        ],
        "next_calls": [f"cross_reference({symbol!r}, destructive=True)", f"get_operand_context({symbol!r})", "get_fbd_sheet(..., sheet=<sheet>)"],
    }


def scope_metadata(workspace: str | Path, issue_text: str | None = None) -> JsonDict:
    """Describe what evidence is in-scope for this offline PLC workspace."""

    root = Path(workspace)
    manifest = _read_json_file(root / "ir" / "manifest.json") or {}
    coverage = _read_json_file(root / "ir" / "coverage.json") or manifest.get("coverage") or {}
    datasets = set(manifest.get("datasets") or [])
    counts = manifest.get("counts") or {}
    text = issue_text or ""
    inferred_limits = ["read_only_offline_l5x_only"]
    issue_lower = text.lower()
    if re.search(r"\b(hmi|screen|display|color|colour|red|green|mcc screen|factorytalk|view|slow)\b", issue_lower):
        inferred_limits.append("needs_hmi_export_or_runtime")
    if re.search(r"\b(runtime|live|online|actual state|currently|breaker off|feedback mismatch)\b", issue_lower):
        inferred_limits.append("needs_runtime_or_field_state")
    if re.search(r"\b(prosoft|modbus|serial|gateway|external|genset|ge engine|engine ecu)\b", issue_lower):
        inferred_limits.append("may_depend_on_external_controller_or_gateway")

    available = [
        {
            "name": "plc_l5x_logic",
            "available": bool(manifest),
            "supports": ["tags", "routines", "RLL/ST/FBD/SFC", "AOIs", "modules", "comments", "offline alarm config"],
            "counts": {key: counts.get(key) for key in ["controller_tags", "program_tags", "routines", "aois", "modules"] if key in counts},
        },
        {
            "name": "sqlite_search_index",
            "available": db.has_index(workspace),
            "supports": ["compact search", "cross references", "routine context"],
        },
        {
            "name": "fbd_graph",
            "available": "fbd_nodes" in datasets and "fbd_wires" in datasets,
            "supports": ["get_fbd_sheet pseudo-equations", "trace_signal FBD wires", "ICon/OCon connectors"],
            "counts": {key: counts.get(key) for key in ["fbd_nodes", "fbd_wires"] if key in counts},
        },
        {
            "name": "alarm_config",
            "available": "alarms" in datasets,
            "supports": ["resolve_alarm", "decode_summary", "alarm messages"],
            "counts": {key: counts.get(key) for key in ["alarms", "messages"] if key in counts},
        },
    ]
    unavailable = [
        {
            "name": "factorytalk_hmi_export",
            "available": False,
            "limits": ["screen colors", "object bindings", "screen navigation", "HMI performance cannot be proven from PLC L5X alone"],
        },
        {
            "name": "live_controller_runtime",
            "available": False,
            "limits": ["current tag values", "latched fault state", "breaker physical state", "network latency cannot be proven offline"],
        },
        {
            "name": "external_controller_or_gateway_runtime",
            "available": False,
            "limits": ["data originating outside this controller may only appear as tags/modules/messages"],
        },
    ]
    missing_p0 = ((coverage.get("missing") or {}).get("P0") or []) if isinstance(coverage, dict) else []
    return {
        "workspace": str(root),
        "controller": manifest.get("controller"),
        "issue_text": issue_text,
        "available_evidence": available,
        "unavailable_evidence": unavailable,
        "coverage": {
            "p0_missing": missing_p0,
            "p1_missing": ((coverage.get("missing") or {}).get("P1") or []) if isinstance(coverage, dict) else [],
        },
        "limits": _dedupe_strings(inferred_limits),
        "next_calls": [
            "search_project(<specific term>)",
            "cross_reference(<tag>, mode='members')",
            "trace_signal(<tag>, direction='upstream')",
            "get_fbd_sheet(..., sheet=<sheet>)",
        ],
    }


def triage_issue(workspace: str | Path, issue_text: str, limit: int = 5) -> JsonDict:
    """Create a compact PLC-first evidence bundle for a field issue."""

    limit = _bounded_int(limit, 5, 20)
    scope = scope_metadata(workspace, issue_text)
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
        "scope": {
            "limits": scope.get("limits", []),
            "available": [row["name"] for row in scope.get("available_evidence", []) if row.get("available")],
            "unavailable": [row["name"] for row in scope.get("unavailable_evidence", [])],
        },
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


def resolve_alarm(workspace: str | Path, name_or_class: str, limit: int = 10) -> JsonDict:
    """Resolve alarm records to compact source tags, messages, and PLC evidence."""

    limit = _bounded_int(limit, 10, 50)
    alarms = _alarm_candidates(workspace, name_or_class)[:limit]
    fallback_summary = decode_summary(workspace, name_or_class, limit=limit) if not alarms else None
    rows = []
    for alarm in alarms:
        source_tags = _alarm_source_tags(alarm)
        source_context = []
        for tag in source_tags[:5]:
            refs = cross_reference(workspace, tag, mode="members", limit=5)
            trace = trace_signal(workspace, tag, limit=5)
            source_context.append(
                {
                    "tag": tag,
                    "reference_total": refs.get("total", 0),
                    "sample_references": refs.get("rows", [])[:3],
                    "trace_status": trace.get("status"),
                    "first_writer": (trace.get("paths") or [{}])[0].get("reference"),
                }
            )

        alarm_refs = cross_reference(workspace, str(alarm.get("tag_name") or ""), mode="members", destructive=True, limit=5)
        rows.append(
            {
                "alarm": _compact_alarm(alarm),
                "source_tags": source_tags,
                "condition": _alarm_condition(alarm, alarm_refs, source_context),
                "source_context": source_context,
                "alarm_tag_writers": alarm_refs.get("rows", [])[:5],
                "messages": _alarm_messages(alarm),
                "limits": _alarm_limits(alarm, alarm_refs, source_context),
            }
        )

    return {
        "query": name_or_class,
        "total": len(_alarm_candidates(workspace, name_or_class)),
        "limit": limit,
        "alarms": rows,
        "summary_decode": fallback_summary if fallback_summary and fallback_summary.get("status") == "ok" else None,
        "limits": ["no_alarm_record_matched_query"] if not alarms else [],
        "next_calls": [
            "decode_summary(<summary alarm tag>)",
            "cross_reference(<alarm/source tag>, mode='members')",
            "trace_signal(<alarm/source tag>)",
        ],
    }


def decode_summary(workspace: str | Path, tag: str, limit: int = 50) -> JsonDict:
    """Expand a summary coil/tag into candidate member bits and alarm records."""

    limit = _bounded_int(limit, 50, 200)
    writers = cross_reference(workspace, tag, mode="exact", destructive=True, limit=25)
    members: list[JsonDict] = []
    for writer in writers.get("rows", []):
        context = _routine_context(workspace, None, None, writer.get("routine"))
        unit = _source_unit(context, writer.get("source")) if context else None
        text = str(unit.get("text") or writer.get("snippet") or "") if unit else str(writer.get("snippet") or "")
        for symbol in _symbols_from_logic_text(text):
            if symbol == tag or _base_symbol(symbol) == _base_symbol(tag):
                continue
            members.append(_summary_member(workspace, symbol, writer, unit))

    members = _dedupe_members(members)[:limit]
    return {
        "summary_tag": tag,
        "writer_count": writers.get("total", 0),
        "writers": writers.get("rows", [])[:10],
        "members": members,
        "member_count": len(members),
        "status": "ok" if members else "unresolved",
        "limits": [] if members else ["No summary members could be decoded from destructive writer logic."],
        "next_calls": [f"resolve_alarm({tag!r})", f"trace_signal({tag!r})"],
    }


def aoi_instance_bindings(workspace: str | Path, instance: str, limit: int = 10) -> JsonDict:
    """Return FBD AOI instance pin bindings, including unwired parameters."""

    limit = _bounded_int(limit, 10, 50)
    nodes = _find_aoi_instance_nodes(workspace, instance)[:limit]
    instances = []
    for node in nodes:
        aoi_name = str(node.get("instruction") or "")
        params = _aoi_parameters(workspace, aoi_name)
        context = _routine_context(workspace, None, None, node.get("routine_id"))
        bindings = _bindings_for_aoi_node(context or {}, node, params)
        instances.append(
            {
                "instance": node.get("operand"),
                "aoi": aoi_name,
                "routine": node.get("routine_id"),
                "program": node.get("program"),
                "owner": node.get("owner"),
                "sheet": node.get("sheet_number"),
                "node_id": node.get("node_id"),
                "evidence_ref": node.get("id"),
                "summary": {
                    "total": len(bindings),
                    "wired": sum(1 for row in bindings if row.get("wired")),
                    "unwired": sum(1 for row in bindings if not row.get("wired")),
                    "required_unwired": [
                        row.get("param")
                        for row in bindings
                        if not row.get("wired") and str(row.get("required")).lower() == "true"
                    ],
                },
                "bindings": bindings,
            }
        )

    return {
        "query": instance,
        "found": bool(instances),
        "instances": instances,
        "limits": [] if instances else ["No FBD AddOnInstruction node matched this instance/name."],
        "next_calls": [
            "get_routine_slice(..., sheet=<sheet>)",
            "trace_signal(<bound output or motor tag>)",
        ],
    }


def _alarm_candidates(workspace: str | Path, query: str) -> list[JsonDict]:
    query_lower = query.lower()
    alarms = _dataset(workspace, "alarms")
    exact = [
        alarm
        for alarm in alarms
        if str(alarm.get("tag_name") or "").lower() == query_lower
        or str(alarm.get("id") or "").lower() == query_lower
    ]
    if exact:
        return exact
    rows = []
    for alarm in alarms:
        haystack = " ".join(
            [
                str(alarm.get("tag_name") or ""),
                str(alarm.get("id") or ""),
                str(alarm.get("alarm_type") or ""),
                str(alarm.get("alarm_class") or ""),
                " ".join(str(tag) for tag in alarm.get("assoc_tags") or []),
                " ".join(str(msg.get("text") or "") for msg in alarm.get("messages") or []),
            ]
        ).lower()
        if query_lower in haystack:
            rows.append(alarm)
    return rows


def _compact_alarm(alarm: JsonDict) -> JsonDict:
    params = alarm.get("parameters") or {}
    return {
        "id": alarm.get("id"),
        "tag_name": alarm.get("tag_name"),
        "data_type": alarm.get("data_type"),
        "alarm_type": alarm.get("alarm_type"),
        "alarm_class": alarm.get("alarm_class"),
        "severity": alarm.get("severity"),
        "latched": params.get("Latched"),
        "ack_required": params.get("AckRequired"),
        "enabled": params.get("ProgEnable") or params.get("EnableIn"),
        "assoc_tags": _alarm_source_tags(alarm),
    }


def _alarm_source_tags(alarm: JsonDict) -> list[str]:
    params = alarm.get("parameters") or {}
    tags = []
    tags.extend(str(tag) for tag in alarm.get("assoc_tags") or [])
    for key, value in params.items():
        if str(key).startswith("AssocTag"):
            tags.append(str(value))
    out = []
    for tag in tags:
        clean = tag.strip()
        if not clean or clean.upper() in {"SPACE", "NULL", "0", "FALSE", "TRUE"}:
            continue
        if clean not in out:
            out.append(clean)
    return out


def _alarm_messages(alarm: JsonDict) -> list[JsonDict]:
    rows = []
    for message in alarm.get("messages") or []:
        rows.append(
            {
                "type": message.get("message_type"),
                "lang": message.get("lang"),
                "text": _clip(message.get("text"), 240),
            }
        )
    return rows


def _alarm_condition(alarm: JsonDict, alarm_refs: JsonDict, source_context: list[JsonDict]) -> JsonDict:
    params = alarm.get("parameters") or {}
    if alarm_refs.get("total"):
        return {"status": "plc_logic_found", "basis": "alarm tag destructive writer"}
    if any(row.get("reference_total") for row in source_context):
        return {"status": "source_tag_logic_found", "basis": "associated tag references"}
    if params.get("Condition") or params.get("In"):
        return {
            "status": "alarm_config_only",
            "condition": params.get("Condition"),
            "in": params.get("In"),
            "basis": "alarm configuration has no extracted destructive writer",
        }
    return {"status": "none_found", "basis": "no PLC writer/source reference extracted"}


def _alarm_limits(alarm: JsonDict, alarm_refs: JsonDict, source_context: list[JsonDict]) -> list[str]:
    limits = []
    if not alarm_refs.get("total") and not any(row.get("reference_total") for row in source_context):
        limits.append("no_trip_logic_found_in_this_controller")
    if any("%" in str(msg.get("text") or "") for msg in alarm.get("messages") or []):
        limits.append("message_uses_hmi_or_alarm_server_substitution")
    return limits


def _summary_member(workspace: str | Path, symbol: str, writer: JsonDict, unit: JsonDict | None) -> JsonDict:
    alarms = [
        _compact_alarm(alarm)
        for alarm in _dataset(workspace, "alarms")
        if str(alarm.get("tag_name") or "").lower() == symbol.lower()
        or symbol in _alarm_source_tags(alarm)
    ][:5]
    refs = cross_reference(workspace, symbol, mode="members", limit=5)
    context = get_operand_context(workspace, symbol)
    return {
        "symbol": symbol,
        "references": refs.get("total", 0),
        "alarms": alarms,
        "comments": context.get("comments", [])[:3],
        "tag_comments": context.get("tag_comments", [])[:3],
        "source_writer": {
            "routine": writer.get("routine"),
            "location": writer.get("location"),
            "evidence_ref": writer.get("evidence_ref") or writer.get("source"),
            "snippet": _clip(unit.get("text") if unit else writer.get("snippet"), 220),
        },
    }


def _find_aoi_instance_nodes(workspace: str | Path, instance: str) -> list[JsonDict]:
    target = instance.lower()
    return [
        node
        for node in _dataset(workspace, "fbd_nodes")
        if node.get("node_type") == "AddOnInstruction"
        and (
            str(node.get("operand") or "").lower() == target
            or str(node.get("instruction") or "").lower() == target
            or str(node.get("id") or "").lower() == target
        )
    ]


def _aoi_parameters(workspace: str | Path, aoi: str) -> list[JsonDict]:
    return [
        param
        for param in _dataset(workspace, "aoi_parameters")
        if str(param.get("aoi") or "").lower() == aoi.lower()
    ]


def _bindings_for_aoi_node(context: JsonDict, node: JsonDict, params: list[JsonDict]) -> list[JsonDict]:
    node_params = {param.get("name"): param for param in node.get("parameters", [])}
    sheet_id = node.get("sheet_id")
    node_id = str(node.get("node_id"))
    nodes_by_id = {
        str(item.get("node_id")): item
        for item in context.get("fbd_nodes", [])
        if item.get("sheet_id") == sheet_id
    }
    in_wires: dict[str, list[JsonDict]] = {}
    out_wires: dict[str, list[JsonDict]] = {}
    for wire in context.get("fbd_wires", []):
        if wire.get("sheet_id") != sheet_id:
            continue
        if str(wire.get("to_id")) == node_id and wire.get("to_param"):
            in_wires.setdefault(str(wire["to_param"]), []).append(wire)
        if str(wire.get("from_id")) == node_id and wire.get("from_param"):
            out_wires.setdefault(str(wire["from_param"]), []).append(wire)

    bindings = []
    for param in params:
        name = str(param.get("name") or "")
        if name in {"EnableIn", "EnableOut"}:
            continue
        explicit = node_params.get(name)
        sources = [_wire_source_expr(wire, nodes_by_id) for wire in in_wires.get(name, [])]
        destinations = [_wire_destination_expr(wire, nodes_by_id) for wire in out_wires.get(name, [])]
        argument = explicit.get("argument") if explicit else None
        wired = bool(argument or sources or destinations)
        bindings.append(
            {
                "param": name,
                "usage": param.get("usage"),
                "data_type": param.get("data_type"),
                "required": param.get("required"),
                "visible": param.get("visible"),
                "wired": wired,
                "argument": argument,
                "sources": [source for source in sources if source],
                "destinations": [dest for dest in destinations if dest],
                "default": _param_default(param),
                "description": _clip(param.get("description"), 160),
            }
        )
    return bindings


def _wire_source_expr(wire: JsonDict, nodes_by_id: dict[str, JsonDict]) -> str | None:
    node = nodes_by_id.get(str(wire.get("from_id")))
    if not node:
        return f"unresolved:{wire.get('from_id')}"
    expr = node.get("operand") or node.get("connector_name") or node.get("instruction") or node.get("node_type")
    if wire.get("from_param"):
        return f"{expr}.{wire.get('from_param')}"
    return str(expr) if expr is not None else None


def _wire_destination_expr(wire: JsonDict, nodes_by_id: dict[str, JsonDict]) -> str | None:
    node = nodes_by_id.get(str(wire.get("to_id")))
    if not node:
        return f"unresolved:{wire.get('to_id')}"
    expr = node.get("operand") or node.get("connector_name") or node.get("instruction") or node.get("node_type")
    if wire.get("to_param"):
        return f"{expr}.{wire.get('to_param')}"
    return str(expr) if expr is not None else None


def _param_default(param: JsonDict) -> str | None:
    attrs = param.get("attributes") or {}
    for key in ["Default", "DefaultValue", "DefaultData", "Value"]:
        if attrs.get(key) is not None:
            return str(attrs[key])
    return None


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
        "sfc_charts": [row for row in _read_jsonl(workspace, "sfc_charts") if row.get("routine_id") == rid],
        "sfc_nodes": [row for row in _read_jsonl(workspace, "sfc_nodes") if row.get("routine_id") == rid],
        "sfc_links": [row for row in _read_jsonl(workspace, "sfc_links") if row.get("routine_id") == rid],
        "sfc_branches": [row for row in _read_jsonl(workspace, "sfc_branches") if row.get("routine_id") == rid],
        "sfc_legs": [row for row in _read_jsonl(workspace, "sfc_legs") if row.get("routine_id") == rid],
    }


def _selected_unit_indexes(
    context: JsonDict,
    units: list[JsonDict],
    sheet: str | int | None,
    unit_id: str | None,
    chart_id: str | None,
    query: str | None,
    before: int,
    after: int,
) -> list[int]:
    if unit_id:
        return [i for i, unit in enumerate(units) if unit.get("id") == unit_id]
    if chart_id:
        return [
            i
            for i, unit in enumerate(units)
            if unit.get("id") == chart_id or unit.get("chart_id") == chart_id or unit.get("unit_id") == chart_id
        ]
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


def _select_fbd_sheet(units: list[JsonDict], sheet: str | int | None) -> JsonDict | None:
    if not units:
        return None
    if sheet is None:
        return units[0] if len(units) == 1 else None
    target = str(sheet)
    for unit in units:
        if str(unit.get("number")) == target or str(unit.get("sheet_number")) == target or str(unit.get("id")) == target:
            return unit
    return None


def _fbd_sheet_index(context: JsonDict, units: list[JsonDict]) -> list[JsonDict]:
    by_sheet = _fbd_by_sheet(context)
    rows = []
    for unit in units:
        bundle = by_sheet.get(unit.get("id"), {})
        rows.append(
            {
                "id": unit.get("id"),
                "number": unit.get("number") or unit.get("sheet_number"),
                "node_count": len(bundle.get("nodes") or []),
                "wire_count": len(bundle.get("wires") or []),
                "comment": _clip(unit.get("comment"), 160),
            }
        )
    return rows


def _compact_fbd_sheet_unit(unit: JsonDict) -> JsonDict:
    return {
        key: unit.get(key)
        for key in ["id", "number", "sheet_number", "sequence", "comment"]
        if unit.get(key) not in (None, "")
    }


def _build_fbd_sheet_pseudo(workspace: str | Path, context: JsonDict, unit: JsonDict, limit: int) -> JsonDict:
    sheet_id = unit.get("id")
    nodes = sorted(
        [node for node in context.get("fbd_nodes") or [] if node.get("sheet_id") == sheet_id and node.get("node_type") != "TextBox"],
        key=_fbd_node_sort_key,
    )
    wires = sorted([wire for wire in context.get("fbd_wires") or [] if wire.get("sheet_id") == sheet_id], key=_fbd_wire_sort_key)
    index = _fbd_signal_index(context)
    unresolved: list[JsonDict] = []
    equations: list[JsonDict] = []
    seen_equations: set[tuple[object, object, object]] = set()
    aoi_instances = []
    source_tags: list[str] = []
    output_tags: list[str] = []

    def add_equation(row: JsonDict) -> None:
        key = (row.get("kind"), row.get("target"), row.get("evidence_ref"))
        if key in seen_equations:
            return
        seen_equations.add(key)
        text = f"{row.get('target')} := {row.get('expr')}"
        if row.get("kind") == "aoi_output_unwired":
            text = f"{row.get('target')} -> UNWIRED"
        elif row.get("kind") == "jsr_call":
            text = f"CALL {row.get('expr')}"
        row["text"] = text
        equations.append(row)

    selected_icon_names = sorted(
        {
            str(node.get("connector_name"))
            for node in nodes
            if node.get("node_type") == "ICon" and node.get("connector_name")
        }
    )
    for name in selected_icon_names:
        for producer in index["connector_sources"].get(name, []):
            if producer.get("sheet_id") != sheet_id:
                row = _fbd_connector_equation(producer, index, unresolved, remote=True)
                if row:
                    add_equation(row)

    for node in nodes:
        node_type = node.get("node_type")
        if node_type == "JSR":
            add_equation(
                {
                    "kind": "jsr_call",
                    "target": f"JSR:{node.get('node_id')}",
                    "expr": node.get("callee") or "UNKNOWN_ROUTINE",
                    "node_id": node.get("node_id"),
                    "sheet": node.get("sheet_number"),
                    "evidence_ref": node.get("id"),
                    "unresolved": [],
                }
            )
            continue
        if node_type == "IRef":
            _append_symbol(source_tags, node.get("operand"))
            continue
        if node_type == "ORef":
            _append_symbol(output_tags, node.get("operand"))
            row = _fbd_oref_equation(node, index, unresolved)
            if row:
                add_equation(row)
            continue
        if node_type == "OCon":
            row = _fbd_connector_equation(node, index, unresolved, remote=False)
            if row:
                add_equation(row)
            continue
        if node_type == "Block":
            for row in _fbd_block_equations(node, index, unresolved):
                add_equation(row)
            continue
        if node_type == "AddOnInstruction":
            params = _aoi_parameters(workspace, str(node.get("instruction") or ""))
            bindings = _fbd_aoi_bindings(context, node, params, index, unresolved)
            instance = _fbd_aoi_instance_summary(node, bindings)
            aoi_instances.append(instance)
            for binding in bindings:
                usage = str(binding.get("usage") or "").lower()
                if usage in {"input", "inout"}:
                    _append_symbol(source_tags, binding.get("argument"))
                if usage in {"output", "inout"}:
                    _append_symbol(output_tags, binding.get("argument"))
                    for dest in binding.get("destinations", []):
                        _append_symbol(output_tags, dest)
            for row in _fbd_aoi_equations(node, bindings):
                add_equation(row)

    unwired_pins = [
        {
            "instance": instance.get("instance"),
            "aoi": instance.get("aoi"),
            "param": binding.get("param"),
            "usage": binding.get("usage"),
            "required": binding.get("required"),
            "default": binding.get("default"),
        }
        for instance in aoi_instances
        for binding in instance.get("bindings", [])
        if not binding.get("wired")
    ]
    connectors = [_fbd_connector_summary(node) for node in nodes if node.get("node_type") in {"ICon", "OCon"}]
    output = {
        "found": True,
        "status": "ok",
        "routine": _compact_routine(context.get("routine") or {}),
        "sheet": _compact_fbd_sheet_unit(unit),
        "form": "pseudo",
        "summary": {
            "node_count": len(nodes),
            "wire_count": len(wires),
            "input_refs": sum(1 for node in nodes if node.get("node_type") == "IRef"),
            "output_refs": sum(1 for node in nodes if node.get("node_type") == "ORef"),
            "blocks": sum(1 for node in nodes if node.get("node_type") == "Block"),
            "jsr_calls": sum(1 for node in nodes if node.get("node_type") == "JSR"),
            "aois": len(aoi_instances),
            "connectors": len(connectors),
            "unresolved_edges": len(_dedupe_dicts(unresolved)),
            "unwired_aoi_pins": len(unwired_pins),
            "equation_count": len(equations),
        },
        "source_tags": source_tags[:50],
        "output_tags": output_tags[:50],
        "connectors": connectors[:50],
        "aoi_instances": aoi_instances[:20],
        "unwired_pins": unwired_pins[:100],
        "unresolved_edges": _dedupe_dicts(unresolved)[:100],
        # Structured rows (without text) plus a parallel text list; the text
        # used to be duplicated inside every row.
        "pseudo_equations": [{key: value for key, value in row.items() if key != "text"} for row in equations[:limit]],
        "equations": [row["text"] for row in equations[:limit]],
        "truncated_equations": max(0, len(equations) - limit),
        "limits": _fbd_sheet_limits(equations, limit, unresolved),
        "next_calls": [
            "trace_signal(<output tag>, direction='upstream')",
            "aoi_instance_bindings(<AOI instance>)",
            "get_routine_slice(..., sheet=<sheet>)",
        ],
    }
    if not nodes:
        # Never report an empty sheet as a plain "ok": either the routine truly
        # has no logic or the extractor failed to model its node types.
        output["status"] = "empty_sheet"
        output["limits"] = [
            "No FBD nodes were extracted for this sheet. The routine may be empty, "
            "or it uses node types the extractor does not model; verify against the "
            "source L5X before concluding the routine does nothing.",
            *output["limits"],
        ]
    return output


def _fbd_signal_index(context: JsonDict) -> JsonDict:
    nodes_by_key: dict[tuple[object, str], JsonDict] = {}
    incoming_by_node: dict[tuple[object, str], list[JsonDict]] = {}
    outgoing_by_node: dict[tuple[object, str], list[JsonDict]] = {}
    connector_sources: dict[str, list[JsonDict]] = {}
    for node in context.get("fbd_nodes") or []:
        key = (node.get("sheet_id"), str(node.get("node_id")))
        nodes_by_key[key] = node
        if node.get("node_type") == "OCon" and node.get("connector_name"):
            connector_sources.setdefault(str(node["connector_name"]), []).append(node)
    for wire in context.get("fbd_wires") or []:
        to_key = (wire.get("sheet_id"), str(wire.get("to_id")))
        from_key = (wire.get("sheet_id"), str(wire.get("from_id")))
        incoming_by_node.setdefault(to_key, []).append(wire)
        outgoing_by_node.setdefault(from_key, []).append(wire)
    for rows in incoming_by_node.values():
        rows.sort(key=_fbd_wire_sort_key)
    for rows in outgoing_by_node.values():
        rows.sort(key=_fbd_wire_sort_key)
    return {
        "nodes_by_key": nodes_by_key,
        "incoming_by_node": incoming_by_node,
        "outgoing_by_node": outgoing_by_node,
        "connector_sources": connector_sources,
    }


def _fbd_connector_equation(node: JsonDict, index: JsonDict, unresolved: list[JsonDict], remote: bool) -> JsonDict | None:
    name = node.get("connector_name")
    target = _fbd_connector_expr(name)
    expr = _merge_exprs(_fbd_node_input_exprs(node, index, unresolved))
    if expr is None:
        unresolved.append({"node": node.get("id"), "connector": name, "reason": "no_incoming_wire"})
        expr = "UNRESOLVED"
    return {
        "kind": "connector_source_remote" if remote else "connector_source",
        "target": target,
        "expr": expr,
        "node_id": node.get("node_id"),
        "sheet": node.get("sheet_number"),
        "evidence_ref": node.get("id"),
        "unresolved": ["no_incoming_wire"] if expr == "UNRESOLVED" else [],
    }


def _fbd_oref_equation(node: JsonDict, index: JsonDict, unresolved: list[JsonDict]) -> JsonDict | None:
    target = node.get("operand")
    if not target:
        unresolved.append({"node": node.get("id"), "reason": "oref_without_operand"})
        return None
    expr = _merge_exprs(_fbd_node_input_exprs(node, index, unresolved))
    if expr is None:
        unresolved.append({"node": node.get("id"), "operand": target, "reason": "no_incoming_wire"})
        expr = "UNRESOLVED"
    return {
        "kind": "oref_write",
        "target": str(target),
        "expr": expr,
        "node_id": node.get("node_id"),
        "sheet": node.get("sheet_number"),
        "evidence_ref": node.get("id"),
        "unresolved": ["no_incoming_wire"] if expr == "UNRESOLVED" else [],
    }


def _fbd_block_equations(node: JsonDict, index: JsonDict, unresolved: list[JsonDict]) -> list[JsonDict]:
    inputs = _fbd_node_inputs(node, index, unresolved)
    output_params = _fbd_output_params(node, index)
    rows = []
    for output_param in output_params:
        target = f"{_fbd_node_label(node)}.{output_param}"
        expr = _fbd_block_expr(str(node.get("instruction") or "Block"), inputs, output_param)
        rows.append(
            {
                "kind": "block_output",
                "target": target,
                "expr": expr,
                "instruction": node.get("instruction"),
                "node_id": node.get("node_id"),
                "sheet": node.get("sheet_number"),
                "evidence_ref": node.get("id"),
                "unresolved": ["no_inputs"] if expr == "UNRESOLVED" else [],
            }
        )
    return rows


def _fbd_aoi_bindings(
    context: JsonDict,
    node: JsonDict,
    params: list[JsonDict],
    index: JsonDict,
    unresolved: list[JsonDict],
) -> list[JsonDict]:
    effective_params = params or _params_from_fbd_node(node)
    base_rows = _bindings_for_aoi_node(context, node, effective_params)
    node_key = (node.get("sheet_id"), str(node.get("node_id")))
    incoming_by_param: dict[str, list[JsonDict]] = {}
    outgoing_by_param: dict[str, list[JsonDict]] = {}
    for wire in index["incoming_by_node"].get(node_key, []):
        if wire.get("to_param"):
            incoming_by_param.setdefault(str(wire["to_param"]), []).append(wire)
    for wire in index["outgoing_by_node"].get(node_key, []):
        if wire.get("from_param"):
            outgoing_by_param.setdefault(str(wire["from_param"]), []).append(wire)

    rows = []
    for row in base_rows:
        name = str(row.get("param") or "")
        updated = dict(row)
        updated["sources"] = [
            source
            for wire in incoming_by_param.get(name, [])
            if (source := _fbd_wire_source_expr(wire, index, unresolved))
        ]
        updated["destinations"] = [
            dest
            for wire in outgoing_by_param.get(name, [])
            if (dest := _fbd_wire_destination_expr(wire, index, unresolved))
        ]
        updated["wired"] = bool(updated.get("argument") or updated.get("sources") or updated.get("destinations"))
        rows.append(updated)
    return rows


def _fbd_aoi_equations(node: JsonDict, bindings: list[JsonDict]) -> list[JsonDict]:
    instance = _fbd_node_label(node)
    rows = []
    for binding in bindings:
        param = binding.get("param")
        if not param:
            continue
        usage = str(binding.get("usage") or "").lower()
        source = f"{instance}.{param}"
        if usage in {"input", "inout"}:
            expr = _merge_exprs([binding.get("argument"), *binding.get("sources", [])])
            if expr is None:
                expr = "UNWIRED"
            rows.append(
                {
                    "kind": "aoi_input",
                    "target": source,
                    "expr": expr,
                    "instruction": node.get("instruction"),
                    "node_id": node.get("node_id"),
                    "sheet": node.get("sheet_number"),
                    "evidence_ref": node.get("id"),
                    "unresolved": ["unwired"] if expr == "UNWIRED" else [],
                }
            )
        if usage in {"output", "inout"}:
            destinations = [binding.get("argument"), *binding.get("destinations", [])]
            destinations = [str(dest) for dest in destinations if dest]
            if destinations:
                for dest in destinations:
                    rows.append(
                        {
                            "kind": "aoi_output",
                            "target": dest,
                            "expr": source,
                            "instruction": node.get("instruction"),
                            "node_id": node.get("node_id"),
                            "sheet": node.get("sheet_number"),
                            "evidence_ref": node.get("id"),
                            "unresolved": [],
                        }
                    )
            else:
                rows.append(
                    {
                        "kind": "aoi_output_unwired",
                        "target": source,
                        "expr": "UNWIRED",
                        "instruction": node.get("instruction"),
                        "node_id": node.get("node_id"),
                        "sheet": node.get("sheet_number"),
                        "evidence_ref": node.get("id"),
                        "unresolved": ["unwired"],
                    }
                )
    return rows


def _fbd_aoi_instance_summary(node: JsonDict, bindings: list[JsonDict]) -> JsonDict:
    return {
        "instance": node.get("operand"),
        "aoi": node.get("instruction"),
        "sheet": node.get("sheet_number"),
        "node_id": node.get("node_id"),
        "evidence_ref": node.get("id"),
        "summary": {
            "total": len(bindings),
            "wired": sum(1 for row in bindings if row.get("wired")),
            "unwired": sum(1 for row in bindings if not row.get("wired")),
            "required_unwired": [
                row.get("param")
                for row in bindings
                if not row.get("wired") and str(row.get("required")).lower() == "true"
            ],
        },
        "bindings": bindings,
    }


def _params_from_fbd_node(node: JsonDict) -> list[JsonDict]:
    rows = []
    for param in node.get("parameters") or []:
        kind = param.get("kind")
        if kind not in {"InputParameter", "OutputParameter", "InOutParameter"}:
            continue
        rows.append(
            {
                "name": param.get("name"),
                "usage": kind.replace("Parameter", "").replace("InOut", "InOut"),
                "required": "false",
                "visible": "true",
            }
        )
    return rows


def _fbd_node_inputs(node: JsonDict, index: JsonDict, unresolved: list[JsonDict]) -> list[JsonDict]:
    node_key = (node.get("sheet_id"), str(node.get("node_id")))
    rows = []
    used_params = set()
    for ordinal, wire in enumerate(index["incoming_by_node"].get(node_key, []), start=1):
        param = str(wire.get("to_param") or f"In{ordinal}")
        expr = _fbd_wire_source_expr(wire, index, unresolved) or "UNRESOLVED"
        rows.append({"param": param, "expr": expr})
        used_params.add(param)
    for param in node.get("parameters") or []:
        if param.get("kind") not in {"InputParameter", "InOutParameter"}:
            continue
        name = str(param.get("name") or "")
        argument = param.get("argument")
        if argument and name not in used_params:
            rows.append({"param": name, "expr": str(argument)})
    return rows


def _fbd_node_input_exprs(node: JsonDict, index: JsonDict, unresolved: list[JsonDict]) -> list[str]:
    return [row["expr"] for row in _fbd_node_inputs(node, index, unresolved) if row.get("expr")]


def _fbd_output_params(node: JsonDict, index: JsonDict) -> list[str]:
    node_key = (node.get("sheet_id"), str(node.get("node_id")))
    params = []
    for wire in index["outgoing_by_node"].get(node_key, []):
        if wire.get("from_param"):
            params.append(str(wire["from_param"]))
    for param in node.get("parameters") or []:
        if param.get("kind") in {"OutputParameter", "InOutParameter"} and param.get("name"):
            params.append(str(param["name"]))
    if not params and node.get("node_type") == "Block" and index["incoming_by_node"].get(node_key):
        params.append("Out")
    out = []
    seen = set()
    for param in params:
        if param not in seen:
            seen.add(param)
            out.append(param)
    return out


def _fbd_wire_source_expr(wire: JsonDict, index: JsonDict, unresolved: list[JsonDict]) -> str | None:
    node = index["nodes_by_key"].get((wire.get("sheet_id"), str(wire.get("from_id"))))
    if not node:
        unresolved.append({"wire": wire.get("id"), "from_id": wire.get("from_id"), "reason": "source_node_not_found"})
        return f"unresolved:{wire.get('from_id')}"
    return _fbd_node_expr(node, wire.get("from_param"))


def _fbd_wire_destination_expr(wire: JsonDict, index: JsonDict, unresolved: list[JsonDict]) -> str | None:
    node = index["nodes_by_key"].get((wire.get("sheet_id"), str(wire.get("to_id"))))
    if not node:
        unresolved.append({"wire": wire.get("id"), "to_id": wire.get("to_id"), "reason": "destination_node_not_found"})
        return f"unresolved:{wire.get('to_id')}"
    return _fbd_node_expr(node, wire.get("to_param"), destination=True)


def _fbd_node_expr(node: JsonDict, param: object | None = None, destination: bool = False) -> str:
    node_type = node.get("node_type")
    if node_type == "IRef":
        return str(node.get("operand") or "IRef")
    if node_type == "ORef":
        return str(node.get("operand") or "ORef")
    if node_type in {"ICon", "OCon"}:
        return _fbd_connector_expr(node.get("connector_name"))
    label = _fbd_node_label(node)
    if param:
        return f"{label}.{param}"
    return label


def _fbd_node_label(node: JsonDict) -> str:
    if node.get("operand"):
        return str(node["operand"])
    instruction = str(node.get("instruction") or node.get("node_type") or "Node")
    return f"{instruction}_{node.get('node_id')}"


def _fbd_connector_expr(name: object) -> str:
    return f'net "{name or "UNNAMED"}"'


def _fbd_block_expr(instruction: str, inputs: list[JsonDict], output_param: str) -> str:
    opcode = instruction.upper()
    values = [str(row.get("expr")) for row in inputs if row.get("expr")]
    if opcode in {"BAND", "AND"}:
        return _fbd_infix(values, "AND")
    if opcode in {"BOR", "OR"}:
        return _fbd_infix(values, "OR")
    if opcode in {"BNOT", "NOT"}:
        return f"NOT({values[0]})" if values else "UNRESOLVED"
    if opcode in {"ADD", "SUB", "MUL", "DIV"}:
        symbol = {"ADD": "+", "SUB": "-", "MUL": "*", "DIV": "/"}[opcode]
        return _fbd_binary(values, symbol)
    if opcode in {"EQU", "EQ"}:
        return _fbd_binary(values, "==")
    if opcode in {"NEQ", "NE"}:
        return _fbd_binary(values, "!=")
    if opcode in {"GRT", "GT"}:
        return _fbd_binary(values, ">")
    if opcode in {"GEQ", "GE"}:
        return _fbd_binary(values, ">=")
    if opcode in {"LES", "LT"}:
        return _fbd_binary(values, "<")
    if opcode in {"LEQ", "LE"}:
        return _fbd_binary(values, "<=")
    args = ", ".join(f"{row.get('param')}={row.get('expr')}" for row in inputs if row.get("expr"))
    suffix = "" if output_param == "Out" else f".{output_param}"
    return f"{instruction}({args}){suffix}"


def _fbd_infix(values: list[str], op: str) -> str:
    if not values:
        return "UNRESOLVED"
    if len(values) == 1:
        return values[0]
    return f" {op} ".join(_fbd_paren(value) for value in values)


def _fbd_binary(values: list[str], op: str) -> str:
    if len(values) < 2:
        return "UNRESOLVED"
    return f"{_fbd_paren(values[0])} {op} {_fbd_paren(values[1])}"


def _fbd_paren(value: str) -> str:
    if " AND " in value or " OR " in value:
        return f"({value})"
    return value


def _merge_exprs(values: list[object | None]) -> str | None:
    clean = [str(value) for value in values if value not in (None, "")]
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    return "MERGE(" + ", ".join(clean) + ")"


def _fbd_connector_summary(node: JsonDict) -> JsonDict:
    return {
        "name": node.get("connector_name"),
        "kind": node.get("node_type"),
        "sheet": str(node.get("sheet_number")) if node.get("sheet_number") is not None else None,
        "node_id": node.get("node_id"),
        "evidence_ref": node.get("id"),
    }


def _append_symbol(rows: list[str], value: object) -> None:
    if value is None:
        return
    text = str(value)
    for symbol in _symbols_from_logic_text(text):
        if _is_probable_fbd_symbol(symbol) and symbol not in rows:
            rows.append(symbol)


def _is_probable_fbd_symbol(symbol: str) -> bool:
    if not symbol or symbol.upper() in {"TRUE", "FALSE", "UNWIRED", "UNRESOLVED", "MERGE"}:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?", symbol):
        return False
    if symbol.startswith("net"):
        return False
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.\[\]-]*$", symbol))


def _fbd_sheet_limits(equations: list[JsonDict], limit: int, unresolved: list[JsonDict]) -> list[str]:
    limits = []
    if len(equations) > limit:
        limits.append("pseudo_equations_truncated")
    if unresolved:
        limits.append("unresolved_edges_present")
    limits.append("Block semantics are V1: common boolean/math blocks are simplified; complex instructions remain neutral calls.")
    return limits


def _trace_fbd_pseudo_sheet(
    workspace: str | Path,
    context: JsonDict,
    symbol: str,
    ref: JsonDict,
    limit: int,
) -> JsonDict:
    node = _fbd_trace_start_node(context, symbol, ref)
    if not node:
        return {"found": False, "reason": "fbd_trace_start_node_not_found"}
    sheet = node.get("sheet_number")
    if sheet is None:
        sheet = node.get("sheet_id")
    return get_fbd_sheet(workspace, routine_id=(context.get("routine") or {}).get("id"), sheet=sheet, limit=limit)


def _fbd_trace_start_node(context: JsonDict, symbol: str, ref: JsonDict) -> JsonDict | None:
    nodes = context.get("fbd_nodes") or []
    by_full_id = {node.get("id"): node for node in nodes}
    if ref.get("source") in by_full_id:
        return by_full_id[ref.get("source")]
    targets = _find_fbd_target_nodes(nodes, symbol)
    return targets[0] if targets else None


def _fbd_node_sort_key(node: JsonDict) -> tuple[int, str]:
    text = str(node.get("node_id") or "")
    return (int(text) if text.isdigit() else 10**9, text)


def _fbd_wire_sort_key(wire: JsonDict) -> tuple[int, int, str]:
    from_id = str(wire.get("from_id") or "")
    to_id = str(wire.get("to_id") or "")
    return (
        int(from_id) if from_id.isdigit() else 10**9,
        int(to_id) if to_id.isdigit() else 10**9,
        str(wire.get("id") or ""),
    )


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
        "chart_id": unit.get("chart_id"),
        "online_edit_type": unit.get("online_edit_type"),
        "node_count": unit.get("node_count"),
        "link_count": unit.get("link_count"),
        "branch_count": unit.get("branch_count"),
        "leg_count": unit.get("leg_count"),
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


def _dataset(workspace: str | Path, name: str) -> list[JsonDict]:
    if db.has_index(workspace):
        return db.dataset(workspace, name)
    return _read_jsonl(workspace, name)


def _read_json_file(path: Path) -> JsonDict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


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


def _symbols_from_logic_text(text: str) -> list[str]:
    symbols = []
    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\[[A-Za-z0-9_]+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[[A-Za-z0-9_]+\])?)*\b", text):
        if token.upper() in {"XIC", "XIO", "OTE", "OTL", "OTU", "MOV", "ALMD", "ONS", "OR", "AND", "NOT"}:
            continue
        if token not in symbols:
            symbols.append(token)
    return symbols


def _dedupe_members(rows: list[JsonDict]) -> list[JsonDict]:
    out = []
    seen = set()
    for row in rows:
        symbol = row.get("symbol")
        if symbol in seen:
            continue
        seen.add(symbol)
        out.append(row)
    return out


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


def _dedupe_strings(rows: list[str]) -> list[str]:
    out = []
    seen = set()
    for row in rows:
        if row in seen:
            continue
        seen.add(row)
        out.append(row)
    return out
