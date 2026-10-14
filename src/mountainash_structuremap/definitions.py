from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scalar(Declaration):
    kind: Literal["scalar"] = "scalar"
    type: Literal[
        "str", "int", "float", "bool", "decimal", "date", "time", "datetime",
        "duration", "period", "binary", "qname", "enum"
    ]
    native_type: str | None = None


class XmlField(Declaration):
    kind: str
    name: str | None = None
    namespace: str | None = None
    wrapper: str | None = None
    wrapper_namespace: str | None = None
    nillable: bool = False
    tokens: bool = False
    mixed: bool = False
    sequence: int | None = None
    format: str | None = None
    process_contents: str | None = None


class RecordRef(Declaration):
    kind: Literal["record"] = "record"
    definition: str


class Sequence(Declaration):
    kind: Literal["sequence"] = "sequence"
    item: Shape
    container: Literal["list", "tuple"] = "list"


class Map(Declaration):
    kind: Literal["map"] = "map"
    key: Shape
    value: Shape


class Positional(Declaration):
    kind: Literal["positional"] = "positional"
    items: tuple[Shape, ...]


class Nullable(Declaration):
    kind: Literal["nullable"] = "nullable"
    value: Shape


class Alternatives(Declaration):
    kind: Literal["alternatives"] = "alternatives"
    choices: tuple[Shape, ...]


Shape = Annotated[
    Scalar | RecordRef | Sequence | Map | Positional | Nullable | Alternatives, Field(discriminator="kind")
]


class Member(Declaration):
    name: str
    shape: Shape
    default_kind: Literal["required", "value", "factory"] = "required"
    xml: XmlField | None = None


class RecordDefinition(Declaration):
    name: str
    members: tuple[Member, ...]


class StructureSpec(Declaration):
    root: str
    definitions: dict[str, RecordDefinition]


class DerivedModel(BaseModel, Generic[T]):
    structure: StructureSpec
    native_types: dict[str, type]
    binding: str


__all__ = [
    "Alternatives",
    "DerivedModel",
    "Map",
    "Member",
    "Nullable",
    "Positional",
    "RecordDefinition",
    "RecordRef",
    "Scalar",
    "Sequence",
    "Shape",
    "StructureSpec",
    "XmlField",
]

