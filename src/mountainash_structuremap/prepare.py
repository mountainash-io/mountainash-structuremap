from __future__ import annotations

import re
from typing import TypeVar, cast

from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap.bindings.dataclass import DataclassBinding
from mountainash_structuremap.definitions import DerivedModel, Nullable, RecordRef, Scalar, Sequence, Shape
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import MappingSpec
from mountainash_structuremap.prepared import (
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
    TablePlan,
    ValueProgram,
)

T = TypeVar("T")
_TYPES = {
    "str": UniversalType.STRING,
    "int": UniversalType.INTEGER,
    "float": UniversalType.NUMBER,
    "bool": UniversalType.BOOLEAN,
}


def _snake(value: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()


def _program_fields(program: ValueProgram) -> list[FieldSpec]:
    if isinstance(program, ScalarProgram):
        return [FieldSpec(name=program.column, type=_TYPES[program.scalar_type], missing_values=[])]
    if isinstance(program, RecordProgram):
        return [field for _, child in program.members for field in _program_fields(child)]
    return []


def prepare(
    model: DerivedModel[T],
    mapping: MappingSpec | None = None,
    *,
    validate_native: bool = False,
) -> PreparedMapping[T]:
    mapping = mapping or MappingSpec()
    if mapping.root not in (None, model.structure.root):
        raise StructureMapError(
            "unknown_root",
            "Mapping root does not select the derived root",
            path=mapping.root or "",
            remedy="Select the derived model root",
        )
    if mapping.defaults.binding not in (None, model.binding):
        raise StructureMapError(
            "binding_mismatch",
            "Requested mapping binding differs from derivation",
            remedy="Derive and prepare with the same binding",
        )
    if validate_native or mapping.defaults.presence != "normalize" or mapping.defaults.positions != "strict":
        raise StructureMapError(
            "unavailable_capability",
            "This binding supports Normalize presence and strict positions, without Validate",
            owner="binding gap",
            remedy="Use presence='normalize', positions='strict', validate_native=False",
        )
    if mapping.overrides:
        raise StructureMapError(
            "unavailable_capability",
            "Per-use overrides are not available in this delivery",
            remedy="Prepare without overrides; per-use authoring is not delivered yet",
        )
    naming = mapping.defaults.naming
    key_col = naming.metadata_namespace + "key"
    parent_col = naming.metadata_namespace + "parent"
    position_col = naming.metadata_namespace + "position"
    tables: dict[str, TablePlan] = {}
    active: set[str] = set()

    def norm(name: str) -> str:
        return _snake(name) if naming.column_naming == "snake_case" else name

    def add_table(name: str, path: str, parent: str | None, program: ValueProgram) -> None:
        if name in tables:
            raise StructureMapError(
                "table_collision",
                f"Multiple uses resolve to table {name!r}",
                path=path,
                remedy="Use distinct field paths or a different naming policy; per-use overrides are not available yet",
            )
        fields = [FieldSpec(name=key_col, type=UniversalType.STRING, missing_values=[])]
        if parent is not None:
            fields.extend(
                (
                    FieldSpec(name=parent_col, type=UniversalType.STRING, missing_values=[]),
                    FieldSpec(name=position_col, type=UniversalType.INTEGER, missing_values=[]),
                )
            )
        fields.extend(_program_fields(program))
        columns = tuple(field.name for field in fields)
        if len(set(columns)) != len(columns):
            raise StructureMapError(
                "column_collision",
                f"Columns collide in table {name!r}",
                path=path,
                table=name,
                remedy="Change column_naming or metadata_namespace, or use distinct field names",
            )
        tables[name] = TablePlan(
            name,
            path,
            (parent,) if parent is not None else (),
            columns,
            TypeSpec(fields=fields, missing_values=[]),
        )

    def record_program(
        definition: str,
        path: str,
        owner_table: str,
        prefix: str = "",
        nullable: bool = False,
    ) -> RecordProgram:
        if definition in active:
            raise StructureMapError(
                "unavailable_construct",
                "Recursive execution is not available in this delivery",
                path=path,
                owner="binding gap",
                remedy="No recursive execution strategy is available yet",
            )
        active.add(definition)
        members: list[tuple[str, ValueProgram]] = []
        for member in model.structure.definitions[definition].members:
            member_path = "/".join(filter(None, (path, member.name)))
            column = norm("_".join(filter(None, (prefix, member.name))))
            members.append((member.name, compile_value(member.shape, member_path, owner_table, column)))
        active.remove(definition)
        return RecordProgram(path, definition, tuple(members), nullable)

    def compile_value(shape: Shape, path: str, owner_table: str, column: str) -> ValueProgram:
        nullable = isinstance(shape, Nullable)
        if isinstance(shape, Nullable):
            shape = shape.value
        if isinstance(shape, Scalar):
            return ScalarProgram(path, column, shape.type, nullable)
        if isinstance(shape, RecordRef):
            return record_program(shape.definition, path, owner_table, column, nullable)
        if isinstance(shape, Sequence):
            table = "__".join(norm(part) for part in path.split("/"))
            item = shape.item
            item_nullable = isinstance(item, Nullable)
            if isinstance(item, Nullable):
                item = item.value
            item_program: ValueProgram
            if isinstance(item, Scalar):
                item_program = ScalarProgram(path, "value", item.type, item_nullable)
            elif isinstance(item, RecordRef) and not item_nullable:
                item_program = record_program(item.definition, path, table)
            else:
                raise StructureMapError(
                    "unavailable_construct",
                    "Nested or nullable structured collection members are not available",
                    path=path,
                    owner="binding gap",
                    remedy="Use scalar or non-null record list members",
                )
            add_table(table, path, owner_table, item_program)
            return SequenceProgram(path, table, item_program, nullable)
        raise StructureMapError(
            "unavailable_construct",
            "This structural shape has no executor in this delivery",
            path=path,
            owner="binding gap",
            remedy="Use scalar fields, inline records, or scalar/record lists",
        )

    root = record_program(model.structure.root, "", "root")
    add_table("root", "", None, root)
    binding = DataclassBinding()
    return PreparedMapping(
        root,
        cast(type[T], model.native_types[model.structure.root]),
        tables,
        model.native_types,
        binding,
        binding.capabilities,
        key_col,
        parent_col,
        position_col,
    )
