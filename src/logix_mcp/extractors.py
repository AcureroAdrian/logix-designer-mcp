"""Generic read-only XML extractors for Logix Designer L5X ElementTree nodes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
import xml.etree.ElementTree as ET


JsonDict = dict[str, Any]
XmlPath = str | Sequence[str]


def local_name(tag: str) -> str:
    """Return an XML tag or attribute name without its namespace."""

    return tag.split("}", 1)[-1]


def xml_attributes(elem: ET.Element) -> JsonDict:
    """Return namespace-stripped attributes as a JSON-serializable dict."""

    return {local_name(key): value for key, value in elem.attrib.items()}


def xml_text(elem: ET.Element | None) -> str:
    """Return all text below an element, trimmed."""

    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def xml_direct_text(elem: ET.Element | None) -> str:
    """Return only the direct text stored on an element, trimmed."""

    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def xml_children(elem: ET.Element | None, name: str | None = None) -> list[ET.Element]:
    """Return direct children, optionally matched by local tag name."""

    if elem is None:
        return []
    if name is None or name == "*":
        return list(elem)
    names = _name_options(name)
    return [child for child in list(elem) if local_name(child.tag) in names]


def xml_child(elem: ET.Element | None, name: str) -> ET.Element | None:
    """Return the first direct child matched by local tag name."""

    children = xml_children(elem, name)
    return children[0] if children else None


def xml_descendants(elem: ET.Element | None, name: str | None = None) -> list[ET.Element]:
    """Return descendants, optionally matched by local tag name."""

    if elem is None:
        return []
    names = _name_options(name) if name and name != "*" else None
    return [
        child
        for child in elem.iter()
        if child is not elem and (names is None or local_name(child.tag) in names)
    ]


def xml_path(elem: ET.Element | None, path: XmlPath) -> ET.Element | None:
    """Return the first element matching a simple local-name path.

    Path segments are separated by ``/``. Each segment can be a local tag name,
    ``*`` for any child, or ``NameA|NameB`` for alternatives.
    """

    matches = xml_path_all(elem, path)
    return matches[0] if matches else None


def xml_path_all(elem: ET.Element | None, path: XmlPath) -> list[ET.Element]:
    """Return every element matching a simple local-name path."""

    if elem is None:
        return []
    current = [elem]
    for part in _path_parts(path):
        if part in ("", "."):
            continue
        next_matches: list[ET.Element] = []
        for node in current:
            next_matches.extend(xml_children(node, part))
        current = next_matches
        if not current:
            break
    return current


def xml_path_segment(elem: ET.Element, index: int | None = None) -> str:
    """Return a compact, local-name segment for paths built during traversal."""

    name = local_name(elem.tag)
    attrs = xml_attributes(elem)
    for key in ("Name", "Operand", "Type", "Format", "Index", "Id"):
        value = attrs.get(key)
        if value:
            return f"{name}[@{key}={value!r}]"
    if index is not None:
        return f"{name}[{index}]"
    return name


def extract_descriptions_comments(owner: ET.Element) -> list[JsonDict]:
    """Extract direct Description and Comment records owned by an XML element."""

    records: list[JsonDict] = []
    owner_ref = _owner_ref(owner)

    for index, description in enumerate(xml_children(owner, "Description"), start=1):
        text = xml_text(description)
        if not text:
            continue
        records.append(
            _compact(
                {
                    "kind": "description",
                    "owner": owner_ref,
                    "target": owner_ref.get("name"),
                    "text": text,
                    "attributes": xml_attributes(description),
                    "path": f"{xml_path_segment(owner)}/Description[{index}]",
                }
            )
        )

    comments_parent = xml_child(owner, "Comments")
    for index, comment in enumerate(xml_children(comments_parent, "Comment"), start=1):
        record = _comment_record(owner, comment, index, prefix="Comments/")
        if record:
            records.append(record)

    for index, comment in enumerate(xml_children(owner, "Comment"), start=1):
        record = _comment_record(owner, comment, index, prefix="")
        if record:
            records.append(record)

    return records


def extract_data_nodes(
    owner: ET.Element,
    *,
    decorated_max_depth: int = 4,
    decorated_max_children: int | None = None,
) -> list[JsonDict]:
    """Extract Data and DefaultData nodes with raw text and decorated shape."""

    records: list[JsonDict] = []
    for elem, path, ancestors in _walk_with_paths(owner):
        elem_name = local_name(elem.tag)
        if elem_name not in {"Data", "DefaultData"}:
            continue
        raw_text = xml_text(elem)
        nearest_owner = _nearest_record_owner(ancestors)
        record_owner = nearest_owner if nearest_owner is not None else owner
        parsed = [
            _decorated_tree(child, max_depth=decorated_max_depth, max_children=decorated_max_children)
            for child in list(elem)
        ]
        records.append(
            _compact(
                {
                    "kind": "data_node",
                    "element": elem_name,
                    "owner": _owner_ref(record_owner),
                    "format": elem.attrib.get("Format"),
                    "raw_text": raw_text,
                    "parsed": parsed,
                    "attributes": xml_attributes(elem),
                    "path": path,
                }
            )
        )
    return records


def extract_alarm_message_records(owner: ET.Element) -> list[JsonDict]:
    """Extract alarm message records from Data Format=\"Alarm\" nodes."""

    records: list[JsonDict] = []
    for data, path, ancestors in _walk_with_paths(owner):
        if local_name(data.tag) != "Data" or data.attrib.get("Format") != "Alarm":
            continue

        nearest_tag = _nearest_tag_owner(ancestors)
        nearest_owner = _nearest_record_owner(ancestors)
        tag_owner = nearest_tag if nearest_tag is not None else nearest_owner
        if tag_owner is None:
            tag_owner = owner
        alarm_params = _first_child_named_suffix(data, "Parameters")
        alarm_config = xml_child(data, "AlarmConfig")
        alarm_class = xml_text(xml_child(alarm_config, "AlarmClass")) or None
        messages = _alarm_messages(alarm_config)
        base_record = {
            "kind": "alarm_message",
            "owner": _owner_ref(tag_owner),
            "tag_name": tag_owner.attrib.get("Name"),
            "tag_type": tag_owner.attrib.get("TagType"),
            "data_type": tag_owner.attrib.get("DataType"),
            "alarm_type": _alarm_type(alarm_params),
            "alarm_class": alarm_class,
            "severity": alarm_params.attrib.get("Severity") if alarm_params is not None else None,
            "assoc_tags": _assoc_tags(alarm_params),
            "parameters": xml_attributes(alarm_params) if alarm_params is not None else {},
            "path": path,
        }

        if not messages:
            records.append(_compact(base_record))
            continue

        for message in messages:
            record = dict(base_record)
            record.update(message)
            records.append(_compact(record))

    return records


