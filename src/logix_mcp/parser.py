"""Read-only parser for Studio 5000 Logix Designer L5X exports."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from .extractors import (
    extract_alarm_message_records,
    extract_data_nodes,
    extract_descriptions_comments,
    extract_produce_consume_info,
    extract_tag_comment_records,
    local_name,
    xml_attributes,
    xml_child,
    xml_children,
    xml_text,
)
from .hardware import extract_hardware_ir
from .routines import extract_routine_ir
from .xrefs import aoi_signature_from_parameters


JsonDict = dict[str, Any]


class L5xParseError(RuntimeError):
    """Raised when an L5X file cannot be parsed as expected."""


def parse_l5x(path: str | Path) -> dict:
    source_path = Path(path)
    if not source_path.exists():
        raise L5xParseError(f"L5X file not found: {source_path}")
    try:
        root = ET.parse(source_path).getroot()
    except ET.ParseError as exc:
        raise L5xParseError(f"Invalid L5X XML: {exc}") from exc

    if local_name(root.tag) != "RSLogix5000Content":
        raise L5xParseError("Expected RSLogix5000Content root element")

    controller = xml_child(root, "Controller")
    if controller is None:
        raise L5xParseError("L5X file does not contain a Controller element")

    collector = _collector()
    aoi_signatures = _build_aoi_signatures(controller)
    hardware = extract_hardware_ir(root)
    module_ports = _module_ports(hardware["modules"])
    module_io_tags = _module_io_tags(hardware["modules"])
    tag_data = extract_data_nodes(root, decorated_max_depth=8)
    alarm_messages = extract_alarm_message_records(root)
    comments = _comment_records(root)
    tag_comments = extract_tag_comment_records(root)
    produce_consume = extract_produce_consume_info(root)
    coverage_counts = _source_quality_counts(root)

    project = {
        "source_path": str(source_path),
        "root": xml_attributes(root),
        "controller": _controller(controller),
        "data_types": [_data_type(elem) for elem in xml_children(xml_child(controller, "DataTypes"), "DataType")],
        "aois": [_aoi(elem, collector, aoi_signatures) for elem in xml_children(xml_child(controller, "AddOnInstructionDefinitions"), "AddOnInstructionDefinition")],
        "controller_tags": [_tag(elem, "Controller", "Controller") for elem in xml_children(xml_child(controller, "Tags"), "Tag")],
        "programs": [_program(elem, collector, aoi_signatures) for elem in xml_children(xml_child(controller, "Programs"), "Program")],
        "modules": hardware["modules"],
        "module_ports": module_ports,
        "module_connections": hardware["module_connections"],
        "module_io_tags": module_io_tags,
        "module_io_points": hardware["io_points"],
        "module_config": [row for row in module_io_tags if row.get("role") == "Config"],
        "device_tree": hardware["device_tree"],
        "tasks": [_task(elem) for elem in xml_children(xml_child(controller, "Tasks"), "Task")],
        "routines": collector["routines"],
        "routine_units": collector["routine_units"],
        "fbd_nodes": collector["fbd_nodes"],
        "fbd_wires": collector["fbd_wires"],
        "sfc_nodes": collector["sfc_nodes"],
        "sfc_links": collector["sfc_links"],
        "xrefs": collector["xrefs"],
        "comments": comments,
        "tag_comments": tag_comments,
        "tag_data": tag_data,
        "alarms": _alarm_rows(alarm_messages),
        "messages": alarm_messages,
        "produce_consume": produce_consume,
        "warnings": [],
    }
    project["aoi_definitions"] = [_aoi_definition(aoi) for aoi in project["aois"]]
    project["aoi_parameters"] = _aoi_parameters(project["aois"])
    project["aoi_local_tags"] = _aoi_local_tags(project["aois"])
    project["entities"] = _entities(project)
    project["edges"] = _edges(project)
    project["source_nodes"] = _source_nodes(root, coverage_counts)
    project["coverage"] = _coverage(root, project, coverage_counts)
    project["counts"] = _counts(project, coverage_counts)
    return project


def _collector() -> dict[str, list[JsonDict]]:
    return {
        "routines": [],
        "routine_units": [],
        "fbd_nodes": [],
        "fbd_wires": [],
        "sfc_nodes": [],
        "sfc_links": [],
        "xrefs": [],
    }


def _controller(elem: ET.Element) -> dict:
    return {
        "name": elem.attrib.get("Name"),
        "processor_type": elem.attrib.get("ProcessorType"),
        "major_rev": elem.attrib.get("MajorRev"),
        "minor_rev": elem.attrib.get("MinorRev"),
        "project_creation_date": elem.attrib.get("ProjectCreationDate"),
        "last_modified_date": elem.attrib.get("LastModifiedDate"),
        "comm_path": elem.attrib.get("CommPath"),
        "project_sn": elem.attrib.get("ProjectSN"),
        "attributes": xml_attributes(elem),
    }


def _data_type(elem: ET.Element) -> dict:
    name = elem.attrib.get("Name", "")
    return _compact(
        {
            "kind": "udt",
            "id": f"UDT:{name}",
            "name": name,
            "family": elem.attrib.get("Family"),
            "class": elem.attrib.get("Class"),
            "description": _description(elem),
            "comments": extract_descriptions_comments(elem),
            "members": [_member(member) for member in xml_children(xml_child(elem, "Members"), "Member")],
            "attributes": xml_attributes(elem),
        }
    )


def _member(elem: ET.Element) -> dict:
    return _compact(
        {
            "name": elem.attrib.get("Name"),
            "data_type": elem.attrib.get("DataType"),
            "dimension": elem.attrib.get("Dimension"),
            "radix": elem.attrib.get("Radix"),
            "hidden": elem.attrib.get("Hidden"),
            "external_access": elem.attrib.get("ExternalAccess"),
            "target": elem.attrib.get("Target"),
            "bit_number": elem.attrib.get("BitNumber"),
            "description": _description(elem),
            "comments": extract_descriptions_comments(elem),
            "attributes": xml_attributes(elem),
        }
    )


def _build_aoi_signatures(controller: ET.Element) -> dict[str, list[str]]:
    """Map UPPERCASE AOI name to the operand roles of its call signature."""

    signatures: dict[str, list[str]] = {}
    definitions = xml_child(controller, "AddOnInstructionDefinitions")
    for aoi in xml_children(definitions, "AddOnInstructionDefinition"):
        name = aoi.attrib.get("Name", "")
        if not name:
            continue
        parameters = [
            {"name": param.attrib.get("Name"), "usage": param.attrib.get("Usage")}
            for param in xml_children(xml_child(aoi, "Parameters"), "Parameter")
        ]
        signatures[name.upper()] = aoi_signature_from_parameters(parameters)
    return signatures


def _aoi(elem: ET.Element, collector: dict[str, list[JsonDict]], aoi_signatures: dict[str, list[str]] | None = None) -> dict:
    name = elem.attrib.get("Name", "")
    routines = [_routine(routine, program=None, owner=f"AOI:{name}", collector=collector, aoi_signatures=aoi_signatures) for routine in xml_children(xml_child(elem, "Routines"), "Routine")]
    return _compact(
        {
            "kind": "aoi",
            "id": f"AOI:{name}",
            "name": name,
            "revision": elem.attrib.get("Revision"),
            "vendor": elem.attrib.get("Vendor"),
            "description": _description(elem),
            "comments": extract_descriptions_comments(elem),
            "parameters": [_parameter(param, name) for param in xml_children(xml_child(elem, "Parameters"), "Parameter")],
            "local_tags": [_tag(tag, "AOI", name) for tag in xml_children(xml_child(elem, "LocalTags"), "LocalTag")],
            "routines": routines,
            "attributes": xml_attributes(elem),
        }
    )


def _parameter(elem: ET.Element, aoi_name: str) -> dict:
    name = elem.attrib.get("Name")
    return _compact(
        {
            "kind": "aoi_parameter",
            "id": f"AOI:{aoi_name}.Parameter:{name}",
            "aoi": aoi_name,
            "name": name,
            "tag_type": elem.attrib.get("TagType"),
            "data_type": elem.attrib.get("DataType"),
            "usage": elem.attrib.get("Usage"),
            "required": elem.attrib.get("Required"),
            "visible": elem.attrib.get("Visible"),
            "external_access": elem.attrib.get("ExternalAccess"),
            "description": _description(elem),
            "comments": extract_descriptions_comments(elem),
            "data_formats": _data_formats(elem),
            "attributes": xml_attributes(elem),
        }
    )


def _tag(elem: ET.Element, scope_type: str, scope_name: str) -> dict:
    name = elem.attrib.get("Name", "")
    tag_type = local_name(elem.tag)
    tag_id = f"{scope_type}:{scope_name}.{name}" if scope_type != "Controller" else f"Controller:{name}"
    return _compact(
        {
            "kind": "tag",
            "id": tag_id,
            "name": name,
            "scope_type": scope_type,
            "scope": scope_name,
            "tag_type": elem.attrib.get("TagType", tag_type),
            "data_type": elem.attrib.get("DataType"),
            "alias_for": elem.attrib.get("AliasFor"),
            "radix": elem.attrib.get("Radix"),
            "dimensions": elem.attrib.get("Dimensions"),
            "external_access": elem.attrib.get("ExternalAccess"),
            "constant": elem.attrib.get("Constant"),
            "description": _description(elem),
            "comments": extract_descriptions_comments(elem),
            "comment_count": len(xml_children(xml_child(elem, "Comments"), "Comment")),
            "data_formats": _data_formats(elem),
            "attributes": xml_attributes(elem),
        }
    )


def _program(elem: ET.Element, collector: dict[str, list[JsonDict]], aoi_signatures: dict[str, list[str]] | None = None) -> dict:
    name = elem.attrib.get("Name", "")
    routines = [_routine(routine, program=name, owner=f"Program:{name}", collector=collector, aoi_signatures=aoi_signatures) for routine in xml_children(xml_child(elem, "Routines"), "Routine")]
    return _compact(
        {
            "kind": "program",
            "id": f"Program:{name}",
            "name": name,
            "main_routine": elem.attrib.get("MainRoutineName"),
            "fault_routine": elem.attrib.get("FaultRoutineName"),
            "description": _description(elem),
            "comments": extract_descriptions_comments(elem),
            "tags": [_tag(tag, "Program", name) for tag in xml_children(xml_child(elem, "Tags"), "Tag")],
            "routines": routines,
            "attributes": xml_attributes(elem),
        }
    )


def _routine(elem: ET.Element, program: str | None, owner: str, collector: dict[str, list[JsonDict]], aoi_signatures: dict[str, list[str]] | None = None) -> dict:
    parsed = extract_routine_ir(elem, owner=owner, program=program, aoi_signatures=aoi_signatures)
    for key in ["routine_units", "fbd_nodes", "fbd_wires", "sfc_nodes", "sfc_links", "xrefs"]:
        collector[key].extend(parsed.get(key, []))
    collector["routines"].append(parsed["routine"])
    return parsed["routine"]


def _task(elem: ET.Element) -> dict:
    scheduled = xml_child(elem, "ScheduledPrograms")
    programs = [program.attrib.get("Name") for program in xml_children(scheduled, "ScheduledProgram")]
    return _compact(
        {
            "kind": "task",
            "id": f"Task:{elem.attrib.get('Name', '')}",
            "name": elem.attrib.get("Name"),
            "task_type": elem.attrib.get("Type"),
            "rate": elem.attrib.get("Rate"),
            "priority": elem.attrib.get("Priority"),
            "watchdog": elem.attrib.get("Watchdog"),
            "scheduled_programs": [program for program in programs if program],
            "attributes": xml_attributes(elem),
        }
    )


def _module_ports(modules: list[JsonDict]) -> list[JsonDict]:
    rows = []
    for module in modules:
        for port in module.get("ports", []):
            row = dict(port)
            row["kind"] = "module_port"
            row["id"] = f"{module.get('id')}.Port:{row.get('id') or len(rows)}"
            row["module"] = module.get("name")
            row["module_id"] = module.get("id")
            rows.append(_compact(row))
    return rows


def _module_io_tags(modules: list[JsonDict]) -> list[JsonDict]:
    rows = []
    for module in modules:
        for index, tag in enumerate(module.get("io_tags", [])):
            row = dict(tag)
            row["kind"] = "module_io_tag"
            row["id"] = f"{module.get('id')}.IOTag:{row.get('role') or row.get('tag') or index}:{index}"
            rows.append(_compact(row))
    return rows


def _aoi_definition(aoi: JsonDict) -> JsonDict:
    return {key: value for key, value in aoi.items() if key not in {"parameters", "local_tags", "routines"}}


def _aoi_parameters(aois: list[JsonDict]) -> list[JsonDict]:
    return [param for aoi in aois for param in aoi.get("parameters", [])]


def _aoi_local_tags(aois: list[JsonDict]) -> list[JsonDict]:
    return [tag for aoi in aois for tag in aoi.get("local_tags", [])]


def _comment_records(root: ET.Element) -> list[JsonDict]:
    records = []
    for elem, path, ancestors in _walk_with_paths(root):
        elem_name = local_name(elem.tag)
        if elem_name not in {"Comment", "Description"}:
            continue
        text = xml_text(elem)
        if not text:
            continue
        owner = _nearest_owner(ancestors)
        operand = elem.attrib.get("Operand")
        records.append(
            _compact(
                {
                    "kind": "comment" if elem_name == "Comment" else "description",
                    "id": f"{elem_name}:{len(records):06d}",
                    "owner": _owner_ref(owner) if owner is not None else None,
                    "operand": operand,
                    "target": _qualified_operand(owner.attrib.get("Name") if owner is not None else None, operand),
                    "text": text,
                    "path": path,
                    "attributes": xml_attributes(elem),
                }
            )
        )
    return records


def _alarm_rows(messages: list[JsonDict]) -> list[JsonDict]:
    rows: dict[tuple[str | None, str | None], JsonDict] = {}
    for message in messages:
        key = (message.get("tag_name"), message.get("alarm_type"))
        if key not in rows:
            row = _compact(
                {
                    "kind": "alarm",
                    "id": f"Alarm:{message.get('tag_name') or len(rows)}:{message.get('alarm_type') or 'Unknown'}",
                    "tag_name": message.get("tag_name"),
                    "tag_type": message.get("tag_type"),
                    "data_type": message.get("data_type"),
                    "alarm_type": message.get("alarm_type"),
                    "alarm_class": message.get("alarm_class"),
                    "severity": message.get("severity"),
                    "assoc_tags": message.get("assoc_tags"),
                    "parameters": message.get("parameters"),
                }
            )
            row["messages"] = []
            rows[key] = row
        row = rows[key]
        row["messages"].append(
            _compact(
                {
                    "message_type": message.get("message_type"),
                    "lang": message.get("lang"),
                    "text": message.get("text"),
                    "message_attributes": message.get("message_attributes"),
                }
            )
        )
    return list(rows.values())


def _entities(project: JsonDict) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for row in _symbol_rows(project):
        rows.append(_entity(row))
    for collection in [
        "routines",
        "routine_units",
        "fbd_nodes",
        "fbd_wires",
        "sfc_nodes",
        "sfc_links",
        "module_ports",
        "module_connections",
        "module_io_tags",
        "module_io_points",
        "alarms",
        "messages",
    ]:
        for row in project.get(collection, []):
            rows.append(_entity(row, fallback_kind=collection.removesuffix("s")))
    return rows


def _entity(row: JsonDict, fallback_kind: str | None = None) -> JsonDict:
    kind = row.get("kind") or fallback_kind or "entity"
    entity_id = row.get("id") or row.get("path") or f"{kind}:{row.get('name') or row.get('tag_name') or row.get('routine') or len(str(row))}"
    return _compact(
        {
            "kind": kind,
            "id": entity_id,
            "name": row.get("name") or row.get("tag_name") or row.get("operand") or row.get("routine"),
            "scope": row.get("scope") or row.get("program") or row.get("owner") or row.get("module"),
            "data_type": row.get("data_type"),
            "description": row.get("description") or row.get("text"),
            "source_id": row.get("id"),
        }
    )


def _edges(project: JsonDict) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for task in project["tasks"]:
        for program in task.get("scheduled_programs", []):
            rows.append({"kind": "scheduled_program", "from": task["id"], "to": f"Program:{program}"})
    for program in project["programs"]:
        for routine in program.get("routines", []):
            rows.append({"kind": "contains", "from": program["id"], "to": routine["id"]})
        for tag in program.get("tags", []):
            rows.append({"kind": "contains", "from": program["id"], "to": tag["id"]})
    for aoi in project["aois"]:
        for routine in aoi.get("routines", []):
            rows.append({"kind": "contains", "from": aoi["id"], "to": routine["id"]})
        for param in aoi.get("parameters", []):
            rows.append({"kind": "contains", "from": aoi["id"], "to": param["id"]})
        for tag in aoi.get("local_tags", []):
            rows.append({"kind": "contains", "from": aoi["id"], "to": tag["id"]})
    for ref in project["xrefs"]:
        rows.append(
            _compact(
                {
                    "kind": "xref",
                    "from": ref.get("routine"),
                    "to": ref.get("symbol"),
                    "access": ref.get("access"),
                    "instruction": ref.get("instruction"),
                    "source": ref.get("source"),
                }
            )
        )
    for wire in project["fbd_wires"]:
        rows.append(
            _compact(
                {
                    "kind": "fbd_wire",
                    "from": f"{wire.get('sheet_id')}.Node:{wire.get('from_id')}",
                    "to": f"{wire.get('sheet_id')}.Node:{wire.get('to_id')}",
                    "source": wire.get("id"),
                    "from_param": wire.get("from_param"),
                    "to_param": wire.get("to_param"),
                }
            )
        )
    for link in project["sfc_links"]:
        rows.append(
            _compact(
                {
                    "kind": "sfc_link",
                    "from": f"{link.get('unit_id')}.Node:{link.get('from_id')}",
                    "to": f"{link.get('unit_id')}.Node:{link.get('to_id')}",
                    "source": link.get("id"),
                }
            )
        )
    return [_compact(row) for row in rows]


def _source_nodes(root: ET.Element, counts: dict[str, int]) -> list[JsonDict]:
    element_counts = Counter(local_name(elem.tag) for elem in root.iter())
    rows = [{"kind": "xml_element_count", "name": name, "count": count} for name, count in sorted(element_counts.items())]
    rows.append({"kind": "quality_gate_counts", "counts": counts})
    return rows


def _coverage(root: ET.Element, project: JsonDict, counts: dict[str, int]) -> JsonDict:
    fbd_source_nodes = _descendant_counts(root, "FBDContent")
    sfc_source_nodes = _descendant_counts(root, "SFCContent")
    fbd_node_source = sum(fbd_source_nodes[name] for name in ["IRef", "ORef", "Block", "AddOnInstruction", "TextBox"])
    sfc_node_source = sum(sfc_source_nodes[name] for name in ["Step", "Transition", "Action", "Branch"])
    rung_comment_count = sum(1 for unit in project["routine_units"] if unit.get("kind") == "rll_rung" and unit.get("comment"))
    aoi_routine_count = sum(1 for routine in project["routines"] if str(routine.get("owner") or "").startswith("AOI:"))
    data_node_count = counts["data_blocks"] + counts["default_data_blocks"]
    io_tag_count = counts["module_input_tags"] + counts["module_output_tags"] + counts["module_config_tags"]
    covered_io_tag_count = sum(1 for row in project["module_io_tags"] if row.get("tag") in {"ConfigTag", "InputTag", "OutputTag"})
    surfaces = {
        "comments": _surface("P0", counts["comments"], sum(1 for row in project["comments"] if row.get("kind") == "comment")),
        "data_blocks": _surface("P0", data_node_count, len(project["tag_data"])),
        "fbd_nodes": _surface("P0", fbd_node_source, len(project["fbd_nodes"])),
        "sfc_nodes": _surface("P0", sfc_node_source, len(project["sfc_nodes"])),
        "module_io_points": _surface("P0", io_tag_count, covered_io_tag_count),
        "routine_markdown_comments": _surface("P0", rung_comment_count, rung_comment_count),
        "aoi_routine_pages": _surface("P0", aoi_routine_count, aoi_routine_count),
        "fbd_wires": _surface("P1", counts["fbd_wires"], len(project["fbd_wires"])),
        "sfc_links": _surface("P1", sfc_source_nodes["DirectedLink"], len(project["sfc_links"])),
        "alarm_messages": _surface("P1", len(project["messages"]), len(project["messages"])),
    }
    missing = {"P0": [], "P1": []}
    for name, surface in surfaces.items():
        if surface["missing_count"]:
            missing.setdefault(surface["priority"], []).append(name)
    return {"counts": counts, "surfaces": surfaces, "missing": missing}


def _surface(priority: str, source_count: int, covered_count: int) -> JsonDict:
    missing_count = max(0, source_count - covered_count)
    return {
        "priority": priority,
        "source_count": source_count,
        "covered_count": covered_count,
        "missing_count": missing_count,
        "missing": [] if missing_count == 0 else [{"source_count": source_count, "covered_count": covered_count}],
    }


def _counts(project: JsonDict, coverage_counts: dict[str, int]) -> JsonDict:
    return {
        "data_types": len(project["data_types"]),
        "aois": len(project["aois"]),
        "controller_tags": len(project["controller_tags"]),
        "programs": len(project["programs"]),
        "program_tags": sum(len(program.get("tags", [])) for program in project["programs"]),
        "routines": len(project["routines"]),
        "routine_units": len(project["routine_units"]),
        "fbd_nodes": len(project["fbd_nodes"]),
        "fbd_wires": len(project["fbd_wires"]),
        "sfc_nodes": len(project["sfc_nodes"]),
        "sfc_links": len(project["sfc_links"]),
        "modules": len(project["modules"]),
        "module_io_tags": len(project["module_io_tags"]),
        "module_io_points": len(project["module_io_points"]),
        "tasks": len(project["tasks"]),
        "xrefs": len(project["xrefs"]),
        "comments": coverage_counts["comments"],
        "comment_records": len(project["comments"]),
        "tag_data": len(project["tag_data"]),
        "alarms": len(project["alarms"]),
        "messages": len(project["messages"]),
        "edges": len(project["edges"]),
        "entities": len(project["entities"]),
    }


def _source_quality_counts(root: ET.Element) -> dict[str, int]:
    element_counts = Counter(local_name(elem.tag) for elem in root.iter())
    routine_types = Counter(elem.attrib.get("Type", "") for elem in root.iter() if local_name(elem.tag) == "Routine")
    fbd_counts = _descendant_counts(root, "FBDContent")
    sfc_counts = _descendant_counts(root, "SFCContent")
    module_counts = _module_descendant_counts(root)
    return {
        "comments": element_counts["Comment"],
        "data_blocks": element_counts["Data"],
        "default_data_blocks": element_counts["DefaultData"],
        "fbd_routines": routine_types["FBD"],
        "fbd_sheets": fbd_counts["Sheet"],
        "fbd_blocks": fbd_counts["Block"],
        "fbd_wires": fbd_counts["Wire"],
        "sfc_routines": routine_types["SFC"],
        "sfc_steps": sfc_counts["Step"],
        "sfc_transitions": sfc_counts["Transition"],
        "sfc_actions": sfc_counts["Action"],
        "module_input_tags": module_counts["InputTag"],
        "module_output_tags": module_counts["OutputTag"],
        "module_config_tags": module_counts["ConfigTag"],
        "module_io_points": module_counts["InputTag"] + module_counts["OutputTag"] + module_counts["ConfigTag"],
    }


def _descendant_counts(root: ET.Element, container_name: str) -> Counter:
    counts: Counter[str] = Counter()
    for container in root.iter():
        if local_name(container.tag) != container_name:
            continue
        for descendant in container.iter():
            if descendant is not container:
                counts[local_name(descendant.tag)] += 1
    return counts


def _module_descendant_counts(root: ET.Element) -> Counter:
    counts: Counter[str] = Counter()
    for module in root.iter():
        if local_name(module.tag) != "Module":
            continue
        for descendant in module.iter():
            if descendant is not module:
                counts[local_name(descendant.tag)] += 1
    return counts


def _symbol_rows(project: dict) -> list[dict]:
    rows: list[dict] = []
    rows.extend(project["controller_tags"])
    rows.extend(project["data_types"])
    rows.extend(project["aois"])
    rows.extend(project["modules"])
    rows.extend(project["tasks"])
    for program in project["programs"]:
        rows.append({k: v for k, v in program.items() if k not in {"tags", "routines"}})
        rows.extend(program.get("tags", []))
    return rows


def _description(elem: ET.Element) -> str | None:
    return xml_text(xml_child(elem, "Description")) or None


def _data_formats(elem: ET.Element) -> list[str]:
    formats = []
    for child in elem.iter():
        if local_name(child.tag) in {"Data", "DefaultData"}:
            data_format = child.attrib.get("Format")
            if data_format and data_format not in formats:
                formats.append(data_format)
    return formats


def _walk_with_paths(root: ET.Element, path: str | None = None, ancestors: tuple[ET.Element, ...] = ()):
    root_path = path or _path_segment(root)
    yield root, root_path, ancestors
    counts: dict[str, int] = {}
    for child in list(root):
        child_name = local_name(child.tag)
        counts[child_name] = counts.get(child_name, 0) + 1
        yield from _walk_with_paths(child, f"{root_path}/{_path_segment(child, counts[child_name])}", ancestors + (root,))


def _path_segment(elem: ET.Element, index: int | None = None) -> str:
    attrs = xml_attributes(elem)
    for key in ["Name", "Operand", "Number", "ID", "Type", "Format"]:
        value = attrs.get(key)
        if value:
            return f"{local_name(elem.tag)}[@{key}={value!r}]"
    if index is not None:
        return f"{local_name(elem.tag)}[{index}]"
    return local_name(elem.tag)


def _nearest_owner(ancestors: tuple[ET.Element, ...]) -> ET.Element | None:
    skip = {"Comments", "Comment", "Description", "Text", "Data", "DefaultData"}
    for elem in reversed(ancestors):
        if local_name(elem.tag) not in skip:
            return elem
    return None


def _owner_ref(elem: ET.Element) -> JsonDict:
    attrs = xml_attributes(elem)
    return _compact(
        {
            "element": local_name(elem.tag),
            "name": attrs.get("Name"),
            "number": attrs.get("Number"),
            "type": attrs.get("Type"),
            "tag_type": attrs.get("TagType"),
            "data_type": attrs.get("DataType"),
            "attributes": attrs,
        }
    )


def _qualified_operand(owner_name: str | None, operand: str | None) -> str | None:
    if not owner_name:
        return operand
    if not operand:
        return owner_name
    if operand.startswith((".", "[")):
        return f"{owner_name}{operand}"
    return f"{owner_name}.{operand}"


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _compact(item)
            for key, item in value.items()
            if item is not None and item != "" and item != [] and item != {}
        }
    if isinstance(value, list):
        return [_compact(item) for item in value if item is not None and item != "" and item != [] and item != {}]
    if isinstance(value, tuple):
        return [_compact(item) for item in value if item is not None and item != "" and item != [] and item != {}]
    return value
