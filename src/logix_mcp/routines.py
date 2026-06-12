"""Routine-level IR extraction for Logix Designer L5X exports."""

from __future__ import annotations

from typing import Any
import xml.etree.ElementTree as ET

from .extractors import local_name, xml_attributes, xml_child, xml_children, xml_text
from .xrefs import (
    extract_tag_references,
    parse_ladder_instructions,
    xrefs_from_ladder_neutral_text,
    xrefs_from_structured_text,
)


JsonDict = dict[str, Any]

FBD_NODE_ELEMENTS = {"IRef", "ORef", "ICon", "OCon", "Block", "AddOnInstruction", "TextBox", "JSR"}
SFC_NODE_ELEMENTS = {"Step", "Transition", "Action", "Branch"}


def routine_ir_from_element(elem: ET.Element, *, routine_id: str, owner: str | None = None, program: str | None = None) -> JsonDict:
    """Compatibility wrapper returning the full routine IR bundle."""

    resolved_owner = owner or routine_id.rsplit(".Routine:", 1)[0]
    return extract_routine_ir(elem, owner=resolved_owner, program=program)


def extract_rll_units(elem: ET.Element, *, routine_id: str, owner: str | None = None, program: str | None = None, aoi_signatures: dict[str, list[str]] | None = None) -> list[JsonDict]:
    """Return RLL rung units with per-unit xrefs and calls."""

    bundle = _extract_rll(elem, routine_id, owner or _owner_from_id(routine_id), program, aoi_signatures)
    refs_by_source = _group_by_source(bundle.get("xrefs", []))
    units = []
    for unit in bundle.get("routine_units", []):
        row = dict(unit)
        row["xrefs"] = refs_by_source.get(row.get("id"), [])
        row["calls"] = _calls_from_ladder(row.get("instructions", []))
        units.append(row)
    return units


def extract_st_units(elem: ET.Element, *, routine_id: str, owner: str | None = None, program: str | None = None) -> list[JsonDict]:
    """Return ST line units with per-line xrefs and calls."""

    resolved_owner = owner or _owner_from_id(routine_id)
    units = []
    for line_unit in _extract_st(elem, routine_id, resolved_owner, program).get("routine_units", []):
        text = line_unit.get("text") or ""
        row = dict(line_unit)
        row["xrefs"] = _xrefs_from_st_line(text, routine_id, str(row.get("id") or routine_id), f"Line {row.get('number')}", elem, resolved_owner, program)
        row["calls"] = _calls_from_st(text)
        units.append(row)
    return units


def extract_fbd_units(elem: ET.Element, *, routine_id: str, owner: str | None = None, program: str | None = None) -> list[JsonDict]:
    """Return FBD sheets with embedded nodes, wires, calls, and xrefs."""

    resolved_owner = owner or _owner_from_id(routine_id)
    bundle = _extract_fbd(elem, routine_id, resolved_owner, program)
    units = []
    for unit in bundle.get("routine_units", []):
        row = dict(unit)
        sheet_id = row.get("id")
        nodes = [node for node in bundle.get("fbd_nodes", []) if node.get("sheet_id") == sheet_id and node.get("node_type") != "TextBox"]
        textboxes = [node for node in bundle.get("fbd_nodes", []) if node.get("sheet_id") == sheet_id and node.get("node_type") == "TextBox"]
        wires = [wire for wire in bundle.get("fbd_wires", []) if wire.get("sheet_id") == sheet_id]
        row["nodes"] = nodes
        row["wires"] = wires
        row["textboxes"] = textboxes
        row["xrefs"] = [ref for ref in bundle.get("xrefs", []) if str(ref.get("source") or "").startswith(str(sheet_id))]
        row["calls"] = _calls_from_fbd_nodes(nodes)
        units.append(row)
    return units


def extract_sfc_units(elem: ET.Element, *, routine_id: str, owner: str | None = None, program: str | None = None) -> list[JsonDict]:
    """Return SFC units in source order, including nested action and condition ST lines."""

    resolved_owner = owner or _owner_from_id(routine_id)
    units = []
    content = xml_child(elem, "SFCContent")
    unit_id = f"{routine_id}.SFC"
    for index, child in enumerate(list(content) if content is not None else []):
        child_name = local_name(child.tag)
        if child_name == "Step":
            units.append(_compat_sfc_step(child, routine_id, unit_id, resolved_owner, program, index))
        elif child_name == "Transition":
            units.append(_compat_sfc_transition(child, routine_id, unit_id, resolved_owner, program, index))
        elif child_name == "Branch":
            units.append(_compat_sfc_branch(child, routine_id, unit_id, resolved_owner, program, index))
        elif child_name == "DirectedLink":
            units.append(_compat_sfc_link(child, routine_id, unit_id, resolved_owner, program, index))
    return units


