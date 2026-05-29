"""Relational/graph queries over an ingested Logix workspace.

These build on the cross-reference and edge data to answer the questions an
engineer actually asks of a controller project:

* ``tag_producers_consumers`` - who writes a tag vs who reads it.
* ``impact_of`` - transitive change propagation from a tag through the logic.
* ``io_trace`` - follow an alias chain from a tag to physical I/O and alarms.
* ``call_graph`` - task -> program -> routine scheduling and JSR/AOI call edges.

All functions accept a workspace path and prefer the SQLite index, falling back
to the JSONL readers when the index is absent.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable

from . import db
from .workspace import find_references, read_jsonl


READ_ACCESS = {"read", "read_write"}
WRITE_ACCESS = {"write", "read_write"}


def base_symbol(symbol: str) -> str:
    """First component of a symbol path (drops members and array indices)."""

    return str(symbol or "").split(".", 1)[0].split("[", 1)[0]


# --------------------------------------------------------------------------- #
# Loaders (SQLite-first, JSONL fallback)
# --------------------------------------------------------------------------- #


def _xrefs_min(workspace: str | Path) -> list[dict]:
    if db.has_index(workspace):
        return db.xref_rows_min(workspace)
    return [
        {
            "symbol": row.get("symbol"),
            "base_symbol": row.get("base_symbol") or base_symbol(row.get("symbol") or ""),
            "routine": row.get("routine"),
            "access": row.get("access"),
            "instruction": row.get("instruction"),
        }
        for row in read_jsonl(workspace, "xrefs.jsonl")
    ]


def _routine_index(workspace: str | Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in read_jsonl(workspace, "routines.jsonl"):
        index[row.get("id")] = {
            "routine_id": row.get("id"),
            "routine": row.get("name"),
            "program": row.get("program"),
            "owner": row.get("owner"),
        }
    return index


def _tag_symbols(workspace: str | Path) -> list[dict]:
    return [row for row in read_jsonl(workspace, "symbols.jsonl") if row.get("kind") == "tag"]


def _aoi_names(workspace: str | Path) -> set[str]:
    return {
        str(row.get("name") or "").upper()
        for row in read_jsonl(workspace, "symbols.jsonl")
        if row.get("kind") == "aoi"
    }


# --------------------------------------------------------------------------- #
# Producers / consumers
# --------------------------------------------------------------------------- #


def tag_producers_consumers(workspace: str | Path, name: str) -> dict:
    """Routines that write a tag (producers) vs read it (consumers)."""

    refs = find_references(workspace, name, limit=1_000_000)
    producers: dict[str, dict] = {}
    consumers: dict[str, dict] = {}
    for ref in refs:
        access = ref.get("access")
        entry_key = ref.get("routine")
        display = {
            "routine_id": ref.get("routine"),
            "routine": ref.get("routine_name"),
            "program": ref.get("program"),
            "owner": ref.get("owner"),
        }
        if access in WRITE_ACCESS:
            _accumulate(producers, entry_key, display, ref.get("instruction"))
        if access in READ_ACCESS:
            _accumulate(consumers, entry_key, display, ref.get("instruction"))

    return {
        "tag": name,
        "producer_count": len(producers),
        "consumer_count": len(consumers),
        "producers": _finalize(producers),
        "consumers": _finalize(consumers),
    }


def _accumulate(bucket: dict[str, dict], key: str | None, display: dict, instruction: object) -> None:
    if key is None:
        return
    entry = bucket.setdefault(key, {**display, "_instructions": set()})
    if instruction:
        entry["_instructions"].add(str(instruction))


def _finalize(bucket: dict[str, dict]) -> list[dict]:
    rows = []
    for entry in bucket.values():
        instructions = sorted(entry.pop("_instructions"))
        rows.append({**entry, "instructions": instructions})
    rows.sort(key=lambda row: (row.get("program") or "", row.get("routine") or ""))
    return rows


# --------------------------------------------------------------------------- #
# Impact analysis
# --------------------------------------------------------------------------- #


def impact_of(workspace: str | Path, name: str, max_depth: int = 3, limit: int = 300) -> dict:
    """Transitive change propagation from a tag.

    Models a forward data-flow: a routine that *reads* an affected tag is itself
    affected, and every tag it *writes* becomes affected at the next level. The
    walk stops at ``max_depth`` levels.
    """

    reads_by_tag: dict[str, set[str]] = defaultdict(set)
    writes_by_routine: dict[str, set[str]] = defaultdict(set)
    for ref in _xrefs_min(workspace):
        base = ref.get("base_symbol") or base_symbol(ref.get("symbol") or "")
        routine = ref.get("routine")
        access = ref.get("access")
        if not base or not routine:
            continue
        if access in READ_ACCESS:
            reads_by_tag[base].add(routine)
        if access in WRITE_ACCESS:
            writes_by_routine[routine].add(base)

    start = base_symbol(name)
    seen_tags: dict[str, int] = {start: 0}
    seen_routines: dict[str, int] = {}
    frontier = {start}
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        new_routines = set()
        for tag in frontier:
            for routine in reads_by_tag.get(tag, ()):  # routines reading an affected tag
                if routine not in seen_routines:
                    seen_routines[routine] = depth
                    new_routines.add(routine)
        next_tags = set()
        for routine in new_routines:
            for tag in writes_by_routine.get(routine, ()):  # tags they drive
                if tag not in seen_tags:
                    seen_tags[tag] = depth
                    next_tags.add(tag)
        frontier = next_tags

    routine_index = _routine_index(workspace)
    affected_routines = [
        {**routine_index.get(rid, {"routine_id": rid}), "depth": depth}
        for rid, depth in sorted(seen_routines.items(), key=lambda kv: kv[1])
    ]
    affected_tags = [
        {"tag": tag, "depth": depth}
        for tag, depth in sorted(seen_tags.items(), key=lambda kv: kv[1])
        if tag != start
    ]
    impacted_alarms = [
        {"tag_name": row.get("tag_name"), "alarm_type": row.get("alarm_type"), "severity": row.get("severity")}
        for row in read_jsonl(workspace, "alarms.jsonl")
        if base_symbol(row.get("tag_name") or "") in seen_tags
    ]

    return {
        "tag": name,
        "start_base": start,
        "max_depth": max_depth,
        "affected_routine_count": len(affected_routines),
        "affected_tag_count": len(affected_tags),
        "affected_routines": affected_routines[:limit],
        "affected_tags": affected_tags[:limit],
        "impacted_alarms": impacted_alarms[:limit],
        "truncated": len(affected_routines) > limit or len(affected_tags) > limit,
    }


# --------------------------------------------------------------------------- #
# I/O traceability
# --------------------------------------------------------------------------- #


def io_trace(workspace: str | Path, name: str) -> dict:
    """Resolve a tag's alias chain and trace it to logic, I/O, and alarms."""

    tags = _tag_symbols(workspace)
    alias_of = {row.get("name"): row.get("alias_for") for row in tags if row.get("name")}

    # Walk forward to the ultimate (non-alias) base of the requested tag.
    root = name
    visited: set[str] = set()
    while alias_of.get(root) and base_symbol(alias_of[root]) not in visited:
        visited.add(root)
        root = base_symbol(alias_of[root])

    # Collect every tag that resolves (transitively) to the same root.
    chain = {name, root}
    changed = True
    while changed:
        changed = False
        for tag_name, target in alias_of.items():
            if not target:
                continue
            if base_symbol(target) in chain and tag_name not in chain:
                chain.add(tag_name)
                changed = True

    references: list[dict] = []
    for member in sorted(chain):
        references.extend(find_references(workspace, member, limit=1_000_000))

    io_points = [
        row
        for row in read_jsonl(workspace, "module_io_points.jsonl")
        if base_symbol(row.get("operand") or "") in chain or (row.get("operand") in chain)
    ]
    alarms = [
        row
        for row in read_jsonl(workspace, "alarms.jsonl")
        if base_symbol(row.get("tag_name") or "") in chain
    ]

    routine_index = _routine_index(workspace)
    routine_ids = sorted({ref.get("routine") for ref in references if ref.get("routine")})
    routines = [routine_index.get(rid, {"routine_id": rid}) for rid in routine_ids]
    return {
        "tag": name,
        "alias_root": root,
        "alias_chain": sorted(chain),
        "reference_count": len(references),
        "routines": routines,
        "io_points": io_points,
        "alarms": alarms,
    }


