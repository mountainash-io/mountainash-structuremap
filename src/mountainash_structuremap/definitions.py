from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)



class NumericFacets(Declaration):
    total_digits: int | None = Field(default=None, gt=0)
    fraction_digits: int | None = Field(default=None, ge=0)
    min_inclusive: str | None = None
    max_inclusive: str | None = None
    min_exclusive: str | None = None
    max_exclusive: str | None = None

    @model_validator(mode="after")
    def _digits_are_consistent(self) -> NumericFacets:
        if (
            self.total_digits is not None
            and self.fraction_digits is not None
            and self.fraction_digits > self.total_digits
        ):
            raise ValueError("fraction_digits cannot exceed total_digits")
        return self

class Scalar(Declaration):
    kind: Literal["scalar"] = "scalar"
    type: Literal[
        "str", "int", "float", "bool", "decimal", "date", "time", "datetime",
        "duration", "period", "binary", "qname", "enum"
    ]
    native_type: str | None = None
    facets: NumericFacets | None = None
    format: str | None = None

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
    choices: tuple["Alternative", ...]


Shape = Annotated[
    Scalar | RecordRef | Sequence | Map | Positional | Nullable | Alternatives, Field(discriminator="kind")
]


class Alternative(Declaration):
    key: str
    shape: Shape
    xml: XmlField | None = None


Alternatives.model_rebuild()



class Member(Declaration):
    name: str
    shape: Shape
    default_kind: Literal["required", "value", "factory"] = "required"
    xml: XmlField | None = None


class RecordDefinition(Declaration):
    name: str
    members: tuple[Member, ...]
    xml: XmlField | None = None


class StructureSpec(Declaration):
    root: str
    definitions: dict[str, RecordDefinition]


class DerivedModel(BaseModel, Generic[T]):
    structure: StructureSpec
    native_types: dict[str, type]
    binding: str


__all__ = [
    "Alternative",
    "NumericFacets",
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

