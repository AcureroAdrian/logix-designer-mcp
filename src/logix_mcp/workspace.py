"""Workspace materialization and query helpers for parsed Logix projects."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re
import shutil
import sqlite3

from . import db
from .parser import parse_l5x


JsonDict = dict[str, Any]


def ingest_l5x(l5x_path: str | Path, out_dir: str | Path | None = None, copy_source: bool = True) -> dict:
    source = Path(l5x_path).resolve()
    workspace = Path(out_dir).resolve() if out_dir else source.with_suffix(".logix").resolve()
    project = parse_l5x(source)

    _prepare_dirs(workspace)
    if copy_source:
        target = workspace / "source" / "original" / source.name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)

    summary = _project_summary(project, source, workspace)
    _write_json(workspace / "ir" / "manifest.json", _manifest(project, source, workspace))
    _write_json(workspace / "ir" / "project.json", summary)
    _write_json(workspace / "ir" / "coverage.json", project["coverage"])
    _write_json(workspace / "ir" / "device_tree.json", project["device_tree"])
    for name, rows in _jsonl_datasets(project).items():
        _write_jsonl(workspace / "ir" / f"{name}.jsonl", rows)
    _write_ai_files(workspace, project)
    _write_sqlite(workspace / "index" / "logix.sqlite", project)
    _write_diagnostics(workspace)
    return load_workspace(workspace)


def _write_diagnostics(workspace: Path) -> None:
    """Run the diagnostic rules and persist machine- and human-readable reports."""

    from .diagnostics import diagnostics_markdown, run_diagnostics

    result = run_diagnostics(workspace)
    _write_json(workspace / "ir" / "diagnostics.json", result)
    (workspace / "ai" / "diagnostics.md").write_text(diagnostics_markdown(result), encoding="utf-8")


def load_workspace(workspace: str | Path) -> dict:
    workspace_path = Path(workspace).resolve()
    project_file = workspace_path / "ir" / "project.json"
    if not project_file.exists():
        raise FileNotFoundError(f"Logix workspace not found or not ingested: {workspace_path}")
    return {
        "workspace": str(workspace_path),
        "project": json.loads(project_file.read_text(encoding="utf-8")),
    }


def inspect_workspace(workspace: str | Path) -> dict:
    loaded = load_workspace(workspace)
    root = Path(loaded["workspace"])
    files = {}
    for path in sorted((root / "ir").glob("*.jsonl")):
        files[path.stem] = _count_jsonl(path)
    return {
        **loaded["project"],
        "files": files,
        "has_coverage": (root / "ir" / "coverage.json").exists(),
    }


def read_jsonl(workspace: str | Path, name: str) -> list[dict]:
    path = Path(workspace) / "ir" / name
    if not path.exists() and not name.endswith(".jsonl"):
        path = Path(workspace) / "ir" / f"{name}.jsonl"
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def query_symbols(workspace: str | Path, kind: str | None = None, limit: int = 200) -> list[dict]:
    rows = read_jsonl(workspace, "symbols.jsonl")
    if kind:
        rows = [row for row in rows if row.get("kind") == kind]
    return rows[:limit]


def find_symbol(workspace: str | Path, name: str, scope: str | None = None) -> dict | None:
    if db.has_index(workspace):
        return db.find_symbol(workspace, name, scope)
    for row in read_jsonl(workspace, "symbols.jsonl"):
        if row.get("name") != name:
            continue
        if scope and row.get("scope") != scope:
            continue
        return row
    return None


def find_routine(workspace: str | Path, program: str, routine: str) -> dict | None:
    for row in read_jsonl(workspace, "routines.jsonl"):
        if row.get("program") == program and row.get("name") == routine:
            return row
    return None


def search_logic(workspace: str | Path, pattern: str, limit: int = 50) -> list[dict]:
    matches = []
    needle = pattern.lower()
    for row in read_jsonl(workspace, "routines.jsonl"):
        body = row.get("body") or ""
        if needle in body.lower():
            matches.append(
                {
                    "program": row.get("program"),
                    "owner": row.get("owner"),
                    "routine": row.get("name"),
                    "language": row.get("language"),
                    "snippet": _snippet(body, pattern),
                }
            )
        if len(matches) >= limit:
            break
    return matches


def find_references(workspace: str | Path, symbol: str, limit: int = 200) -> list[dict]:
    if db.has_index(workspace):
        return db.find_references(workspace, symbol, limit)
    target = symbol.lower()
    matches = []
    for row in read_jsonl(workspace, "xrefs.jsonl"):
        row_symbol = row.get("symbol", "").lower()
        if row_symbol == target or row_symbol.startswith(target + ".") or row_symbol.startswith(target + "["):
            matches.append(row)
        if len(matches) >= limit:
            break
    return matches


def query_entities(workspace: str | Path, kind: str | None = None, limit: int = 200) -> list[dict]:
    rows = read_jsonl(workspace, "entities.jsonl")
    if kind:
        rows = [row for row in rows if row.get("kind") == kind]
    return rows[:limit]


def get_entity(workspace: str | Path, entity_id: str) -> dict | None:
    if db.has_index(workspace):
        return db.get_entity(workspace, entity_id)
    for file_name in [
        "entities.jsonl",
        "symbols.jsonl",
        "routines.jsonl",
        "routine_units.jsonl",
        "modules.jsonl",
        "module_io_points.jsonl",
        "alarms.jsonl",
    ]:
        for row in read_jsonl(workspace, file_name):
            if row.get("id") == entity_id:
                return row
    return None


def search_entities(workspace: str | Path, query: str, limit: int = 50) -> list[dict]:
    needle = query.lower()
    matches = []
    for file_name in [
        "entities.jsonl",
        "comments.jsonl",
        "routines.jsonl",
        "routine_units.jsonl",
        "module_io_points.jsonl",
        "alarms.jsonl",
        "messages.jsonl",
    ]:
        for row in read_jsonl(workspace, file_name):
            text = _search_text(row)
            if needle in text.lower():
                result = dict(row)
                result["_source_file"] = file_name
                matches.append(result)
                if len(matches) >= limit:
                    return matches
    return matches


def get_routine_context(workspace: str | Path, program: str | None = None, routine: str | None = None, routine_id: str | None = None) -> dict | None:
    if db.has_index(workspace):
        return db.routine_context(workspace, program, routine, routine_id)
    routines = read_jsonl(workspace, "routines.jsonl")
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
        "units": [row for row in read_jsonl(workspace, "routine_units.jsonl") if row.get("routine_id") == rid],
        "xrefs": [row for row in read_jsonl(workspace, "xrefs.jsonl") if row.get("routine") == rid],
        "fbd_nodes": [row for row in read_jsonl(workspace, "fbd_nodes.jsonl") if row.get("routine_id") == rid],
        "fbd_wires": [row for row in read_jsonl(workspace, "fbd_wires.jsonl") if row.get("routine_id") == rid],
        "sfc_nodes": [row for row in read_jsonl(workspace, "sfc_nodes.jsonl") if row.get("routine_id") == rid],
        "sfc_links": [row for row in read_jsonl(workspace, "sfc_links.jsonl") if row.get("routine_id") == rid],
    }


def get_module_bundle(workspace: str | Path, module: str) -> dict | None:
    modules = [row for row in read_jsonl(workspace, "modules.jsonl") if row.get("name") == module or row.get("id") == module]
    if not modules:
        return None
    selected = modules[0]
    name = selected["name"]
    return {
        "module": selected,
        "ports": [row for row in read_jsonl(workspace, "module_ports.jsonl") if row.get("module") == name],
        "connections": [row for row in read_jsonl(workspace, "module_connections.jsonl") if row.get("module") == name],
        "io_tags": [row for row in read_jsonl(workspace, "module_io_tags.jsonl") if row.get("module") == name],
        "io_points": [row for row in read_jsonl(workspace, "module_io_points.jsonl") if row.get("module") == name],
    }


def get_tag_bundle(workspace: str | Path, name: str, scope: str | None = None) -> dict | None:
    tag = find_symbol(workspace, name, scope)
    if not tag:
        return None
    if db.has_index(workspace):
        return {
            "tag": tag,
            "comments": db.comments_for_target(workspace, name),
            "tag_comments": db.tag_comments(workspace, name),
            "data": db.tag_data(workspace, name),
            "references": find_references(workspace, name, limit=500),
        }
    return {
        "tag": tag,
        "comments": [
            row for row in read_jsonl(workspace, "comments.jsonl")
            if row.get("target") == name or str(row.get("target") or "").startswith(name + ".") or str(row.get("target") or "").startswith(name + "[")
        ],
        "tag_comments": [
            row for row in read_jsonl(workspace, "tag_comments.jsonl")
            if row.get("tag_name") == name
        ],
        "data": [
            row for row in read_jsonl(workspace, "tag_data.jsonl")
            if (row.get("owner") or {}).get("name") == name
        ],
        "references": find_references(workspace, name, limit=500),
    }


def get_aoi_bundle(workspace: str | Path, name: str) -> dict | None:
    definitions = [row for row in read_jsonl(workspace, "aoi_definitions.jsonl") if row.get("name") == name]
    if not definitions:
        return None
    return {
        "definition": definitions[0],
        "parameters": [row for row in read_jsonl(workspace, "aoi_parameters.jsonl") if row.get("aoi") == name],
        "local_tags": [row for row in read_jsonl(workspace, "aoi_local_tags.jsonl") if row.get("scope") == name],
        "routines": [row for row in read_jsonl(workspace, "routines.jsonl") if row.get("owner") == f"AOI:{name}"],
    }


def _prepare_dirs(workspace: Path) -> None:
    for part in [
        "source/original",
        "ir",
        "ai/tags",
        "ai/udts",
        "ai/aois",
        "ai/programs",
        "ai/modules",
        "ai/alarms",
        "index",
        "bundles",
    ]:
        (workspace / part).mkdir(parents=True, exist_ok=True)


def _manifest(project: dict, source: Path, workspace: Path) -> dict:
    return {
        "format": "logix-mcp-workspace",
        "version": 2,
        "source_path": str(source),
        "workspace": str(workspace),
        "controller": project["controller"].get("name"),
        "datasets": sorted(_jsonl_datasets(project).keys()),
        "counts": project["counts"],
        "coverage": project["coverage"],
    }


def _project_summary(project: dict, source: Path, workspace: Path) -> dict:
    return {
        "source_path": str(source),
        "workspace": str(workspace),
        "root": project["root"],
        "controller": project["controller"],
        "counts": project["counts"],
        "coverage": project["coverage"],
        "warnings": project["warnings"],
    }


def _jsonl_datasets(project: dict) -> dict[str, list[dict]]:
    tags = list(project["controller_tags"])
    for program in project["programs"]:
        tags.extend(program.get("tags", []))
    tags.extend(project["aoi_local_tags"])
    return {
        "symbols": _symbol_rows(project),
        "entities": project["entities"],
        "tags": tags,
        "data_types": project["data_types"],
        "aoi_definitions": project["aoi_definitions"],
        "aoi_parameters": project["aoi_parameters"],
        "aoi_local_tags": project["aoi_local_tags"],
        "programs": [{k: v for k, v in program.items() if k not in {"tags", "routines"}} for program in project["programs"]],
        "tasks": project["tasks"],
        "routines": project["routines"],
        "routine_units": project["routine_units"],
        "fbd_nodes": project["fbd_nodes"],
        "fbd_wires": project["fbd_wires"],
        "sfc_nodes": project["sfc_nodes"],
        "sfc_links": project["sfc_links"],
        "modules": project["modules"],
        "module_ports": project["module_ports"],
        "module_connections": project["module_connections"],
        "module_io_tags": project["module_io_tags"],
        "module_io_points": project["module_io_points"],
        "module_config": project["module_config"],
        "comments": project["comments"],
        "tag_comments": project["tag_comments"],
        "tag_data": project["tag_data"],
        "data_values": project["tag_data"],
        "alarms": project["alarms"],
        "messages": project["messages"],
        "produce_consume": project["produce_consume"],
        "xrefs": project["xrefs"],
        "edges": project["edges"],
        "source_nodes": project["source_nodes"],
    }


def _symbol_rows(project: dict) -> list[dict]:
    rows: list[dict] = []
    rows.extend(project["controller_tags"])
    rows.extend(project["data_types"])
    rows.extend(project["aois"])
    rows.extend(project["aoi_parameters"])
    rows.extend(project["aoi_local_tags"])
    rows.extend(project["modules"])
    rows.extend(project["tasks"])
    for program in project["programs"]:
        rows.append({k: v for k, v in program.items() if k not in {"tags", "routines"}})
        rows.extend(program.get("tags", []))
    return rows


def _write_json(path: Path, value: dict | list) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_ai_files(workspace: Path, project: dict) -> None:
    (workspace / "ai" / "overview.md").write_text(_overview_md(project), encoding="utf-8")
    (workspace / "ai" / "coverage.md").write_text(_coverage_md(project["coverage"]), encoding="utf-8")
    (workspace / "ai" / "tags" / "controller.md").write_text(_tags_md("Controller Tags", project["controller_tags"]), encoding="utf-8")
    for program in project["programs"]:
        program_dir = workspace / "ai" / "programs" / _safe_name(program["name"])
        program_dir.mkdir(parents=True, exist_ok=True)
        (program_dir / "program.md").write_text(_program_md(program), encoding="utf-8")
        (program_dir / "tags.md").write_text(_tags_md(f"Program Tags: {program['name']}", program.get("tags", [])), encoding="utf-8")
        routines_dir = program_dir / "routines"
        routines_dir.mkdir(exist_ok=True)
        for routine in program.get("routines", []):
            (routines_dir / f"{_safe_name(routine['name'])}.md").write_text(_routine_md(routine, project), encoding="utf-8")

    for udt in project["data_types"]:
        (workspace / "ai" / "udts" / f"{_safe_name(udt['name'])}.md").write_text(_udt_md(udt), encoding="utf-8")

    for aoi in project["aois"]:
        aoi_dir = workspace / "ai" / "aois" / _safe_name(aoi["name"])
        aoi_dir.mkdir(parents=True, exist_ok=True)
        (aoi_dir / "definition.md").write_text(_aoi_definition_md(aoi), encoding="utf-8")
        (aoi_dir / "parameters.md").write_text(_aoi_parameters_md(aoi), encoding="utf-8")
        (aoi_dir / "local_tags.md").write_text(_tags_md(f"AOI Local Tags: {aoi['name']}", aoi.get("local_tags", [])), encoding="utf-8")
        routines_dir = aoi_dir / "routines"
        routines_dir.mkdir(exist_ok=True)
        for routine in aoi.get("routines", []):
            (routines_dir / f"{_safe_name(routine['name'])}.md").write_text(_routine_md(routine, project), encoding="utf-8")
        (workspace / "ai" / "aois" / f"{_safe_name(aoi['name'])}.md").write_text(_aoi_summary_md(aoi), encoding="utf-8")

    (workspace / "ai" / "modules" / "modules.md").write_text(_modules_md(project["modules"]), encoding="utf-8")
    (workspace / "ai" / "modules" / "tree.md").write_text(_device_tree_md(project["device_tree"]), encoding="utf-8")
    for module in project["modules"]:
        module_dir = workspace / "ai" / "modules" / _safe_name(module["name"])
        module_dir.mkdir(parents=True, exist_ok=True)
        name = module["name"]
        (module_dir / "module.md").write_text(_module_md(module), encoding="utf-8")
        (module_dir / "io_tags.md").write_text(_module_io_tags_md([row for row in project["module_io_tags"] if row.get("module") == name]), encoding="utf-8")
        (module_dir / "io_points.md").write_text(_module_io_points_md([row for row in project["module_io_points"] if row.get("module") == name]), encoding="utf-8")

    (workspace / "ai" / "alarms" / "alarms.md").write_text(_alarms_md(project["alarms"]), encoding="utf-8")


def _overview_md(project: dict) -> str:
    controller = project["controller"]
    counts = project["counts"]
    lines = [
        f"# Logix Project: {controller.get('name')}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Processor | {controller.get('processor_type') or ''} |",
        f"| Revision | {controller.get('major_rev') or ''}.{controller.get('minor_rev') or ''} |",
        f"| Last Modified | {controller.get('last_modified_date') or ''} |",
        f"| Export Date | {project['root'].get('ExportDate') or ''} |",
        "",
        "## Counts",
        "",
    ]
    for key, value in counts.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Programs", ""])
    for program in project["programs"]:
        lines.append(f"- {program['name']}: {len(program.get('tags', []))} tags, {len(program.get('routines', []))} routines")
    lines.extend(["", "## AI Navigation", "", "- Canonical data is in `ir/*.jsonl` and `ir/coverage.json`.", "- Markdown files are derived reading views, not the source of truth."])
    return "\n".join(lines) + "\n"


def _coverage_md(coverage: dict) -> str:
    lines = ["# Extraction Coverage", "", "## Quality Counts", ""]
    for key, value in coverage.get("counts", {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Surfaces", "", "| Surface | Priority | Source | Covered | Missing |", "| --- | --- | ---: | ---: | ---: |"])
    for name, surface in coverage.get("surfaces", {}).items():
        lines.append(
            f"| {name} | {surface.get('priority')} | {surface.get('source_count', 0)} | "
            f"{surface.get('covered_count', 0)} | {surface.get('missing_count', 0)} |"
        )
    return "\n".join(lines) + "\n"


def _tags_md(title: str, tags: list[dict]) -> str:
    lines = [
        f"# {title}",
        "",
        "| Name | Scope | Data Type | Tag Type | Alias For | External Access | Description | Comments | Data |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: | --- |",
    ]
    for tag in tags:
        lines.append(
            f"| {tag.get('name') or ''} | {tag.get('scope') or ''} | {tag.get('data_type') or ''} | {tag.get('tag_type') or ''} | "
            f"{tag.get('alias_for') or ''} | {tag.get('external_access') or ''} | {_one_line(tag.get('description') or '')} | "
            f"{tag.get('comment_count', 0)} | {', '.join(tag.get('data_formats') or [])} |"
        )
    return "\n".join(lines) + "\n"


def _program_md(program: dict) -> str:
    return (
        f"# Program: {program['name']}\n\n"
        f"- Main routine: {program.get('main_routine') or ''}\n"
        f"- Fault routine: {program.get('fault_routine') or ''}\n"
        f"- Tags: {len(program.get('tags', []))}\n"
        f"- Routines: {len(program.get('routines', []))}\n"
        f"- Description: {_one_line(program.get('description') or '')}\n"
    )


def _routine_md(routine: dict, project: dict) -> str:
    rid = routine["id"]
    units = [row for row in project["routine_units"] if row.get("routine_id") == rid]
    fbd_nodes = [row for row in project["fbd_nodes"] if row.get("routine_id") == rid]
    fbd_wires = [row for row in project["fbd_wires"] if row.get("routine_id") == rid]
    sfc_nodes = [row for row in project["sfc_nodes"] if row.get("routine_id") == rid]
    sfc_links = [row for row in project["sfc_links"] if row.get("routine_id") == rid]
    refs = [row for row in project["xrefs"] if row.get("routine") == rid]
    lines = [
        f"# Routine: {routine['name']}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Owner | {routine.get('owner') or ''} |",
        f"| Program | {routine.get('program') or ''} |",
        f"| Language | {routine.get('language') or ''} |",
        f"| Units | {len(units)} |",
        f"| XRefs | {len(refs)} |",
        "",
    ]
    if routine.get("description"):
        lines.extend(["## Description", "", routine["description"], ""])

    language = str(routine.get("language") or "").upper()
    if language == "RLL":
        lines.extend(_rll_units_md(units))
    elif language == "ST":
        lines.extend(_st_units_md(units))
    elif language == "FBD":
        lines.extend(_fbd_units_md(units, fbd_nodes, fbd_wires))
    elif language == "SFC":
        lines.extend(_sfc_units_md(sfc_nodes, sfc_links))
    else:
        lines.extend(["## Body", "", "```text", routine.get("body") or "", "```", ""])

    lines.extend(_xrefs_md(refs[:200], truncated=len(refs) > 200))
    return "\n".join(lines) + "\n"


def _rll_units_md(units: list[dict]) -> list[str]:
    lines = ["## RLL Rungs", ""]
    for unit in units:
        lines.extend([f"### Rung {unit.get('number')}", ""])
        if unit.get("comment"):
            lines.extend(["Comment:", "", "```text", unit["comment"], "```", ""])
        lines.extend(["Logic:", "", "```ladder", unit.get("text") or "", "```", ""])
        if unit.get("instructions"):
            ops = ", ".join(str(item.get("instruction")) for item in unit["instructions"])
            lines.extend([f"Instructions: {ops}", ""])
    return lines


def _st_units_md(units: list[dict]) -> list[str]:
    lines = ["## Structured Text", "", "```st"]
    for unit in units:
        lines.append(f"{unit.get('number')}: {unit.get('text') or ''}")
    lines.extend(["```", ""])
    return lines


def _fbd_units_md(units: list[dict], nodes: list[dict], wires: list[dict]) -> list[str]:
    lines = ["## Function Block Diagram", ""]
    for unit in units:
        sheet = unit.get("number")
        sheet_nodes = [node for node in nodes if node.get("sheet_id") == unit.get("id")]
        sheet_wires = [wire for wire in wires if wire.get("sheet_id") == unit.get("id")]
        lines.extend([f"### Sheet {sheet}", "", f"- Nodes: {len(sheet_nodes)}", f"- Wires: {len(sheet_wires)}", ""])
        if sheet_nodes:
            lines.extend(["| ID | Type | Instruction | Operand | Pins/Args |", "| --- | --- | --- | --- | --- |"])
            for node in sheet_nodes:
                pins = node.get("visible_pins") or _node_args(node)
                lines.append(
                    f"| {node.get('node_id') or ''} | {node.get('node_type') or ''} | {node.get('instruction') or ''} | "
                    f"{node.get('operand') or ''} | {_one_line(pins)} |"
                )
            lines.append("")
        if sheet_wires:
            lines.extend(["| From | From Param | To | To Param |", "| --- | --- | --- | --- |"])
            for wire in sheet_wires:
                lines.append(f"| {wire.get('from_id') or ''} | {wire.get('from_param') or ''} | {wire.get('to_id') or ''} | {wire.get('to_param') or ''} |")
            lines.append("")
    return lines


def _sfc_units_md(nodes: list[dict], links: list[dict]) -> list[str]:
    lines = ["## Sequential Function Chart", "", f"- Nodes: {len(nodes)}", f"- Directed links: {len(links)}", ""]
    if nodes:
        lines.extend(["| ID | Type | Operand | Initial/Qualifier |", "| --- | --- | --- | --- |"])
        for node in nodes:
            lines.append(
                f"| {node.get('node_id') or ''} | {node.get('node_type') or ''} | {node.get('operand') or ''} | "
                f"{node.get('initial_step') or node.get('qualifier') or ''} |"
            )
        lines.append("")
    for node in nodes:
        if node.get("condition_body") or node.get("st_body"):
            lines.extend([f"### {node.get('node_type')} {node.get('node_id')}: {node.get('operand') or ''}", "", "```st", node.get("condition_body") or node.get("st_body") or "", "```", ""])
    return lines


def _xrefs_md(refs: list[dict], *, truncated: bool) -> list[str]:
    lines = ["## References", ""]
    if not refs:
        lines.extend(["No references extracted.", ""])
        return lines
    lines.extend(["| Symbol | Access | Instruction | Location |", "| --- | --- | --- | --- |"])
    for ref in refs:
        lines.append(f"| {ref.get('symbol') or ''} | {ref.get('access') or ''} | {ref.get('instruction') or ''} | {ref.get('location') or ''} |")
    if truncated:
        lines.append("| ... | ... | ... | Full list in `ir/xrefs.jsonl` |")
    lines.append("")
    return lines


def _udt_md(udt: dict) -> str:
    lines = [f"# UDT: {udt['name']}", ""]
    if udt.get("description"):
        lines.extend([udt["description"], ""])
    lines.extend(["| Member | Data Type | Dimension | External Access | Description |", "| --- | --- | --- | --- | --- |"])
    for member in udt["members"]:
        lines.append(
            f"| {member.get('name') or ''} | {member.get('data_type') or ''} | {member.get('dimension') or ''} | "
            f"{member.get('external_access') or ''} | {_one_line(member.get('description') or '')} |"
        )
    return "\n".join(lines) + "\n"


def _aoi_summary_md(aoi: dict) -> str:
    return (
        f"# AOI: {aoi['name']}\n\n"
        f"- Parameters: {len(aoi.get('parameters', []))}\n"
        f"- Local tags: {len(aoi.get('local_tags', []))}\n"
        f"- Routines: {len(aoi.get('routines', []))}\n\n"
        f"Detailed AI views are in `ai/aois/{_safe_name(aoi['name'])}/`.\n"
    )


def _aoi_definition_md(aoi: dict) -> str:
    lines = [f"# AOI Definition: {aoi['name']}", ""]
    for key in ["revision", "vendor", "description"]:
        lines.append(f"- {key}: {_one_line(aoi.get(key) or '')}")
    lines.extend(["", "## Routines", ""])
    for routine in aoi.get("routines", []):
        lines.append(f"- {routine['name']} ({routine.get('language') or ''})")
    return "\n".join(lines) + "\n"


def _aoi_parameters_md(aoi: dict) -> str:
    lines = [f"# AOI Parameters: {aoi['name']}", "", "| Name | Usage | Data Type | Required | Description | Comments | Data |", "| --- | --- | --- | --- | --- | ---: | --- |"]
    for param in aoi.get("parameters", []):
        lines.append(
            f"| {param.get('name') or ''} | {param.get('usage') or ''} | {param.get('data_type') or ''} | {param.get('required') or ''} | "
            f"{_one_line(param.get('description') or '')} | {len(param.get('comments') or [])} | {', '.join(param.get('data_formats') or [])} |"
        )
    return "\n".join(lines) + "\n"


def _modules_md(modules: list[dict]) -> str:
    lines = ["# Modules", "", "| Name | Catalog | Parent | Slot | Address | Inhibited | Fault |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for module in modules:
        lines.append(
            f"| {module.get('name') or ''} | {module.get('catalog_number') or ''} | {module.get('parent_module') or ''} | "
            f"{module.get('slot') or ''} | {module.get('network_address') or module.get('address') or ''} | {module.get('inhibited') or ''} | {module.get('major_fault') or ''} |"
        )
    return "\n".join(lines) + "\n"


def _device_tree_md(tree: dict) -> str:
    lines = ["# Module Tree", ""]
    for root in tree.get("roots", []):
        _tree_lines(root, lines, 0)
    return "\n".join(lines) + "\n"


def _tree_lines(node: dict, lines: list[str], depth: int) -> None:
    indent = "  " * depth
    lines.append(f"{indent}- {node.get('name')} ({node.get('catalog_number') or ''})")
    for child in node.get("children", []):
        _tree_lines(child, lines, depth + 1)


def _module_md(module: dict) -> str:
    lines = [f"# Module: {module.get('name')}", "", "| Field | Value |", "| --- | --- |"]
    for key in ["catalog_number", "vendor", "product_type", "product_code", "major", "minor", "parent_module", "slot", "address", "network_address", "inhibited", "major_fault", "description"]:
        lines.append(f"| {key} | {_one_line(module.get(key) or '')} |")
    return "\n".join(lines) + "\n"


def _module_io_tags_md(rows: list[dict]) -> str:
    lines = ["# Module I/O Tags", "", "| Role | Direction | Data Type | Comments | Description |", "| --- | --- | --- | ---: | --- |"]
    for row in rows:
        lines.append(f"| {row.get('role') or ''} | {row.get('direction') or ''} | {row.get('data_type') or ''} | {row.get('comment_count', 0)} | {_one_line(row.get('description') or '')} |")
    return "\n".join(lines) + "\n"


def _module_io_points_md(rows: list[dict]) -> str:
    lines = ["# Module I/O Point Comments", "", "| Direction | Role | Operand | Point | Description | Tag Description |", "| --- | --- | --- | ---: | --- | --- |"]
    for row in rows:
        lines.append(
            f"| {row.get('direction') or ''} | {row.get('role') or ''} | {row.get('operand') or ''} | {row.get('point') if row.get('point') is not None else ''} | "
            f"{_one_line(row.get('description') or '')} | {_one_line(row.get('tag_description') or '')} |"
        )
    return "\n".join(lines) + "\n"


def _alarms_md(alarms: list[dict]) -> str:
    lines = ["# Alarms", "", "| Tag | Type | Class | Severity | Messages |", "| --- | --- | --- | --- | ---: |"]
    for alarm in alarms:
        lines.append(
            f"| {alarm.get('tag_name') or ''} | {alarm.get('alarm_type') or ''} | {alarm.get('alarm_class') or ''} | "
            f"{alarm.get('severity') or ''} | {len(alarm.get('messages') or [])} |"
        )
    return "\n".join(lines) + "\n"


def _write_sqlite(path: Path, project: dict) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE symbols (id TEXT PRIMARY KEY, kind TEXT, name TEXT, scope TEXT, data_type TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE routines (id TEXT PRIMARY KEY, program TEXT, owner TEXT, name TEXT, language TEXT, body TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE routine_units (id TEXT PRIMARY KEY, routine_id TEXT, kind TEXT, program TEXT, text TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE xrefs (symbol TEXT, base_symbol TEXT, routine TEXT, access TEXT, instruction TEXT, source TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE entities (id TEXT, kind TEXT, name TEXT, scope TEXT, data_type TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE comments (id TEXT, kind TEXT, target TEXT, text TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE data_values (id TEXT, owner_name TEXT, element TEXT, format TEXT, raw_text TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE alarms (id TEXT, tag_name TEXT, alarm_type TEXT, severity TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE messages (tag_name TEXT, message_type TEXT, lang TEXT, text TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE modules (id TEXT PRIMARY KEY, name TEXT, catalog_number TEXT, parent_module TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE module_ports (id TEXT, module TEXT, type TEXT, address TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE module_connections (module TEXT, kind TEXT, name TEXT, type TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE module_io_tags (id TEXT, module TEXT, role TEXT, direction TEXT, data_type TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE module_io_points (module TEXT, role TEXT, direction TEXT, operand TEXT, point INTEGER, description TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE edges (kind TEXT, source TEXT, target TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE coverage_checks (name TEXT PRIMARY KEY, priority TEXT, source_count INTEGER, covered_count INTEGER, missing_count INTEGER, json TEXT NOT NULL)")
        conn.execute("CREATE TABLE ir_rows (dataset TEXT, id TEXT, kind TEXT, name TEXT, json TEXT NOT NULL)")
        conn.execute("CREATE VIRTUAL TABLE search_index USING fts5(kind, name, scope, text)")

        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("project", json.dumps(_project_summary(project, Path(project["source_path"]), path.parent.parent), ensure_ascii=False)))
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("coverage", json.dumps(project["coverage"], ensure_ascii=False)))
        _insert_sqlite_rows(conn, project)
        _create_sqlite_indexes(conn)
        conn.commit()
    finally:
        conn.close()


def _create_sqlite_indexes(conn: sqlite3.Connection) -> None:
    """Create indexes after bulk insert so the MCP query layer stays fast."""

    for statement in [
        "CREATE INDEX idx_xrefs_symbol ON xrefs(symbol)",
        "CREATE INDEX idx_xrefs_base ON xrefs(base_symbol)",
        "CREATE INDEX idx_xrefs_routine ON xrefs(routine)",
        "CREATE INDEX idx_edges_source ON edges(source)",
        "CREATE INDEX idx_edges_target ON edges(target)",
        "CREATE INDEX idx_edges_kind ON edges(kind)",
        "CREATE INDEX idx_routine_units_routine ON routine_units(routine_id)",
        "CREATE INDEX idx_routines_program ON routines(program)",
        "CREATE INDEX idx_symbols_name ON symbols(name)",
        "CREATE INDEX idx_symbols_kind ON symbols(kind)",
        "CREATE INDEX idx_entities_id ON entities(id)",
        "CREATE INDEX idx_entities_kind ON entities(kind)",
        "CREATE INDEX idx_comments_target ON comments(target)",
        "CREATE INDEX idx_data_values_owner ON data_values(owner_name)",
        "CREATE INDEX idx_alarms_tag ON alarms(tag_name)",
        "CREATE INDEX idx_modules_parent ON modules(parent_module)",
        "CREATE INDEX idx_module_io_points_module ON module_io_points(module)",
        "CREATE INDEX idx_ir_rows_dataset ON ir_rows(dataset)",
        "CREATE INDEX idx_ir_rows_id ON ir_rows(id)",
    ]:
        conn.execute(statement)


def _insert_sqlite_rows(conn: sqlite3.Connection, project: dict) -> None:
    for row in _symbol_rows(project):
        conn.execute(
            "INSERT OR REPLACE INTO symbols(id, kind, name, scope, data_type, json) VALUES (?, ?, ?, ?, ?, ?)",
            (row.get("id"), row.get("kind"), row.get("name"), row.get("scope"), row.get("data_type"), _json(row)),
        )
        _insert_search(conn, row.get("kind"), row.get("name"), row.get("scope"), _search_text(row))
    for routine in project["routines"]:
        conn.execute(
            "INSERT OR REPLACE INTO routines(id, program, owner, name, language, body, json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (routine.get("id"), routine.get("program"), routine.get("owner"), routine.get("name"), routine.get("language"), routine.get("body"), _json(routine)),
        )
        _insert_search(conn, "routine", routine.get("name"), routine.get("program") or routine.get("owner"), routine.get("body") or "")
    for unit in project["routine_units"]:
        conn.execute(
            "INSERT OR REPLACE INTO routine_units(id, routine_id, kind, program, text, json) VALUES (?, ?, ?, ?, ?, ?)",
            (unit.get("id"), unit.get("routine_id"), unit.get("kind"), unit.get("program"), unit.get("text") or unit.get("comment"), _json(unit)),
        )
        _insert_search(conn, unit.get("kind"), unit.get("routine"), unit.get("program") or unit.get("owner"), _search_text(unit))
    for ref in project["xrefs"]:
        conn.execute(
            "INSERT INTO xrefs(symbol, base_symbol, routine, access, instruction, source, json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ref.get("symbol"), ref.get("base_symbol"), ref.get("routine"), ref.get("access"), ref.get("instruction"), ref.get("source"), _json(ref)),
        )
    for entity in project["entities"]:
        conn.execute(
            "INSERT INTO entities(id, kind, name, scope, data_type, json) VALUES (?, ?, ?, ?, ?, ?)",
            (_sql_scalar(entity.get("id")), _sql_scalar(entity.get("kind")), _sql_scalar(entity.get("name")), _sql_scalar(entity.get("scope")), _sql_scalar(entity.get("data_type")), _json(entity)),
        )
        _insert_search(conn, entity.get("kind"), entity.get("name"), entity.get("scope"), _search_text(entity))
    for comment in project["comments"]:
        conn.execute(
            "INSERT INTO comments(id, kind, target, text, json) VALUES (?, ?, ?, ?, ?)",
            (comment.get("id"), comment.get("kind"), comment.get("target"), comment.get("text"), _json(comment)),
        )
        _insert_search(conn, comment.get("kind"), comment.get("target"), None, comment.get("text") or "")
    for index, data in enumerate(project["tag_data"]):
        owner = data.get("owner") or {}
        conn.execute(
            "INSERT INTO data_values(id, owner_name, element, format, raw_text, json) VALUES (?, ?, ?, ?, ?, ?)",
            (f"Data:{index:06d}", owner.get("name"), data.get("element"), data.get("format"), data.get("raw_text"), _json(data)),
        )
    for alarm in project["alarms"]:
        conn.execute(
            "INSERT INTO alarms(id, tag_name, alarm_type, severity, json) VALUES (?, ?, ?, ?, ?)",
            (alarm.get("id"), alarm.get("tag_name"), alarm.get("alarm_type"), alarm.get("severity"), _json(alarm)),
        )
        _insert_search(conn, "alarm", alarm.get("tag_name"), alarm.get("alarm_type"), _search_text(alarm))
    for message in project["messages"]:
        conn.execute(
            "INSERT INTO messages(tag_name, message_type, lang, text, json) VALUES (?, ?, ?, ?, ?)",
            (message.get("tag_name"), message.get("message_type"), message.get("lang"), message.get("text"), _json(message)),
        )
    for module in project["modules"]:
        conn.execute(
            "INSERT OR REPLACE INTO modules(id, name, catalog_number, parent_module, json) VALUES (?, ?, ?, ?, ?)",
            (module.get("id"), module.get("name"), module.get("catalog_number"), module.get("parent_module"), _json(module)),
        )
        _insert_search(conn, "module", module.get("name"), module.get("parent_module"), _search_text(module))
    for row in project["module_ports"]:
        conn.execute("INSERT INTO module_ports(id, module, type, address, json) VALUES (?, ?, ?, ?, ?)", (row.get("id"), row.get("module"), row.get("type"), row.get("address"), _json(row)))
    for row in project["module_connections"]:
        conn.execute("INSERT INTO module_connections(module, kind, name, type, json) VALUES (?, ?, ?, ?, ?)", (row.get("module"), row.get("kind"), row.get("name"), row.get("type"), _json(row)))
    for row in project["module_io_tags"]:
        conn.execute("INSERT INTO module_io_tags(id, module, role, direction, data_type, json) VALUES (?, ?, ?, ?, ?, ?)", (row.get("id"), row.get("module"), row.get("role"), row.get("direction"), row.get("data_type"), _json(row)))
    for row in project["module_io_points"]:
        conn.execute(
            "INSERT INTO module_io_points(module, role, direction, operand, point, description, json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row.get("module"), row.get("role"), row.get("direction"), row.get("operand"), row.get("point"), row.get("description"), _json(row)),
        )
        _insert_search(conn, "module_io_point", row.get("operand"), row.get("module"), _search_text(row))
    for edge in project["edges"]:
        conn.execute("INSERT INTO edges(kind, source, target, json) VALUES (?, ?, ?, ?)", (edge.get("kind"), edge.get("from"), edge.get("to"), _json(edge)))
    for name, surface in project["coverage"]["surfaces"].items():
        conn.execute(
            "INSERT INTO coverage_checks(name, priority, source_count, covered_count, missing_count, json) VALUES (?, ?, ?, ?, ?, ?)",
            (name, surface.get("priority"), surface.get("source_count"), surface.get("covered_count"), surface.get("missing_count"), _json(surface)),
        )
    for dataset, rows in _jsonl_datasets(project).items():
        for row in rows:
            conn.execute(
                "INSERT INTO ir_rows(dataset, id, kind, name, json) VALUES (?, ?, ?, ?, ?)",
                (dataset, row.get("id"), row.get("kind"), row.get("name") or row.get("tag_name") or row.get("routine"), _json(row)),
            )


def _insert_search(conn: sqlite3.Connection, kind: object, name: object, scope: object, text: str) -> None:
    conn.execute("INSERT INTO search_index(kind, name, scope, text) VALUES (?, ?, ?, ?)", (_sql_scalar(kind), _sql_scalar(name), _sql_scalar(scope), text))


def _sql_scalar(value: object) -> object:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _json(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False)


def _search_text(row: dict) -> str:
    values = []
    for key in ["name", "id", "description", "text", "data_type", "alias_for", "catalog_number", "operand", "tag_name", "raw_text"]:
        if row.get(key):
            values.append(str(row[key]))
    if row.get("comments"):
        values.append(json.dumps(row["comments"], ensure_ascii=False))
    if row.get("messages"):
        values.append(json.dumps(row["messages"], ensure_ascii=False))
    return " ".join(values)


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _snippet(text: str, pattern: str, radius: int = 180) -> str:
    match = re.search(re.escape(pattern), text, flags=re.IGNORECASE)
    if not match:
        return text[: radius * 2]
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return text[start:end].strip()


def _safe_name(name: str | None) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "unnamed")
    safe = safe.strip(" .")
    return safe[:120] or "unnamed"


def _one_line(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _node_args(node: dict) -> str:
    values = []
    for param in node.get("parameters") or []:
        values.append(f"{param.get('name')}={param.get('argument')}")
    for array in node.get("arrays") or []:
        values.append(f"{array.get('name')}={array.get('operand')}")
    return ", ".join(values)
