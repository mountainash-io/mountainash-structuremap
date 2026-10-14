from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

NS = "urn:structuremap:variants"
DOG = f"{{{NS}}}dog"
CAT = f"{{{NS}}}cat"


@dataclass
class Dog:
    calls: ClassVar[int] = 0
    name: str = field(metadata={"type": "Attribute"})

    def __post_init__(self):
        type(self).calls += 1


@dataclass
class Cat:
    name: str = field(metadata={"type": "Attribute"})


@dataclass
class Pets:
    class Meta:
        name = "pets"
        namespace = NS

    pets: list[object] = field(default_factory=list, metadata={
        "type": "Elements", "choices": (
            {"name": "dog", "namespace": NS, "type": Dog},
            {"name": "cat", "namespace": NS, "type": Cat},
        ),
    })


@dataclass
class BooleanOrInteger:
    values: list[bool | int | None]


@dataclass
class Leaf:
    label: str | None


@dataclass
class Branch:
    label: str
    children: list[Branch | Leaf] = field(default_factory=list)


@dataclass
class NullablePets:
    pets: list[AllNullDog | AllNullCat | None]


@dataclass
class AllNullDog:
    label: str | None


@dataclass
class AllNullCat:
    label: str | None


@dataclass
class AllNullPets:
    pets: list[AllNullDog | AllNullCat]


@dataclass
class VariantUses:
    first: list[Branch | Leaf]
    second: list[Branch | Leaf]


@dataclass
class RecursiveChoice:
    children: list[RecursiveChoice | Leaf]


@dataclass
class VariantBranch:
    label: str
    children: list[VariantBranch | Leaf] = field(default_factory=list)


@dataclass
class DogWithChildren:
    label: str
    children: list[Leaf]


@dataclass
class ChoiceEnvelope:
    items: list[DogWithChildren | Leaf]


@dataclass
class Null:
    label: str


@dataclass
class NullAlternativePets:
    pets: list[Null | AllNullDog | None]


@dataclass
class MapVariants:
    values: dict[str, Branch | Leaf | None]

@dataclass
class SplitDetail:
    values: list[int | str]


@dataclass
class NullableDetailRoot:
    detail: SplitDetail | None


@dataclass
class OverrideX:
    other: str


@dataclass
class OverrideY:
    label: str


@dataclass
class OverrideA:
    detail: OverrideX


@dataclass
class OverrideB:
    detail: OverrideY


@dataclass
class OverrideRoot:
    items: list[OverrideA | OverrideB]


@dataclass
class OverrideRootReversed:
    items: list[OverrideB | OverrideA]


@dataclass
class SharedTags:
    tags: list[str]


@dataclass
class SharedA:
    shared: SharedTags


@dataclass
class SharedB:
    shared: SharedTags


@dataclass
class SharedRoot:
    item: SharedA | SharedB


@dataclass
class SharedChoices:
    values: list[int | str | None]


@dataclass
class NestedA:
    shared: SharedChoices


@dataclass
class NestedB:
    shared: SharedChoices


@dataclass
class NestedVariants:
    item: NestedA | NestedB