def extract_routine_ir(elem: ET.Element, *, owner: str, program: str | None = None, aoi_signatures: dict[str, list[str]] | None = None) -> JsonDict:
    """Extract a routine plus normalized routine units and graph records."""

    name = elem.attrib.get("Name", "")
    language = elem.attrib.get("Type", "")
    routine_id = f"{owner}.Routine:{name}"
    units: list[JsonDict] = []
    fbd_nodes: list[JsonDict] = []
    fbd_wires: list[JsonDict] = []
    sfc_nodes: list[JsonDict] = []
    sfc_links: list[JsonDict] = []
    xrefs: list[JsonDict] = []
    body_lines: list[str] = []
    rungs: list[JsonDict] = []
    st_lines: list[JsonDict] = []

    language_key = language.upper()
    if language_key == "RLL":
        extracted = _extract_rll(elem, routine_id, owner, program, aoi_signatures)
    elif language_key == "ST":
        extracted = _extract_st(elem, routine_id, owner, program)
    elif language_key == "FBD":
        extracted = _extract_fbd(elem, routine_id, owner, program)
    elif language_key == "SFC":
        extracted = _extract_sfc(elem, routine_id, owner, program)
    else:
        extracted = {}

    units.extend(extracted.get("routine_units", []))
    fbd_nodes.extend(extracted.get("fbd_nodes", []))
    fbd_wires.extend(extracted.get("fbd_wires", []))
    sfc_nodes.extend(extracted.get("sfc_nodes", []))
    sfc_links.extend(extracted.get("sfc_links", []))
    xrefs.extend(extracted.get("xrefs", []))
    body_lines.extend(extracted.get("body_lines", []))
    rungs.extend(extracted.get("rungs", []))
    st_lines.extend(extracted.get("st_lines", []))

    routine: JsonDict = _compact(
        {
            "kind": "routine",
            "id": routine_id,
            "name": name,
            "program": program,
            "owner": owner,
            "owner_type": owner.split(":", 1)[0] if ":" in owner else None,
            "owner_name": owner.split(":", 1)[1] if ":" in owner else owner,
            "language": language,
            "description": _description(elem),
            "rungs": rungs,
            "st_lines": st_lines,
            "body": "\n".join(line for line in body_lines if line is not None),
            "unit_count": len(units),
            "fbd_node_count": len(fbd_nodes),
            "fbd_wire_count": len(fbd_wires),
            "sfc_node_count": len(sfc_nodes),
            "sfc_link_count": len(sfc_links),
            "attributes": xml_attributes(elem),
        }
    )
    return {
        "routine": routine,
        "routine_units": units,
        "fbd_nodes": fbd_nodes,
        "fbd_wires": fbd_wires,
        "sfc_nodes": sfc_nodes,
        "sfc_links": sfc_links,
        "xrefs": _annotate_xrefs(xrefs, routine),
    }


def _extract_rll(elem: ET.Element, routine_id: str, owner: str, program: str | None, aoi_signatures: dict[str, list[str]] | None = None) -> JsonDict:
    units: list[JsonDict] = []
    xrefs: list[JsonDict] = []
    body_lines: list[str] = []
    rungs: list[JsonDict] = []
    for index, rung in enumerate(xml_children(xml_child(elem, "RLLContent"), "Rung")):
        number = rung.attrib.get("Number") or str(index)
        text = xml_text(xml_child(rung, "Text"))
        comment = xml_text(xml_child(rung, "Comment"))
        unit_id = f"{routine_id}.Rung:{number}"
        instructions = parse_ladder_instructions(text)
        record = _compact(
            {
                "kind": "rll_rung",
                "id": unit_id,
                "routine_id": routine_id,
                "routine": elem.attrib.get("Name"),
                "owner": owner,
                "program": program,
                "language": "RLL",
                "sequence": index,
                "number": number,
                "type": rung.attrib.get("Type"),
                "comment": comment,
                "text": text,
                "instructions": instructions,
                "attributes": xml_attributes(rung),
            }
        )
        units.append(record)
        rungs.append(
            _compact(
                {
                    "number": number,
                    "type": rung.attrib.get("Type"),
                    "comment": comment,
                    "text": text,
                    "instructions": instructions,
                    "attributes": xml_attributes(rung),
                }
            )
        )
        body_lines.append(f"RUNG {number}")
        if comment:
            body_lines.append(f"COMMENT: {comment}")
        if text:
            body_lines.append(text)
            xrefs.extend(
                xrefs_from_ladder_neutral_text(
                    text,
                    routine=routine_id,
                    source=unit_id,
                    location=f"Rung {number}",
                    aoi_signatures=aoi_signatures,
                )
            )
    return {"routine_units": units, "xrefs": xrefs, "body_lines": body_lines, "rungs": rungs}


