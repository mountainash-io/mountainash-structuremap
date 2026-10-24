from __future__ import annotations

from typing import Literal, Mapping, Protocol

from mountainash.typespec.spec import FieldDefinition

from mountainash_structuremap.codecs import DecodingCodec, EncodingCodec
from mountainash_structuremap.definitions import (
    Alternatives,
    Map,
    Nullable,
    Positional,
    RecordRef,
    Scalar,
    Sequence,
    Shape,
)
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import MappingNode, MappingUse, UseOverride

Direction = Literal["decompose", "reconstruct"]


class MappingComponent(Protocol):
    identity: str
    directions: frozenset[Direction]

    def prepare(self, use: MappingUse, context: PreparationContext) -> None: ...


class PreparationContext(Protocol):
    shape: Shape
    path: str
    table: str
    directions: frozenset[Direction]

    def field(self, column: str, *, table: str | None = None) -> FieldDefinition: ...
    def apply(self, node: MappingNode) -> None: ...
    def value(self, *, codec: EncodingCodec | DecodingCodec | None = None,
              options: UseOverride | None = None) -> None: ...
    def record(self, children: Mapping[str, MappingNode], *, options: UseOverride | None = None) -> None: ...
    def sequence(self, item: MappingNode | None = None, *, options: UseOverride | None = None) -> None: ...
    def map(self, *, key: MappingNode | None = None, value: MappingNode | None = None,
            options: UseOverride | None = None) -> None: ...
    def positional(self, items: Mapping[int, MappingNode], *, options: UseOverride | None = None) -> None: ...
    def alternatives(self, branches: Mapping[str, MappingNode], *, options: UseOverride | None = None) -> None: ...


def _shape(shape: Shape) -> Shape:
    while isinstance(shape, Nullable):
        shape = shape.value
    return shape


def _config(use: MappingUse, path: str) -> UseOverride | None:
    if not use.config:
        return None
    try:
        return UseOverride.model_validate(use.config)
    except Exception as exc:
        raise StructureMapError("invalid_component", f"Invalid configuration for {use.component!r}: {exc}", path=path,
                                remedy="Use only valid UseOverride fields and values") from exc


def _children(use: MappingUse, allowed: set[str], path: str) -> None:
    wrong = sorted(set(use.children) - allowed)
    if wrong:
        details = ", ".join(f"unexpected {name!r}" for name in wrong)
        raise StructureMapError("invalid_component", f"Invalid children for {use.component!r}: {details}", path=path,
                                remedy=f"Use child keys from {sorted(allowed)!r}")


class _CoreComponent:
    directions: frozenset[Direction] = frozenset({"decompose", "reconstruct"})

    def __init__(self, identity: str):
        self.identity = identity

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        shape, options = _shape(context.shape), _config(use, context.path)
        children = use.children
        if self.identity == "core.value":
            if not isinstance(shape, Scalar) or children:
                raise StructureMapError("invalid_component", "core.value requires a scalar and no children", path=context.path,
                                        remedy="Apply core.value to a scalar shape without children")
            context.value(options=options)
        elif self.identity == "core.record":
            if not isinstance(shape, RecordRef):
                raise StructureMapError("invalid_component", "core.record requires a record shape", path=context.path,
                                        remedy="Apply core.record to a record")
            context.record(children, options=options)
        elif self.identity == "core.sequence":
            if not isinstance(shape, Sequence):
                raise StructureMapError("invalid_component", "core.sequence requires a sequence shape", path=context.path,
                                        remedy="Apply core.sequence to a sequence")
            _children(use, {"item"}, context.path)
            context.sequence(children.get("item"), options=options)
        elif self.identity == "core.map":
            if not isinstance(shape, Map):
                raise StructureMapError("invalid_component", "core.map requires a map shape", path=context.path,
                                        remedy="Apply core.map to a map")
            _children(use, {"key", "value"}, context.path)
            context.map(key=children.get("key"), value=children.get("value"), options=options)
        elif self.identity == "core.positional":
            if not isinstance(shape, Positional):
                raise StructureMapError("invalid_component", "core.positional requires a positional shape", path=context.path,
                                        remedy="Apply core.positional to a fixed positional shape")
            try:
                items = {int(key): node for key, node in children.items()}
            except ValueError as exc:
                raise StructureMapError("invalid_component", "Positional child keys must be numeric strings", path=context.path,
                                        remedy="Use child keys '0', '1', and so on") from exc
            _children(use, {str(index) for index in range(len(shape.items))}, context.path)
            context.positional(items, options=options)
        elif self.identity == "core.alternatives":
            if not isinstance(shape, Alternatives):
                raise StructureMapError("invalid_component", "core.alternatives requires alternatives", path=context.path,
                                        remedy="Apply core.alternatives to an alternatives shape")
            _children(use, {choice.key for choice in shape.choices}, context.path)
            context.alternatives(children, options=options)


_COMPONENTS = tuple(_CoreComponent(identity) for identity in (
    "core.value", "core.record", "core.sequence", "core.map", "core.positional", "core.alternatives"
))


def builtin_components() -> tuple[MappingComponent, ...]:
    return _COMPONENTS


def default_component(shape: Shape) -> MappingComponent:
    shape = _shape(shape)
    identities = {
        Scalar: "core.value", RecordRef: "core.record", Sequence: "core.sequence", Map: "core.map",
        Positional: "core.positional", Alternatives: "core.alternatives",
    }
    try:
        identity = identities[type(shape)]
    except KeyError as exc:
        raise StructureMapError("invalid_component", f"No built-in component for shape {type(shape).__name__}",
                                remedy="Declare a component that accepts this shape") from exc
    return next(component for component in builtin_components() if component.identity == identity)


__all__ = ["MappingComponent", "PreparationContext", "builtin_components"]
