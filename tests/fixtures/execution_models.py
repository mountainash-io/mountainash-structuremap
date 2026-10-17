from dataclasses import dataclass, field

from mountainash_structuremap import MappingSpec, UseOverride
from mountainash_structuremap.mapping import TypeCast


@dataclass
class Entry:
    value: str = field(metadata={"type": "Element"})


@dataclass
class Document:
    class Meta:
        name = "document"

    label: str = field(metadata={"type": "Attribute"})
    entries: list[Entry] = field(default_factory=list, metadata={"type": "Element", "name": "entry"})
    footer: str = field(default="", metadata={"type": "Element"})


def execution_mapping():
    return MappingSpec(overrides={"entries/value": UseOverride(cast=TypeCast(to="i8"))})
