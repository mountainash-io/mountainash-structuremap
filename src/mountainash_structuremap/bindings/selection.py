from __future__ import annotations

from dataclasses import fields, is_dataclass

from pydantic import BaseModel

from mountainash_structuremap.bindings.contracts import NativeBinding
from mountainash_structuremap.bindings.dataclass import DataclassBinding
from mountainash_structuremap.errors import StructureMapError

_XML_TYPES = {"Text", "Element", "Elements", "Attribute", "Attributes", "Wildcard", "Ignore"}
_XML_FIELD_KEYS = {
    "name", "namespace", "nillable", "required", "sequence", "tokens", "format",
    "choices", "wrapper", "mixed", "process_contents",
}
_XML_CLASS_KEYS = {
    "name", "namespace", "nillable", "target_namespace", "global_type",
    "element_name_generator", "attribute_name_generator",
}


def _has_xml_metadata(root_type: object) -> bool:
    meta = getattr(root_type, "Meta", None)
    if meta is not None and any(hasattr(meta, key) for key in _XML_CLASS_KEYS):
        return True
    if not is_dataclass(root_type):
        return False
    for field in fields(root_type):
        xml_type = field.metadata.get("type")
        if _XML_FIELD_KEYS.intersection(field.metadata) or (
            isinstance(xml_type, str) and xml_type in _XML_TYPES
        ):
            return True
    return False


def select_binding(root_type: object, binding: str | None = None) -> NativeBinding:
    if isinstance(root_type, type) and issubclass(root_type, BaseModel):
        if binding in (None, "pydantic"):
            from mountainash_structuremap.bindings.pydantic import PydanticBinding

            return PydanticBinding()
        raise StructureMapError(
            "unavailable_binding", "Pydantic model requires the pydantic binding",
            owner="binding gap", remedy="Use binding='pydantic'",
        )
    selected = binding if binding is not None else ("xsdata" if _has_xml_metadata(root_type) else "dataclass")
    if selected == "dataclass":
        return DataclassBinding()
    if selected != "xsdata":
        raise StructureMapError(
            "unsupported_binding",
            f"Unknown native binding {selected!r}",
            owner="binding gap",
            remedy="Use binding='dataclass' or install the xml extra and use binding='xsdata'",
        )
    try:
        from mountainash_structuremap.bindings.xsdata import XsdataBinding

        return XsdataBinding()
    except ModuleNotFoundError as exc:
        if exc.name != "xsdata":
            raise
        raise StructureMapError(
            "unavailable_binding",
            "The xsdata binding requires the optional XML dependency",
            owner="binding gap",
            remedy="Install mountainash-structuremap[xml] to use XML-native models",
        ) from exc
