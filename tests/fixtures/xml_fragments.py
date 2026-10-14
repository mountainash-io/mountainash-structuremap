from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from tests.fixtures.xml_models import NS, CountedDocument


@dataclass
class TypedFragment:
    class Meta:
        name = "typedFragment"
        namespace = "urn:fragment:typed"

    constructions: ClassVar[int] = 0
    should_fail: ClassVar[bool] = False
    value: str = field(metadata={"type": "Element"})

    def __post_init__(self) -> None:
        type(self).constructions += 1
        if type(self).should_fail:
            raise RuntimeError("typed fragment constructor failed")


@dataclass
class Mixed:
    class Meta:
        name = "mixed"
        namespace = NS

    content: list[object] = field(default_factory=list, metadata={
        "type": "Wildcard", "namespace": "##any", "mixed": True,
    })


@dataclass
class AttributeWildcard:
    class Meta:
        name = "attributed"
        namespace = NS

    attributes: dict[str, str] = field(default_factory=dict, metadata={
        "type": "Attributes", "namespace": "##any", "process_contents": "skip",
    })


@dataclass
class MixedSibling:
    class Meta:
        name = "mixedSibling"
        namespace = NS

    sibling: CountedDocument = field(metadata={"type": "Element", "name": "counted", "namespace": NS})
    content: list[object] = field(default_factory=list, metadata={
        "type": "Wildcard", "namespace": "##any",
    })


@dataclass
class FragmentChoice:
    class Meta:
        name = "fragmentChoice"
        namespace = NS

    branches: list[CountedDocument | Mixed] = field(default_factory=list, metadata={
        "type": "Elements",
        "choices": (
            {"name": "record", "type": CountedDocument},
            {"name": "open", "type": Mixed},
        ),
    })


@dataclass
class NamedMixed:
    class Meta:
        name = "namedMixed"
        namespace = NS

    content: list[object] = field(default_factory=list, metadata={
        "type": "Wildcard", "namespace": "##any", "mixed": True,
        "choices": ({"name": "selected", "type": TypedFragment},),
        "process_contents": "strict",
    })


@dataclass
class OptionalWildcard:
    class Meta:
        name = "optional"
        namespace = NS

    content: object | None = field(default=None, metadata={
        "type": "Wildcard", "namespace": "##any", "process_contents": "skip",
    })


@dataclass
class WrappedWildcard:
    class Meta:
        name = "wrapped"
        namespace = NS

    content: list[object] = field(default_factory=list, metadata={
        "type": "Wildcard", "namespace": "##any", "wrapper": "items", "process_contents": "skip",
    })


@dataclass
class ObjectMixed:
    class Meta:
        name = "objectMixed"
        namespace = NS

    content: object = field(default_factory=list, metadata={
        "type": "Wildcard", "namespace": "##any", "mixed": True,
    })


@dataclass
class CompoundWildcard:
    class Meta:
        name = "compound"
        namespace = NS

    content: list[object] = field(default_factory=list, metadata={
        "type": "Elements", "choices": (
            {"name": "known", "type": TypedFragment},
            {"wildcard": True, "type": object, "namespace": "urn:outside", "process_contents": "skip"},
        ),
    })