def extract_tag_comment_records(owner: ET.Element) -> list[JsonDict]:
    """Extract Logix tag comment records from a tag element or container."""

    records: list[JsonDict] = []
    for tag, _path, _ancestors in _iter_tag_elements(owner):
        tag_ref = _owner_ref(tag)
        for doc_record in extract_descriptions_comments(tag):
            if doc_record.get("kind") != "comment":
                continue
            operand = doc_record.get("operand")
            records.append(
                _compact(
                    {
                        "kind": "tag_comment",
                        "tag_name": tag.attrib.get("Name"),
                        "tag_type": tag.attrib.get("TagType"),
                        "data_type": tag.attrib.get("DataType"),
                        "operand": operand,
                        "target": _qualified_operand(tag.attrib.get("Name"), operand),
                        "text": doc_record.get("text"),
                        "attributes": doc_record.get("attributes", {}),
                        "owner": tag_ref,
                        "path": doc_record.get("path"),
                    }
                )
            )
    return records


def extract_produce_consume_info(owner: ET.Element) -> list[JsonDict]:
    """Extract ProduceInfo and ConsumeInfo records from tag nodes."""

    records: list[JsonDict] = []
    for tag, path, _ancestors in _iter_tag_elements(owner):
        tag_type = tag.attrib.get("TagType")
        for info in xml_children(tag, "ProduceInfo|ConsumeInfo"):
            info_name = local_name(info.tag)
            direction = "produced" if info_name == "ProduceInfo" else "consumed"
            info_attrs = xml_attributes(info)
            records.append(
                _compact(
                    {
                        "kind": "produce_consume_info",
                        "direction": direction,
                        "tag_name": tag.attrib.get("Name"),
                        "tag_type": tag_type,
                        "data_type": tag.attrib.get("DataType"),
                        "info_element": info_name,
                        "info": info_attrs,
                        "producer": info_attrs.get("Producer"),
                        "remote_tag": info_attrs.get("RemoteTag"),
                        "remote_instance": info_attrs.get("RemoteInstance"),
                        "produce_count": info_attrs.get("ProduceCount"),
                        "rpi": info_attrs.get("RPI") or info_attrs.get("DefaultRPI"),
                        "unicast": info_attrs.get("Unicast") or info_attrs.get("UnicastPermitted"),
                        "minimum_rpi": info_attrs.get("MinimumRPI"),
                        "maximum_rpi": info_attrs.get("MaximumRPI"),
                        "owner": _owner_ref(tag),
                        "path": f"{path}/{xml_path_segment(info)}",
                    }
                )
            )
    return records


def _path_parts(path: XmlPath) -> list[str]:
    if isinstance(path, str):
        return [part for part in path.replace("\\", "/").split("/") if part]
    return [str(part) for part in path]


def _name_options(name: str | None) -> set[str]:
    if not name:
        return set()
    return {part for part in name.split("|") if part}


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


