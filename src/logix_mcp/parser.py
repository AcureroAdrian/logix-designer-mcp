"""Read-only parser for Studio 5000 Logix Designer L5X exports."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import hashlib
import re
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


# XML elements the pipeline extracts into IR datasets or fields. The coverage
# gate compares every element found in the source against these sets, so an
# element missing from all three lists turns the P0 semaphore red instead of
# passing silently.
HANDLED_ELEMENTS = {
    # Root / controller / tags
    "RSLogix5000Content", "Controller", "Tags", "Tag", "Description",
    "Comments", "Comment",
    # Tag data trees
    "Data", "DefaultData", "DataValue", "DataValueMember", "Structure",
    "StructureMember", "Array", "ArrayMember", "Element",
    # Data types
    "DataTypes", "DataType", "Members", "Member",
    # AOI definitions
    "AddOnInstructionDefinitions", "AddOnInstructionDefinition",
    "Parameters", "Parameter", "LocalTags", "LocalTag", "EKey",
    # Programs / tasks
    "Programs", "Program", "Routines", "Routine", "Body",
    "Tasks", "Task", "ScheduledPrograms", "ScheduledProgram",
    # Routine content
    "RLLContent", "Rung", "Text", "STContent", "Line",
    "FBDContent", "Sheet", "IRef", "ORef", "ICon", "OCon", "Block",
    "AddOnInstruction", "InputParameter", "OutputParameter", "InOutParameter",
    "TextBox", "Wire", "JSR",
    "SFCContent", "Step", "Transition", "Action", "Branch", "Leg",
    "DirectedLink", "Condition",
    # Alarms / messages / produce-consume
    "AlarmConfig", "AlarmDigitalParameters", "AlarmAnalogParameters",
    "AlarmClass", "Messages", "Message", "ProduceInfo", "ConsumeInfo",
    # Hardware
    "Modules", "Module", "Ports", "Port", "Bus", "Communications",
    "Connections", "Connection", "RackConnection",
    "ConfigTag", "InputTag", "OutputTag", "InAliasTag", "OutAliasTag",
    # ExtendedProperties text payloads preserved by _extended_properties
    "ExtendedProperties", "public", "ConfigID", "CatNum", "Vendor",
    "ADDAVersion",
}

# Elements we know exist in real exports but do not extract yet. Each entry
# documents the data loss so the gate reports it as a P1 coverage gap instead
# of hiding it; move an element to HANDLED_ELEMENTS when its extractor lands.
KNOWN_UNEXTRACTED_ELEMENTS = {
    "MessageParameters": "normalized into message_parameters with raw source anchor",
    "DataTypeFormats": "module profile group normalized into module_profile_fragments with raw source anchor",
    "DataTypeFormat": "module profile CIP instance paths normalized into module_profile_fragments",
    "PL": "module profile PL normalized into module_profile_fragments",
    "Version": "module profile version normalized into module_profile_fragments",
    "EngineeringUnits": "engineering unit group preserved and summarized",
    "EngineeringUnit": "per-channel engineering units normalized into engineering_units",
    "RedundancyInfo": "controller metadata normalized with raw source anchor",
    "Security": "controller metadata normalized with raw source anchor",
    "SafetyInfo": "controller metadata normalized with raw source anchor",
    "CST": "controller time metadata normalized with raw source anchor",
    "WallClockTime": "controller time metadata normalized with raw source anchor",
    "TimeSynchronize": "controller time metadata normalized with raw source anchor",
    "Trends": "preserved as raw source fragment",
    "DataLogs": "preserved as raw source fragment",
    "ChildPrograms": "preserved as raw source fragment and child rows normalized",
    "ChildProgram": "normalized into program_children with raw source anchor",
    "RevisionNote": "preserved as raw source fragment",
    "HMICmd": "preserved as raw source fragment",
}

SEMANTIC_SOURCE_ELEMENTS = {
    "MessageParameters",
    "DataTypeFormats",
    "DataTypeFormat",
    "PL",
    "Version",
    "EngineeringUnits",
    "EngineeringUnit",
    "RedundancyInfo",
    "Security",
    "SafetyInfo",
    "CST",
    "WallClockTime",
    "TimeSynchronize",
    "ChildProgram",
}


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
    source_fragments = _source_fragments(root)
    controller_metadata = _controller_metadata(root)
    engineering_units = _engineering_units(root)
    message_parameters = _message_parameters(root)
    module_profile_fragments = _module_profile_fragments(root)
    program_children = _program_children(root)
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
        "sfc_charts": collector["sfc_charts"],
        "sfc_nodes": collector["sfc_nodes"],
        "sfc_links": collector["sfc_links"],
        "sfc_branches": collector["sfc_branches"],
        "sfc_legs": collector["sfc_legs"],
        "xrefs": collector["xrefs"],
        "comments": comments,
        "tag_comments": tag_comments,
        "tag_data": tag_data,
        "alarms": _alarm_rows(alarm_messages),
        "messages": alarm_messages,
        "produce_consume": produce_consume,
        "source_fragments": source_fragments,
        "controller_metadata": controller_metadata,
        "engineering_units": engineering_units,
        "message_parameters": message_parameters,
        "module_profile_fragments": module_profile_fragments,
        "program_children": program_children,
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
        "sfc_charts": [],
        "sfc_nodes": [],
        "sfc_links": [],
        "sfc_branches": [],
        "sfc_legs": [],
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
    for key in ["routine_units", "fbd_nodes", "fbd_wires", "sfc_charts", "sfc_nodes", "sfc_links", "sfc_branches", "sfc_legs", "xrefs"]:
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


def _source_fragments(root: ET.Element) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for elem, path, ancestors in _walk_with_paths(root):
        elem_name = local_name(elem.tag)
        if elem_name not in KNOWN_UNEXTRACTED_ELEMENTS:
            continue
        raw_xml = ET.tostring(elem, encoding="unicode", short_empty_elements=True)
        source_hash = _source_hash(raw_xml)
        owner = _nearest_owner(ancestors)
        if owner is None:
            owner = ancestors[-1] if ancestors else elem
        coverage_mode = "semantic_dataset" if elem_name in SEMANTIC_SOURCE_ELEMENTS else "raw_preserved"
        rows.append(
            _compact(
                {
                    "kind": "source_fragment",
                    "id": _source_anchor(path, source_hash),
                    "anchor": _source_anchor(path, source_hash),
                    "element": elem_name,
                    "owner_kind": local_name(owner.tag) if owner is not None else None,
                    "owner_id": _owner_identifier(owner) if owner is not None else None,
                    "owner": _owner_ref(owner) if owner is not None else None,
                    "xml_path": path,
                    "source_hash": source_hash,
                    "attributes": xml_attributes(elem),
                    "summary": _fragment_summary(elem),
                    "raw_xml": raw_xml,
                    "byte_size": len(raw_xml.encode("utf-8")),
                    "coverage_mode": coverage_mode,
                }
            )
        )
    return rows


def _controller_metadata(root: ET.Element) -> list[JsonDict]:
    names = {"RedundancyInfo", "Security", "SafetyInfo", "CST", "WallClockTime", "TimeSynchronize"}
    rows: list[JsonDict] = []
    for elem, path, _ancestors in _walk_with_paths(root):
        elem_name = local_name(elem.tag)
        if elem_name not in names:
            continue
        source_hash = _element_source_hash(elem)
        rows.append(
            _compact(
                {
                    "kind": "controller_metadata",
                    "id": f"ControllerMetadata:{elem_name}:{len(rows):04d}",
                    "name": elem_name,
                    "element": elem_name,
                    "text": xml_text(elem),
                    "attributes": xml_attributes(elem),
                    "xml_path": path,
                    "source_hash": source_hash,
                    "raw_anchor": _source_anchor(path, source_hash),
                }
            )
        )
    return rows


def _engineering_units(root: ET.Element) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for elem, path, ancestors in _walk_with_paths(root):
        if local_name(elem.tag) != "EngineeringUnit":
            continue
        module = _nearest_ancestor(ancestors, {"Module"})
        io_tag = _nearest_ancestor(ancestors, {"ConfigTag", "InputTag", "OutputTag", "InAliasTag", "OutAliasTag"})
        module_name = _module_name(module) if module is not None else None
        tag_name = local_name(io_tag.tag) if io_tag is not None else None
        role, direction = _io_tag_role(tag_name)
        operand = elem.attrib.get("Operand")
        source_hash = _element_source_hash(elem)
        rows.append(
            _compact(
                {
                    "kind": "engineering_unit",
                    "id": f"EngineeringUnit:{module_name or 'unknown'}:{tag_name or 'tag'}:{operand or len(rows)}",
                    "module": module_name,
                    "module_id": f"Module:{module_name}" if module_name else None,
                    "module_catalog_number": module.attrib.get("CatalogNumber") if module is not None else None,
                    "tag": tag_name,
                    "role": role,
                    "direction": direction,
                    "operand": operand,
                    "point": _point_from_operand(operand),
                    "engineering_unit": xml_text(elem),
                    "attributes": xml_attributes(elem),
                    "xml_path": path,
                    "source_hash": source_hash,
                    "raw_anchor": _source_anchor(path, source_hash),
                }
            )
        )
    return rows


def _message_parameters(root: ET.Element) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for elem, path, ancestors in _walk_with_paths(root):
        if local_name(elem.tag) != "MessageParameters":
            continue
        tag = _nearest_ancestor(ancestors, {"Tag", "LocalTag"})
        attrs = xml_attributes(elem)
        tag_name = tag.attrib.get("Name") if tag is not None else None
        source_hash = _element_source_hash(elem)
        rows.append(
            _compact(
                {
                    "kind": "message_parameters",
                    "id": f"MessageParameters:{tag_name or len(rows)}:{len(rows):04d}",
                    "tag_name": tag_name,
                    "tag_type": tag.attrib.get("TagType") if tag is not None else None,
                    "data_type": tag.attrib.get("DataType") if tag is not None else None,
                    "message_type": attrs.get("MessageType"),
                    "service_code": attrs.get("ServiceCode"),
                    "object_type": attrs.get("ObjectType"),
                    "class": attrs.get("Class") or attrs.get("ClassName"),
                    "instance": attrs.get("Instance"),
                    "attribute": attrs.get("Attribute"),
                    "connection_path": attrs.get("ConnectionPath"),
                    "local_element": attrs.get("LocalElement"),
                    "destination_tag": attrs.get("DestinationTag"),
                    "requested_length": attrs.get("RequestedLength"),
                    "attributes": attrs,
                    "xml_path": path,
                    "source_hash": source_hash,
                    "raw_anchor": _source_anchor(path, source_hash),
                }
            )
        )
    return rows


def _module_profile_fragments(root: ET.Element) -> list[JsonDict]:
    rows: list[JsonDict] = []
    public_by_module: dict[int, JsonDict] = {}
    for elem, _path, ancestors in _walk_with_paths(root):
        if local_name(elem.tag) != "public":
            continue
        module = _nearest_ancestor(ancestors, {"Module"})
        if module is None:
            continue
        public_by_module[id(module)] = {
            "vendor": xml_text(xml_child(elem, "Vendor")),
            "cat_num": xml_text(xml_child(elem, "CatNum")),
        }

    for elem, path, ancestors in _walk_with_paths(root):
        elem_name = local_name(elem.tag)
        if elem_name not in {"PL", "DataTypeFormat"}:
            continue
        module = _nearest_ancestor(ancestors, {"Module"})
        if module is None:
            continue
        module_name = _module_name(module)
        public_info = public_by_module.get(id(module), {})
        source_hash = _element_source_hash(elem)
        base = {
            "kind": "module_profile_fragment",
            "id": f"ModuleProfile:{module_name}:{elem_name}:{len(rows):04d}",
            "module": module_name,
            "module_id": f"Module:{module_name}" if module_name else None,
            "catalog_number": module.attrib.get("CatalogNumber"),
            "vendor": module.attrib.get("Vendor") or public_info.get("vendor"),
            "cat_num": public_info.get("cat_num"),
            "element": elem_name,
            "attributes": xml_attributes(elem),
            "xml_path": path,
            "source_hash": source_hash,
            "raw_anchor": _source_anchor(path, source_hash),
        }
        if elem_name == "PL":
            version = xml_child(elem, "Version")
            connection = xml_child(elem, "Connection")
            base.update(
                {
                    "profile_level": _direct_text(elem),
                    "version_name": version.attrib.get("Name") if version is not None else None,
                    "connection_name": connection.attrib.get("Name") if connection is not None else None,
                    "connection_format": connection.attrib.get("Format") if connection is not None else None,
                }
            )
        elif elem_name == "DataTypeFormat":
            base.update(
                {
                    "datatype_format_type": elem.attrib.get("Type"),
                    "instance_application_path": elem.attrib.get("InstanceApplicationPath"),
                    "format": elem.attrib.get("Format"),
                }
            )
        rows.append(_compact(base))
    return rows


def _program_children(root: ET.Element) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for elem, path, ancestors in _walk_with_paths(root):
        if local_name(elem.tag) != "ChildProgram":
            continue
        program = _nearest_ancestor(ancestors, {"Program"})
        parent_name = program.attrib.get("Name") if program is not None else None
        child_name = elem.attrib.get("Name") or xml_text(elem)
        source_hash = _element_source_hash(elem)
        rows.append(
            _compact(
                {
                    "kind": "program_child",
                    "id": f"ProgramChild:{parent_name or 'unknown'}:{child_name or len(rows)}",
                    "parent_program": parent_name,
                    "parent_program_id": f"Program:{parent_name}" if parent_name else None,
                    "child_program": child_name,
                    "child_program_id": f"Program:{child_name}" if child_name else None,
                    "attributes": xml_attributes(elem),
                    "xml_path": path,
                    "source_hash": source_hash,
                    "raw_anchor": _source_anchor(path, source_hash),
                }
            )
        )
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
        "sfc_charts",
        "sfc_nodes",
        "sfc_links",
        "sfc_branches",
        "sfc_legs",
        "module_ports",
        "module_connections",
        "module_io_tags",
        "module_io_points",
        "alarms",
        "messages",
        "controller_metadata",
        "engineering_units",
        "message_parameters",
        "module_profile_fragments",
        "program_children",
        "source_fragments",
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
    for child in project.get("program_children", []):
        rows.append(
            _compact(
                {
                    "kind": "program_child",
                    "from": child.get("parent_program_id"),
                    "to": child.get("child_program_id"),
                    "source": child.get("id"),
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
    element_counts = Counter(local_name(elem.tag) for elem in root.iter())
    sfc_source_nodes = _descendant_counts(root, "SFCContent")
    # Count every child of every FBD Sheet (minus sheet metadata) so node types
    # the extractor does not know about still raise the missing count.
    fbd_node_source = _fbd_sheet_child_count(root)
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
        # Independent XML numerators; previously these compared the pipeline's
        # own output against itself and could never report a miss.
        "routine_markdown_comments": _surface("P0", _rung_comment_source_count(root), rung_comment_count),
        "aoi_routine_pages": _surface("P0", _aoi_routine_source_count(root), aoi_routine_count),
        "fbd_wires": _surface("P1", counts["fbd_wires"], len(project["fbd_wires"])),
        "sfc_links": _surface("P1", sfc_source_nodes["DirectedLink"], len(project["sfc_links"])),
        "alarm_messages": _surface("P1", element_counts["Message"], len(project["messages"])),
        "unextracted_elements": _unextracted_elements_surface(element_counts, project.get("source_fragments", [])),
        "unknown_elements": _unknown_elements_surface(element_counts),
    }
    missing = {"P0": [], "P1": []}
    for name, surface in surfaces.items():
        if surface["missing_count"]:
            missing.setdefault(surface["priority"], []).append(name)
    return {"counts": counts, "surfaces": surfaces, "missing": missing}


def _fbd_sheet_child_count(root: ET.Element) -> int:
    total = 0
    for content in root.iter():
        if local_name(content.tag) != "FBDContent":
            continue
        for sheet in content:
            if local_name(sheet.tag) != "Sheet":
                continue
            # Wires and sheet descriptions have their own surfaces; everything
            # else under a Sheet is expected to become an FBD node.
            total += sum(1 for child in sheet if local_name(child.tag) not in {"Description", "Wire"})
    return total


def _rung_comment_source_count(root: ET.Element) -> int:
    return sum(
        1
        for elem in root.iter()
        if local_name(elem.tag) == "Rung" and xml_child(elem, "Comment") is not None
    )


def _aoi_routine_source_count(root: ET.Element) -> int:
    total = 0
    for aoi in root.iter():
        if local_name(aoi.tag) != "AddOnInstructionDefinition":
            continue
        total += sum(1 for elem in aoi.iter() if local_name(elem.tag) == "Routine")
    return total


def _unknown_elements_surface(element_counts: Counter) -> JsonDict:
    unknown = {
        name: count
        for name, count in sorted(element_counts.items())
        if name not in HANDLED_ELEMENTS and name not in KNOWN_UNEXTRACTED_ELEMENTS
    }
    total = sum(unknown.values())
    return {
        "priority": "P0",
        "source_count": total,
        "covered_count": 0,
        "missing_count": total,
        "missing": [{"element": name, "count": count} for name, count in unknown.items()],
    }


def _unextracted_elements_surface(element_counts: Counter, source_fragments: list[JsonDict]) -> JsonDict:
    found = {
        name: count
        for name, count in sorted(element_counts.items())
        if name in KNOWN_UNEXTRACTED_ELEMENTS
    }
    total = sum(found.values())
    fragment_modes: dict[str, Counter[str]] = {}
    for fragment in source_fragments:
        element = fragment.get("element")
        if not element:
            continue
        fragment_modes.setdefault(str(element), Counter())[str(fragment.get("coverage_mode") or "raw_preserved")] += 1
    elements = []
    total_semantic = 0
    total_raw = 0
    total_missing = 0
    for name, count in found.items():
        modes = fragment_modes.get(name, Counter())
        semantic_count = min(count, modes.get("semantic_dataset", 0))
        raw_count = min(max(0, count - semantic_count), modes.get("raw_preserved", 0))
        covered = min(count, semantic_count + raw_count)
        missing = max(0, count - covered)
        total_semantic += semantic_count
        total_raw += raw_count
        total_missing += missing
        elements.append(
            _compact(
                {
                    "element": name,
                    "source_count": count,
                    "semantic_covered_count": semantic_count,
                    "raw_preserved_count": raw_count,
                    "covered_count": covered,
                    "missing_count": missing,
                    "coverage_modes": dict(modes),
                    "note": KNOWN_UNEXTRACTED_ELEMENTS[name],
                }
            )
        )
    return {
        "priority": "P1",
        "source_count": total,
        "covered_count": min(total, total_semantic + total_raw),
        "semantic_covered_count": total_semantic,
        "raw_preserved_count": total_raw,
        "missing_count": total_missing,
        "coverage_mode_counts": {"semantic_dataset": total_semantic, "raw_preserved": total_raw, "not_covered": total_missing},
        "elements": elements,
        "missing": [
            {"element": item["element"], "count": item["missing_count"], "note": item.get("note")}
            for item in elements
            if item.get("missing_count")
        ],
    }


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
        "sfc_charts": len(project["sfc_charts"]),
        "sfc_nodes": len(project["sfc_nodes"]),
        "sfc_links": len(project["sfc_links"]),
        "sfc_branches": len(project["sfc_branches"]),
        "sfc_legs": len(project["sfc_legs"]),
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
        "source_fragments": len(project["source_fragments"]),
        "controller_metadata": len(project["controller_metadata"]),
        "engineering_units": len(project["engineering_units"]),
        "message_parameters": len(project["message_parameters"]),
        "module_profile_fragments": len(project["module_profile_fragments"]),
        "program_children": len(project["program_children"]),
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


def _owner_identifier(elem: ET.Element) -> str:
    attrs = xml_attributes(elem)
    name = attrs.get("Name") or attrs.get("Operand") or attrs.get("Number") or attrs.get("ID")
    elem_name = local_name(elem.tag)
    return f"{elem_name}:{name}" if name else elem_name


def _nearest_ancestor(ancestors: tuple[ET.Element, ...], names: set[str]) -> ET.Element | None:
    for elem in reversed(ancestors):
        if local_name(elem.tag) in names:
            return elem
    return None


def _source_hash(raw_xml: str) -> str:
    return hashlib.sha256(raw_xml.encode("utf-8")).hexdigest()


def _element_source_hash(elem: ET.Element) -> str:
    return _source_hash(ET.tostring(elem, encoding="unicode", short_empty_elements=True))


def _source_anchor(path: str, source_hash: str) -> str:
    digest = hashlib.sha256(f"{path}\0{source_hash}".encode("utf-8")).hexdigest()[:16]
    return f"xml:{digest}"


def _fragment_summary(elem: ET.Element) -> str:
    attrs = xml_attributes(elem)
    pieces = [local_name(elem.tag)]
    if attrs:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(attrs.items())[:8])
        pieces.append(rendered)
    text = " ".join(xml_text(elem).split())
    if text:
        pieces.append(text[:240])
    return " | ".join(pieces)


def _module_name(module: ET.Element | None) -> str | None:
    if module is None:
        return None
    raw_name = module.attrib.get("Name")
    if raw_name:
        return raw_name
    parent = module.attrib.get("ParentModule")
    slot = _first_slot(module)
    parent_port = module.attrib.get("ParentModPortId")
    if parent and slot:
        return f"{parent}:{slot}"
    if parent and parent_port:
        return f"{parent}:port{parent_port}"
    return None


def _first_slot(module: ET.Element) -> str | None:
    ports = xml_child(module, "Ports")
    for port in xml_children(ports, "Port"):
        address = port.attrib.get("Address")
        if address and address.isdigit():
            return address
    return None


def _io_tag_role(tag_name: str | None) -> tuple[str | None, str | None]:
    mapping = {
        "ConfigTag": ("Config", "config"),
        "InputTag": ("Input", "input"),
        "OutputTag": ("Output", "output"),
        "InAliasTag": ("InAlias", "input"),
        "OutAliasTag": ("OutAlias", "output"),
    }
    return mapping.get(str(tag_name or ""), (None, None))


def _point_from_operand(operand: str | None) -> int | None:
    if not operand:
        return None
    text = operand.strip()
    match = re.fullmatch(r"\.?\[?(\d+)\]?", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(?:^|[^0-9])(\d+)$", text)
    if match:
        return int(match.group(1))
    return None


def _direct_text(elem: ET.Element | None) -> str | None:
    if elem is None or elem.text is None:
        return None
    text = elem.text.strip()
    return text or None


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
