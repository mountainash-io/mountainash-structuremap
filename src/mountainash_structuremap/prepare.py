from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from typing import Literal, TypeVar, cast

from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap.bindings.contracts import RecordAdapter
from mountainash_structuremap.bindings.selection import select_binding
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
from mountainash_structuremap.mapping import DeclaredBinding, MappingSpec
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
        return list(program.codec.fields)
    if isinstance(program, RecordProgram):
        return [field for _, child in program.members for field in _program_fields(child, programs)]
    if isinstance(program, TupleProgram):
        return [field for child in program.items for field in _program_fields(child, programs)]
    return []


def _program_scalar_paths(program_id: int, programs: dict[int, ValueProgram]) -> list[tuple[str, str]]:
    program = programs[program_id]
    if isinstance(program, ScalarProgram):
        return [(field.name, program.path) for field in program.codec.fields]
    if isinstance(program, RecordProgram):
        return [item for _, child in program.members for item in _program_scalar_paths(child, programs)]
    if isinstance(program, TupleProgram):
        return [item for child in program.items for item in _program_scalar_paths(child, programs)]
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
    requested_binding = mapping.defaults.binding
    declared_binding = requested_binding.name if isinstance(requested_binding, DeclaredBinding) else requested_binding
    if declared_binding not in (None, "detected", model.binding):
        raise StructureMapError(
            "binding_mismatch",
            "Requested mapping binding differs from derivation",
            remedy="Derive and prepare with the same binding",
        )
    native_binding = select_binding(model.native_types[model.structure.root], model.binding)
    record_adapters: dict[str, RecordAdapter] = {}
    if validate_native:
        raise StructureMapError(
            "unavailable_capability",
            f"The {model.binding} binding does not provide native validation",
            owner="binding gap",
            remedy=f"Set validate_native=False; the {model.binding} binding does not provide native validation",
        )
    if mapping.defaults.presence != "normalize" or mapping.defaults.positions != "strict":
        path = "presence" if mapping.defaults.presence != "normalize" else "positions"
        raise StructureMapError(
            "unavailable_capability",
            f"The requested {path} policy is not available in this delivery",
            owner="binding gap",
            path=path,
            remedy="Use presence='normalize' and positions='strict'",
        )
    global_unavailable = (
        "construction",
        "variant_layout",
        "role_discriminator",
        "shared_types",
        "value_space",
        "json_numbers",
        "duplicate_members",
        "open_content",
    )
    for policy in global_unavailable:
        if getattr(mapping.defaults, policy) is not None:
            raise StructureMapError(
                "unavailable_capability",
                f"The declared {policy} policy is not available in this delivery",
                owner="binding gap",
                path=policy,
                remedy=f"No {policy} strategy is available in Foundation; omit this request to use the plain-dataclass binding",
            )

    recursive_component = _components(cast(DerivedModel[object], model))

    def collection_payload(shape: Shape) -> Shape:
        while isinstance(shape, (Nullable, Sequence, Map)):
            shape = shape.item if isinstance(shape, Sequence) else shape.value
        return shape

    def target(path: str) -> tuple[str, Shape]:
        if not path:
            return "root", RecordRef(definition=model.structure.root)
        current: Shape = RecordRef(definition=model.structure.root)
        source_definition = model.structure.root
        for segment in path.split("/"):
            current = collection_payload(current)
            if isinstance(current, RecordRef):
                source_definition = current.definition
                members = {
                    member.name: member.shape for member in model.structure.definitions[current.definition].members
                }
                if segment in members:
                    current = members[segment]
                    continue
            elif isinstance(current, Positional) and segment.isdecimal():
                index = int(segment)
                if str(index) == segment and index < len(current.items):
                    current = current.items[index]
                    continue
            raise StructureMapError(
                "unknown_path",
                f"Mapping path does not exist at {segment!r}",
                path=path,
                remedy="Use declared native member names, transparent collection membership and fixed tuple ordinals",
            )
        unwrapped = current.value if isinstance(current, Nullable) else current
        if isinstance(unwrapped, (Sequence, Map)):
            kind = "collection"
        elif isinstance(unwrapped, RecordRef):
            kind = (
                "link"
                if recursive_component[source_definition] == recursive_component[unwrapped.definition]
                else "record"
            )
        else:
            kind = (
                "scalar"
                if isinstance(unwrapped, Scalar)
                else "tuple"
                if isinstance(unwrapped, Positional)
                else "unsupported"
            )
        return kind, current

    def unavailable(path: str, policy: str) -> None:
        raise StructureMapError(
            "unavailable_capability",
            f"The declared {policy} policy is not available in this delivery",
            owner="binding gap",
            path=path,
            remedy={
                "presence": "Use presence='normalize'; plain dataclasses cannot retain supplied-field presence",
                "positions": "Use positions='strict'; lenient reconstruction is unavailable in Foundation",
                "key": "Use key='generated'; source-backed keys are unavailable in Foundation",
                "ordered": "Use ordered=True; unordered collections are unavailable in Foundation",
                "collection_table": "Use collection_table='separate' and distinct table names",
            }.get(policy, f"No {policy} strategy is available in Foundation; omit the request to use this binding"),
        )

    for path, override in mapping.overrides.items():
        kind, shape = target(path)
        payload = collection_payload(shape) if kind == "collection" else shape
        if override.table is not None and kind not in ("root", "collection", "link"):
            raise StructureMapError(
                "inapplicable_override",
                "Table names apply only to root, collection or recursive link uses",
                path=path,
                remedy="Set table on a use that owns a row; nonrecursive records are inline",
            )
        if override.column is not None and (
            kind not in ("scalar", "collection") or (kind == "collection" and not isinstance(payload, Scalar))
        ):
            raise StructureMapError(
                "inapplicable_override",
                "Column names apply only to scalar payload uses",
                path=path,
                remedy="Set column on a scalar or scalar collection use",
            )
        if override.prefix is not None and not (
            kind in ("root", "record", "link") or (kind == "collection" and isinstance(payload, RecordRef))
        ):
            raise StructureMapError(
                "inapplicable_override",
                "Prefixes apply only to record projections",
                path=path,
                remedy="Set prefix on a record or record collection use",
            )
        binding = override.binding
        if binding is not None:
            selected = binding.name if isinstance(binding, DeclaredBinding) else binding
            if selected not in ("detected", model.binding):
                raise StructureMapError(
                    "binding_mismatch",
                    "Requested mapping binding differs from derivation",
                    path=path,
                    remedy="Derive and prepare with the same binding",
                )
        for policy in global_unavailable:
            if getattr(override, policy) is not None:
                unavailable(path, policy)
        collection_policies = ("key", "positions", "ordered", "collection_table")
        for policy in collection_policies:
            if getattr(override, policy) is not None and kind != "collection":
                raise StructureMapError(
                    "inapplicable_override",
                    f"The {policy} policy applies only to a collection use",
                    path=path,
                    remedy="Set this policy on a collection use",
                )
        unsupported = (
            "key",
            "positions",
            "ordered",
            "collection_table",
            "discriminator",
            "variant_layout",
            "role_discriminator",
            "shared_types",
            "cast",
            "presence",
            "value_space",
            "json_numbers",
            "duplicate_members",
            "open_content",
        )
        for policy in unsupported:
            value = getattr(override, policy)
            if value is not None and not (
                (policy == "key" and value == "generated")
                or (policy == "positions" and value == "strict")
                or (policy == "ordered" and value is True)
                or (policy == "collection_table" and value == "separate")
                or (policy == "presence" and value == "normalize")
            ):
                unavailable(path, policy)

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
    roles: dict[tuple[str, str, str, str, tuple[tuple[str, str], ...]], str] = {}
    pending: deque[Callable[[], None]] = deque()
    layout_overrides = tuple(
        path
        for path, override in sorted(mapping.overrides.items())
        if any(value is not None for value in (override.table, override.column, override.prefix))
    )

    def norm(value: str) -> str:
        return _snake(value) if naming.column_naming == "snake_case" else value

    def join(path: str, segment: str) -> str:
        return f"{path}/{segment}" if path else segment

    def reserve() -> int:
        nonlocal next_program
        program_id = next_program
        next_program += 1
        return program_id

    def emit(program: ValueProgram, destination: int | None = None) -> int:
        program_id = reserve() if destination is None else destination
        programs[program_id] = program
        return program_id

    def record_program(
        definition: str,
        path: str,
        authored_path: str,
        prefix: str,
        *,
        family: str,
        owner: str,
        nullable: bool = False,
        destination: int | None = None,
    ) -> int:
        if definition not in record_adapters:
            record_adapters[definition] = native_binding.prepare_record(
                model.native_types[definition], model.structure.definitions[definition], path=authored_path
            )
        members = model.structure.definitions[definition].members
        children = tuple(
            (
                member.name,
                compile_shape(
                    member.shape,
                    join(path, member.name),
                    join(authored_path, member.name),
                    "_".join(filter(None, (prefix, norm(member.xml.name if member.xml and member.xml.name else member.name)))),
                    family=family,
                    owner=owner,
                    source_definition=definition,
                    relative_path=member.name,
                    format=member.xml.format if member.xml else None,
                ),
            )
            for member in members
        )
        return emit(RecordProgram(authored_path, definition, children, nullable), destination)

    def row_table(
        payload: Shape,
        path: str,
        authored_path: str,
        *,
        family: str,
        owner: str,
        source_definition: str,
        relative_path: str,
        kind: Literal["single", "sequence", "map"],
        nullable_member: bool,
        anonymous_parent: str | None = None,
    ) -> str:
        # Context contains only suffixes of finite authored selectors and their
        # declaration identities. Runtime depth is never an interning key.
        context = tuple(
            (selector[len(authored_path) :].lstrip("/"), selector)
            for selector in layout_overrides
            if selector == authored_path or selector.startswith(authored_path + "/")
        )
        role = (family, source_definition, relative_path, kind, context)
        if role in roles:
            table = roles[role]
            cast(set[str], tables[table]["parents"]).add(owner)
            return table
        override = mapping.overrides.get(authored_path)
        if anonymous_parent is not None:
            table = anonymous_parent + "__item"
        elif override is not None and override.table is not None:
            table = override.table
        else:
            table = "__".join(norm(part) for part in path.split("/"))
        if table in tables:
            prior = tables[table]["path"]
            raise StructureMapError(
                "table_collision",
                f"Multiple uses at {prior!r} and {authored_path!r} resolve to table {table!r}",
                path=authored_path,
                table=table,
                remedy="Choose distinct explicit table names; shared tables are unavailable in Foundation",
            )
        payload_id = reserve()
        roles[role] = table
        # Reserve every row role before descending, including container payloads.
        tables[table] = {
            "path": authored_path,
            "parents": {owner},
            "program": payload_id,
            "kind": kind,
            "nullable_member": nullable_member,
        }

        def compile_payload() -> None:
            if isinstance(payload, RecordRef):
                next_family = (
                    family
                    if recursive_component[source_definition] == recursive_component[payload.definition]
                    else path
                )
                prefix = override.prefix if override is not None and override.prefix is not None else ""
                record_program(
                    payload.definition,
                    path,
                    authored_path,
                    prefix,
                    family=next_family,
                    owner=table,
                    destination=payload_id,
                )
            else:
                nested = isinstance(payload, (Sequence, Map))
                compile_shape(
                    payload,
                    join(path, "item") if nested else path,
                    authored_path,
                    "value" if isinstance(payload, (Scalar, Nullable)) else "",
                    family=family,
                    owner=table,
                    source_definition=source_definition,
                    relative_path=join(relative_path, "item") if nested else relative_path,
                    anonymous_parent=table if nested else None,
                    destination=payload_id,
                )

        pending.append(compile_payload)
        return table

    def compile_shape(
        shape: Shape,
        path: str,
        authored_path: str,
        column: str,
        *,
        family: str,
        owner: str,
        source_definition: str,
        relative_path: str,
        format: str | None = None,
        anonymous_parent: str | None = None,
        destination: int | None = None,
    ) -> int:
        nullable = isinstance(shape, Nullable)
        value_shape = shape.value if isinstance(shape, Nullable) else shape
        override = mapping.overrides.get(authored_path)
        if isinstance(value_shape, Scalar):
            scalar_column = override.column if override is not None and override.column is not None else column
            native_type = model.native_types.get(value_shape.native_type) if value_shape.native_type else None
            codec = native_binding.prepare_scalar(
                value_shape, native_type=native_type, format=format, column=scalar_column,
                policy=None, path=authored_path,
            )
            return emit(ScalarProgram(authored_path, codec, nullable), destination)
        if isinstance(value_shape, RecordRef):
            if recursive_component[source_definition] == recursive_component[value_shape.definition]:
                table = row_table(
                    value_shape,
                    path,
                    authored_path,
                    family=family,
                    owner=owner,
                    source_definition=source_definition,
                    relative_path=relative_path,
                    kind="single",
                    nullable_member=False,
                )
                return emit(LinkProgram(authored_path, table, nullable), destination)
            prefix = override.prefix if override is not None and override.prefix is not None else column
            return record_program(
                value_shape.definition,
                path,
                authored_path,
                prefix,
                family=path,
                owner=owner,
                nullable=nullable,
                destination=destination,
            )
        if isinstance(value_shape, (Sequence, Map)):
            if isinstance(value_shape, Map):
                if not isinstance(value_shape.key, Scalar) or value_shape.key.type != "str":
                    raise StructureMapError(
                        "unavailable_construct",
                        "Maps require string keys",
                        path=authored_path,
                        owner="binding gap",
                        remedy="Declare string map keys; other key types are unavailable in Foundation",
                    )
                member = value_shape.value
                kind: Literal["single", "sequence", "map"] = "map"
            else:
                member = value_shape.item
                kind = "sequence"
            inner = member.value if isinstance(member, Nullable) else member
            marked = isinstance(member, Nullable) and not isinstance(inner, Scalar)
            table = row_table(
                inner if marked else member,
                path,
                authored_path,
                family=family,
                owner=owner,
                source_definition=source_definition,
                relative_path=relative_path,
                kind=kind,
                nullable_member=marked,
                anonymous_parent=anonymous_parent,
            )
            edge: ValueProgram = (
                MapProgram(authored_path, table, nullable)
                if isinstance(value_shape, Map)
                else SequenceProgram(authored_path, table, nullable, value_shape.container)
            )
            return emit(edge, destination)
        if isinstance(value_shape, Positional):
            items = tuple(
                compile_shape(
                    item,
                    join(path, str(index)),
                    join(authored_path, str(index)),
                    f"{column}_{index}",
                    family=family,
                    owner=owner,
                    source_definition=source_definition,
                    relative_path=join(relative_path, str(index)),
                )
                for index, item in enumerate(value_shape.items)
            )
            return emit(TupleProgram(authored_path, items, nullable), destination)
        raise StructureMapError(
            "unavailable_construct",
            "This structural shape has no executor in this delivery",
            path=authored_path,
            owner="binding gap",
            remedy="Use scalar, record, sequence, map, or fixed tuple shapes; variants are unavailable in Foundation",
        )

    root_override = mapping.overrides.get("")
    root_name = root_override.table if root_override and root_override.table is not None else "root"
    root_prefix = root_override.prefix if root_override and root_override.prefix is not None else ""
    root_id = reserve()
    tables[root_name] = {
        "path": "",
        "parents": {""},
        "program": root_id,
        "kind": "root",
        "nullable_member": False,
    }
    record_program(model.structure.root, "", "", root_prefix, family="", owner=root_name, destination=root_id)
    # Discover sibling row roles before descending: recursive children must not
    # lend their traversal depth to an ordinary sibling's default table name.
    while pending:
        pending.popleft()()
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
            scalar_sources = _program_scalar_paths(program_id, programs)
            seen: dict[str, str] = {}
            conflicts: list[tuple[str, str, str]] = []
            for column, source_path in scalar_sources:
                if column in seen:
                    conflicts.append((column, seen[column], source_path))
                else:
                    seen[column] = source_path
            if not conflicts:
                metadata_columns = (
                    {field.name for field in fields[: -len(scalar_sources)]}
                    if scalar_sources
                    else {field.name for field in fields}
                )
                conflicts = [
                    (column, f"metadata:{column}", source_path)
                    for column, source_path in scalar_sources
                    if column in metadata_columns
                ]
            column, first_path, second_path = conflicts[0]
            raise StructureMapError(
                "column_collision",
                f"Column {column!r} in table {name!r} maps both {first_path!r} and {second_path!r}",
                path=second_path,
                table=name,
                remedy="Change column_naming or metadata_namespace, or use distinct explicit names",
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
    return PreparedMapping(
        root=root_id,
        programs=programs,
        root_table=root_name,
        root_type=cast(type[T], model.native_types[model.structure.root]),
        tables=plans,
        native_types=model.native_types,
        binding=native_binding,
        record_adapters=record_adapters,
        capabilities=native_binding.capabilities,
        key_column=key_col,
        parent_column=parent_col,
        position_column=position_col,
        map_key_column=map_key_col,
        null_column=null_col,
        parent_table_column=parent_table_col,
    )
