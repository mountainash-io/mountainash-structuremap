from __future__ import annotations

import re
from typing import Literal, TypeVar, cast

from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap.bindings.dataclass import DataclassBinding
from mountainash_structuremap.definitions import (
    DerivedModel,
    Map,
    Nullable,
    Positional,
    RecordRef,
    Scalar,
    Sequence,
    Shape,
)
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import MappingSpec
from mountainash_structuremap.prepared import (
    LinkProgram,
    MapProgram,
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
    TablePlan,
    TupleProgram,
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


def _record_refs(shape: Shape) -> set[str]:
    if isinstance(shape, RecordRef):
        return {shape.definition}
    if isinstance(shape, Nullable):
        return _record_refs(shape.value)
    if isinstance(shape, Sequence):
        return _record_refs(shape.item)
    if isinstance(shape, Map):
        return _record_refs(shape.key) | _record_refs(shape.value)
    if isinstance(shape, Positional):
        return set().union(*(_record_refs(item) for item in shape.items)) if shape.items else set()
    return set()


def _components(model: DerivedModel[object]) -> dict[str, int]:
    graph = {
        name: set().union(*(_record_refs(member.shape) for member in definition.members))
        for name, definition in model.structure.definitions.items()
    }
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    component: dict[str, int] = {}
    component_id = 0

    def visit(node: str) -> None:
        nonlocal index, component_id
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for child in graph[node]:
            if child not in graph:
                continue
            if child not in indices:
                visit(child)
                low[node] = min(low[node], low[child])
            elif child in on_stack:
                low[node] = min(low[node], indices[child])
        if low[node] == indices[node]:
            while True:
                child = stack.pop()
                on_stack.remove(child)
                component[child] = component_id
                if child == node:
                    break
            component_id += 1

    for name in graph:
        if name not in indices:
            visit(name)
    return component


def _program_fields(program_id: int, programs: dict[int, ValueProgram]) -> list[FieldSpec]:
    program = programs[program_id]
    if isinstance(program, ScalarProgram):
        return [FieldSpec(name=program.column, type=_TYPES[program.scalar_type], missing_values=[])]
    if isinstance(program, RecordProgram):
        return [field for _, child in program.members for field in _program_fields(child, programs)]
    if isinstance(program, TupleProgram):
        return [field for child in program.items for field in _program_fields(child, programs)]
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
    map_key_col = naming.metadata_namespace + "map_key"
    null_col = naming.metadata_namespace + "null"
    parent_table_col = naming.metadata_namespace + "parent_table"
    programs: dict[int, ValueProgram] = {}
    next_program = 0
    tables: dict[str, dict[str, object]] = {}
    recursive_component = _components(cast(DerivedModel[object], model))
    role_records: dict[tuple[str, str, bool], int] = {}
    collection_roles: dict[str, tuple[str, str, str, str]] = {}
    recursive_roles: dict[tuple[str, str, str], str] = {}

    def norm(value: str) -> str:
        return _snake(value) if naming.column_naming == "snake_case" else value

    def path_table(path: str) -> str:
        return "__".join(norm(part) for part in path.split("/") if part) or "root"

    def recursive_table(entry_path: str, source_definition: str, member_role: str) -> str:
        role = (entry_path, source_definition, member_role)
        table = recursive_roles.get(role)
        if table is None:
            table = path_table("/".join(filter(None, (entry_path, member_role))))
            recursive_roles[role] = table
        return table

    def claim_collection_role(
        table: str, entry_path: str, source_definition: str, member_role: str, kind: str, path: str
    ) -> None:
        role = (entry_path, source_definition, member_role, kind)
        existing = collection_roles.get(table)
        if existing is not None and existing != role:
            raise StructureMapError(
                "table_collision",
                f"Multiple uses resolve to table {table!r}",
                path=path,
                remedy="Use distinct field paths or a different naming policy",
            )
        collection_roles[table] = role

    def alloc(program: ValueProgram) -> int:
        nonlocal next_program
        program_id = next_program
        next_program += 1
        programs[program_id] = program
        return program_id

    def add_table(
        table: str,
        path: str,
        parent: str,
        program_id: int,
        kind: str,
        nullable_member: bool,
    ) -> None:
        existing = tables.get(table)
        if existing is None:
            tables[table] = {
                "path": path,
                "parents": {parent},
                "program": program_id,
                "kind": kind,
                "nullable_member": nullable_member,
            }
        else:
            if existing["program"] != program_id:
                raise StructureMapError(
                    "table_collision",
                    f"Multiple uses resolve to table {table!r}",
                    path=path,
                    remedy="Use distinct field paths or a different naming policy",
                )
            cast(set[str], existing["parents"]).add(parent)

    def record_program(
        definition: str,
        path: str,
        prefix: str = "",
        *,
        nullable: bool = False,
        entry_path: str = "",
        owner_role: str = "root",
        row_table: str | None = None,
        row_kind: str = "single",
        row_parent: str = "root",
        nullable_member: bool = False,
    ) -> int:
        role_key = (row_table, definition, nullable) if row_table is not None else None
        if role_key is not None and role_key in role_records:
            assert row_table is not None
            program_id = role_records[role_key]
            add_table(row_table, path, row_parent, program_id, row_kind, nullable_member)
            return program_id
        program_id = alloc(RecordProgram(path, definition, (), nullable))
        if role_key is not None:
            assert row_table is not None
            role_records[role_key] = program_id
            add_table(row_table, path, row_parent, program_id, row_kind, nullable_member)
        members: list[tuple[str, int]] = []
        for member in model.structure.definitions[definition].members:
            member_path = "/".join(filter(None, (path, member.name)))
            col = norm("_".join(filter(None, (prefix, member.name))))
            child = compile_shape(
                member.shape,
                member_path,
                col,
                owner_role=row_table or owner_role,
                source_definition=definition,
                entry_path=entry_path,
                relative_path=member.name,
            )
            members.append((member.name, child))
        programs[program_id] = RecordProgram(path, definition, tuple(members), nullable)
        return program_id

    def compile_shape(
        shape: Shape,
        path: str,
        column: str,
        *,
        owner_role: str,
        source_definition: str,
        entry_path: str,
        relative_path: str,
    ) -> int:
        nullable = isinstance(shape, Nullable)
        value_shape = shape.value if isinstance(shape, Nullable) else shape
        if isinstance(value_shape, Scalar):
            return alloc(ScalarProgram(path, column, value_shape.type, nullable))
        if isinstance(value_shape, RecordRef):
            same_component = recursive_component.get(source_definition) == recursive_component.get(
                value_shape.definition
            )
            if same_component:
                table = recursive_table(entry_path, source_definition, relative_path)
                record_program(
                    value_shape.definition,
                    path,
                    nullable=False,
                    entry_path=entry_path,
                    row_table=table,
                    row_kind="single",
                    row_parent=owner_role,
                )
                return alloc(LinkProgram(path, table, nullable))
            return record_program(
                value_shape.definition,
                path,
                prefix=column,
                nullable=nullable,
                entry_path=path,
                owner_role=owner_role,
            )
        if isinstance(value_shape, Sequence):
            table = path_table("/".join(filter(None, (entry_path, relative_path))))
            member_nullable = isinstance(value_shape.item, Nullable)
            member_shape = value_shape.item.value if isinstance(value_shape.item, Nullable) else value_shape.item
            item_path = f"{path}/item" if isinstance(member_shape, (Sequence, Map)) else path
            item_relative = f"{relative_path}/item" if isinstance(member_shape, (Sequence, Map)) else relative_path
            structured = not isinstance(member_shape, Scalar)
            claim_collection_role(table, entry_path, source_definition, relative_path, "sequence", path)
            existing = tables.get(table)
            if existing is not None:
                if existing["kind"] != "sequence" or existing["nullable_member"] != (member_nullable and structured):
                    raise StructureMapError(
                        "table_collision",
                        f"Multiple uses resolve to table {table!r}",
                        path=path,
                        remedy="Choose distinct table names for distinct collection uses",
                    )
                add_table(
                    table,
                    path,
                    owner_role,
                    cast(int, existing["program"]),
                    "sequence",
                    member_nullable and structured,
                )
                return alloc(SequenceProgram(path, table, nullable))
            if isinstance(member_shape, RecordRef):
                item_entry = (
                    entry_path
                    if recursive_component.get(source_definition) == recursive_component.get(member_shape.definition)
                    else item_path
                )
                payload = record_program(
                    member_shape.definition,
                    item_path,
                    entry_path=item_entry,
                    row_table=table,
                    row_kind="sequence",
                    row_parent=owner_role,
                    nullable_member=member_nullable and structured,
                )
            else:
                payload_shape = (
                    value_shape.item if member_nullable and isinstance(member_shape, Scalar) else member_shape
                )
                payload = compile_shape(
                    payload_shape,
                    item_path,
                    "value" if isinstance(member_shape, Scalar) else "",
                    owner_role=table,
                    source_definition=source_definition,
                    entry_path=entry_path,
                    relative_path=item_relative,
                )
                add_table(
                    table,
                    path,
                    owner_role,
                    payload,
                    "sequence",
                    member_nullable and structured,
                )
            return alloc(SequenceProgram(path, table, nullable))
        if isinstance(value_shape, Map):
            if not isinstance(value_shape.key, Scalar) or value_shape.key.type != "str":
                raise StructureMapError(
                    "unavailable_construct",
                    "Maps require string keys",
                    path=path,
                    owner="binding gap",
                    remedy="Declare string map keys; other key types are unavailable in Foundation",
                )
            table = path_table("/".join(filter(None, (entry_path, relative_path))))
            member_nullable = isinstance(value_shape.value, Nullable)
            member_shape = value_shape.value.value if isinstance(value_shape.value, Nullable) else value_shape.value
            item_path = f"{path}/item" if isinstance(member_shape, (Sequence, Map)) else path
            item_relative = f"{relative_path}/item" if isinstance(member_shape, (Sequence, Map)) else relative_path
            structured = not isinstance(member_shape, Scalar)
            claim_collection_role(table, entry_path, source_definition, relative_path, "map", path)
            existing = tables.get(table)
            if existing is not None:
                if existing["kind"] != "map" or existing["nullable_member"] != (member_nullable and structured):
                    raise StructureMapError(
                        "table_collision",
                        f"Multiple uses resolve to table {table!r}",
                        path=path,
                        remedy="Choose distinct table names for distinct collection uses",
                    )
                add_table(
                    table,
                    path,
                    owner_role,
                    cast(int, existing["program"]),
                    "map",
                    member_nullable and structured,
                )
                return alloc(MapProgram(path, table, nullable))
            if isinstance(member_shape, RecordRef):
                item_entry = (
                    entry_path
                    if recursive_component.get(source_definition) == recursive_component.get(member_shape.definition)
                    else item_path
                )
                payload = record_program(
                    member_shape.definition,
                    item_path,
                    entry_path=item_entry,
                    row_table=table,
                    row_kind="map",
                    row_parent=owner_role,
                    nullable_member=member_nullable and structured,
                )
            else:
                payload_shape = (
                    value_shape.value if member_nullable and isinstance(member_shape, Scalar) else member_shape
                )
                payload = compile_shape(
                    payload_shape,
                    item_path,
                    "value" if isinstance(member_shape, Scalar) else "",
                    owner_role=table,
                    source_definition=source_definition,
                    entry_path=entry_path,
                    relative_path=item_relative,
                )
                add_table(
                    table,
                    path,
                    owner_role,
                    payload,
                    "map",
                    member_nullable and structured,
                )
            return alloc(MapProgram(path, table, nullable))
        if isinstance(value_shape, Positional):
            items = tuple(
                compile_shape(
                    item,
                    f"{path}/{index}",
                    norm(f"{column}_{index}"),
                    owner_role=owner_role,
                    source_definition=source_definition,
                    entry_path=entry_path,
                    relative_path=f"{relative_path}/{index}",
                )
                for index, item in enumerate(value_shape.items)
            )
            return alloc(TupleProgram(path, items, nullable))
        raise StructureMapError(
            "unavailable_construct",
            "This structural shape has no executor in this delivery",
            path=path,
            owner="binding gap",
            remedy="Use scalar, record, sequence, map, or fixed tuple shapes",
        )

    root_id = record_program(model.structure.root, "")
    # Recursive tables are interned by row role before their payload is expanded. Complete
    # their possible-parent sets before fixing schemas and parent-table metadata.
    add_table("root", "", "", root_id, "root", False)
    plans: dict[str, TablePlan] = {}
    for name, descriptor in tables.items():
        program_id = cast(int, descriptor["program"])
        kind = cast(str, descriptor["kind"])
        parents = tuple(sorted(cast(set[str], descriptor["parents"])))
        fields = [FieldSpec(name=key_col, type=UniversalType.STRING, missing_values=[])]
        if parents != ("",):
            fields.append(FieldSpec(name=parent_col, type=UniversalType.STRING, missing_values=[]))
            if kind in ("sequence", "map"):
                fields.append(FieldSpec(name=position_col, type=UniversalType.INTEGER, missing_values=[]))
            if kind == "map":
                fields.append(FieldSpec(name=map_key_col, type=UniversalType.STRING, missing_values=[]))
            if len(parents) > 1:
                fields.append(FieldSpec(name=parent_table_col, type=UniversalType.STRING, missing_values=[]))
        if cast(bool, descriptor["nullable_member"]):
            fields.append(FieldSpec(name=null_col, type=UniversalType.BOOLEAN, missing_values=[]))
        fields.extend(_program_fields(program_id, programs))
        columns = tuple(field.name for field in fields)
        if len(set(columns)) != len(columns):
            raise StructureMapError(
                "column_collision",
                f"Columns collide in table {name!r}",
                path=cast(str, descriptor["path"]),
                table=name,
                remedy="Change column_naming or metadata_namespace, or use distinct field names",
            )
        plans[name] = TablePlan(
            name=name,
            source_path=cast(str, descriptor["path"]),
            parent_tables=tuple(parent for parent in parents if parent),
            columns=columns,
            _typespec=TypeSpec(fields=fields, missing_values=[]),
            _program=program_id,
            _kind=cast(Literal["root", "single", "sequence", "map"], kind),
            _nullable_member=cast(bool, descriptor["nullable_member"]),
        )
    binding = DataclassBinding()
    return PreparedMapping(
        root=root_id,
        programs=programs,
        root_table="root",
        root_type=cast(type[T], model.native_types[model.structure.root]),
        tables=plans,
        native_types=model.native_types,
        binding=binding,
        capabilities=binding.capabilities,
        key_column=key_col,
        parent_column=parent_col,
        position_column=position_col,
        map_key_column=map_key_col,
        null_column=null_col,
        parent_table_column=parent_table_col,
    )
