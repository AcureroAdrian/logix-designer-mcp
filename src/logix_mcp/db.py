"""Read-only SQLite query layer over the materialized Logix index.

The workspace ingest step already builds ``index/logix.sqlite`` with one row per
IR record (plus a generic ``ir_rows`` mirror and an FTS table). Historically the
MCP query layer ignored that index and re-parsed the full ``ir/*.jsonl`` files on
every call, which is expensive for the larger surfaces (34k+ xrefs, 20k+ data
values, 12k+ comments).

This module turns the index into the primary query path. Every public function
opens the index in read-only mode, runs an indexed query, and returns plain
``dict`` records identical to the JSONL rows. When the index is missing (older or
partially-built workspaces) :func:`has_index` returns ``False`` so callers can
fall back to the JSONL readers.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.request import pathname2url
import json
import sqlite3


def index_path(workspace: str | Path) -> Path:
    return Path(workspace) / "index" / "logix.sqlite"


def has_index(workspace: str | Path) -> bool:
    return index_path(workspace).exists()


@contextmanager
def connect(workspace: str | Path) -> Iterator[sqlite3.Connection]:
    """Open the workspace index read-only. Caller must ensure it exists."""

    path = index_path(workspace)
    uri = f"file:{pathname2url(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    out: list[dict] = []
    for row in cursor:
        keys = row.keys()
        if "json" in keys and row["json"]:
            out.append(json.loads(row["json"]))
        else:
            out.append({key: row[key] for key in keys})
    return out


def _like_escape(value: str) -> str:
    """Escape SQL LIKE wildcards so tag names with ``_`` match literally."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def dataset(workspace: str | Path, name: str, kind: str | None = None, limit: int | None = None) -> list[dict]:
    """Return all rows for an IR dataset from the ``ir_rows`` mirror table."""

    with connect(workspace) as conn:
        if kind is not None:
            cursor = conn.execute("SELECT json FROM ir_rows WHERE dataset = ? AND kind = ?", (name, kind))
        else:
            cursor = conn.execute("SELECT json FROM ir_rows WHERE dataset = ?", (name,))
        rows = _rows(cursor)
    return rows[:limit] if limit is not None else rows


def _dataset_filter(conn: sqlite3.Connection, name: str, field: str, value: object) -> list[dict]:
    cursor = conn.execute("SELECT json FROM ir_rows WHERE dataset = ?", (name,))
    return [obj for obj in _rows(cursor) if obj.get(field) == value]


# --------------------------------------------------------------------------- #
# Hot query paths
# --------------------------------------------------------------------------- #


def find_references(workspace: str | Path, symbol: str, limit: int = 200) -> list[dict]:
    """Cross-references for ``symbol`` and its members (case-insensitive)."""

    pattern = _like_escape(symbol)
    with connect(workspace) as conn:
        cursor = conn.execute(
            "SELECT json FROM xrefs "
            "WHERE symbol = ? COLLATE NOCASE "
            "OR symbol LIKE ? ESCAPE '\\' "
            "OR symbol LIKE ? ESCAPE '\\' "
            "LIMIT ?",
            (symbol, pattern + ".%", pattern + "[%", limit),
        )
        return _rows(cursor)


def routine_context(
    workspace: str | Path,
    program: str | None = None,
    routine: str | None = None,
    routine_id: str | None = None,
) -> dict | None:
    with connect(workspace) as conn:
        selected = _select_routine(conn, program, routine, routine_id)
        if selected is None:
            return None
        rid = selected["id"]
        return {
            "routine": selected,
            "units": _rows(conn.execute("SELECT json FROM routine_units WHERE routine_id = ?", (rid,))),
            "xrefs": _rows(conn.execute("SELECT json FROM xrefs WHERE routine = ?", (rid,))),
            "fbd_nodes": _dataset_filter(conn, "fbd_nodes", "routine_id", rid),
            "fbd_wires": _dataset_filter(conn, "fbd_wires", "routine_id", rid),
            "sfc_nodes": _dataset_filter(conn, "sfc_nodes", "routine_id", rid),
            "sfc_links": _dataset_filter(conn, "sfc_links", "routine_id", rid),
        }