def _extract_st(elem: ET.Element, routine_id: str, owner: str, program: str | None) -> JsonDict:
    units: list[JsonDict] = []
    body_lines: list[str] = []
    st_lines: list[JsonDict] = []
    for index, line in enumerate(xml_children(xml_child(elem, "STContent"), "Line")):
        text = xml_text(line)
        number = line.attrib.get("Number") or str(index)
        unit_id = f"{routine_id}.Line:{number}"
        record = _compact(
            {
                "kind": "st_line",
                "id": unit_id,
                "routine_id": routine_id,
                "routine": elem.attrib.get("Name"),
                "owner": owner,
                "program": program,
                "language": "ST",
                "sequence": index,
                "number": number,
                "text": text,
                "attributes": xml_attributes(line),
            }
        )
        units.append(record)
        st_lines.append(record)
        body_lines.append(text)
    body = "\n".join(body_lines)
    return {
        "routine_units": units,
        "xrefs": xrefs_from_structured_text(body, routine=routine_id, source=routine_id, location="STContent"),
        "body_lines": body_lines,
        "st_lines": st_lines,
    }


def _extract_fbd(elem: ET.Element, routine_id: str, owner: str, program: str | None) -> JsonDict:
    units: list[JsonDict] = []
    nodes: list[JsonDict] = []
    wires: list[JsonDict] = []
    xrefs: list[JsonDict] = []
    body_lines: list[str] = []
    content = xml_child(elem, "FBDContent")
    for sheet_index, sheet in enumerate(xml_children(content, "Sheet")):
        sheet_number = sheet.attrib.get("Number") or str(sheet_index + 1)
        sheet_id = f"{routine_id}.Sheet:{sheet_number}"
        sheet_nodes: list[str] = []
        sheet_wires = 0
        text_boxes: list[str] = []
        for child in list(sheet):
            child_name = local_name(child.tag)
            if child_name == "Wire":
                wire = _fbd_wire(child, routine_id, sheet_id, sheet_number, owner, program, len(wires))
                wires.append(wire)
                sheet_wires += 1
                continue
            if child_name not in FBD_NODE_ELEMENTS:
                continue
            node = _fbd_node(child, routine_id, sheet_id, sheet_number, owner, program, len(nodes))
            nodes.append(node)
            sheet_nodes.append(str(node.get("id_on_sheet") or node.get("node_id") or ""))
            node_text = _fbd_node_text(node)
            if node_text:
                body_lines.append(node_text)
            if child_name == "TextBox":
                text = xml_text(child)
                if text:
                    text_boxes.append(text)
            xrefs.extend(_fbd_xrefs(node, routine_id))
        unit = _compact(
            {
                "kind": "fbd_sheet",
                "id": sheet_id,
                "routine_id": routine_id,
                "routine": elem.attrib.get("Name"),
                "owner": owner,
                "program": program,
                "language": "FBD",
                "sequence": sheet_index,
                "number": sheet_number,
                "node_ids": sheet_nodes,
                "node_count": len(sheet_nodes),
                "wire_count": sheet_wires,
                "text_boxes": text_boxes,
                "attributes": xml_attributes(sheet),
            }
        )
        units.append(unit)
        body_lines.insert(max(len(body_lines) - len(sheet_nodes), 0), f"FBD SHEET {sheet_number}")
        for text in text_boxes:
            body_lines.append(f"TEXTBOX: {text}")
    return {"routine_units": units, "fbd_nodes": nodes, "fbd_wires": wires, "xrefs": xrefs, "body_lines": body_lines}


