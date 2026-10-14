from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

NS = "urn:structuremap:test"


@dataclass
class TextDocument:
    class Meta:
        name = "document"
        namespace = NS

    label: str = field(metadata={"type": "Attribute"})
    values: list[str] = field(default_factory=list, metadata={
        "type": "Element", "name": "value", "wrapper": "values",
    })


@dataclass
class AmbiguousDocument:
    choices: list[object] = field(default_factory=list, metadata={
        "type": "Elements", "choices": (
            {"name": "left", "type": str},
            {"name": "right", "type": str},
        ),
    })


@dataclass(frozen=True, slots=True, kw_only=True)
class EmptyChild:
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class CollisionDocument:
    class Meta:
        name = "collision"
        namespace = NS

    attribute: str = field(metadata={"type": "Attribute", "name": "same"})
    element: str = field(metadata={"type": "Element", "name": "same"})
    foreign: str = field(metadata={"type": "Element", "name": "same", "namespace": "urn:foreign"})
    empty: EmptyChild = field(metadata={"type": "Element"})


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class TokenDocument:
    class Meta:
        name = "tokens"

    words: list[str] = field(metadata={"type": "Element", "tokens": True})
    colour: Colour = field(metadata={"type": "Attribute"})


@dataclass
class CountedDocument:
    class Meta:
        name = "counted"

    constructions: ClassVar[int] = 0
    factories: ClassVar[int] = 0

    @staticmethod
    def fresh() -> str:
        CountedDocument.factories += 1
        return "factory-value"

    value: str = field(default_factory=fresh, metadata={"type": "Element"})

    def __post_init__(self):
        type(self).constructions += 1


@dataclass
class BooleanDocument:
    class Meta:
        name = "boolean"

    value: bool = field(metadata={"type": "Element"})


@dataclass
class CountedEnumDocument:
    child: CountedDocument = field(metadata={"type": "Element"})
    colour: Colour = field(metadata={"type": "Attribute"})


@dataclass(frozen=True, slots=True, kw_only=True)
class InheritedDocument(CollisionDocument):
    note: str | None = field(default=None, metadata={"type": "Element", "nillable": True})


@dataclass
class TextValue:
    class Meta:
        name = "text"

    value: str = field(metadata={"type": "Text"})
    language: str = field(metadata={"type": "Attribute", "name": "lang"})


@dataclass
class EnumTokens:
    class Meta:
        name = "enum-tokens"

    colours: list[Colour] = field(metadata={"type": "Element", "tokens": True})


@dataclass
class NillableItems:
    class Meta:
        name = "nillable-items"

    values: list[str] = field(default_factory=list, metadata={"type": "Element", "name": "value", "nillable": True})


@dataclass
class TupleTokens:
    class Meta:
        name = "tuple-tokens"

    words: tuple[str] = field(metadata={"type": "Element", "tokens": True})


class Measurement(Enum):
    UNKNOWN = float("nan")
    KNOWN = 1.0


@dataclass
class Measured:
    child: CountedDocument = field(metadata={"type": "Element"})
    reading: Measurement = field(metadata={"type": "Attribute"})


@dataclass
class PlainEnvelope:
    child: TextValue
