from __future__ import annotations

from enum import Enum
from typing import TypeVar

from mountainash_structuremap.bindings.selection import _has_xml_metadata, select_binding
from mountainash_structuremap.definitions import DerivedModel, RecordDefinition, StructureSpec
from mountainash_structuremap.errors import StructureMapError

T = TypeVar("T")


def derive(root_type: type[T], *, binding: str | None = None) -> DerivedModel[T]:
    if not isinstance(root_type, type) or issubclass(root_type, Enum):
        raise StructureMapError("unsupported_root", "Root must be a native model class", owner="binding gap",
                                remedy="Pass a supported dataclass model type")
    native: dict[str, type] = {}
    definitions: dict[str, RecordDefinition] = {}
    selected = select_binding(root_type, binding)
    detected_plain = binding is None and not _has_xml_metadata(root_type)

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
        if issubclass(cls, Enum):
            return name
        definitions[name] = RecordDefinition(name=name, members=())
        described = selected.describe_record(cls, register, path=path)
        definitions[name] = described
        return name

    root = register(root_type, "")
    return DerivedModel(structure=StructureSpec(root=root, definitions=definitions),
                        native_types=native, binding="xsdata" if type(selected).__name__ == "XsdataBinding" else "dataclass")
