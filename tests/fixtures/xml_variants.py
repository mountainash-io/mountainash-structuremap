from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Animal:
    class Meta:
        name = "AnimalType"
        namespace = "urn:animals"

    name: str = field(metadata={"type": "Attribute"})


@dataclass
class Hound(Animal):
    class Meta:
        name = "HoundType"
        namespace = "urn:animals"

    scent: int = field(default=0, metadata={"type": "Attribute"})


@dataclass
class TypedAnimal:
    animal: Animal = field(metadata={"type": "Element", "namespace": "urn:animals"})


@dataclass
class EmptyA:
    text: str | None = field(default=None, metadata={"type": "Element"})


@dataclass
class EmptyB:
    text: str | None = field(default=None, metadata={"type": "Element"})


@dataclass
class SelectedNil:
    values: list[object] = field(default_factory=list, metadata={"type": "Elements", "choices": (
        {"name": "a", "namespace": "urn:a", "type": EmptyA, "nillable": True},
        {"name": "b", "namespace": "urn:b", "type": EmptyB, "nillable": True},
    )})


@dataclass
class LocalChoices:
    values: list[object] = field(default_factory=list, metadata={"type": "Elements", "choices": (
        {"name": "item", "namespace": "urn:a", "type": EmptyA},
        {"name": "item", "namespace": "urn:b", "type": EmptyB},
    )})


@dataclass
class InheritedChoices:
    class Meta:
        namespace = "urn:inherited"

    values: list[object] = field(default_factory=list, metadata={"type": "Elements", "choices": (
        {"name": "a", "type": EmptyA},
        {"name": "b", "type": EmptyB},
    )})


@dataclass
class AnimalChoices:
    values: list[object] = field(default_factory=list, metadata={"type": "Elements", "choices": (
        {"name": "animal", "namespace": "urn:animals", "type": Animal},
        {"name": "other", "namespace": "urn:other", "type": EmptyB},
    )})


@dataclass
class TokenChoices:
    values: list[object] = field(default_factory=list, metadata={"type": "Elements", "choices": (
        {"name": "ints", "type": list[int], "tokens": True, "nillable": True},
        {"name": "words", "type": list[str], "tokens": True},
    )})


@dataclass
class LocalUnionA:
    class Meta:
        global_type = False

    a: str = field(default="", metadata={"type": "Attribute"})


@dataclass
class LocalUnionB:
    class Meta:
        global_type = False

    b: str = field(default="", metadata={"type": "Attribute"})


@dataclass
class LocalUnion:
    value: LocalUnionA | LocalUnionB = field(metadata={"type": "Element"})


@dataclass
class SiblingAnimal:
    class Meta:
        name = "SiblingAnimalType"
        namespace = "urn:animals"


@dataclass
class SiblingHound(SiblingAnimal):
    class Meta:
        name = "SiblingHoundType"
        namespace = "urn:animals"

    scent: int = field(default=0, metadata={"type": "Attribute"})


@dataclass
class SiblingCat(SiblingAnimal):
    class Meta:
        name = "SiblingCatType"
        namespace = "urn:animals"

    lives: int = field(default=9, metadata={"type": "Attribute"})


@dataclass
class SiblingType:
    animal: SiblingHound = field(metadata={"type": "Element", "namespace": "urn:animals"})
