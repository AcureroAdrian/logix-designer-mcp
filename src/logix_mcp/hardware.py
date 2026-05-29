"""Read-only hardware IR helpers for Logix Designer Module XML."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
import re
import xml.etree.ElementTree as ET


JsonDict = dict[str, Any]

_IO_TAG_ROLES = {
    "ConfigTag": ("Config", "config"),
    "InputTag": ("Input", "input"),
    "OutputTag": ("Output", "output"),
    "InAliasTag": ("InAlias", "input"),
    "OutAliasTag": ("OutAlias", "output"),
}

__all__ = [
    "build_device_tree",
    "extract_hardware_ir",
    "extract_module_ir",
    "hardware_ir",
    "io_points",
    "io_tags",
    "module_connections",
    "module_elements",
    "module_io_points",
    "module_io_tags",
    "module_ir",
    "module_ports",
    "normalize_module",
]


def module_elements(root: ET.Element) -> list[ET.Element]:
    """Return Module elements from a Module, Modules, Controller, or content root."""

    if _local_name(root.tag) == "Module":
        return [root]
    modules = _child(root, "Modules")
    if modules is not None:
        return _children(modules, "Module")
    controller = _child(root, "Controller")
    if controller is not None:
        return module_elements(controller)
    return []


def normalize_module(module: ET.Element, index: int = 0) -> JsonDict:
    """Normalize one Module element without mutating or depending on parser state."""

    ports = module_ports(module)
    slot = _first_slot(ports)
    address = _first_value(port.get("address") for port in ports)
    network_address = _first_value(
        port.get("address")
        for port in ports
        if str(port.get("type") or "").lower() == "ethernet" and port.get("address")
    )
    raw_name = _clean(module.attrib.get("Name"))
    parent = _clean(module.attrib.get("ParentModule"))
    parent_port = _clean(module.attrib.get("ParentModPortId"))
    name = raw_name or _generated_module_name(parent, parent_port, slot, index)
    ekey = _child(module, "EKey")
    extended = _extended_properties(module)

    result: JsonDict = {
        "kind": "module",
        "id": f"Module:{name}",
        "name": name,
        "name_source": "Name" if raw_name else "generated",
        "catalog_number": _clean(module.attrib.get("CatalogNumber")),
        "vendor": _clean(module.attrib.get("Vendor")),
        "product_type": _clean(module.attrib.get("ProductType")),
        "product_code": _clean(module.attrib.get("ProductCode")),
        "major": _clean(module.attrib.get("Major")),
        "minor": _clean(module.attrib.get("Minor")),
        "parent_module": parent,
        "parent_mod_port_id": parent_port,
        "slot": slot,
        "address": address,
        "network_address": network_address,
        "inhibited": _clean(module.attrib.get("Inhibited")),
        "major_fault": _clean(module.attrib.get("MajorFault")),
        "description": _description(module),
        "attributes": dict(module.attrib),
    }
    if raw_name:
        result["configured_name"] = raw_name
    if ekey is not None:
        result["ekey"] = _compact({"state": _clean(ekey.attrib.get("State")), "attributes": dict(ekey.attrib)})
    if extended:
        result["extended_properties"] = extended
    return _compact(result)


def module_ports(module: ET.Element) -> list[JsonDict]:
    """Extract normalized Port records from one Module element."""

    records: list[JsonDict] = []
    for port in _children(_child(module, "Ports"), "Port"):
        bus = _child(port, "Bus")
        record: JsonDict = {
            "id": _clean(port.attrib.get("Id")),
            "address": _clean(port.attrib.get("Address")),
            "type": _clean(port.attrib.get("Type")),
            "upstream": _clean(port.attrib.get("Upstream")),
            "attributes": dict(port.attrib),
        }
        if bus is not None:
            record["bus"] = _compact({"size": _clean(bus.attrib.get("Size")), "attributes": dict(bus.attrib)})
        records.append(_compact(record))
    return records


def module_connections(module: ET.Element, index: int = 0) -> list[JsonDict]:
    """Extract Connection and RackConnection records for one Module element."""

    module_record = normalize_module(module, index)
    records: list[JsonDict] = []
    for connections in _iter_named(module, "Connections"):
        for elem in list(connections):
            connection_kind = _local_name(elem.tag)
            if connection_kind not in {"Connection", "RackConnection"}:
                continue
            record: JsonDict = {
                "module": module_record["name"],
                "module_id": module_record["id"],
                "module_catalog_number": module_record.get("catalog_number"),
                "kind": "rack_connection" if connection_kind == "RackConnection" else "connection",
                "name": _clean(elem.attrib.get("Name")),
                "type": _clean(elem.attrib.get("Type")),
                "rpi": _clean(elem.attrib.get("RPI")),
                "attributes": dict(elem.attrib),
                "io_tags": [_tag_record(tag, module_record) for tag in _direct_io_tag_elements(elem)],
            }
            records.append(_compact(record))
    return records


def io_tags(module: ET.Element, index: int = 0) -> list[JsonDict]:
    """Extract Config/Input/Output/InAlias/OutAlias tag metadata from a Module."""

    module_record = normalize_module(module, index)
    return [_tag_record(tag, module_record) for tag in _io_tag_elements(module)]


def io_points(module: ET.Element, index: int = 0) -> list[JsonDict]:
    """Extract point descriptions from Comments/Comment Operand entries."""

    module_record = normalize_module(module, index)
    points: list[JsonDict] = []
    for tag in _io_tag_elements(module):
        role, direction = _IO_TAG_ROLES[_local_name(tag.tag)]
        tag_description = _description(tag)
        comments = _child(tag, "Comments")
        if comments is None:
            continue
        for comment in _children(comments, "Comment"):
            operand = _clean(comment.attrib.get("Operand"))
            description = _text_of(comment) or None
            point = _point_from_operand(operand)
            points.append(
                _compact(
                    {
                        "module": module_record["name"],
                        "module_id": module_record["id"],
                        "module_catalog_number": module_record.get("catalog_number"),
                        "parent_module": module_record.get("parent_module"),
                        "slot": module_record.get("slot"),
                        "role": role,
                        "direction": direction,
                        "operand": operand,
                        "point": point,
                        "description": description,
                        "tag_description": tag_description,
                    }
                )
            )
    return points


def module_ir(module: ET.Element, index: int = 0) -> JsonDict:
    """Return a compact hardware IR record for one Module element."""

    record = normalize_module(module, index)
    ports = module_ports(module)
    connections = module_connections(module, index)
    tags = io_tags(module, index)
    points = io_points(module, index)
    record.update(
        _compact(
            {
                "ports": ports,
                "connections": connections,
                "io_tags": tags,
                "io_points": points,
            }
        )
    )
    return record


def extract_module_ir(module: ET.Element, index: int = 0) -> JsonDict:
    """Alias for callers that prefer extractor-style naming."""

    return module_ir(module, index)


def extract_hardware_ir(root: ET.Element) -> JsonDict:
    """Extract hardware/module IR and a device tree from a Logix XML root."""

    modules = [module_ir(module, index) for index, module in enumerate(module_elements(root))]
    return {
        "modules": modules,
        "module_connections": [
            connection for module in modules for connection in module.get("connections", [])
        ],
        "io_points": [point for module in modules for point in module.get("io_points", [])],
        "device_tree": build_device_tree(modules),
    }


def hardware_ir(root: ET.Element) -> JsonDict:
    """Alias for extract_hardware_ir."""

    return extract_hardware_ir(root)


def module_io_tags(module: ET.Element, index: int = 0) -> list[JsonDict]:
    """Alias for io_tags."""

    return io_tags(module, index)


def module_io_points(module: ET.Element, index: int = 0) -> list[JsonDict]:
    """Alias for io_points."""

    return io_points(module, index)


def build_device_tree(modules: Iterable[ET.Element | JsonDict]) -> JsonDict:
    """Build a parent/child device tree from Module elements or normalized dicts."""

    records = [_as_module_record(module, index) for index, module in enumerate(modules)]
    by_name = {record["name"]: record for record in records}
    nodes = {record["name"]: _tree_node(record) for record in records}
    roots: list[JsonDict] = []
    orphans: list[JsonDict] = []

    for record in records:
        node = nodes[record["name"]]
        parent = record.get("parent_module")
        if not parent or parent == record["name"]:
            roots.append(node)
        elif parent in by_name:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)
            orphans.append(node)

    for node in nodes.values():
        node["children"].sort(key=_node_sort_key)
    roots.sort(key=_node_sort_key)
    return _compact({"roots": roots, "orphans": orphans})


def _as_module_record(module: ET.Element | JsonDict, index: int) -> JsonDict:
    if isinstance(module, ET.Element):
        return normalize_module(module, index)
    return dict(module)


def _tree_node(record: JsonDict) -> JsonDict:
    node = _compact(
        {
            "id": record.get("id"),
            "name": record.get("name"),
            "catalog_number": record.get("catalog_number"),
            "parent_module": record.get("parent_module"),
            "parent_mod_port_id": record.get("parent_mod_port_id"),
            "slot": record.get("slot"),
            "address": record.get("address"),
            "network_address": record.get("network_address"),
            "inhibited": record.get("inhibited"),
            "major_fault": record.get("major_fault"),
        }
    )
    node["children"] = []
    return node


def _tag_record(tag: ET.Element, module_record: JsonDict) -> JsonDict:
    tag_name = _local_name(tag.tag)
    role, direction = _IO_TAG_ROLES[tag_name]
    return _compact(
        {
            "module": module_record["name"],
            "module_id": module_record["id"],
            "module_catalog_number": module_record.get("catalog_number"),
            "parent_module": module_record.get("parent_module"),
            "slot": module_record.get("slot"),
            "tag": tag_name,
            "role": role,
            "direction": direction,
            "external_access": _clean(tag.attrib.get("ExternalAccess")),
            "config_size": _clean(tag.attrib.get("ConfigSize")),
            "description": _description(tag),
            "data_type": _data_type(tag),
            "data_formats": _data_formats(tag),
            "comment_count": len(_children(_child(tag, "Comments"), "Comment")),
            "attributes": dict(tag.attrib),
        }
    )


def _io_tag_elements(module: ET.Element) -> list[ET.Element]:
    return [elem for elem in module.iter() if _local_name(elem.tag) in _IO_TAG_ROLES]


def _direct_io_tag_elements(elem: ET.Element) -> list[ET.Element]:
    return [child for child in list(elem) if _local_name(child.tag) in _IO_TAG_ROLES]


def _data_type(elem: ET.Element) -> str | None:
    for structure in _iter_named(elem, "Structure"):
        data_type = _clean(structure.attrib.get("DataType"))
        if data_type:
            return data_type
    return None


def _data_formats(elem: ET.Element) -> list[str]:
    formats: list[str] = []
    for data in _iter_named(elem, "Data"):
        data_format = _clean(data.attrib.get("Format"))
        if data_format and data_format not in formats:
            formats.append(data_format)
    return formats


def _extended_properties(module: ET.Element) -> JsonDict:
    extended = _child(module, "ExtendedProperties")
    if extended is None:
        return {}
    result: JsonDict = {}
    for group in list(extended):
        group_name = _local_name(group.tag)
        group_values = {_local_name(child.tag): _text_of(child) for child in list(group) if _text_of(child)}
        if group_values:
            result[group_name] = group_values
    return result


def _description(elem: ET.Element) -> str | None:
    return _text_of(_child(elem, "Description")) or None


def _point_from_operand(operand: str | None) -> int | None:
    if not operand:
        return None
    match = re.fullmatch(r"\.?\[?(\d+)\]?", operand.strip())
    if match:
        return int(match.group(1))
    match = re.search(r"(?:^|[^0-9])(\d+)$", operand.strip())
    if match:
        return int(match.group(1))
    return None


def _first_slot(ports: list[JsonDict]) -> str | None:
    for port in ports:
        address = port.get("address")
        if address is not None and str(address).isdigit():
            return str(address)
    return None


def _generated_module_name(parent: str | None, parent_port: str | None, slot: str | None, index: int) -> str:
    if parent and slot:
        return f"{parent}:{slot}"
    if parent and parent_port:
        return f"{parent}:port{parent_port}:{index:04d}"
    return f"unnamed_module_{index:04d}"


def _node_sort_key(node: JsonDict) -> tuple[int, str]:
    slot = node.get("slot")
    if slot is not None and str(slot).isdigit():
        return (int(str(slot)), str(node.get("name") or ""))
    return (999999, str(node.get("name") or ""))


def _first_value(values: Iterable[Any]) -> Any | None:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _child(elem: ET.Element | None, name: str) -> ET.Element | None:
    if elem is None:
        return None
    for child in list(elem):
        if _local_name(child.tag) == name:
            return child
    return None


def _children(elem: ET.Element | None, name: str) -> list[ET.Element]:
    if elem is None:
        return []
    return [child for child in list(elem) if _local_name(child.tag) == name]


def _iter_named(elem: ET.Element, name: str) -> Iterable[ET.Element]:
    for child in elem.iter():
        if _local_name(child.tag) == name:
            yield child


def _text_of(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _clean(value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _compact(item)
            for key, item in value.items()
            if item is not None and item != "" and item != [] and item != {}
        }
    if isinstance(value, list):
        return [_compact(item) for item in value if item is not None]
    return value