def _extract_sfc(elem: ET.Element, routine_id: str, owner: str, program: str | None) -> JsonDict:
    units: list[JsonDict] = []
    nodes: list[JsonDict] = []
    links: list[JsonDict] = []
    xrefs: list[JsonDict] = []
    body_lines: list[str] = []
    content = xml_child(elem, "SFCContent")
    if content is None:
        return {}

    unit_id = f"{routine_id}.SFC"
    for index, child in enumerate(list(content)):
        child_name = local_name(child.tag)
        if child_name == "DirectedLink":
            links.append(_sfc_link(child, routine_id, unit_id, owner, program, len(links)))
            continue
        if child_name not in SFC_NODE_ELEMENTS:
            continue
        node = _sfc_node(child, routine_id, unit_id, owner, program, len(nodes))
        nodes.append(node)
        for action in node.get("actions", []):
            nodes.append(
                _compact(
                    {
                        "kind": "sfc_node",
                        "id": f"{unit_id}.Node:{action.get('id')}:Action",
                        "routine_id": routine_id,
                        "unit_id": unit_id,
                        "owner": owner,
                        "program": program,
                        "node_type": "Action",
                        "node_id": action.get("id"),
                        "operand": action.get("operand"),
                        "qualifier": action.get("qualifier"),
                        "parent_step_id": node.get("node_id"),
                        "st_body": action.get("st_body"),
                        "attributes": action.get("attributes", {}),
                    }
                )
            )
        body_lines.extend(_sfc_node_text(node))
        for body in node.get("st_bodies", []):
            xrefs.extend(
                xrefs_from_structured_text(
                    body.get("text", ""),
                    routine=routine_id,
                    source=node["id"],
                    location=f"{node.get('node_type')} {node.get('operand') or node.get('node_id')}",
                )
            )
        for symbol in extract_tag_references(str(node.get("operand") or ""), include_calls=False):
            xrefs.append(_ref(symbol, routine_id, "read_write", "SFC_OPERAND", node["id"]))
    units.append(
        _compact(
            {
                "kind": "sfc_chart",
                "id": unit_id,
                "routine_id": routine_id,
                "routine": elem.attrib.get("Name"),
                "owner": owner,
                "program": program,
                "language": "SFC",
                "node_count": len(nodes),
                "link_count": len(links),
                "attributes": xml_attributes(content),
            }
        )
    )
    return {"routine_units": units, "sfc_nodes": nodes, "sfc_links": links, "xrefs": xrefs, "body_lines": body_lines}


def _fbd_node(
    elem: ET.Element,
    routine_id: str,
    sheet_id: str,
    sheet_number: str,
    owner: str,
    program: str | None,
    index: int,
) -> JsonDict:
    node_type = local_name(elem.tag)
    node_id = elem.attrib.get("ID") or str(index)
    params = []
    arrays = []
    for child in list(elem):
        child_name = local_name(child.tag)
        if child_name in {"InputParameter", "OutputParameter", "InOutParameter"}:
            params.append(
                _compact(
                    {
                        "kind": child_name,
                        "name": child.attrib.get("Name"),
                        "argument": child.attrib.get("Argument"),
                        "attributes": xml_attributes(child),
                    }
                )
            )
        elif child_name == "Array":
            arrays.append(
                _compact(
                    {
                        "name": child.attrib.get("Name"),
                        "operand": child.attrib.get("Operand"),
                        "attributes": xml_attributes(child),
                    }
                )
            )
    return _compact(
        {
            "kind": "fbd_node",
            "id": f"{sheet_id}.Node:{node_id}:{node_type}",
            "routine_id": routine_id,
            "sheet_id": sheet_id,
            "sheet_number": sheet_number,
            "owner": owner,
            "program": program,
            "node_type": node_type,
            "node_id": node_id,
            "id_on_sheet": node_id,
            "instruction": "JSR" if node_type == "JSR" else (elem.attrib.get("Type") or elem.attrib.get("Name") or node_type),
            "callee": elem.attrib.get("Routine") if node_type == "JSR" else None,
            "operand": elem.attrib.get("Operand"),
            "connector_name": elem.attrib.get("Name") if node_type in {"ICon", "OCon"} else None,
            "visible_pins": elem.attrib.get("VisiblePins"),
            "x": elem.attrib.get("X"),
            "y": elem.attrib.get("Y"),
            "description": _description(elem),
            "text": xml_text(elem) if node_type == "TextBox" else None,
            "parameters": params,
            "arrays": arrays,
            "attributes": xml_attributes(elem),
        }
    )


