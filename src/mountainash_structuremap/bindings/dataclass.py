from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import MISSING, Field, fields, is_dataclass
from datetime import date
from decimal import Decimal
from inspect import signature
from types import ModuleType, UnionType
from typing import Annotated, Any, ForwardRef, Literal, Tuple, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from mountainash_structuremap.bindings.contracts import CapabilityReport, VariantAdapter
from mountainash_structuremap.codecs import PrimitiveCodec, ScalarCodec
from mountainash_structuremap.definitions import (
    Alternative,
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
from mountainash_structuremap.native_scalars import prepare_native_scalar

_SCALARS: dict[type, Literal["str", "int", "float", "bool"]] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
}


class DataclassBinding:
    name = "dataclass"
    capabilities = CapabilityReport()

    def describe_root(self, annotation: object, register: Callable[[type, str], str], *, path: str) -> Shape:
        if isinstance(annotation, type) and is_dataclass(annotation):
            return RecordRef(definition=register(annotation, path))
        if get_origin(annotation) in (list, tuple):
            shape = describe_annotation(annotation, path, register)
            if isinstance(shape, Sequence):
                return shape
        raise StructureMapError(
            "unsupported_root", "Dataclass roots require a record or typed sequence",
            path=path, owner="binding gap",
            remedy="Use a dataclass record or list[T]/tuple[T, ...] with binding='dataclass'",
        )

    def describe_record(
        self, cls: type, register: Callable[[type, str], str], *, path: str,
        record_predicate: Callable[[type], bool] = is_dataclass,
    ) -> RecordDefinition:
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
            members.append(Member(name=item.name, shape=describe_annotation(hints[item.name], member_path, register,
                                                                              record_predicate=record_predicate),
                                  default_kind=default))
        return RecordDefinition(name=f"{cls.__module__}.{cls.__qualname__}", members=tuple(members))

    def prepare_record(self, cls: type, definition: RecordDefinition, *, path: str) -> DataclassRecordAdapter:
        validate_constructor(cls, tuple(member.name for member in definition.members), path=path)
        return DataclassRecordAdapter(cls)
    def prepare_scalar(
        self,
        declaration: Scalar,
        *,
        native_type: type | None,
        format: str | None,
        column: str,
        policy: str | None,
        metadata_namespace: str = "_sm_",
        path: str,
        field_context: tuple[type, str, str | None] | None = None,
    ) -> ScalarCodec:
        if declaration.type in ("decimal", "date"):
            return prepare_native_scalar(declaration, column, policy, path)
        if declaration.type not in ("str", "int", "float", "bool"):
            raise StructureMapError("unavailable_scalar", f"Scalar {declaration.type!r} is unavailable",
                                    path=path, owner="binding gap", remedy="Use a primitive, Decimal or date scalar")
        return PrimitiveCodec(declaration.type, column, path, allowed_values=declaration.allowed_values)

    def prepare_variant(
        self,
        alternatives: tuple[Alternative, ...],
        *,
        native_types: Mapping[str, type],
        namespace: str | None = None,
        path: str,
    ) -> VariantAdapter:
        types: dict[str, type] = {}
        for alternative in alternatives:
            shape = alternative.shape
            while isinstance(shape, Nullable):
                shape = shape.value
            native_type: type | None = None
            if isinstance(shape, RecordRef):
                native_type = native_types.get(shape.definition)
            elif isinstance(shape, Scalar):
                native_type = native_types.get(shape.native_type) if shape.native_type else {
                    "str": str,
                    "bool": bool,
                    "int": int,
                    "float": float,
                }.get(shape.type)
            if native_type is None:
                raise StructureMapError(
                    "unsupported_variant",
                    f"Cannot associate alternative {alternative.key!r} with a native dataclass type",
                    path=path,
                    owner="binding gap",
                    remedy="Use distinct supported dataclass or primitive alternatives",
                )
            if native_type in types.values():
                previous = next(key for key, value in types.items() if value is native_type)
                raise StructureMapError(
                    "ambiguous_variant",
                    f"Alternatives {previous!r} and {alternative.key!r} share native type {native_type.__name__}",
                    path=path,
                    owner="binding gap",
                    remedy="Use distinct native alternative types",
                )
            types[alternative.key] = native_type
        return DataclassVariantAdapter(types, path)





