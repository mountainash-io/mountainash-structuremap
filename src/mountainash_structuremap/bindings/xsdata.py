from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import MISSING, dataclass, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from io import StringIO
from types import ModuleType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints
from xml.etree.ElementTree import QName

from xsdata.exceptions import XmlContextError
from xsdata.formats.dataclass.context import XmlContext
from xsdata.formats.dataclass.models.elements import XmlVar
from xsdata.formats.dataclass.models.generics import DerivedElement
from xsdata.formats.dataclass.parsers.config import ParserConfig
from xsdata.formats.dataclass.parsers.handlers import XmlEventHandler
from xsdata.formats.dataclass.parsers.xml import XmlParser
from xsdata.formats.dataclass.serializers.xml import XmlSerializer
from xsdata.models.datatype import (
    XmlBase64Binary,
    XmlDate,
    XmlDateTime,
    XmlDuration,
    XmlHexBinary,
    XmlPeriod,
    XmlTime,
)

from mountainash_structuremap.bindings.contracts import CapabilityReport, DocumentBinding, NativeBinding, RecordAdapter
from mountainash_structuremap.bindings.dataclass import record_fields, validate_constructor
from mountainash_structuremap.bindings.xml_fragments import FragmentCodec
from mountainash_structuremap.bindings.xml_scalars import _integer_text, prepare_xml_scalar
from mountainash_structuremap.codecs import ScalarCodec
from mountainash_structuremap.definitions import (
    Alternative,
    Alternatives,
    Member,
    Nullable,
    NumericFacets,
    RecordDefinition,
    RecordRef,
    Scalar,
    Sequence,
    Shape,
    XmlField,
)
from mountainash_structuremap.errors import StructureMapError

_SCALARS: dict[type, Literal[
    "str", "bool", "int", "float", "decimal", "date", "time", "datetime",
    "duration", "period", "binary", "qname",
]] = {
    str: "str", bool: "bool", int: "int", float: "float", Decimal: "decimal",
    XmlDate: "date", XmlTime: "time", XmlDateTime: "datetime",
    XmlDuration: "duration", XmlPeriod: "period", bytes: "binary",
    XmlHexBinary: "binary", XmlBase64Binary: "binary", QName: "qname",
}