def _fbd_wire(
    elem: ET.Element,
    routine_id: str,
    sheet_id: str,
    sheet_number: str,
    owner: str,
    program: str | None,
    index: int,
) -> JsonDict:
    return _compact(
        {
            "kind": "fbd_wire",
            "id": f"{sheet_id}.Wire:{index:04d}",
            "routine_id": routine_id,
            "sheet_id": sheet_id,
            "sheet_number": sheet_number,
            "owner": owner,
            "program": program,
            "from_id": elem.attrib.get("FromID"),
            "from_param": elem.attrib.get("FromParam"),
            "to_id": elem.attrib.get("ToID"),
            "to_param": elem.attrib.get("ToParam"),
            "attributes": xml_attributes(elem),
        }
    )


def _fbd_xrefs(node: JsonDict, routine_id: str) -> list[JsonDict]:
    refs: list[JsonDict] = []
    node_type = str(node.get("node_type") or "")
    if node_type == "IRef":
        access = "read"
    elif node_type == "ORef":
        access = "write"
    else:
        access = "read_write"

    if node_type == "JSR" and node.get("callee"):
        refs.append(_ref(str(node["callee"]), routine_id, "call", "FBD_JSR", str(node.get("id"))))

    for operand in [node.get("operand")]:
        for symbol in extract_tag_references(str(operand or ""), include_calls=False):
            refs.append(_ref(symbol, routine_id, access, f"FBD_{node_type}", str(node.get("id"))))

    for array in node.get("arrays", []):
        for symbol in extract_tag_references(str(array.get("operand") or ""), include_calls=False):
            refs.append(_ref(symbol, routine_id, "read_write", "FBD_ARRAY", str(node.get("id"))))

    for param in node.get("parameters", []):
        param_kind = param.get("kind")
        if param_kind == "InputParameter":
            param_access = "read"
        elif param_kind == "OutputParameter":
            param_access = "write"
        else:
            param_access = "read_write"
        for symbol in extract_tag_references(str(param.get("argument") or ""), include_calls=False):
            refs.append(_ref(symbol, routine_id, param_access, f"FBD_{param_kind}", str(node.get("id"))))
    return refs


def _sfc_node(
    elem: ET.Element,
    routine_id: str,
    unit_id: str,
    owner: str,
    program: str | None,
    index: int,
) -> JsonDict:
    node_type = local_name(elem.tag)
    node_id = elem.attrib.get("ID") or str(index)
    actions = []
    st_bodies = []
    for action in xml_children(elem, "Action"):
        action_body = _st_body(xml_child(xml_child(action, "Body"), "STContent"))
        actions.append(
            _compact(
                {
                    "id": action.attrib.get("ID"),
                    "operand": action.attrib.get("Operand"),
                    "qualifier": action.attrib.get("Qualifier"),
                    "st_body": action_body,
                    "attributes": xml_attributes(action),
                }
            )
        )
        if action_body:
            st_bodies.append({"kind": "action", "name": action.attrib.get("Operand"), "text": action_body})

    condition_body = _st_body(xml_child(xml_child(elem, "Condition"), "STContent"))
    if condition_body:
        st_bodies.append({"kind": "condition", "name": elem.attrib.get("Operand"), "text": condition_body})

    return _compact(
        {
            "kind": "sfc_node",
            "id": f"{unit_id}.Node:{node_id}:{node_type}",
            "routine_id": routine_id,
            "unit_id": unit_id,
            "owner": owner,
            "program": program,
            "node_type": node_type,
            "node_id": node_id,
            "operand": elem.attrib.get("Operand"),
            "initial_step": elem.attrib.get("InitialStep"),
            "x": elem.attrib.get("X"),
            "y": elem.attrib.get("Y"),
            "description": _description(elem),
            "actions": actions,
            "condition_body": condition_body,
            "st_bodies": st_bodies,
            "attributes": xml_attributes(elem),
        }
    )


def _sfc_link(
    elem: ET.Element,
    routine_id: str,
    unit_id: str,
    owner: str,
    program: str | None,
    index: int,
) -> JsonDict:
    return _compact(
        {
            "kind": "sfc_link",
            "id": f"{unit_id}.Link:{index:04d}",
            "routine_id": routine_id,
            "unit_id": unit_id,
            "owner": owner,
            "program": program,
            "from_id": elem.attrib.get("FromID"),
            "to_id": elem.attrib.get("ToID"),
            "attributes": xml_attributes(elem),
        }
    )


