from __future__ import annotations

from dataclasses import is_dataclass
from enum import Enum
from typing import TypeVar, get_origin

from pydantic import BaseModel

from mountainash_structuremap.bindings.selection import _has_xml_metadata, select_binding
from mountainash_structuremap.definitions import DerivedModel, RecordDefinition, RecordRef, StructureSpec
from mountainash_structuremap.errors import StructureMapError

T = TypeVar("T")


def derive(root_type: type[T], *, binding: str | None = None) -> DerivedModel[T]:
    is_pydantic = isinstance(root_type, type) and issubclass(root_type, BaseModel)
    is_record = isinstance(root_type, type) and not issubclass(root_type, Enum) and (is_dataclass(root_type) or is_pydantic)
    if not is_record and not (binding == "dataclass" and get_origin(root_type) in (tuple, list)):
        raise StructureMapError("unsupported_root", "Root must be a supported native record or explicitly bound sequence",
                                owner="binding gap",
                                remedy="Pass a native record, or use binding='dataclass' for list[T]/tuple[T, ...]")
    native: dict[str, type] = {}
    definitions: dict[str, RecordDefinition] = {}
    selected = select_binding(root_type, binding)
    detected_plain = binding is None and not _has_xml_metadata(root_type) and not is_pydantic

    def register(cls: type, path: str) -> str:
        if detected_plain and _has_xml_metadata(cls):
            raise StructureMapError(
                "unavailable_binding", "A nested XML model requires the xsdata binding", path=path,
                owner="binding gap",
                remedy="Use binding='xsdata' for the root, or explicitly choose binding='dataclass' for a plain view",
            )
        name = f"{cls.__module__}.{cls.__qualname__}"
        if name in native:
            if native[name] is not cls:
                raise StructureMapError("definition_collision", f"Distinct native classes share {name!r}",
                                        path=path, remedy="Give the classes distinct qualified names")
            return name
        native[name] = cls
        setattr(selected, "_native_types", native)
        if not is_dataclass(cls) and not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            return name
        definitions[name] = RecordDefinition(name=name, members=())
        described = selected.describe_record(cls, register, path=path)
        definitions[name] = described
        return name

    root_shape = selected.describe_root(root_type, register, path="")
    root = root_shape.definition if isinstance(root_shape, RecordRef) else root_shape
    return DerivedModel(structure=StructureSpec(root=root, definitions=definitions),
                        native_types=native, binding=selected.name, root_native=root_type)