class DataclassVariantAdapter:
    def __init__(self, native_types: Mapping[str, type], path: str):
        self._types = dict(native_types)
        self._keys = {native_type: key for key, native_type in native_types.items()}
        self._path = path

    def select(self, value: object) -> tuple[str, object]:
        key = self._keys.get(type(value))
        if key is None:
            raise StructureMapError(
                "unknown_variant",
                f"Runtime type {type(value).__name__!r} is not a declared alternative",
                path=self._path,
                owner="executor restriction",
                remedy="Supply an instance of one declared variant alternative",
            )
        return key, value

    def construct(self, key: str, payload: object) -> object:
        if key not in self._types:
            raise StructureMapError(
                "invalid_discriminator",
                f"Alternative key {key!r} is not declared",
                path=self._path,
                owner="executor restriction",
                remedy="Use a discriminator for one declared alternative",
            )
        return payload


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

def _unwrap_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def describe_annotation(
    annotation: object,
    path: str,
    register: Callable[[type, str], str],
    *,
    record_predicate: Callable[[type], bool] = is_dataclass,
    allow_literals: bool = True,
) -> Shape:
    annotation = _unwrap_annotated(annotation)
    if isinstance(annotation, (str, ForwardRef)):
        raise StructureMapError(
            "annotation_resolution_failed", "Native field annotation is unresolved",
            path=path, owner="binding gap",
            remedy="Resolve forward references or rebuild the native model before derivation",
        )
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal and allow_literals and args:
        literal_type = type(args[0])
        if literal_type in _SCALARS and all(type(value) is literal_type for value in args):
            return Scalar(type=_SCALARS[literal_type], allowed_values=args)
    if isinstance(annotation, type) and annotation in _SCALARS:
        return Scalar(type=_SCALARS[annotation])
    if isinstance(annotation, type) and annotation in (Decimal, date):
        return Scalar(type="decimal" if annotation is Decimal else "date", native_type=register(annotation, path))
    if isinstance(annotation, type) and record_predicate(annotation):
        return RecordRef(definition=register(annotation, path))
    if origin in (Union, UnionType):
        choices: list[Alternative] = []
        for arg in args:
            if arg is type(None):
                continue
            arg = _unwrap_annotated(arg)
            branch = describe_annotation(
                arg, path, register, record_predicate=record_predicate, allow_literals=allow_literals,
            )
            choices.append(Alternative(key=arg.__name__, shape=branch))
        shape: Shape = choices[0].shape if len(choices) == 1 else Alternatives(choices=tuple(choices))
        return Nullable(value=shape) if type(None) in args else shape
    if origin is list and len(args) == 1:
        return Sequence(item=describe_annotation(
            args[0], path, register, record_predicate=record_predicate, allow_literals=allow_literals,
        ))
    if origin is dict and len(args) == 2:
        return Map(
            key=describe_annotation(
                args[0], path, register, record_predicate=record_predicate, allow_literals=allow_literals,
            ),
            value=describe_annotation(
                args[1], path, register, record_predicate=record_predicate, allow_literals=allow_literals,
            ),
        )
    if origin is tuple and len(args) == 2 and args[-1] is Ellipsis:
        return Sequence(item=describe_annotation(
            args[0], path, register, record_predicate=record_predicate, allow_literals=allow_literals,
        ), container="tuple")
    if origin is tuple and annotation is not Tuple and (not args or args[-1] is not Ellipsis):
        return Positional(items=tuple(describe_annotation(
            arg, path, register, record_predicate=record_predicate, allow_literals=allow_literals,
        ) for arg in args))
    raise StructureMapError(
        "unsupported_annotation",
        f"Unsupported annotation {annotation!r}",
        path=path,
        owner="binding gap",
        remedy="Use a supported scalar, container or native record annotation; "
        "Literal fields must share one supported primitive type",
    )