def _st_body(content: ET.Element | None) -> str:
    return "\n".join(xml_text(line) for line in xml_children(content, "Line") if xml_text(line))


def _fbd_node_text(node: JsonDict) -> str:
    node_type = str(node.get("node_type") or "")
    if node_type == "TextBox":
        return f"TEXTBOX {node.get('node_id')}: {node.get('text') or ''}".strip()
    parts = [node_type, str(node.get("node_id") or "")]
    if node.get("instruction"):
        parts.append(f"instruction={node['instruction']}")
    if node.get("callee"):
        parts.append(f"callee={node['callee']}")
    if node.get("operand"):
        parts.append(f"operand={node['operand']}")
    if node.get("connector_name"):
        parts.append(f"connector={node['connector_name']}")
    if node.get("visible_pins"):
        parts.append(f"pins={node['visible_pins']}")
    for array in node.get("arrays", []):
        parts.append(f"array:{array.get('name')}={array.get('operand')}")
    for param in node.get("parameters", []):
        parts.append(f"{param.get('kind')}:{param.get('name')}={param.get('argument')}")
    return " ".join(part for part in parts if part)


def _sfc_node_text(node: JsonDict) -> list[str]:
    lines = [f"SFC {node.get('node_type')} {node.get('node_id')} operand={node.get('operand') or ''}".strip()]
    if node.get("condition_body"):
        lines.append(f"CONDITION: {node['condition_body']}")
    for action in node.get("actions", []):
        lines.append(f"ACTION {action.get('operand') or action.get('id')}: {action.get('st_body') or ''}".strip())
    return lines


def _description(elem: ET.Element) -> str | None:
    return xml_text(xml_child(elem, "Description")) or None


def _ref(symbol: str, routine_id: str, access: str, instruction: str, source: str) -> JsonDict:
    return {
        "symbol": symbol,
        "routine": routine_id,
        "access": access,
        "instruction": instruction,
        "confidence": "heuristic",
        "source": source,
        "location": source,
        "operand": symbol,
        "base_symbol": symbol.split(".", 1)[0].split("[", 1)[0],
    }


def _annotate_xrefs(xrefs: list[JsonDict], routine: JsonDict) -> list[JsonDict]:
    annotated = []
    seen = set()
    for ref in xrefs:
        row = dict(ref)
        row.setdefault("routine", routine["id"])
        row["program"] = routine.get("program")
        row["routine_name"] = routine.get("name")
        row["owner"] = routine.get("owner")
        row["language"] = routine.get("language")
        key = (
            row.get("symbol"),
            row.get("routine"),
            row.get("access"),
            row.get("instruction"),
            row.get("source"),
            row.get("location"),
        )
        if key in seen:
            continue
        seen.add(key)
        annotated.append(_compact(row))
    return annotated


def _owner_from_id(routine_id: str) -> str:
    return routine_id.rsplit(".Routine:", 1)[0] if ".Routine:" in routine_id else routine_id


def _group_by_source(xrefs: list[JsonDict]) -> dict[object, list[JsonDict]]:
    grouped: dict[object, list[JsonDict]] = {}
    for ref in xrefs:
        grouped.setdefault(ref.get("source"), []).append(ref)
    return grouped


def _calls_from_ladder(instructions: list[JsonDict]) -> list[JsonDict]:
    calls: list[JsonDict] = []
    for instruction in instructions:
        op = str(instruction.get("instruction") or "")
        args = instruction.get("args") or []
        if op == "JSR" and args:
            calls.append({"call_type": "routine", "callee": str(args[0]), "instruction": op})
        elif op and op not in {"XIC", "XIO", "OTE", "OTL", "OTU"}:
            calls.append({"call_type": "instruction", "callee": op, "instruction": op})
    return _dedupe_call_rows(calls)


def _calls_from_st(text: str) -> list[JsonDict]:
    import re

    calls = [{"call_type": "function", "callee": match.group(1)} for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)]
    return _dedupe_call_rows(calls)


