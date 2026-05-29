"""Serializable domain models for Logix Designer project analysis.

The models intentionally stay small and dependency-free. They are designed for
read-only extraction from L5X and for later materialization as JSON/JSONL.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Iterable


JsonDict = dict[str, Any]


def _clean_value(value: Any) -> Any:
    if is_dataclass(value):
        return compact_dict(value)
    if isinstance(value, list):
        return [_clean_value(item) for item in value if item is not None]
    if isinstance(value, tuple):
        return [_clean_value(item) for item in value if item is not None]
    if isinstance(value, dict):
        return {
            str(key): _clean_value(item)
            for key, item in value.items()
            if item is not None and item != "" and item != [] and item != {}
        }
    return value


def compact_dict(instance: Any) -> JsonDict:
    """Convert a dataclass-like object to a compact JSON-serializable dict."""

    result: JsonDict = {}
    if is_dataclass(instance):
        items = ((item.name, getattr(instance, item.name)) for item in fields(instance))
    else:
        items = instance.__dict__.items()
    for name, value in items:
        if value is None or value == "" or value == [] or value == {}:
            continue
        result[name] = _clean_value(value)
    return result


def records_to_jsonl(records: Iterable[Any]) -> list[JsonDict]:
    """Return JSONL-ready dict records without forcing a JSON dependency here."""

    return [_clean_value(record) for record in records]


@dataclass(slots=True)
class SourceRef:
    file: str | None = None
    path: str | None = None
    scope: str | None = None

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class ControllerMetadata:
    name: str | None = None
    processor_type: str | None = None
    major_rev: str | None = None
    minor_rev: str | None = None
    software_revision: str | None = None
    schema_revision: str | None = None
    export_date: str | None = None
    export_options: str | None = None
    attributes: JsonDict = field(default_factory=dict)
    source: SourceRef | None = None

    @property
    def revision(self) -> str | None:
        if self.major_rev and self.minor_rev:
            return f"{self.major_rev}.{self.minor_rev}"
        return self.major_rev or self.minor_rev

    def to_dict(self) -> JsonDict:
        data = compact_dict(self)
        if self.revision:
            data["revision"] = self.revision
        return data


@dataclass(slots=True)
class Tag:
    name: str
    scope: str
    data_type: str | None = None
    tag_type: str | None = None
    alias_for: str | None = None
    dimensions: str | None = None
    radix: str | None = None
    external_access: str | None = None
    constant: str | None = None
    description: str | None = None
    data: JsonDict = field(default_factory=dict)
    attributes: JsonDict = field(default_factory=dict)
    source: SourceRef | None = None

    @property
    def qualified_name(self) -> str:
        if self.scope == "Controller":
            return self.name
        return f"{self.scope}.{self.name}"

    def to_dict(self) -> JsonDict:
        data = compact_dict(self)
        data["qualified_name"] = self.qualified_name
        return data


@dataclass(slots=True)
class DataTypeMember:
    name: str
    data_type: str | None = None
    dimensions: str | None = None
    radix: str | None = None
    external_access: str | None = None
    hidden: str | None = None
    target: str | None = None
    description: str | None = None
    attributes: JsonDict = field(default_factory=dict)
    source: SourceRef | None = None

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class DataType:
    name: str
    family: str | None = None
    class_name: str | None = None
    description: str | None = None
    members: list[DataTypeMember] = field(default_factory=list)
    attributes: JsonDict = field(default_factory=dict)
    source: SourceRef | None = None

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class AoiParameter:
    name: str
    data_type: str | None = None
    usage: str | None = None
    required: str | None = None
    visible: str | None = None
    external_access: str | None = None
    description: str | None = None
    attributes: JsonDict = field(default_factory=dict)
    source: SourceRef | None = None

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class ModulePort:
    id: str | None = None
    address: str | None = None
    type: str | None = None
    upstream: str | None = None
    attributes: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class Module:
    name: str
    catalog_number: str | None = None
    vendor: str | None = None
    product_type: str | None = None
    product_code: str | None = None
    major: str | None = None
    minor: str | None = None
    parent: str | None = None
    slot: str | None = None
    address: str | None = None
    description: str | None = None
    ports: list[ModulePort] = field(default_factory=list)
    attributes: JsonDict = field(default_factory=dict)
    source: SourceRef | None = None

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class Rung:
    number: str | None = None
    type: str | None = None
    text: str | None = None
    comment: str | None = None
    source: SourceRef | None = None

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class CrossReference:
    symbol: str
    access: str
    program: str | None = None
    routine: str | None = None
    routine_scope: str | None = None
    language: str | None = None
    instruction: str | None = None
    rung: str | None = None
    line: int | None = None
    text: str | None = None
    confidence: str = "heuristic"
    source: SourceRef | None = None

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class Routine:
    name: str
    program: str | None = None
    language: str | None = None
    type: str | None = None
    text: str | None = None
    rungs: list[Rung] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    references: list[CrossReference] = field(default_factory=list)
    attributes: JsonDict = field(default_factory=dict)
    source: SourceRef | None = None

    @property
    def qualified_name(self) -> str:
        if self.program:
            return f"{self.program}.{self.name}"
        return self.name

    def to_dict(self) -> JsonDict:
        data = compact_dict(self)
        data["qualified_name"] = self.qualified_name
        return data


@dataclass(slots=True)
class Program:
    name: str
    main_routine: str | None = None
    fault_routine: str | None = None
    disabled: str | None = None
    tags: list[Tag] = field(default_factory=list)
    routines: list[Routine] = field(default_factory=list)
    attributes: JsonDict = field(default_factory=dict)
    source: SourceRef | None = None

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class AddOnInstruction:
    name: str
    revision: str | None = None
    vendor: str | None = None
    description: str | None = None
    parameters: list[AoiParameter] = field(default_factory=list)
    local_tags: list[Tag] = field(default_factory=list)
    routines: list[Routine] = field(default_factory=list)
    attributes: JsonDict = field(default_factory=dict)
    source: SourceRef | None = None

    def to_dict(self) -> JsonDict:
        return compact_dict(self)


@dataclass(slots=True)
class LogixProject:
    metadata: ControllerMetadata
    controller_tags: list[Tag] = field(default_factory=list)
    data_types: list[DataType] = field(default_factory=list)
    modules: list[Module] = field(default_factory=list)
    programs: list[Program] = field(default_factory=list)
    add_on_instructions: list[AddOnInstruction] = field(default_factory=list)
    routines: list[Routine] = field(default_factory=list)
    xrefs: list[CrossReference] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return compact_dict(self)

    def symbol_records(self) -> list[JsonDict]:
        records: list[JsonDict] = []
        for tag in self.controller_tags:
            record = tag.to_dict()
            record["kind"] = "tag"
            records.append(record)
        for data_type in self.data_types:
            record = data_type.to_dict()
            record["kind"] = "udt"
            records.append(record)
        for module in self.modules:
            record = module.to_dict()
            record["kind"] = "module"
            records.append(record)
        for program in self.programs:
            record = program.to_dict()
            record["kind"] = "program"
            records.append(record)
            for tag in program.tags:
                tag_record = tag.to_dict()
                tag_record["kind"] = "tag"
                records.append(tag_record)
        for aoi in self.add_on_instructions:
            record = aoi.to_dict()
            record["kind"] = "aoi"
            records.append(record)
            for tag in aoi.local_tags:
                tag_record = tag.to_dict()
                tag_record["kind"] = "tag"
                records.append(tag_record)
        return records

    def routine_records(self) -> list[JsonDict]:
        return [routine.to_dict() for routine in self.routines]

    def xref_records(self) -> list[JsonDict]:
        return [xref.to_dict() for xref in self.xrefs]
