from dataclasses import dataclass, field, fields
from datetime import date

from xsdata.models.datatype import XmlDate

from mountainash_structuremap import StructureMapError
from tests.fixtures.execution_models import Entry
from tests.fixtures.external_mapping import DatePartsCodec
from tests.fixtures.xml_fragments import Mixed
from tests.fixtures.xml_models import TextValue
from tests.fixtures.xml_variants import SelectedNil


@dataclass
class WrappedDocument:
    class Meta:
        name = "document"

    label: str = field(metadata={"type": "Attribute"})
    entries: list[Entry] = field(
        default_factory=list,
        metadata={
            "type": "Element",
            "name": "entry",
            "wrapper": "entries",
        },
    )
    footer: str = field(default="", metadata={"type": "Element"})


@dataclass
class Group:
    code: str = field(metadata={"type": "Attribute"})
    entries: list[Entry] = field(default_factory=list, metadata={"type": "Element", "name": "entry"})
    footer: str = field(default="default-group-footer", metadata={"type": "Element"})


@dataclass
class NestedDocument:
    class Meta:
        name = "nested"

    label: str = field(metadata={"type": "Attribute"})
    group: Group = field(metadata={"type": "Element"})
    footer: str = field(default="default-footer", metadata={"type": "Element"})


@dataclass
class GroupedDocument:
    class Meta:
        name = "grouped"

    label: str = field(metadata={"type": "Attribute"})
    groups: list[Group] = field(default_factory=list, metadata={"type": "Element", "name": "group"})
    footer: str = field(default="default-footer", metadata={"type": "Element"})


@dataclass
class NillableGroups:
    class Meta:
        name = "nillable-groups"

    groups: list[Group] = field(
        default_factory=list,
        metadata={
            "type": "Element",
            "name": "group",
            "nillable": True,
        },
    )


@dataclass
class HeaderDocument:
    class Meta:
        name = "headers"

    label: str = field(metadata={"type": "Attribute"})
    before: TextValue = field(metadata={"type": "Element"})
    entries: list[Entry] = field(default_factory=list, metadata={"type": "Element", "name": "entry"})
    after: TextValue | None = field(default=None, metadata={"type": "Element"})
    footer: str = field(default="native-default", metadata={"type": "Element"})


@dataclass
class CompleteRecord:
    code: str = field(metadata={"type": "Attribute"})
    title: TextValue = field(metadata={"type": "Element"})
    entries: list[Entry] = field(default_factory=list, metadata={"type": "Element", "name": "entry"})
    fragment: Mixed | None = field(default=None, metadata={"type": "Element"})


@dataclass
class CompleteDocument:
    class Meta:
        name = "complete"

    records: list[CompleteRecord] = field(default_factory=list, metadata={"type": "Element", "name": "record"})


@dataclass
class CollisionEnvelope:
    class Meta:
        name = "collision"

    before: Group = field(metadata={"type": "Element", "name": "section"})
    records: list[Entry] = field(
        default_factory=list,
        metadata={
            "type": "Element",
            "name": "entry",
            "namespace": "urn:selected",
        },
    )
    foreign: list[Entry] = field(
        default_factory=list,
        metadata={
            "type": "Element",
            "name": "entry",
            "namespace": "urn:foreign",
        },
    )


@dataclass
class DatedRecord:
    issued: XmlDate = field(metadata={"type": "Element"})


@dataclass
class DatedDocument:
    class Meta:
        name = "dated"

    records: list[DatedRecord] = field(default_factory=list, metadata={"type": "Element", "name": "record"})


class XmlDatePartsCodec(DatePartsCodec):
    def encode(self, value):
        if not isinstance(value, XmlDate) or value.offset is not None:
            raise StructureMapError("native_type_mismatch", "Expected a timezone-free XML date")
        return super().encode(date(value.year, value.month, value.day))

    def decode(self, checked):
        return XmlDate(checked.year, checked.month, checked.day)


class XmlDateParts:
    identity = "example.xml-date-parts.v1"
    directions = frozenset({"decompose", "reconstruct"})

    def prepare(self, use, context):
        context.value(codec=XmlDatePartsCodec(tuple(use.config["columns"])))


@dataclass
class NullableSelectedNil(SelectedNil):
    values: list[object | None] = field(default_factory=list, metadata=fields(SelectedNil)[0].metadata)


@dataclass
class CollectionHeader:
    entries: list[Entry] = field(default_factory=list, metadata={"type": "Element", "name": "entry"})


@dataclass
class NestedKeyEnvelope:
    class Meta:
        name = "envelope"

    before: CollectionHeader = field(metadata={"type": "Element"})
    group: Group = field(metadata={"type": "Element"})


@dataclass
class WrappedEntryGroup:
    code: str = field(metadata={"type": "Attribute"})
    entries: list[Entry] = field(
        default_factory=list,
        metadata={"type": "Element", "name": "entry", "wrapper": "entries"},
    )
    footer: str = field(default="default-group-footer", metadata={"type": "Element"})


@dataclass
class WrappedGroups:
    class Meta:
        name = "wrapped-groups"

    groups: list[WrappedEntryGroup] = field(
        default_factory=list,
        metadata={"type": "Element", "name": "group", "wrapper": "groups"},
    )
