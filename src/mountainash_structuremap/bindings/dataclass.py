from __future__ import annotations

from collections.abc import Callable
from dataclasses import Field, fields, is_dataclass
from types import UnionType
from typing import Any, Literal, Tuple, Union, get_args, get_origin

from pydantic import BaseModel

from mountainash_structuremap.bindings.contracts import CapabilityReport
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

_SCALARS: dict[type, Literal["str", "int", "float", "bool"]] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
}
_XML_TYPES = {"Text", "Element", "Elements", "Attribute", "Attributes", "Wildcard", "Ignore"}
_XML_FIELDS = {
    "name",
    "namespace",
    "nillable",
    "required",
    "sequence",
    "tokens",
    "format",
    "choices",
    "wrapper",
    "mixed",
    "process_contents",
}
_XML_META = {
    "name",
    "namespace",
    "nillable",
    "target_namespace",
    "global_type",
    "element_name_generator",
    "attribute_name_generator",
}


class DataclassBinding:
    capabilities = CapabilityReport()

    def read(self, instance: object, member: str, *, path: str) -> object:
        try:
            value: object = getattr(instance, member)
            return value
        except Exception as exc:
            raise StructureMapError(
                "native_read_failed",
                "Could not read dataclass member",
                path=path,
                owner="native limitation",
                remedy="Provide a readable dataclass member",
            ) from exc

    def construct(self, cls: type, values: dict[str, object], *, path: str) -> object:
        try:
            result: object = cls(**values)
            return result
        except Exception as exc:
            raise StructureMapError(
                "native_construct_failed",
                "Dataclass construction failed",
                path=path,
                owner="native limitation",
                remedy="Ensure supplied values satisfy the native constructor",
            ) from exc


def record_fields(cls: type, *, detected: bool, path: str) -> tuple[Field[Any], ...]:
    if detected and issubclass(cls, BaseModel):
        raise StructureMapError(
            "unavailable_binding",
            "This class requires the Pydantic binding",
            path=path,
            owner="binding gap",
            remedy="The native Pydantic binding is not available in this delivery",
        )
    if not is_dataclass(cls):
        raise StructureMapError(
            "unsupported_binding",
            "Expected a dataclass record",
            path=path,
            owner="binding gap",
            remedy="Pass a plain dataclass class",
        )
    members = fields(cls)
    meta = getattr(cls, "Meta", None)
    xml = any(hasattr(meta, key) for key in _XML_META) or any(
        bool(_XML_FIELDS.intersection(item.metadata))
        or (isinstance(item.metadata.get("type"), str) and item.metadata["type"] in _XML_TYPES)
        for item in members
    )
    if detected and xml:
        raise StructureMapError(
            "unavailable_binding",
            "XML-shaped metadata requires the xsdata binding",
            path=path,
            owner="binding gap",
            remedy="The xsdata binding is unavailable; declare binding='dataclass' only if this metadata is not XML",
        )
    for item in members:
        if not item.init:
            raise StructureMapError(
                "unsupported_constructor",
                "Mapped field does not participate in construction",
                path="/".join(filter(None, (path, item.name))),
                owner="native limitation",
                remedy="Use a constructor-participating field",
            )
    return members


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
