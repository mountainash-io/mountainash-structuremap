from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from io import StringIO
from types import ModuleType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from xsdata.exceptions import XmlContextError
from xsdata.formats.dataclass.context import XmlContext
from xsdata.formats.dataclass.parsers.config import ParserConfig
from xsdata.formats.dataclass.parsers.handlers import XmlEventHandler
from xsdata.formats.dataclass.parsers.xml import XmlParser
from xsdata.formats.dataclass.serializers.xml import XmlSerializer

from mountainash_structuremap.bindings.contracts import CapabilityReport, DocumentBinding, NativeBinding, RecordAdapter
from mountainash_structuremap.bindings.dataclass import record_fields, validate_constructor
from mountainash_structuremap.bindings.xml_scalars import prepare_xml_scalar
from mountainash_structuremap.codecs import ScalarCodec
from mountainash_structuremap.definitions import (
    Member,
    Nullable,
    RecordDefinition,
    RecordRef,
    Scalar,
    Sequence,
    Shape,
    XmlField,
)
from mountainash_structuremap.errors import StructureMapError

_SCALARS: dict[type, Literal["str", "bool", "int", "float"]] = {str: "str", bool: "bool", int: "int", float: "float"}


class XsdataBinding(NativeBinding, DocumentBinding):
    capabilities = CapabilityReport(Parse=True, Serialize=True)

    def __init__(self) -> None:
        self._native_types: dict[str, type] = {}

    def describe_record(
        self, cls: type, register: Callable[[type, str], str], *, path: str
    ) -> RecordDefinition:
        declared = record_fields(cls, path=path)
        try:
            metadata = XmlContext().build(cls)
        except XmlContextError as exc:
            reason = str(exc)
            if "Compound field contains ambiguous types" in reason:
                field_name = reason.split("::", 1)[1].split(":", 1)[0] if "::" in reason else ""
                alternatives = _choice_names(cls, field_name)
                detail = ", ".join(alternatives) if alternatives else "overlapping native types"
                raise StructureMapError(
                    "unsupported_native_shape",
                    f"xsdata cannot distinguish compound alternatives {detail}",
                    path="/".join(filter(None, (path, field_name))),
                    owner="native limitation",
                    remedy="Use distinct supported native intermediate types/subclasses for the conflicting alternatives",
                ) from exc
            raise StructureMapError(
                "unsupported_native_shape",
                f"xsdata could not bind native XML metadata: {exc}",
                path=path,
                owner="native limitation",
                remedy="Use supported xsdata field metadata and native annotation types",
            ) from exc
        except Exception as exc:
            raise StructureMapError(
                "unsupported_native_shape",
                f"Could not prepare native XML metadata: {exc}",
                path=path,
                owner="native limitation",
                remedy="Use supported xsdata field metadata and native annotation types",
            ) from exc
        try:
            hints = _class_hints(cls, self._native_types)
        except Exception as exc:
            raise StructureMapError(
                "annotation_resolution_failed",
                "Could not resolve XML model annotations in their declaring-class scope",
                path=path,
                owner="native limitation",
                remedy="Make forward-referenced classes available in their declaring module or class namespace",
            ) from exc

        variables = {var.name: var for var in metadata.get_all_vars()}
        members: list[Member] = []
        for item in declared:
            member_path = "/".join(filter(None, (path, item.name)))
            variable = variables.get(item.name)
            if variable is None:
                if item.metadata.get("type") == "Ignore":
                    raise StructureMapError(
                        "unsupported_native_shape", "Ignored XML fields are not table members", path=member_path,
                        owner="binding gap", remedy="Remove the ignored field from the mapped native record",
                    )
                raise StructureMapError(
                    "unsupported_native_shape", "xsdata did not expose this dataclass field as an XML member",
                    path=member_path, owner="native limitation",
                    remedy="Use a supported xsdata field kind or remove the field from the native model",
                )
            shape = _describe_annotation(hints[item.name], member_path, register)
            if variable.nillable:
                payload = shape.value if isinstance(shape, Nullable) else shape
                if variable.list_element and isinstance(payload, Sequence):
                    item_shape = payload.item
                    nullable_items = Sequence(
                        item=item_shape if isinstance(item_shape, Nullable) else Nullable(value=item_shape),
                        container=payload.container,
                    )
                    shape = Nullable(value=nullable_items) if isinstance(shape, Nullable) else nullable_items
                elif not isinstance(shape, Nullable):
                    shape = Nullable(value=shape)
            xml = XmlField(
                kind=_xml_kind(variable),
                name=variable.local_name,
                namespace=item.metadata.get("namespace"),
                wrapper=variable.wrapper,
                wrapper_namespace=item.metadata.get("namespace") if variable.wrapper else None,
                nillable=variable.nillable,
                tokens=variable.tokens,
                mixed=variable.mixed,
                sequence=variable.sequence,
                format=variable.format,
                process_contents=variable.process_contents,
            )
            members.append(Member(
                name=item.name,
                shape=shape,
                default_kind=("factory" if item.default_factory is not MISSING else
                              "value" if item.default is not MISSING else "required"),
                xml=xml,
            ))
        return RecordDefinition(name=f"{cls.__module__}.{cls.__qualname__}", members=tuple(members))

    def prepare_record(self, cls: type, definition: RecordDefinition, *, path: str) -> RecordAdapter:
        validate_constructor(cls, tuple(member.name for member in definition.members), path=path)
        return XsdataRecordAdapter(cls)

    def prepare_scalar(
        self, declaration: Scalar, *, native_type: type | None, format: str | None,
        column: str, policy: str | None, path: str,
    ) -> ScalarCodec:
        return prepare_xml_scalar(declaration, native_type, column, path)

    def parse(self, source: str | bytes, root_type: type[Any]) -> Any:
        try:
            parser = XmlParser(
                context=XmlContext(),
                config=ParserConfig(fail_on_converter_warnings=True, fail_on_unknown_properties=True),
                handler=XmlEventHandler,
            )
            if isinstance(source, str):
                return parser.parse(StringIO(source), root_type)
            return parser.from_bytes(source, root_type)
        except Exception as exc:
            raise StructureMapError(
                "native_parse_failed", "xsdata could not parse the XML document", path=_parse_error_path(exc),
                owner="native limitation", remedy="Provide a valid document matching the prepared native XML model",
            ) from exc

    def serialize(self, value: object) -> str:
        try:
            serializer = XmlSerializer(context=XmlContext())
            return serializer.render(value)
        except Exception as exc:
            raise StructureMapError(
                "native_serialize_failed", "xsdata could not serialize the native model", path="",
                owner="native limitation", remedy="Provide values supported by the prepared native XML model",
            ) from exc


