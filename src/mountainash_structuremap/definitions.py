from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scalar(Declaration):
    kind: Literal["scalar"] = "scalar"
    type: Literal["str", "int", "float", "bool"]


class RecordRef(Declaration):
    kind: Literal["record"] = "record"
    definition: str


class Sequence(Declaration):
    kind: Literal["sequence"] = "sequence"
    item: Shape


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
]