class XsdataBinding(NativeBinding, DocumentBinding):
    name = "xsdata"
    capabilities = CapabilityReport(Parse=True, Serialize=True)

    def describe_root(self, annotation: object, register: Callable[[type, str], str], *, path: str) -> Shape:
        if not isinstance(annotation, type) or not is_dataclass(annotation):
            raise StructureMapError("unsupported_root", "xsdata requires a dataclass record root",
                                    path=path, owner="binding gap", remedy="Use an xsdata dataclass root")
        return RecordRef(definition=register(annotation, path))

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
            field_name = _xml_context_field(exc, cls)
            member_path = "/".join(filter(None, (path, field_name)))
            if "Compound field contains ambiguous types" in reason:
                alternatives = _choice_names(cls, field_name)
                detail = ", ".join(alternatives) if alternatives else "overlapping native types"
                raise StructureMapError(
                    "unsupported_native_shape",
                    f"xsdata cannot distinguish compound alternatives {detail}",
                    path=member_path,
                    owner="native limitation",
                    remedy="Use distinct supported native intermediate types/subclasses for the conflicting alternatives",
                ) from exc
            raise StructureMapError(
                "unsupported_native_shape",
                f"xsdata could not bind native XML metadata: {exc}",
                path=member_path,
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
            shape = _describe_variable(variable, item.metadata, hints[item.name], member_path, register)
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
        return RecordDefinition(
            name=f"{cls.__module__}.{cls.__qualname__}", members=tuple(members),
            xml=XmlField(kind="Element", name=metadata.qname.rsplit("}", 1)[-1], namespace=metadata.namespace),
        )

    def prepare_record(self, cls: type, definition: RecordDefinition, *, path: str) -> RecordAdapter:
        validate_constructor(cls, tuple(member.name for member in definition.members), path=path)
        return XsdataRecordAdapter(cls)

    def prepare_scalar(
        self, declaration: Scalar, *, native_type: type | None, format: str | None,
        column: str, policy: str | None, path: str, metadata_namespace: str = "_sm_",
        field_context: tuple[type, str, str | None] | None = None,
    ) -> ScalarCodec:
        if declaration.type == "fragment":
            if field_context is None:
                raise StructureMapError(
                    "unsupported_native_shape", "Fragment codec requires its prepared native field metadata",
                    path=path, owner="binding gap",
                    remedy="Declare fragment content as a native xsdata field",
                )
            field_owner, field_name, namespace = field_context
            return FragmentCodec(
                column, path, field_owner=field_owner, field_name=field_name,
                namespace=namespace, attributes=declaration.format == "attributes",
            )
        return prepare_xml_scalar(
            declaration, native_type, column, path,
            policy=policy, metadata_namespace=metadata_namespace,
        )

    def prepare_variant(
        self, alternatives: tuple[Alternative, ...], *, native_types: Mapping[str, type],
        path: str, namespace: str | None = None,
    ) -> XmlVariantAdapter:
        return XmlVariantAdapter.prepare(alternatives, native_types, path, namespace)

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
        return Scalar(type=_SCALARS[annotation], native_type=register(annotation, path))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return Scalar(type="enum", native_type=register(annotation, path))
    if isinstance(annotation, type) and is_dataclass(annotation):
        return RecordRef(definition=register(annotation, path))
    if origin in (Union, UnionType):
        choices = tuple(arg for arg in args if arg is not type(None))
        if len(choices) == 1:
            shape = _describe_annotation(choices[0], path, register)
        else:
            shape = Alternatives(choices=tuple(
                Alternative(key=arg.__name__, shape=_describe_annotation(arg, path, register))
                for arg in choices
            ))
        return Nullable(value=shape) if type(None) in args else shape
    if origin in (list, tuple) and (len(args) == 1 or origin is tuple and len(args) == 2 and args[1] is Ellipsis):
        return Sequence(
            item=_describe_annotation(args[0], path, register),
            container="tuple" if origin is tuple else "list",
        )
    raise StructureMapError(
        "unsupported_annotation", f"Unsupported xsdata field annotation {annotation!r}", path=path,
        owner="binding gap", remedy="Use a supported XML scalar, dataclass record, or declared finite choice",
    )


def _source_scalar_metadata(shape: Shape, metadata: Mapping[str, Any], path: str) -> Shape:
    if isinstance(shape, Nullable):
        return shape.model_copy(update={"value": _source_scalar_metadata(shape.value, metadata, path)})
    if isinstance(shape, Sequence):
        return shape.model_copy(update={"item": _source_scalar_metadata(shape.item, metadata, path)})
    if not isinstance(shape, Scalar):
        return shape
    values: dict[str, Any] = {}
    if shape.type in ("int", "decimal", "enum"):
        for name in ("total_digits", "fraction_digits", "min_inclusive", "max_inclusive",
                     "min_exclusive", "max_exclusive"):
            value = metadata.get(name)
            if value is None:
                continue
            if name in ("total_digits", "fraction_digits"):
                if type(value) is not int:
                    raise StructureMapError(
                        "invalid_numeric_facets", f"{name} must be an integer", path=path,
                        remedy="Declare an integer digit facet in the native field metadata",
                    )
                values[name] = value
            elif type(value) in (str, int, Decimal):
                values[name] = _integer_text(value) if type(value) is int else str(value)
            else:
                raise StructureMapError(
                    "invalid_numeric_facets", f"{name} does not retain an exact numeric bound", path=path,
                    remedy="Declare numeric bounds as exact strings, integers or Decimal values, not binary floats",
                )
    try:
        facets = NumericFacets(**values) if values else None
    except ValueError as exc:
        raise StructureMapError(
            "invalid_numeric_facets", "Invalid numeric digit facets", path=path,
            remedy="Declare positive total_digits and nonnegative fraction_digits",
        ) from exc
    return shape.model_copy(update={"facets": facets, "format": metadata.get("format")})


def _native_subclasses(cls: type, *, include_siblings: bool = True) -> tuple[type, ...]:
    pending = [base for base in cls.__mro__ if base is not object] if include_siblings else [cls]
    discovered: set[type] = set()
    while pending:
        candidate = pending.pop()
        if candidate in discovered:
            continue
        discovered.add(candidate)
        pending.extend(candidate.__subclasses__())
    compatible = {
        candidate for candidate in discovered
        if is_dataclass(candidate)
        and (
            candidate is cls
            or not issubclass(cls, candidate)
            and (
                include_siblings
                or any(base is not object and base in cls.__mro__ for base in candidate.__mro__)
            )
        )
    }
    return tuple(sorted(compatible, key=lambda candidate: (candidate.__module__, candidate.__qualname__)))


def _variable_payload(
    variable: XmlVar, metadata: Mapping[str, Any], path: str, register: Callable[[type, str], str],
) -> Shape:
    types: list[type] = []
    for declared in variable.types:
        candidates = (
            _native_subclasses(declared, include_siblings=not variable.is_clazz_union)
            if is_dataclass(declared) else (declared,)
        )
        for candidate in candidates:
            if candidate is not declared and is_dataclass(candidate) and not XmlContext().build(candidate).target_qname:
                continue
            if candidate not in types:
                types.append(candidate)
    if len(types) == 1:
        shape = _describe_annotation(types[0], path, register)
    else:
        alternatives: list[Alternative] = []
        for native in types:
            native_meta = XmlContext().build(native) if is_dataclass(native) else None
            type_qname = (
                native_meta.target_qname if native_meta else None
            ) or f"{native.__module__}.{native.__qualname__}"
            if any(alt.key == type_qname for alt in alternatives):
                raise StructureMapError(
                    "ambiguous_native_alternative", "Native alternatives do not have unique XML type names",
                    path=path, owner="native limitation",
                    remedy="Give each supported native alternative a distinct XML type QName",
                )
            substitution = is_dataclass(native) and any(
                is_dataclass(declared)
                and native is not declared
                and not issubclass(native, declared)
                and any(base is not object and base in declared.__mro__ for base in native.__mro__)
                for declared in variable.types
            )
            alternatives.append(Alternative(
                key=type_qname,
                shape=_source_scalar_metadata(_describe_annotation(native, path, register), metadata, path),
                xml=XmlField(
                    kind="TypeSubstitution" if substitution else "Type" if is_dataclass(native) else "Value",
                    name=variable.local_name, namespace=metadata.get("namespace"),
                ),
            ))
        shape = Alternatives(choices=tuple(alternatives))
    shape = _source_scalar_metadata(shape, metadata, path)
    if variable.tokens:
        shape = Sequence(item=shape, container="tuple" if variable.tokens_factory is tuple else "list")
    if variable.nillable:
        shape = Nullable(value=shape)
    return shape


def _describe_variable(
    variable: XmlVar, metadata: Mapping[str, Any], annotation: object, path: str,
    register: Callable[[type, str], str],
) -> Shape:
    if variable.is_wildcard or variable.mixed or variable.wildcards:
        if variable.mixed and not variable.list_element:
            raise StructureMapError(
                "unsupported_native_shape",
                "Mixed content requires a native collection field",
                path=path, owner="native limitation",
                remedy="Declare the mixed field as list[object] or tuple[object, ...]; xsdata 26.2 cannot round-trip its scalar mixed-content carrier",
            )
        fragment: Shape = Scalar(type="fragment")
        fragment_shape = (
            Sequence(item=fragment, container="tuple" if variable.factory is tuple else "list")
            if variable.list_element else fragment
        )
        return _annotation_nullability(fragment_shape, annotation)
    if variable.is_attributes:
        return Scalar(type="fragment", format="attributes")
    if variable.is_elements:
        declared_choices = metadata.get("choices", ())
        if len(declared_choices) != len(variable.elements):
            raise StructureMapError(
                "ambiguous_native_alternative", "Compound metadata does not expose unique declared element choices",
                path=path, owner="native limitation",
                remedy="Use distinct supported element QNames and native types",
            )
        choices: list[Alternative] = []
        for child, declaration in zip(variable.elements.values(), declared_choices, strict=True):
            namespace = declaration.get("namespace")
            key = child.qname if namespace is not None else child.local_name
            choices.append(Alternative(
                key=key, shape=_variable_payload(child, declaration, path, register),
                xml=XmlField(
                    kind="Element", name=child.local_name, namespace=namespace,
                    nillable=child.nillable, tokens=child.tokens, format=child.format,
                ),
            ))
        shape: Shape = Alternatives(choices=tuple(choices))
    else:
        shape = _variable_payload(variable, metadata, path, register)
    if variable.list_element:
        shape = Sequence(item=shape, container="tuple" if variable.factory is tuple else "list")
    return _annotation_nullability(shape, annotation)


def _annotation_nullability(shape: Shape, annotation: object) -> Shape:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType) and type(None) in args:
        remaining = tuple(arg for arg in args if arg is not type(None))
        inner = shape.value if isinstance(shape, Nullable) else shape
        if len(remaining) == 1:
            inner = _annotation_nullability(inner, remaining[0])
        return Nullable(value=inner)
    if isinstance(shape, Nullable):
        return Nullable(value=_annotation_nullability(shape.value, annotation))
    if origin in (list, tuple) and args and isinstance(shape, Sequence):
        return shape.model_copy(update={"item": _annotation_nullability(shape.item, args[0])})
    return shape


