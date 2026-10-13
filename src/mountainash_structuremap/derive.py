from __future__ import annotations

import sys
from dataclasses import MISSING
from types import ModuleType
from typing import Literal, TypeVar, get_type_hints

from mountainash_structuremap.bindings.dataclass import describe_annotation, record_fields
from mountainash_structuremap.definitions import DerivedModel, Member, RecordDefinition, StructureSpec
from mountainash_structuremap.errors import StructureMapError

T = TypeVar("T")


def derive(root_type: type[T], *, binding: str | None = None) -> DerivedModel[T]:
    if not isinstance(root_type, type):
        raise StructureMapError(
            "unsupported_root",
            "Root must be a dataclass type",
            owner="binding gap",
            remedy="Pass a dataclass class",
        )
    if binding not in (None, "dataclass"):
        raise StructureMapError(
            "unavailable_binding",
            f"Binding {binding!r} is unavailable",
            owner="binding gap",
            remedy="Use the available dataclass binding for a plain dataclass",
        )
    native: dict[str, type] = {}
    definitions: dict[str, RecordDefinition] = {}

    def register(cls: type, path: str) -> str:
        name = f"{cls.__module__}.{cls.__qualname__}"
        if name in native:
            if native[name] is not cls:
                raise StructureMapError(
                    "definition_collision",
                    f"Distinct native classes share {name!r}",
                    path=path,
                    remedy="Give the classes distinct qualified names",
                )
            return name
        declared_fields = record_fields(cls, detected=binding is None, path=path)
        native[name] = cls
        # Intern before descending so recursive references stay finite.
        definitions[name] = RecordDefinition(name=name, members=())
        field_names = {item.name for item in declared_fields}
        hints: dict[str, object] = {}
        try:
            for base in reversed(cls.__mro__):
                annotations = {
                    name: annotation
                    for name, annotation in vars(base).get("__annotations__", {}).items()
                    if name in field_names
                }
                if not annotations:
                    continue
                # A class call re-evaluates inherited hints in one supplied namespace.
                # A module carrier resolves only this owner's declarations, without
                # creating a native class or changing any application's globals.
                scope = ModuleType(base.__module__)
                scope.__annotations__ = annotations
                module = sys.modules.get(base.__module__)
                hints.update(
                    get_type_hints(
                        scope,
                        globalns=vars(module) if module else {},
                        localns={**vars(base), base.__name__: base},
                    )
                )
        except Exception as exc:
            raise StructureMapError(
                "annotation_resolution_failed",
                "Could not resolve dataclass annotations",
                path=path,
                owner="binding gap",
                remedy="Make forward-referenced classes available in their declaring module or class namespace",
            ) from exc
        members: list[Member] = []
        for item in declared_fields:
            member_path = "/".join(filter(None, (path, item.name)))
            shape = describe_annotation(hints[item.name], member_path, register)
            default: Literal["required", "value", "factory"] = "required"
            if item.default_factory is not MISSING:
                default = "factory"
            elif item.default is not MISSING:
                default = "value"
            members.append(Member(name=item.name, shape=shape, default_kind=default))
        definitions[name] = RecordDefinition(name=name, members=tuple(members))
        return name

    root = register(root_type, "")
    return DerivedModel(
        structure=StructureSpec(root=root, definitions=definitions),
        native_types=native,
        binding="dataclass",
    )
