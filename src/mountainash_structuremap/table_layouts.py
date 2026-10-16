from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from typing import Callable, Literal, Mapping, cast

from mountainash.core.dtypes import parse_cast_target
from mountainash.typespec.converters import resolve_field_canonical
from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap.codecs import DecodingCodec, EncodingCodec
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import MappingSpec, RowLayout, SharedCollection, SourceKey, UseOverride
from mountainash_structuremap.prepared import (
    KeySource,
    LinkProgram,
    MapProgram,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
    TablePlan,
    TableRole,
    TupleProgram,
    ValueProgram,
    VariantProgram,
)


def same_physical_type(left: FieldSpec, right: FieldSpec) -> bool:
    """Compare effective Mountainash storage types and structured field metadata."""
    def canonical(field: FieldSpec) -> object:
        if field.dtype is not None:
            return field.dtype
        if field.backend_type is not None:
            try:
                return parse_cast_target(field.backend_type)
            except (TypeError, ValueError):
                return "backend_type", field.backend_type
        try:
            return resolve_field_canonical(field)
        except (TypeError, ValueError):
            return field.dtype, field.backend_type, field.type

    structured = ("item_type", "item_object_fields", "object_fields")
    return (
        canonical(left), left.format,
        tuple((name, getattr(left, name, None)) for name in structured),
    ) == (
        canonical(right), right.format,
        tuple((name, getattr(right, name, None)) for name in structured),
    )