def _shape_native_types(shape: Shape, native_types: Mapping[str, type]) -> tuple[type, ...]:
    if isinstance(shape, Nullable):
        return _shape_native_types(shape.value, native_types)
    if isinstance(shape, RecordRef):
        return (native_types[shape.definition],)
    if isinstance(shape, Scalar):
        if shape.native_type is not None:
            return (native_types[shape.native_type],)
        return tuple(native for native, family in _SCALARS.items() if family == shape.type)
    if isinstance(shape, Alternatives):
        return tuple(dict.fromkeys(
            native for alternative in shape.choices for native in _shape_native_types(alternative.shape, native_types)
        ))
    if isinstance(shape, Sequence):
        return (tuple if shape.container == "tuple" else list,)
    return ()


@dataclass(frozen=True)
class XmlChoice:
    key: str
    qname: str
    native_types: tuple[type, ...]
    nullable: bool
    type_names: tuple[str, ...]
    token_types: tuple[type, ...] = ()
    substitution_type: str | None = None


@dataclass(frozen=True)
class XmlVariantAdapter:
    choices: tuple[XmlChoice, ...]
    path: str

    @classmethod
    def prepare(
        cls, alternatives: tuple[Alternative, ...], native_types: Mapping[str, type],
        path: str, namespace: str | None,
    ) -> XmlVariantAdapter:
        choices: list[XmlChoice] = []
        seen: set[type] = set()
        for alternative in alternatives:
            xml = alternative.xml
            local_name = xml.name if xml and xml.name else alternative.key
            effective_ns = xml.namespace if xml and xml.namespace is not None else namespace
            qname = f"{{{effective_ns}}}{local_name}" if effective_ns else local_name
            shape = alternative.shape.value if isinstance(alternative.shape, Nullable) else alternative.shape
            token_types = (
                _shape_native_types(shape.item, native_types)
                if xml is not None and xml.tokens and isinstance(shape, Sequence)
                and isinstance(shape.item, (Scalar, Alternatives))
                else ()
            )
            candidates = (
                ((tuple if shape.container == "tuple" else list),)
                if token_types and isinstance(shape, Sequence)
                else _shape_native_types(alternative.shape, native_types)
            )
            association = set(token_types) if token_types else set(candidates)
            if not candidates or seen.intersection(association):
                raise StructureMapError(
                    "ambiguous_native_alternative", "XML alternatives have overlapping native associations",
                    path=path, owner="native limitation",
                    remedy="Use distinct native intermediate types/subclasses for each XML alternative",
                )
            seen.update(association)
            type_names = tuple(
                name for native in (token_types or candidates) if is_dataclass(native)
                if (name := XmlContext().build(native).target_qname)
            )
            choices.append(XmlChoice(
                alternative.key, qname, candidates, isinstance(alternative.shape, Nullable),
                type_names, token_types,
                type_names[0] if xml and xml.kind == "TypeSubstitution" and type_names else None,
            ))
        return cls(tuple(choices), path)

    def select(self, value: object) -> tuple[str, object]:
        envelope = value if isinstance(value, DerivedElement) else None
        payload = envelope.value if envelope is not None else value
        matches = [
            choice for choice in self.choices
            if (envelope is None or envelope.qname == choice.qname)
            and (
                payload is None and choice.nullable
                or (
                    type(payload) in choice.native_types
                    and all(type(item) in choice.token_types for item in payload)
                    and (bool(payload) or envelope is not None)
                    if choice.token_types and isinstance(payload, (list, tuple))
                    else not choice.token_types and type(payload) in choice.native_types
                )
            )
            and (envelope is None or envelope.type is None or envelope.type in choice.type_names)
        ]
        if len(matches) != 1:
            raise StructureMapError(
                "invalid_variant_value", "Value does not select one prepared XML alternative",
                path=self.path,
                remedy="Provide a declared native type, or a DerivedElement with its declared QName and payload",
            )
        return matches[0].key, payload

    def construct(self, key: str, payload: object) -> object:
        choice = next(choice for choice in self.choices if choice.key == key)
        if payload is None:
            return DerivedElement(qname=choice.qname, value=None)
        if choice.token_types and not payload:
            return DerivedElement(qname=choice.qname, value=payload)
        if choice.substitution_type is not None:
            return DerivedElement(qname=choice.qname, value=payload, type=choice.substitution_type)
        return payload


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


def _xml_context_field(error: XmlContextError, cls: type) -> str:
    match = re.search(r"Error on [^:]+::([^:]+):", str(error))
    field_name = match.group(1) if match else ""
    return field_name if field_name in {item.name for item in fields(cls)} else ""


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