def _select_routine(
    conn: sqlite3.Connection,
    program: str | None,
    routine: str | None,
    routine_id: str | None,
) -> dict | None:
    if routine_id:
        rows = _rows(conn.execute("SELECT json FROM routines WHERE id = ? LIMIT 1", (routine_id,)))
        if rows:
            return rows[0]
    if routine:
        if program is not None:
            rows = _rows(conn.execute("SELECT json FROM routines WHERE name = ? AND program = ? LIMIT 1", (routine, program)))
        else:
            rows = _rows(conn.execute("SELECT json FROM routines WHERE name = ? LIMIT 1", (routine,)))
        if rows:
            return rows[0]
    return None


def get_entity(workspace: str | Path, entity_id: str) -> dict | None:
    with connect(workspace) as conn:
        for table in ["entities", "symbols", "routines", "routine_units", "modules"]:
            rows = _rows(conn.execute(f"SELECT json FROM {table} WHERE id = ? LIMIT 1", (entity_id,)))
            if rows:
                return rows[0]
        for obj in _dataset_filter(conn, "module_io_points", "id", entity_id):
            return obj
        rows = _rows(conn.execute("SELECT json FROM alarms WHERE id = ? LIMIT 1", (entity_id,)))
        if rows:
            return rows[0]
    return None


def find_symbol(workspace: str | Path, name: str, scope: str | None = None) -> dict | None:
    with connect(workspace) as conn:
        cursor = conn.execute("SELECT json FROM symbols WHERE name = ?", (name,))
        for row in _rows(cursor):
            if scope and row.get("scope") != scope:
                continue
            return row
    return None


def tag_comments(workspace: str | Path, name: str) -> list[dict]:
    with connect(workspace) as conn:
        return _dataset_filter(conn, "tag_comments", "tag_name", name)


def tag_data(workspace: str | Path, name: str) -> list[dict]:
    with connect(workspace) as conn:
        cursor = conn.execute("SELECT json FROM data_values WHERE owner_name = ?", (name,))
        return _rows(cursor)


def xref_rows_min(workspace: str | Path) -> list[dict]:
    """All cross-references projected to the columns the graph layer needs.

    Avoids decoding the full JSON payload, so a whole-project scan stays cheap.
    """

    with connect(workspace) as conn:
        cursor = conn.execute("SELECT symbol, base_symbol, routine, access, instruction FROM xrefs")
        return [
            {
                "symbol": row["symbol"],
                "base_symbol": row["base_symbol"],
                "routine": row["routine"],
                "access": row["access"],
                "instruction": row["instruction"],
            }
            for row in cursor
        ]


def comments_for_target(workspace: str | Path, name: str) -> list[dict]:
    """Comments whose target is ``name`` or a member/element of it.

    The SQL predicate is broad (case-insensitive) for index friendliness; the
    exact, case-sensitive membership test is reapplied in Python to match the
    historical JSONL behavior.
    """

    pattern = _like_escape(name)
    with connect(workspace) as conn:
        cursor = conn.execute(
            "SELECT json FROM comments "
            "WHERE target = ? COLLATE NOCASE "
            "OR target LIKE ? ESCAPE '\\' "
            "OR target LIKE ? ESCAPE '\\'",
            (name, pattern + ".%", pattern + "[%"),
        )
        rows = _rows(cursor)
    prefix_dot = name + "."
    prefix_idx = name + "["
    return [
        row
        for row in rows
        if row.get("target") == name
        or str(row.get("target") or "").startswith(prefix_dot)
        or str(row.get("target") or "").startswith(prefix_idx)
    ]