class XsdataRecordAdapter:
    def __init__(self, native_type: type):
        self.native_type = native_type

    def read(self, instance: object, member: str, *, path: str) -> object:
        try:
            return getattr(instance, member)
        except Exception as exc:
            raise StructureMapError(
                "native_read_failed", "Could not read native XML model member", path=path,
                owner="native limitation", remedy="Provide a readable native model member",
            ) from exc

    def construct(self, values: dict[str, object], *, path: str) -> object:
        try:
            return self.native_type(**values)
        except Exception as exc:
            raise StructureMapError(
                "native_construct_failed", "Native XML model construction failed", path=path,
                owner="native limitation", remedy="Ensure supplied values satisfy the native constructor",
            ) from exc


def _class_hints(cls: type, native_types: dict[str, type]) -> dict[str, object]:
    field_names = {item.name for item in fields(cls)}
    hints: dict[str, object] = {}
    for base in reversed(cls.__mro__):
        annotations = {
            name: value for name, value in vars(base).get("__annotations__", {}).items()
            if name in field_names
        }
        if not annotations:
            continue
        scope = ModuleType(base.__module__)
        scope.__annotations__ = annotations
        module = sys.modules.get(base.__module__)
        lexical_scope = base.__qualname__.rpartition(".")[0]
        known = {
            candidate.__name__: candidate
            for candidate in native_types.values()
            if lexical_scope
            and candidate.__module__ == base.__module__
            and candidate.__qualname__.rpartition(".")[0] == lexical_scope
        }
        hints.update(get_type_hints(
            scope,
            globalns=vars(module) if module else {},
            localns={**known, **vars(base), base.__name__: base},
        ))
    return hints


def _describe_annotation(annotation: object, path: str, register: Callable[[type, str], str]) -> Shape:
    origin, args = get_origin(annotation), get_args(annotation)
    if isinstance(annotation, type) and annotation in _SCALARS:
        return Scalar(type=_SCALARS[annotation])
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return Scalar(type="enum", native_type=register(annotation, path))
    if isinstance(annotation, type) and is_dataclass(annotation):
        return RecordRef(definition=register(annotation, path))
    if origin in (Union, UnionType):
        choices = tuple(_describe_annotation(arg, path, register) for arg in args if arg is not type(None))
        if type(None) in args and len(choices) == 1:
            return Nullable(value=choices[0])
        raise StructureMapError(
            "unsupported_native_shape", "Compound annotations are not supported by this XML binding slice",
            path=path, owner="binding gap",
            remedy="Use a single supported native intermediate type; overlapping alternatives require distinct types",
        )
    if origin in (list, tuple) and (len(args) == 1 or origin is tuple and len(args) == 2 and args[1] is Ellipsis):
        return Sequence(
            item=_describe_annotation(args[0], path, register),
            container="tuple" if origin is tuple else "list",
        )
    raise StructureMapError(
        "unsupported_annotation", f"Unsupported xsdata field annotation {annotation!r}", path=path,
        owner="binding gap", remedy="Use str, bool, float, int, enums, dataclass records, or homogeneous sequences",
    )


def _choice_names(cls: type, field_name: str = "") -> tuple[str, ...]:
    names: list[str] = []
    if not is_dataclass(cls):
        return ()
    for item in fields(cls):
        if field_name and item.name != field_name:
            continue
        choices = item.metadata.get("choices")
        if choices:
            names.extend(str(choice.get("name", "any")) for choice in choices)
    return tuple(names)


def _xml_kind(variable: Any) -> str:
    if variable.is_attribute:
        return "Attribute"
    if variable.is_elements:
        return "Elements"
    if variable.is_wildcard:
        return "Wildcard"
    if variable.is_attributes:
        return "Attributes"
    if variable.is_text:
        return "Text"
    return "Element"


def _parse_error_path(error: Exception) -> str:
    match = re.search(r"Failed to convert value for `[^`]*\.([^`.]+)`", str(error))
    return match.group(1) if match else ""