def _owner_ref(elem: ET.Element) -> JsonDict:
    attrs = xml_attributes(elem)
    return _compact(
        {
            "element": local_name(elem.tag),
            "name": attrs.get("Name"),
            "tag_type": attrs.get("TagType"),
            "data_type": attrs.get("DataType"),
            "attributes": attrs,
        }
    )


def _comment_record(owner: ET.Element, comment: ET.Element, index: int, *, prefix: str) -> JsonDict | None:
    text = xml_text(comment)
    if not text:
        return None
    operand = comment.attrib.get("Operand")
    owner_name = owner.attrib.get("Name")
    return _compact(
        {
            "kind": "comment",
            "owner": _owner_ref(owner),
            "operand": operand,
            "target": _qualified_operand(owner_name, operand),
            "text": text,
            "attributes": xml_attributes(comment),
            "path": f"{xml_path_segment(owner)}/{prefix}Comment[{index}]",
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


def _walk_with_paths(
    root: ET.Element,
    path: str | None = None,
    ancestors: tuple[ET.Element, ...] = (),
):
    root_path = path or xml_path_segment(root)
    yield root, root_path, ancestors
    counts: dict[str, int] = {}
    for child in list(root):
        child_name = local_name(child.tag)
        counts[child_name] = counts.get(child_name, 0) + 1
        child_path = f"{root_path}/{xml_path_segment(child, counts[child_name])}"
        yield from _walk_with_paths(child, child_path, ancestors + (root,))


def _decorated_tree(
    elem: ET.Element,
    *,
    max_depth: int,
    max_children: int | None,
    depth: int = 0,
) -> JsonDict:
    children = list(elem)
    visible_children = children if max_children is None else children[:max_children]
    record = _compact(
        {
            "element": local_name(elem.tag),
            "attributes": xml_attributes(elem),
            "text": xml_direct_text(elem),
            "children": [
                _decorated_tree(
                    child,
                    max_depth=max_depth,
                    max_children=max_children,
                    depth=depth + 1,
                )
                for child in visible_children
            ]
            if depth < max_depth
            else [],
            "children_truncated": len(children) - len(visible_children) if max_children is not None else 0,
            "depth_truncated": bool(children and depth >= max_depth),
        }
    )
    return record


def _nearest_record_owner(ancestors: tuple[ET.Element, ...]) -> ET.Element | None:
    skip = {"Data", "DefaultData", "Comments", "AlarmConfig", "Messages", "Message", "Text"}
    for elem in reversed(ancestors):
        if local_name(elem.tag) not in skip:
            return elem
    return None


def _nearest_tag_owner(ancestors: tuple[ET.Element, ...]) -> ET.Element | None:
    for elem in reversed(ancestors):
        if local_name(elem.tag) in {"Tag", "LocalTag"}:
            return elem
    return None


def _iter_tag_elements(owner: ET.Element):
    for elem, path, ancestors in _walk_with_paths(owner):
        if local_name(elem.tag) in {"Tag", "LocalTag"}:
            yield elem, path, ancestors


def _first_child_named_suffix(elem: ET.Element | None, suffix: str) -> ET.Element | None:
    if elem is None:
        return None
    for child in list(elem):
        if local_name(child.tag).endswith(suffix):
            return child
    return None


def _alarm_type(alarm_params: ET.Element | None) -> str | None:
    if alarm_params is None:
        return None
    name = local_name(alarm_params.tag)
    return name.removesuffix("Parameters")


def _assoc_tags(alarm_params: ET.Element | None) -> list[str]:
    if alarm_params is None:
        return []
    tags = []
    for key, value in alarm_params.attrib.items():
        if local_name(key).startswith("AssocTag") and value and value.upper() != "SPACE":
            tags.append(value)
    return tags


def _alarm_messages(alarm_config: ET.Element | None) -> list[JsonDict]:
    messages_parent = xml_path(alarm_config, "Messages")
    records: list[JsonDict] = []
    for message in xml_children(messages_parent, "Message"):
        text_nodes = xml_children(message, "Text")
        if not text_nodes:
            records.append(
                _compact(
                    {
                        "message_type": message.attrib.get("Type"),
                        "message_attributes": xml_attributes(message),
                    }
                )
            )
            continue
        for text_node in text_nodes:
            records.append(
                _compact(
                    {
                        "message_type": message.attrib.get("Type"),
                        "lang": text_node.attrib.get("Lang"),
                        "text": xml_text(text_node),
                        "message_attributes": xml_attributes(message),
                        "text_attributes": xml_attributes(text_node),
                    }
                )
            )
    return records


__all__ = [
    "extract_alarm_message_records",
    "extract_data_nodes",
    "extract_descriptions_comments",
    "extract_produce_consume_info",
    "extract_tag_comment_records",
    "local_name",
    "xml_attributes",
    "xml_child",
    "xml_children",
    "xml_descendants",
    "xml_direct_text",
    "xml_path",
    "xml_path_all",
    "xml_path_segment",
    "xml_text",
]
