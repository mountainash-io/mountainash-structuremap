from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field
from io import StringIO
from typing import Any, cast
from xml.etree import ElementTree

from mountainash.typespec.spec import FieldSpec
from mountainash.typespec.universal_types import UniversalType
from xsdata.formats.dataclass.context import XmlContext
from xsdata.formats.dataclass.models.elements import XmlVar
from xsdata.formats.dataclass.models.generics import AnyElement
from xsdata.formats.dataclass.parsers import XmlParser
from xsdata.formats.dataclass.parsers.config import ParserConfig
from xsdata.formats.dataclass.parsers.handlers import XmlEventHandler
from xsdata.formats.dataclass.parsers.utils import ParserUtils
from xsdata.formats.dataclass.serializers.config import SerializerConfig
from xsdata.formats.dataclass.serializers.xml import XmlSerializer

from mountainash_structuremap.codecs import Cell, CellLocation
from mountainash_structuremap.errors import StructureMapError

_NAMESPACE = "urn:mountainash:structuremap:fragment"
_ENVELOPE = f"{{{_NAMESPACE}}}fragment"


@dataclass
class FragmentEnvelope:
    """Package-owned carrier; the isolated preflight context sees no user models."""

    class Meta:
        name = "fragment"
        namespace = _NAMESPACE

    content: list[object] = field(default_factory=list, metadata={
        "type": "Wildcard", "namespace": "##any", "process_contents": "skip", "mixed": True,
    })
    attributes: dict[str, str] = field(default_factory=dict, metadata={
        "type": "Attributes", "namespace": "##any",
    })


def _carrier_variable(variable: XmlVar) -> XmlVar:
    # Only native metadata is copied. Classes, defaults and user values are not
    # copied or constructed. Choice variables bind to the same carrier member.
    result = copy(variable)
    result.name = "attributes" if variable.is_attributes else "content"
    result.wrapper = None
    result.wrapper_qname = None
    result.elements = {name: _carrier_variable(child) for name, child in variable.elements.items()}
    result.wildcards = [_carrier_variable(child) for child in variable.wildcards]
    return result