def _calls_from_fbd_nodes(nodes: list[JsonDict]) -> list[JsonDict]:
    calls = []
    for node in nodes:
        node_type = node.get("node_type")
        if node_type == "AddOnInstruction":
            calls.append({"call_type": "aoi", "callee": node.get("instruction"), "operand": node.get("operand")})
        elif node_type == "Block":
            calls.append({"call_type": "block", "callee": node.get("instruction"), "operand": node.get("operand")})
        elif node_type == "JSR":
            calls.append({"call_type": "routine", "callee": node.get("callee"), "instruction": "JSR"})
    return _dedupe_call_rows(calls)


def _dedupe_call_rows(calls: list[JsonDict]) -> list[JsonDict]:
    rows = []
    seen = set()
    for call in calls:
        key = tuple(sorted((key, str(value)) for key, value in call.items()))
        if key in seen:
            continue
        seen.add(key)
        rows.append(_compact(call))
    return rows


def _compat_sfc_step(
    elem: ET.Element,
    routine_id: str,
    unit_id: str,
    owner: str,
    program: str | None,
    index: int,
) -> JsonDict:
    node = _sfc_node(elem, routine_id, unit_id, owner, program, index)
    actions = []
    for action in node.get("actions", []):
        lines = _st_lines(action.get("st_body") or "", routine_id, f"{node['id']}.Action:{action.get('id')}")
        row = dict(action)
        row["lines"] = lines
        actions.append(row)
    node["kind"] = "step"
    node["initial_step"] = str(node.get("initial_step") or "").lower() == "true"
    node["actions"] = actions
    return node


def _compat_sfc_transition(
    elem: ET.Element,
    routine_id: str,
    unit_id: str,
    owner: str,
    program: str | None,
    index: int,
) -> JsonDict:
    node = _sfc_node(elem, routine_id, unit_id, owner, program, index)
    node["kind"] = "transition"
    node["condition_lines"] = _st_lines(node.get("condition_body") or "", routine_id, f"{node['id']}.Condition")
    return node


def _compat_sfc_branch(
    elem: ET.Element,
    routine_id: str,
    unit_id: str,
    owner: str,
    program: str | None,
    index: int,
) -> JsonDict:
    return _compact(
        {
            "kind": "branch",
            "id": f"{unit_id}.Branch:{elem.attrib.get('ID') or index}",
            "routine_id": routine_id,
            "owner": owner,
            "program": program,
            "node_id": elem.attrib.get("ID"),
            "branch_type": elem.attrib.get("BranchType"),
            "branch_flow": elem.attrib.get("BranchFlow"),
            "priority": elem.attrib.get("Priority"),
            "legs": [{"id": leg.attrib.get("ID"), "attributes": xml_attributes(leg)} for leg in xml_children(elem, "Leg")],
            "attributes": xml_attributes(elem),
        }
    )


def _compat_sfc_link(
    elem: ET.Element,
    routine_id: str,
    unit_id: str,
    owner: str,
    program: str | None,
    index: int,
) -> JsonDict:
    row = _sfc_link(elem, routine_id, unit_id, owner, program, index)
    row["kind"] = "directed_link"
    row["show"] = str(elem.attrib.get("Show") or "").lower() == "true"
    return row


def _st_lines(text: str, routine_id: str, source: str) -> list[JsonDict]:
    lines = []
    for index, line in enumerate(text.splitlines()):
        row = {
            "number": str(index),
            "text": line,
            "xrefs": _xrefs_from_st_line(line, routine_id, f"{source}.Line:{index}", f"Line {index}", None, None, None),
            "calls": _calls_from_st(line),
        }
        lines.append(_compact(row))
    return lines


def _xrefs_from_st_line(
    text: str,
    routine_id: str,
    source: str,
    location: str,
    elem: ET.Element | None,
    owner: str | None,
    program: str | None,
) -> list[JsonDict]:
    refs = xrefs_from_structured_text(text, routine=routine_id, source=source, location=location)
    if not refs:
        refs = [
            _ref(symbol, routine_id, "read", "ST_EXPR", source)
            for symbol in extract_tag_references(text, include_calls=False)
        ]
    return _annotate_xrefs(
        refs,
        {
            "id": routine_id,
            "program": program,
            "name": elem.attrib.get("Name") if elem is not None else None,
            "owner": owner,
            "language": "ST",
        },
    )


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _compact(item)
            for key, item in value.items()
            if item is not None and item != "" and item != [] and item != {}
        }
    if isinstance(value, list):
        return [_compact(item) for item in value if item is not None and item != "" and item != [] and item != {}]
    return value