def finalize_tables(
    descriptors: dict[str, dict[str, object]],
    programs: dict[int, ValueProgram],
    mapping: MappingSpec,
    schemas: Mapping[str, TypeSpec] | None,
    *,
    fields_for: Callable[[int], list[FieldSpec]],
    map_codec_for: Callable[[dict[str, object], RowLayout, str], EncodingCodec | DecodingCodec | None],
) -> tuple[dict[str, TablePlan], dict[int, ValueProgram]]:
    """Reconcile finite mapped-use table contributions into physical table plans."""
    namespace = mapping.defaults.naming.metadata_namespace

    def paths_for(root: int) -> dict[str, str]:
        found: dict[str, str] = {}
        pending = [root]
        seen: set[int] = set()
        while pending:
            program_id = pending.pop()
            if program_id in seen or program_id not in programs:
                continue
            seen.add(program_id)
            program = programs[program_id]
            if isinstance(program, ScalarProgram):
                for field in program.codec.fields:
                    found.setdefault(field.name, program.path)
            elif isinstance(program, RecordProgram):
                pending.extend(child for _, child in program.members)
            elif isinstance(program, TupleProgram):
                pending.extend(program.items)
            elif isinstance(program, VariantProgram):
                pending.extend(branch.program for branch in program.alternatives)
        return found
    physical_for = {
        identity: cast(str, descriptor.get("physical", identity))
        for identity, descriptor in descriptors.items()
    }
    role_for = {
        identity: cast(str | None, descriptor.get("role"))
        for identity, descriptor in descriptors.items()
    }
    by_physical: dict[str, list[tuple[str, dict[str, object]]]] = defaultdict(list)
    for identity, descriptor in descriptors.items():
        by_physical[physical_for[identity]].append((identity, descriptor))

    if schemas is not None and set(schemas) != set(by_physical):
        raise StructureMapError(
            "table_set_mismatch",
            f"Mapped tables {sorted(by_physical)!r} differ from supplied schemas {sorted(schemas)!r}",
            remedy="Declare schemas for exactly the mapped physical tables",
        )
    extra_layouts = set(mapping.layouts) - set(by_physical)
    if extra_layouts:
        raise StructureMapError(
            "table_set_mismatch", f"Unmapped layouts {sorted(extra_layouts)!r}",
            remedy="Declare layouts only for mapped physical tables",
        )

    def declared_key(physical: str) -> tuple[str, ...]:
        explicit = mapping.layouts.get(physical)
        if explicit is not None:
            return tuple(explicit.key)
        strategies = {
            tuple(options.key.columns)
            for identity, descriptor in descriptors.items()
            if physical_for[identity] == physical
            and (options := cast(UseOverride | None, descriptor.get("options"))) is not None
            and isinstance(options.key, SourceKey)
        }
        if len(strategies) > 1:
            raise StructureMapError(
                "invalid_layout", f"Roles for {physical!r} declare different source keys",
                table=physical, remedy="Use one physical key layout for every role",
            )
        return next(iter(strategies), (namespace + "key",))

    def default_layout(
        parent_columns: tuple[str, ...], parent_tables: set[str], kind: str, nullable: bool,
        *, position_required: bool, role_column: str | None,
    ) -> RowLayout:
        parent = parent_columns if len(parent_columns) > 1 else (
            (namespace + "parent",) if parent_tables else ()
        )
        return RowLayout(
            key=(namespace + "key",), generated=True,
            parent=parent,
            position=namespace + "position" if position_required else None,
            map_key=namespace + "map_key" if kind == "map" else None,
            parent_table=namespace + "parent_table" if len(parent_tables) > 1 else None,
            role_column=role_column,
            null_column=namespace + "null" if nullable else None,
        )

    layouts: dict[str, RowLayout] = {}
    table_fields: dict[str, dict[str, FieldSpec]] = {physical: {} for physical in by_physical}
    source_fields: dict[str, dict[str, FieldSpec]] = {}
    payload_roles: dict[str, dict[str, str]] = defaultdict(dict)
    payload_paths: dict[str, dict[str, str]] = defaultdict(dict)
    role_specs: dict[str, dict[str | None, tuple[str, dict[str, object]]]] = {}

    def add_field(table: str, field: FieldSpec, *, overlap: bool = False) -> None:
        fields = table_fields[table]
        old = fields.get(field.name)
        if old is None:
            fields[field.name] = deepcopy(field)
        elif not same_physical_type(old, field):
            raise StructureMapError(
                "incompatible_schema", f"Incompatible physical types for {table}.{field.name}",
                path=payload_paths[table].get(field.name, ""), table=table, column=field.name,
                remedy="Declare matching physical types for all roles and relationship columns",
            )
        elif not overlap:
            raise StructureMapError(
                "column_collision", f"Multiple mapped contributions use {field.name!r}",
                path=payload_paths[table].get(field.name, ""), table=table, column=field.name,
                remedy="Use distinct columns or an explicitly compatible metadata overlap",
            )

    def renamed(field: FieldSpec, name: str) -> FieldSpec:
        result = deepcopy(field)
        result.name = name
        return result

    def add_payload(table: str, identity: str, field: FieldSpec) -> None:
        old = table_fields[table].get(field.name)
        previous = payload_roles[table].get(field.name)
        if old is None:
            add_field(table, field)
            payload_roles[table][field.name] = identity
        elif previous is not None and previous != identity:
            add_field(table, field, overlap=True)
        else:
            add_field(table, field)

    for physical, members in by_physical.items():
        parent_ids = {
            parent for _, descriptor in members
            for parent in cast(set[str], descriptor.get("parents", set()))
            if parent in descriptors
        }
        kinds = {cast(str, descriptor["kind"]) for _, descriptor in members}
        if len(kinds) != 1:
            raise StructureMapError(
                "invalid_layout", f"Mapped roles for {physical!r} have incompatible table kinds",
                table=physical, remedy="Use compatible mapped roles for a shared table",
            )
        kind = next(iter(kinds))
        nullable = any(bool(descriptor.get("nullable_member")) for _, descriptor in members)
        layout = mapping.layouts.get(physical)
        source_strategies = {
            tuple(options.key.columns)
            for _, descriptor in members
            if (options := cast(UseOverride | None, descriptor.get("options"))) is not None
            and isinstance(options.key, SourceKey)
        }
        if len(source_strategies) > 1:
            raise StructureMapError(
                "invalid_layout", f"Roles for {physical!r} declare different source keys",
                table=physical, remedy="Use one physical key layout for every role",
            )
        position_required = any(
            kind in ("sequence", "map")
            and ((options := cast(UseOverride | None, descriptor.get("options"))) is None
                 or options.ordered is not False)
            for _, descriptor in members
        )
        if layout is None:
            parent_tables = {physical_for[parent] for parent in parent_ids}
            parent_key_shapes = {declared_key(parent) for parent in parent_tables}
            if len(parent_key_shapes) > 1:
                raise StructureMapError(
                    "invalid_layout", f"Default parent layout for {physical!r} has differing parent keys",
                    table=physical,
                    remedy="Declare explicit parent columns and parent_table for differing parent layouts",
                )
            parent_key = next(iter(parent_key_shapes), ())
            parent_columns = parent_key if len(parent_key) > 1 else ()
            shared_role_columns = {
                options.collection_table.role_column
                for _, descriptor in members
                if (options := cast(UseOverride | None, descriptor.get("options"))) is not None
                and isinstance(options.collection_table, SharedCollection)
            }
            if len(shared_role_columns) > 1:
                raise StructureMapError(
                    "invalid_layout", f"Shared roles for {physical!r} declare different role columns",
                    table=physical, remedy="Use one physical role column for every shared role",
                )
            layout = default_layout(
                parent_columns, parent_tables, kind, nullable,
                position_required=position_required,
                role_column=next(iter(shared_role_columns), None),
            )
            if source_strategies:
                layout = layout.model_copy(update={"key": next(iter(source_strategies)), "generated": False})
        layouts[physical] = layout

        role_map: dict[str | None, tuple[str, dict[str, object]]] = {}
        for identity, descriptor in members:
            role = role_for[identity]
            if role in role_map:
                raise StructureMapError(
                    "invalid_role", f"Duplicate role {role!r} for physical table {physical!r}",
                    table=physical, remedy="Assign each shared table use a unique role",
                )
            role_map[role] = (identity, descriptor)
            options = cast(UseOverride | None, descriptor.get("options"))
            collection = options.collection_table if options is not None else None
            if isinstance(collection, SharedCollection):
                if layout.role_column != collection.role_column:
                    raise StructureMapError(
                        "invalid_layout", f"Shared role {identity!r} requires role column {collection.role_column!r}",
                        path=cast(str, descriptor.get("path", "")), table=physical,
                        remedy="Set RowLayout.role_column to the SharedCollection role_column",
                    )
                if collection.role is not None and collection.role != role:
                    raise StructureMapError(
                        "invalid_layout", "SharedCollection role differs from the compiled table role",
                        path=cast(str, descriptor.get("path", "")), table=physical,
                        remedy="Use the same role in SharedCollection and the mapped-use descriptor",
                    )
            raw_fields = fields_for(cast(int, descriptor["program"]))
            source_fields[identity] = {field.name: deepcopy(field) for field in raw_fields}
            for column, path in paths_for(cast(int, descriptor["program"])).items():
                payload_paths[physical].setdefault(column, path)
            for field in raw_fields:
                add_payload(physical, identity, field)
        role_specs[physical] = role_map

    # Relationship metadata and codec fields are reconciled once per physical table.
    for physical, members in by_physical.items():
        layout = layouts[physical]
        has_parents = any(
            parent in descriptors
            for _, descriptor in members
            for parent in cast(set[str], descriptor.get("parents", set()))
        )
        if bool(layout.parent) != has_parents:
            raise StructureMapError(
                "invalid_layout", f"Layout for {physical!r} omits or adds parent metadata",
                table=physical, remedy="Declare parent columns matching mapped parent relationships",
            )
        if any(role is None for role in role_specs[physical]) and len(role_specs[physical]) > 1:
            raise StructureMapError(
                "invalid_role", f"Shared physical table {physical!r} requires a role for every use",
                table=physical, remedy="Declare explicit or stable mapped-use roles",
            )
        shared = any(
            (options := cast(UseOverride | None, descriptor.get("options"))) is not None
            and isinstance(options.collection_table, SharedCollection)
            for _, descriptor in members
        )
        if shared and layout.role_column is None:
            raise StructureMapError(
                "invalid_layout", f"Shared table {physical!r} requires a role column",
                table=physical, remedy="Declare RowLayout.role_column",
            )

        for identity, descriptor in members:
            options = cast(UseOverride | None, descriptor.get("options"))
            ordered = options.ordered if options is not None and options.ordered is not None else True
            positions = options.positions if options is not None and options.positions is not None else mapping.defaults.positions
            kind = cast(str, descriptor["kind"])
            if kind in ("sequence", "map") and ordered and positions == "strict" and layout.position is None:
                raise StructureMapError(
                    "invalid_layout", f"Ordered strict role {identity!r} requires a position column",
                    path=cast(str, descriptor.get("path", "")), table=physical,
                    remedy="Declare a position column, select lenient positions, or mark the collection unordered",
                )
            if kind == "map" and layout.map_key is None:
                raise StructureMapError("invalid_layout", f"Map role {identity!r} requires a map-key column",
                                        path=cast(str, descriptor.get("path", "")), table=physical,
                                        remedy="Declare a map_key column")
            if descriptor.get("nullable_member") and layout.null_column is None:
                raise StructureMapError("invalid_layout", f"Nullable role {identity!r} requires a null column",
                                        path=cast(str, descriptor.get("path", "")), table=physical,
                                        remedy="Declare a null_column")
            parents = {parent for parent in cast(set[str], descriptor.get("parents", set()))
                       if parent in descriptors}
            if len({physical_for[parent] for parent in parents}) > 1 and layout.parent_table is None:
                raise StructureMapError("invalid_layout", f"Role {identity!r} has multiple parent tables",
                                        path=cast(str, descriptor.get("path", "")), table=physical,
                                        remedy="Declare a parent_table column")

        if layout.generated:
            if len(layout.key) != 1:
                raise StructureMapError("invalid_layout", "Generated keys require one column", table=physical,
                                        remedy="Declare one generated key column or use a source composite key")
            add_field(physical, FieldSpec(name=layout.key[0], type=UniversalType.STRING, missing_values=[]))
        metadata_fields: list[tuple[str | None, UniversalType, str]] = [
            (layout.position, UniversalType.INTEGER, "position"),
            (layout.map_key, UniversalType.STRING, "map_key"),
            (layout.null_column, UniversalType.BOOLEAN, "null"),
            (layout.parent_table, UniversalType.STRING, "parent_table"),
            (layout.role_column, UniversalType.STRING, "role"),
        ]
        metadata_fields.extend(
            (cast(str | None, descriptor.get("discriminator_column")), UniversalType.STRING, "discriminator")
            for _, descriptor in members
        )
        metadata_fields.extend(
            (cast(str | None, descriptor.get("payload_null_column")), UniversalType.BOOLEAN, "payload_null")
            for _, descriptor in members
        )
        metadata_seen: dict[str, tuple[UniversalType, str]] = {}
        for metadata_column, datatype, semantic in metadata_fields:
            if metadata_column is None:
                continue
            column = metadata_column
            previous = metadata_seen.get(column)
            if previous is not None:
                if previous != (datatype, semantic):
                    raise StructureMapError(
                        "column_collision", f"Metadata roles conflict on {physical}.{column}",
                        table=physical, column=column,
                        remedy="Assign distinct columns to different metadata roles",
                    )
                continue
            metadata_seen[column] = (datatype, semantic)
            add_field(
                physical, FieldSpec(name=column, type=datatype, missing_values=[]),
                overlap=physical in mapping.layouts and semantic == "position",
            )

    def ancestors(identity: str) -> set[str]:
        found: set[str] = set()
        pending = list(cast(set[str], descriptors[identity].get("parents", set())))
        while pending:
            parent = pending.pop()
            if parent not in descriptors or parent in found:
                continue
            found.add(parent)
            pending.extend(cast(set[str], descriptors[parent].get("parents", set())))
        return found

    key_sources_by_identity: dict[str, tuple[KeySource, ...]] = {}
    for identity, descriptor in descriptors.items():
        physical = physical_for[identity]
        layout = layouts[physical]
        options = cast(UseOverride | None, descriptor.get("options"))
        strategy = options.key if options is not None else None
        if isinstance(strategy, SourceKey) and (layout.generated or tuple(strategy.columns) != tuple(layout.key)):
            raise StructureMapError(
                "invalid_layout", f"Source key strategy for {identity!r} contradicts its physical layout",
                path=cast(str, descriptor.get("path", "")), table=physical,
                remedy="Use matching non-generated RowLayout.key columns and SourceKey.columns",
            )
        if strategy == "generated" and not layout.generated:
            raise StructureMapError(
                "invalid_layout", f"Generated key strategy for {identity!r} contradicts its physical layout",
                path=cast(str, descriptor.get("path", "")), table=physical,
                remedy="Declare a generated RowLayout key or select a source key strategy",
            )
        columns = tuple(strategy.columns) if isinstance(strategy, SourceKey) else (
            () if layout.generated else tuple(layout.key)
        )
        sources: list[KeySource] = []
        ancestor_ids = ancestors(identity)
        for column in columns:
            if column in layout.parent or column in (layout.position, layout.map_key):
                continue
            candidates = [
                (ancestor, source_fields[ancestor][column])
                for ancestor in ancestor_ids
                if column in source_fields.get(ancestor, {})
            ]
            unique = {(physical_for[ancestor], role_for[ancestor], field.name)
                      for ancestor, field in candidates}
            if len(unique) > 1:
                choices = sorted(f"{table}[role={role!r}].{name}" for table, role, name in unique)
                raise StructureMapError(
                    "ambiguous_source_key", f"Source key {column!r} has multiple ancestor origins: {choices!r}",
                    path=cast(str, descriptor.get("path", "")), table=physical, column=column,
                    remedy="Rename the ambiguous ancestor field or provide a unique source key column",
                )
            if unique:
                source_table, source_role, source_column = next(iter(unique))
            elif column in source_fields[identity]:
                source_table, source_role, source_column = physical, role_for[identity], column
            else:
                raise StructureMapError(
                    "invalid_layout", f"Source key {physical}.{column} has no local or ancestor value source",
                    path=cast(str, descriptor.get("path", "")), table=physical, column=column,
                    remedy="Select a mapped source field or an available unique ancestor field",
                )
            sources.append(KeySource(column, source_table, source_role, source_column))
            source_field = next(
                field_map[source_column]
                for source_identity, field_map in source_fields.items()
                if physical_for[source_identity] == source_table and role_for[source_identity] == source_role
                and source_column in field_map
            )
            add_field(physical, renamed(source_field, column), overlap=True)
        key_sources_by_identity[identity] = tuple(sources)

    # Resolve parent schema dependencies independently of descriptor visitation order.
    unresolved = set(descriptors)
    while unresolved:
        progressed = False
        for identity in tuple(unresolved):
            descriptor = descriptors[identity]
            physical = physical_for[identity]
            layout = layouts[physical]
            parents = {parent for parent in cast(set[str], descriptor.get("parents", set()))
                       if parent in descriptors}
            if any(
                key not in table_fields[physical_for[parent]]
                for parent in parents
                for key in layouts[physical_for[parent]].key
            ):
                continue
            for parent in parents:
                parent_layout = layouts[physical_for[parent]]
                if len(layout.parent) != len(parent_layout.key):
                    raise StructureMapError(
                        "invalid_layout", f"Parent key arity differs for {identity!r}",
                        path=cast(str, descriptor.get("path", "")), table=physical,
                        remedy="Declare one parent column for each parent key column",
                    )
                for parent_column, parent_key_column in zip(layout.parent, parent_layout.key, strict=True):
                    parent_field = table_fields[physical_for[parent]][parent_key_column]
                    add_field(physical, renamed(parent_field, parent_column), overlap=True)
            unresolved.remove(identity)
            progressed = True
        if not progressed:
            raise StructureMapError(
                "invalid_layout", f"Cyclic unresolved source keys in {sorted(unresolved)!r}",
                remedy="Use mapped source fields or generated keys rather than cyclic key dependencies",
            )
    for identity, descriptor in descriptors.items():
        physical = physical_for[identity]
        for column in layouts[physical].key:
            if column not in table_fields[physical]:
                raise StructureMapError(
                    "invalid_layout", f"Source key {physical}.{column} has no value source",
                    path=cast(str, descriptor.get("path", "")),
                    table=physical, column=column,
                    remedy="Select a mapped source field, parent column, or position as the key",
                )

    rewritten: dict[int, ValueProgram] = {}
    for program_id, program in programs.items():
        rewritten_program: ValueProgram = program
        if isinstance(program, (SequenceProgram, MapProgram, LinkProgram)) and program.table in physical_for:
            rewritten_program = replace(program, table=physical_for[program.table], role=role_for[program.table])
        elif isinstance(program, VariantProgram):
            branches = tuple(
                replace(branch, table=physical_for[branch.table], role=role_for[branch.table])
                if branch.table in physical_for else branch
                for branch in program.alternatives
            )
            null_table, null_role = program.null_table, program.null_role
            if null_table in physical_for:
                null_role = role_for[null_table]
                null_table = physical_for[null_table]
            rewritten_program = replace(program, alternatives=branches, null_table=null_table, null_role=null_role)
        rewritten[program_id] = rewritten_program

    plans: dict[str, TablePlan] = {}
    for physical, members in by_physical.items():
        layout = layouts[physical]
        fields = table_fields[physical]
        if schemas is not None:
            schema = schemas[physical]
            supplied: dict[str, FieldSpec] = {}
            for field in schema.fields:
                if field.name in supplied:
                    raise StructureMapError("incompatible_schema", f"Table {physical!r} has duplicate field names",
                                            table=physical, column=field.name,
                                            remedy="Declare each physical column exactly once")
                supplied[field.name] = field
            if set(supplied) != set(fields):
                raise StructureMapError(
                    "incompatible_schema",
                    f"Table {physical!r}: missing columns {sorted(set(fields) - set(supplied))!r}; extra columns {sorted(set(supplied) - set(fields))!r}",
                    table=physical, remedy="Declare exactly the mapped payload and relationship columns",
                )
            for column, field in fields.items():
                if not same_physical_type(field, supplied[column]):
                    raise StructureMapError("incompatible_schema", f"Physical type mismatch for {physical}.{column}",
                                            table=physical, column=column,
                                            remedy="Use a compatible declared physical type or an explicit mapping codec")
        else:
            metadata: list[str | None] = [
                *layout.key, *layout.parent, layout.position, layout.map_key,
                layout.parent_table, layout.role_column, layout.null_column,
            ]
            metadata.extend(cast(str | None, descriptor.get("discriminator_column")) for _, descriptor in members)
            metadata.extend(cast(str | None, descriptor.get("payload_null_column")) for _, descriptor in members)
            ordered_columns = dict.fromkeys(column for column in metadata if column is not None)
            ordered_columns.update(dict.fromkeys(fields))
            schema = TypeSpec(fields=[fields[column] for column in ordered_columns], missing_values=[])

        roles: dict[str | None, TableRole] = {}
        physical_parents = sorted({
            physical_for[parent]
            for _, descriptor in members
            for parent in cast(set[str], descriptor.get("parents", set()))
            if parent in descriptors
        })
        for identity, descriptor in members:
            options = cast(UseOverride | None, descriptor.get("options"))
            ordered = options.ordered if options is not None and options.ordered is not None else True
            positions = options.positions if options is not None and options.positions is not None else mapping.defaults.positions
            codec = map_codec_for(descriptor, layout, physical) if descriptor["kind"] == "map" else None
            if descriptor["kind"] == "map" and codec is None:
                raise StructureMapError("invalid_layout", f"Map role {identity!r} has no map-key codec",
                                        path=cast(str, descriptor.get("path", "")), table=physical,
                                        remedy="Provide a map-key codec for the declared map_key column")
            if codec is not None and (len(codec.fields) != 1 or codec.fields[0].name != layout.map_key
                                      or codec.fields[0].type != UniversalType.STRING):
                raise StructureMapError("incompatible_schema", "Map key codecs require one declared STRING field",
                                        table=physical, remedy="Use the layout map_key column with a STRING codec")
            parent_roles = tuple(sorted(
                ((physical_for[parent], role_for[parent])
                 for parent in cast(set[str], descriptor.get("parents", set()))
                 if parent in descriptors),
                key=lambda parent: (parent[0], parent[1] or ""),
            ))
            roles[role_for[identity]] = TableRole(
                source_path=cast(str, descriptor.get("path", "")), parent_roles=parent_roles,
                program=cast(int, descriptor["program"]), kind=cast(Literal["root", "single", "sequence", "map"], descriptor["kind"]),
                nullable_member=bool(descriptor.get("nullable_member")),
                position_group=cast(str | None, descriptor.get("position_group")),
                positions=positions, ordered=ordered, map_key_codec=codec,
                key_sources=key_sources_by_identity[identity],
            )
        plans[physical] = TablePlan(
            name=physical, source_path=cast(str, members[0][1].get("path", "")),
            parent_tables=tuple(physical_parents), columns=tuple(field.name for field in schema.fields),
            layout=layout, _typespec=deepcopy(schema), roles=roles,
        )
    return plans, rewritten