class FragmentCodec:
    def __init__(
        self, column: str, path: str, *, field_owner: type, field_name: str,
        namespace: str | None, attributes: bool = False,
    ) -> None:
        self.column = column
        self.path = path
        self.attributes = attributes
        self.fields: tuple[FieldSpec, ...] = (FieldSpec(name=column, type=UniversalType.STRING, missing_values=[]),)
        config = ParserConfig(fail_on_converter_warnings=True, fail_on_unknown_properties=True)
        self.preflight_parser = XmlParser(
            context=XmlContext(models_package=__name__), config=config, handler=XmlEventHandler,
        )
        context = XmlContext()
        owner_meta = context.build(field_owner, namespace)
        variable = next(item for item in owner_meta.get_all_vars() if item.name == field_name)
        carrier = _carrier_variable(variable)
        self.variable = carrier
        # This metadata belongs solely to the codec's application context. The
        # preflight context never receives it or any application cache entries.
        envelope_meta = context.build(FragmentEnvelope)
        envelope_meta.namespace = owner_meta.namespace
        envelope_meta.wildcards = [carrier] if carrier.is_wildcard else []
        envelope_meta.choices = [carrier] if carrier.is_elements else []
        envelope_meta.any_attributes = [carrier] if attributes else []
        envelope_meta.mixed_content = carrier.mixed
        self.envelope_meta = envelope_meta
        self.application_parser = XmlParser(context=context, config=config, handler=XmlEventHandler)
        self.serializer = XmlSerializer(context=context, config=SerializerConfig(xml_declaration=False))

    def encode(self, value: object) -> tuple[Cell, ...]:
        if self.attributes:
            if not isinstance(value, dict) or not all(
                isinstance(key, str) and isinstance(item, str) for key, item in value.items()
            ):
                raise StructureMapError(
                    "native_type_mismatch", "Expected wildcard attribute names and string values",
                    path=self.path, remedy="Supply the xsdata wildcard attribute mapping",
                )
            envelope = FragmentEnvelope(attributes=value)
        else:
            # The application XmlVar determines whether this internal member is
            # a native scalar aggregate or a repeated occurrence.
            content = [value] if self.variable.list_element else value
            envelope = FragmentEnvelope(content=cast(Any, content))
        try:
            # Keep the envelope intact: extracting a child through ElementTree
            # would discard namespace declarations used only in QName values.
            text = self.serializer.render(envelope, ns_map={"sm": _NAMESPACE})
            self._preflight(text)
        except Exception as exc:
            raise StructureMapError(
                "fragment_encode_failed", "xsdata could not serialize the native fragment occurrence",
                path=self.path, owner="native limitation",
                remedy="Supply content accepted by the declared xsdata field metadata",
            ) from exc
        return (text,)

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if not isinstance(value, str):
            raise StructureMapError(
                "cell_type_mismatch", "Expected standalone fragment XML text", path=location.path,
                table=location.table, row=location.row, column=self.column,
                remedy="Supply a serialized XML fragment envelope",
            )
        try:
            self._preflight(value)
        except Exception as exc:
            raise StructureMapError(
                "invalid_xml_fragment", "Fragment syntax or envelope role is invalid", path=location.path,
                table=location.table, row=location.row, column=self.column,
                remedy="Supply one well-formed fragment envelope matching the declared native field",
            ) from exc
        return value

    def _preflight(self, value: str) -> None:
        root = ElementTree.fromstring(value)
        if root.tag != _ENVELOPE:
            raise ValueError("Fragment envelope has the wrong root QName")
        parsed = self.preflight_parser.parse(StringIO(value), FragmentEnvelope)
        if self.attributes:
            if parsed.content:
                raise ValueError("Attribute envelope contains child content")
            for name in parsed.attributes:
                if not self.variable.match_namespace(name):
                    raise ValueError(f"Attribute {name!r} does not match the native wildcard namespace")
        else:
            if parsed.attributes or (self.variable.list_element and len(parsed.content) > 1):
                raise ValueError("A content fragment envelope must contain one native occurrence and no attributes")
            if not self.variable.mixed and (not parsed.content or isinstance(parsed.content[0], str)):
                raise ValueError("Text occurrences require a native mixed-content field")
            children = (item for item in parsed.content if not isinstance(item, str))
            for child, retained in zip(root, children, strict=True):
                if ParserUtils.normalize_content(child.tail) and not self._retains_generic_tail(child.tag, retained):
                    raise ValueError(
                        "Typed fragment elements cannot carry an outer tail; supply trailing text "
                        "as a separate mixed-content occurrence"
                    )
            for child in root:
                admissible = (
                    self.variable.find_choice(child.tag)
                    if self.variable.is_elements
                    else self.variable.match_namespace(child.tag)
                )
                if not admissible:
                    raise ValueError(f"Element {child.tag!r} does not match the native wildcard namespace")

    def _retains_generic_tail(self, qname: str, retained: object) -> bool:
        # Only a generic AnyElement owns its outer tail. Typed element and
        # primitive nodes split or discard it. Resolve native metadata only:
        # never build parser nodes (union nodes can invoke default factories),
        # and never share this context with the restricted preflight parser.
        if not isinstance(retained, AnyElement):
            return False
        context = self.application_parser.context
        xsi_type = retained.attributes.get("{http://www.w3.org/2001/XMLSchema-instance}type")
        xsi_nil = ParserUtils.xsi_nil(retained.attributes)
        for variable in self.envelope_meta.find_children(qname):
            if variable.is_clazz_union or (
                not variable.clazz and not variable.any_type and not variable.is_wildcard
            ):
                return False
            candidates = (
                (variable.clazz,) if variable.clazz else (
                    context.find_type(xsi_type) if xsi_type else None,
                    context.find_type(qname) if variable.process_contents != "skip" else None,
                )
            )
            for clazz in candidates:
                if clazz is not None:
                    meta = context.fetch(clazz, self.envelope_meta.namespace, xsi_type)
                    if xsi_nil is None or xsi_nil == (variable.nillable or meta.nillable):
                        return False
        return True

    def decode(self, checked: object) -> object:
        try:
            parsed = self.application_parser.parse(StringIO(str(checked)), FragmentEnvelope)
            if self.attributes:
                return parsed.attributes
            if not self.variable.list_element:
                return parsed.content
            return parsed.content[0] if parsed.content else ""
        except Exception as exc:
            raise StructureMapError(
                "fragment_bind_failed", "xsdata could not bind a validated XML fragment",
                path=self.path, owner="native limitation",
                remedy="Supply fragment content accepted by the prepared native XML field",
            ) from exc