# --------------------------------------------------------------------------- #
# Call graph
# --------------------------------------------------------------------------- #


def call_graph(workspace: str | Path, routine: str | None = None, program: str | None = None) -> dict:
    """Callers/callees of a routine, or the task/program scheduling tree."""

    if routine is not None:
        return _routine_call_view(workspace, routine, program)
    return _scheduling_tree(workspace)


def _routine_call_view(workspace: str | Path, routine: str, program: str | None) -> dict:
    routines = read_jsonl(workspace, "routines.jsonl")
    selected = None
    for row in routines:
        if row.get("name") == routine and (program is None or row.get("program") == program):
            selected = row
            break
    if selected is None:
        return {"routine": routine, "found": False}

    aoi_names = _aoi_names(workspace)
    rid = selected.get("id")
    callees: list[dict] = []
    seen_callees: set[tuple] = set()
    callers: list[dict] = []
    seen_callers: set[str] = set()
    routine_by_id = _routine_index(workspace)

    for ref in _xrefs_min(workspace):
        instruction = str(ref.get("instruction") or "")
        # Callees of the selected routine.
        if ref.get("routine") == rid:
            if ref.get("access") == "call":
                key = ("routine", ref.get("symbol"))
                if key not in seen_callees:
                    seen_callees.add(key)
                    callees.append({"type": "routine", "callee": ref.get("symbol")})
            elif instruction.upper() in aoi_names:
                key = ("aoi", instruction)
                if key not in seen_callees:
                    seen_callees.add(key)
                    callees.append({"type": "aoi", "callee": instruction})
        # Callers: any routine that JSRs this routine's name.
        if ref.get("access") == "call" and ref.get("symbol") == selected.get("name"):
            caller_id = ref.get("routine")
            if caller_id and caller_id != rid and caller_id not in seen_callers:
                seen_callers.add(caller_id)
                callers.append(routine_by_id.get(caller_id, {"routine_id": caller_id}))

    return {
        "routine": routine,
        "routine_id": rid,
        "program": selected.get("program"),
        "owner": selected.get("owner"),
        "found": True,
        "callees": callees,
        "callers": callers,
    }


def _scheduling_tree(workspace: str | Path) -> dict:
    tasks = read_jsonl(workspace, "tasks.jsonl")
    programs = read_jsonl(workspace, "programs.jsonl")
    program_names = {row.get("name") for row in programs}

    scheduled: set[str] = set()
    task_rows = []
    for task in tasks:
        names = list(task.get("scheduled_programs") or [])
        scheduled.update(names)
        task_rows.append(
            {
                "task": task.get("name"),
                "type": task.get("task_type"),
                "rate": task.get("rate"),
                "priority": task.get("priority"),
                "watchdog": task.get("watchdog"),
                "programs": names,
            }
        )

    unscheduled = sorted(name for name in program_names if name and name not in scheduled)
    return {
        "tasks": task_rows,
        "program_count": len(program_names),
        "unscheduled_programs": unscheduled,
    }
