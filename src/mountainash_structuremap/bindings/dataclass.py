from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import MISSING, Field, fields, is_dataclass
from inspect import signature
from types import ModuleType, UnionType
from typing import Any, Literal, Tuple, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from mountainash_structuremap.bindings.contracts import CapabilityReport
from mountainash_structuremap.codecs import PrimitiveCodec
from mountainash_structuremap.definitions import (
    Alternatives,
    Map,
    Member,
    Nullable,
    Positional,
    RecordDefinition,
    RecordRef,
    Scalar,
    Sequence,
    Shape,
)
from mountainash_structuremap.errors import StructureMapError

_SCALARS: dict[type, Literal["str", "int", "float", "bool"]] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
}


class DataclassBinding:
    capabilities = CapabilityReport()

    def describe_record(self, cls: type, register: Callable[[type, str], str], *, path: str) -> RecordDefinition:
        if issubclass(cls, BaseModel):
            raise StructureMapError(
                "unavailable_binding", "This class requires the Pydantic binding", path=path,
                owner="binding gap", remedy="The native Pydantic binding is not available in this delivery",
            )
        declared = record_fields(cls, path=path)
        field_names = {item.name for item in declared}
        hints: dict[str, object] = {}
        try:
            for base in reversed(cls.__mro__):
                annotations = {name: value for name, value in vars(base).get("__annotations__", {}).items()
                               if name in field_names}
                if not annotations:
                    continue
                scope = ModuleType(base.__module__)
                scope.__annotations__ = annotations
                module = sys.modules.get(base.__module__)
                lexical_scope = base.__qualname__.rpartition(".")[0]
                known = {candidate.__name__: candidate for candidate in getattr(self, "_native_types", {}).values()
                         if lexical_scope and candidate.__module__ == base.__module__
                         and candidate.__qualname__.rpartition(".")[0] == lexical_scope}
                hints.update(get_type_hints(scope, globalns=vars(module) if module else {},
                                            localns={**known, **vars(base), base.__name__: base}))
        except Exception as exc:
            raise StructureMapError("annotation_resolution_failed", "Could not resolve dataclass annotations",
                                    path=path, owner="binding gap",
                                    remedy="Make forward-referenced classes available in their declaring module or class namespace") from exc
        members = []
        for item in declared:
            member_path = "/".join(filter(None, (path, item.name)))
            default: Literal["factory", "value", "required"] = (
                "factory" if item.default_factory is not MISSING else "value" if item.default is not MISSING else "required"
            )
            members.append(Member(name=item.name, shape=describe_annotation(hints[item.name], member_path, register),
                                  default_kind=default))
        return RecordDefinition(name=f"{cls.__module__}.{cls.__qualname__}", members=tuple(members))

    def prepare_record(self, cls: type, definition: RecordDefinition, *, path: str) -> DataclassRecordAdapter:
        validate_constructor(cls, tuple(member.name for member in definition.members), path=path)
        return DataclassRecordAdapter(cls)

    def prepare_scalar(self, declaration: Scalar, *, native_type: type | None, format: str | None,
                       column: str, policy: str | None, path: str) -> PrimitiveCodec:
        if declaration.type not in ("str", "int", "float", "bool"):
            raise StructureMapError("unavailable_scalar", f"Scalar {declaration.type!r} is unavailable",
                                    path=path, owner="binding gap", remedy="Use a supported primitive scalar")
        return PrimitiveCodec(declaration.type, column, path)





class DataclassRecordAdapter:
    def __init__(self, native_type: type):
        self.native_type = native_type

    def read(self, instance: object, member: str, *, path: str) -> object:
        try:
            return getattr(instance, member)
        except Exception as exc:
            raise StructureMapError(
                "native_read_failed", "Could not read dataclass member", path=path,
                owner="native limitation", remedy="Provide a readable dataclass member",
            ) from exc

    def construct(self, values: dict[str, object], *, path: str) -> object:
        try:
            return self.native_type(**values)
        except Exception as exc:
            raise StructureMapError(
                "native_construct_failed", "Dataclass construction failed", path=path,
                owner="native limitation", remedy="Ensure supplied values satisfy the native constructor",
            ) from exc


def record_fields(cls: type, *, path: str) -> tuple[Field[Any], ...]:
    if not is_dataclass(cls):
        raise StructureMapError(
            "unsupported_binding",
            "Expected a dataclass record",
            path=path,
            owner="binding gap",
            remedy="Pass a plain dataclass class",
        )
    return fields(cls)


def validate_constructor(cls: type, member_names: tuple[str, ...], *, path: str) -> None:
    mapped = set(member_names)
    for item in fields(cls):
        if item.name in mapped and not item.init:
            raise StructureMapError(
                "unsupported_constructor",
                "Mapped field does not participate in construction",
                path="/".join(filter(None, (path, item.name))),
                owner="native limitation",
                remedy="Use a constructor-participating field; custom construction is unavailable in Foundation",
            )
    try:
        signature(cls).bind(**dict.fromkeys(member_names))
    except (TypeError, ValueError) as exc:
        raise StructureMapError(
            "unsupported_constructor",
            f"Mapped fields cannot satisfy the native constructor: {exc}",
            path=path,
            owner="native limitation",
            remedy="Use a constructor accepting the mapped field keywords, with defaults for constructor-only arguments",
        ) from exc


def describe_annotation(
    annotation: object,
    path: str,
    register: Callable[[type, str], str],
) -> Shape:
    origin, args = get_origin(annotation), get_args(annotation)
    if isinstance(annotation, type) and annotation in _SCALARS:
        return Scalar(type=_SCALARS[annotation])
    if isinstance(annotation, type) and is_dataclass(annotation):
        return RecordRef(definition=register(annotation, path))
    if origin in (Union, UnionType):
        choices = tuple(describe_annotation(arg, path, register) for arg in args if arg is not type(None))
        shape: Shape = choices[0] if len(choices) == 1 else Alternatives(choices=choices)
        return Nullable(value=shape) if type(None) in args else shape
    if origin is list and len(args) == 1:
        return Sequence(item=describe_annotation(args[0], path, register))
    if origin is dict and len(args) == 2:
        return Map(
            key=describe_annotation(args[0], path, register),
            value=describe_annotation(args[1], path, register),
        )
    if origin is tuple and annotation is not Tuple and (not args or args[-1] is not Ellipsis):
        return Positional(items=tuple(describe_annotation(arg, path, register) for arg in args))
    raise StructureMapError(
        "unsupported_annotation",
        f"Unsupported annotation {annotation!r}",
        path=path,
        owner="binding gap",
        remedy="Use a supported scalar or dataclass annotation",
    )
