from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import TypeVar, cast

import mountainash as ma

from mountainash_structuremap.codecs import Cell, CellLocation, DecodingCodec
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.prepared import (
    LinkProgram,
    MapProgram,
    NullProgram,
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
    TupleProgram,
    VariantBranch,
    VariantProgram,
)

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
_KeyIdentity = tuple[tuple[type, Cell], ...]
T = TypeVar("T")
_EvalTask = tuple[str, int, Mapping[str, Cell], str, int, object]


def _key_identity(cells: tuple[Cell, ...]) -> _KeyIdentity:
    return tuple((type(cell), cell) for cell in cells)


def _error(
    code: str,
    message: str,
    *,
    path: str,
    remedy: str,
    table: str | None = None,
    row: int | None = None,
    column: str | None = None,
) -> StructureMapError:
    return StructureMapError(
        code,
        message,
        path=path,
        owner="executor restriction",
        table=table,
        row=row,
        column=column,
        remedy=remedy,
    )


def _scalar_cell(
    row: Mapping[str, Cell],
    program: ScalarProgram,
    *,
    table: str,
    row_number: int,
) -> object:
    codec = cast(DecodingCodec, program.codec)
    fields = codec.fields
    cells = tuple(row.get(field.name) for field in fields)
    if all(value is None for value in cells):
        if program.nullable:
            return None
        raise _error("null_not_allowed", "Non-nullable scalar cell is null", path=program.path,
                     remedy="Supply a value or declare the member nullable", table=table,
                     row=row_number, column=fields[0].name)
    return codec.check(cells, CellLocation(path=program.path, table=table, row=row_number,
                                           column=fields[0].name))


def reconstruct(tables: Mapping[str, Sequence[Mapping[str, Cell]]], prepared: PreparedMapping[T]) -> T:
    """Validate and iteratively construct native values from finite occurrence tables."""
    if "reconstruct" not in prepared.directions:
        raise StructureMapError(
            "unavailable_direction",
            "Prepared mapping does not support reconstruction",
            remedy="Prepare with the reconstruct direction",
        )
    if not isinstance(tables, Mapping):
        raise _error(
            "invalid_tables",
            "Tables must be a mapping",
            path="",
            remedy="Supply a mapping from table names to row sequences",
        )
    expected_names = set(prepared.tables)
    actual_names = set(tables)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra_names = sorted(actual_names - expected_names, key=repr)
        raise _error(
            "table_set_mismatch",
            f"Missing tables {missing!r}; unexpected tables {extra_names!r}",
            path="",
            remedy="Supply exactly the tables in the prepared mapping",
        )

    column_cache: dict[int, frozenset[str]] = {}

    def payload_columns(program_id: int) -> frozenset[str]:
        if program_id in column_cache:
            return column_cache[program_id]
        program = prepared.programs[program_id]
        columns: set[str] = set()
        if isinstance(program, ScalarProgram):
            columns.update(field.name for field in program.codec.fields)
        elif isinstance(program, RecordProgram):
            for _, child in program.members:
                columns.update(payload_columns(child))
        elif isinstance(program, TupleProgram):
            for child in program.items:
                columns.update(payload_columns(child))
        elif isinstance(program, VariantProgram) and not (program.repeated and program.layout == "split"):
            if not program.source_discriminator:
                columns.add(program.discriminator_column)
            if program.payload_null_column is not None:
                columns.add(program.payload_null_column)
            for branch in program.alternatives:
                columns.update(payload_columns(branch.program))
        result = frozenset(columns)
        column_cache[program_id] = result
        return result

    indexed: dict[str, dict[str | None, dict[_KeyIdentity, Mapping[str, Cell]]]] = {}
    row_numbers: dict[str, dict[str | None, dict[_KeyIdentity, int]]] = {}
    ordered_rows: dict[str, list[Mapping[str, Cell]]] = {}
    row_roles: dict[int, str | None] = {}
    for table_name, plan in prepared.tables.items():
        supplied = tables[table_name]
        if isinstance(supplied, (str, bytes)) or not isinstance(supplied, Sequence):
            raise _error(
                "invalid_table_rows",
                "Table rows must be a sequence",
                path=plan.source_path,
                remedy="Supply a sequence of row mappings",
                table=table_name,
            )
        rows: list[Mapping[str, Cell]] = []
        by_role: dict[str | None, dict[_KeyIdentity, Mapping[str, Cell]]] = {}
        key_rows: dict[str | None, dict[_KeyIdentity, int]] = {}
        expected_columns = set(plan.columns)
        for index, row in enumerate(supplied):
            if not isinstance(row, Mapping):
                raise _error(
                    "invalid_row",
                    "Table row must be a mapping",
                    path=plan.source_path,
                    remedy="Supply a mapping for each row",
                    table=table_name,
                    row=index,
                )
            columns = set(row)
            role_hint = row.get(plan.layout.role_column) if plan.layout.role_column else None
            hint = plan.roles.get(role_hint) if role_hint is None or type(role_hint) is str else None
            optional_position = (
                {plan.layout.position}
                if hint is not None and hint.positions == "lenient"
                and plan.layout.position is not None
                and plan.layout.position not in (*plan.layout.key, *plan.layout.parent)
                and plan.layout.position not in payload_columns(hint.program)
                else set()
            )
            missing = sorted(expected_columns - columns - optional_position)
            if missing or columns - expected_columns:
                extra_columns = sorted(columns - expected_columns, key=repr)
                raise _error(
                    "row_shape_mismatch",
                    f"Missing columns {missing!r}; unexpected columns {extra_columns!r}",
                    path=plan.source_path,
                    remedy="Supply exactly the prepared table columns",
                    table=table_name,
                    row=index,
                )
            if plan.layout.role_column is None:
                role_name = None
                if role_name not in plan.roles:
                    raise _error(
                        "invalid_role",
                        "Table row has no role selector for its declared roles",
                        path=plan.source_path,
                        remedy="Configure a role column for shared-table mappings",
                        table=table_name,
                        row=index,
                    )
            else:
                role_value = row[plan.layout.role_column]
                if type(role_value) is not str or role_value not in plan.roles:
                    raise _error(
                        "invalid_role",
                        f"Unknown relationship role {role_value!r}",
                        path=plan.source_path,
                        remedy=f"Use one of the declared roles {tuple(plan.roles)!r}",
                        table=table_name,
                        row=index,
                        column=plan.layout.role_column,
                    )
                role_name = role_value
            role_plan = plan.roles[role_name]
            key_columns = plan.layout.key
            key_cells = tuple(row[column] for column in key_columns)
            if plan.layout.generated and (
                len(key_cells) != 1 or type(key_cells[0]) is not str or not key_cells[0]
            ):
                raise _error(
                    "invalid_key",
                    "Generated occurrence key must be one nonempty string cell",
                    path=role_plan.source_path,
                    remedy="Supply a nonempty string generated key",
                    table=table_name,
                    row=index,
                    column=key_columns[0] if key_columns else None,
                )
            if not plan.layout.generated and (
                not key_cells or any(cell is None for cell in key_cells)
            ):
                raise _error(
                    "invalid_key",
                    "Source occurrence key contains a null or missing cell",
                    path=role_plan.source_path,
                    remedy="Supply non-null values for every source key column",
                    table=table_name,
                    row=index,
                    column=next(
                        (column for column, cell in zip(key_columns, key_cells) if cell is None),
                        None,
                    ),
                )
            try:
                key = _key_identity(key_cells)
                by_key = by_role.setdefault(role_name, {})
                duplicate = key in by_key
            except TypeError as exc:
                raise _error(
                    "invalid_key",
                    "Occurrence key cells must be hashable",
                    path=role_plan.source_path,
                    remedy="Use supported scalar key cells",
                    table=table_name,
                    row=index,
                ) from exc
            if duplicate:
                raise _error(
                    "duplicate_key",
                    f"Duplicate occurrence key {key_cells!r}",
                    path=role_plan.source_path,
                    remedy="Use unique key cells within this table role",
                    table=table_name,
                    row=index,
                    column=key_columns[0],
                )
            rows.append(row)
            row_roles[id(row)] = role_name
            by_key[key] = row
            key_rows.setdefault(role_name, {})[key] = index
        indexed[table_name] = by_role
        row_numbers[table_name] = key_rows
        ordered_rows[table_name] = rows

    root_rows = ordered_rows[prepared.root_table]
    if len(root_rows) != 1:
        raise _error(
            "root_cardinality",
            f"Expected exactly one root row, got {len(root_rows)}",
            path="",
            remedy="Supply exactly one root occurrence",
            table=prepared.root_table,
        )
    root_row = root_rows[0]

    def row_key(row: Mapping[str, Cell], table: str) -> _KeyIdentity:
        return _key_identity(
            tuple(row[column] for column in prepared.tables[table].layout.key)
        )

    def row_role(row: Mapping[str, Cell]) -> str | None:
        return row_roles[id(row)]

    def occurrence_number(row: Mapping[str, Cell], table: str) -> int:
        role_name = row_role(row)
        return row_numbers[table][role_name][row_key(row, table)]

    native_map_keys: dict[tuple[str, str | None, _KeyIdentity], str] = {}
    GroupKey = tuple[str, str | None, str, str | None, _KeyIdentity]
    groups: dict[GroupKey, list[Mapping[str, Cell]]] = defaultdict(list)
    positions: dict[tuple[object, str, str | None, _KeyIdentity], set[int]] = defaultdict(set)
    map_keys: dict[tuple[object, str, str | None, _KeyIdentity], set[str]] = defaultdict(set)
    parent_occurrences: dict[int, tuple[Mapping[str, Cell], str, str | None]] = {}
    for table_name, plan in prepared.tables.items():
        for index, row in enumerate(ordered_rows[table_name]):
            child_role_name = row_roles[id(row)]
            child_role = plan.roles[child_role_name]
            if child_role.kind == "root":
                continue
            allowed_parent_roles = set(child_role.parent_roles)
            allowed_parent_tables = {
                candidate_table for candidate_table, _ in allowed_parent_roles
            }
            parent_cells = tuple(row[column] for column in plan.layout.parent)
            if any(cell is None for cell in parent_cells):
                raise _error(
                    "orphan_row",
                    "Parent key contains a null cell",
                    path=child_role.source_path,
                    table=table_name,
                    row=index,
                    remedy="Supply every declared parent-key cell",
                )
            try:
                parent_key = _key_identity(parent_cells)
                hash(parent_key)
            except TypeError as exc:
                raise _error(
                    "orphan_row",
                    "Parent key cells must be scalar",
                    path=child_role.source_path,
                    table=table_name,
                    row=index,
                    remedy="Supply supported scalar parent-key cells",
                ) from exc
            if plan.layout.parent_table is not None:
                parent_table = row[plan.layout.parent_table]
                if (
                    type(parent_table) is not str
                    or parent_table not in allowed_parent_tables
                ):
                    raise _error(
                        "invalid_parent_table",
                        f"Parent table {parent_table!r} is not permitted",
                        path=child_role.source_path,
                        remedy=f"Use one of {sorted(allowed_parent_tables)!r}",
                        table=table_name,
                        row=index,
                        column=plan.layout.parent_table,
                    )
            elif len(allowed_parent_tables) == 1:
                parent_table = next(iter(allowed_parent_tables))
            else:
                raise _error(
                    "invalid_prepared_mapping",
                    "Relationship role has no permitted parent origin",
                    path=child_role.source_path,
                    remedy="Prepare a parent table and role for this relationship",
                    table=table_name,
                )
            matching_parent_roles = [
                (candidate_table, candidate_role)
                for candidate_table, candidate_role in allowed_parent_roles
                if candidate_table == parent_table
                and parent_key
                in indexed.get(parent_table, {}).get(candidate_role, {})
            ]
            if not matching_parent_roles:
                raise _error(
                    "orphan_row",
                    f"Child parent key {parent_key!r} does not identify an allowed role in {parent_table!r}",
                    path=child_role.source_path,
                    remedy=f"Link the row to an enclosing occurrence in {parent_table!r}",
                    table=table_name,
                    row=index,
                    column=plan.layout.parent[0],
                )
            if len(matching_parent_roles) > 1:
                raise _error(
                    "ambiguous_parent_role",
                    f"Parent key {parent_key!r} identifies multiple allowed roles in {parent_table!r}",
                    path=child_role.source_path,
                    remedy="Use a source key that identifies one parent mapped-use role",
                    table=table_name,
                    row=index,
                )
            parent_role_name = matching_parent_roles[0][1]
            parent_occurrences[id(row)] = (
                indexed[parent_table][parent_role_name][parent_key],
                parent_table, parent_role_name,
            )
            identity: GroupKey = (
                table_name,
                child_role_name,
                parent_table,
                parent_role_name,
                parent_key,
            )
            position_group = child_role.position_group or (table_name, child_role_name)
            position_identity = (
                position_group,
                parent_table,
                parent_role_name,
                parent_key,
            )
            position_column = plan.layout.position
            position: int | None = None
            if child_role.kind in ("sequence", "map") and child_role.ordered:
                if position_column is None:
                    if child_role.positions == "strict":
                        raise _error(
                            "missing_position",
                            "Ordered collection has no position column",
                            path=child_role.source_path,
                            remedy="Declare a position column or opt in to lenient ordering",
                            table=table_name,
                            row=index,
                        )
                else:
                    position_value = row.get(position_column)
                    if position_value is None and child_role.positions == "lenient":
                        position = None
                    elif (
                        type(position_value) is not int
                        or not 0 <= position_value <= _I64_MAX
                    ):
                        raise _error(
                            "invalid_position",
                            "Position must be a nonnegative signed-i64 integer",
                            path=child_role.source_path,
                            remedy="Supply an integer position between 0 and 2**63 - 1",
                            table=table_name,
                            row=index,
                            column=position_column,
                        )
                    else:
                        position = position_value
                    if position is not None:
                        if (
                            child_role.positions == "strict"
                            and position in positions[position_identity]
                        ):
                            raise _error(
                                "duplicate_position",
                                f"Duplicate position {position} for parent {parent_key!r}",
                                path=child_role.source_path,
                                remedy="Use a unique position for each collection member",
                                table=table_name,
                                row=index,
                                column=position_column,
                            )
                        positions[position_identity].add(position)
            if child_role.kind == "map":
                map_column = plan.layout.map_key
                if map_column is None:
                    raise _error(
                        "invalid_prepared_mapping",
                        "Map table has no map-key column",
                        path=child_role.source_path,
                        table=table_name,
                        remedy="Prepare the map with a configured map-key column",
                    )
                map_key = row[map_column]
                if type(map_key) is not str:
                    raise _error(
                        "invalid_map_key",
                        "Map key must be a string cell",
                        path=child_role.source_path,
                        remedy="Supply a string map key",
                        table=table_name,
                        row=index,
                        column=map_column,
                    )
                if child_role.map_key_codec is None:
                    raise _error(
                        "invalid_prepared_mapping",
                        "Map table has no key codec",
                        path=child_role.source_path,
                        table=table_name,
                        remedy="Prepare the map with a string key component",
                    )
                codec = cast(DecodingCodec, child_role.map_key_codec)
                checked = codec.check(
                    (map_key,),
                    CellLocation(
                        child_role.source_path, table_name, index, map_column
                    ),
                )
                decoded_key = codec.decode(checked)
                if type(decoded_key) is not str:
                    raise _error(
                        "invalid_map_key",
                        "Map key codec must reconstruct a string",
                        path=child_role.source_path,
                        table=table_name,
                        row=index,
                        remedy="Use a codec whose decoded keys are strings",
                    )
                map_key = decoded_key
                current_key = row_key(row, table_name)
                native_map_keys[(table_name, child_role_name, current_key)] = map_key
                if map_key in map_keys[position_identity]:
                    raise _error(
                        "duplicate_map_key",
                        f"Duplicate map key {map_key!r}",
                        path=child_role.source_path,
                        remedy="Use unique keys within each map occurrence",
                        table=table_name,
                        row=index,
                        column=map_column,
                    )
                map_keys[position_identity].add(map_key)
            elif child_role.kind == "single" and groups[identity]:
                raise _error(
                    "singular_cardinality",
                    f"Multiple rows for singular link from {parent_key!r}",
                    path=child_role.source_path,
                    remedy="Supply at most one linked occurrence",
                    table=table_name,
                    row=index,
                )
            elif child_role.kind not in ("single", "sequence", "map"):
                raise _error(
                    "invalid_prepared_mapping",
                    f"Unknown table kind {child_role.kind!r}",
                    path=child_role.source_path,
                    remedy="Prepare a supported table role",
                    table=table_name,
                )
            groups[identity].append(row)

    for table_name, plan in prepared.tables.items():
        for index, row in enumerate(ordered_rows[table_name]):
            role_name = row_role(row)
            role_plan = plan.roles[role_name]
            for source in role_plan.key_sources:
                origin, origin_table, origin_role = row, table_name, role_name
                seen: set[int] = set()
                while (origin_table, origin_role) != (source.source_table, source.source_role):
                    parent = parent_occurrences.get(id(origin))
                    if id(origin) in seen or parent is None:
                        raise _error(
                            "orphan_row", "Declared source-key origin is absent from occurrence ancestry",
                            path=role_plan.source_path, table=table_name, row=index,
                            remedy="Link the occurrence to its declared source-key ancestor",
                        )
                    seen.add(id(origin))
                    origin, origin_table, origin_role = parent
                actual, expected = row[source.column], origin[source.source_column]
                if type(actual) is not type(expected) or actual != expected:
                    raise _error(
                        "contradictory_parent_key", "Carried source-key value contradicts its ancestor",
                        path=role_plan.source_path, table=table_name, row=index,
                        column=source.column,
                        remedy="Use the declared ancestor's source-key value",
                    )

    def position_order(values: Sequence[Cell]) -> list[int]:
        metadata = [
            {"missing": value is None, "position": value, "delivery": index}
            for index, value in enumerate(values)
        ]
        ordered = ma.relation(metadata).sort("missing", "position", "delivery").to_dicts()
        return [cast(int, item["delivery"]) for item in ordered]

    def members(
        table_name: str,
        role_name: str | None,
        parent_table: str,
        parent_role: str | None,
        parent_key: _KeyIdentity,
    ) -> list[Mapping[str, Cell]]:
        group = groups.get(
            (table_name, role_name, parent_table, parent_role, parent_key), []
        )
        role_plan = prepared.tables[table_name].roles[role_name]
        position_column = prepared.tables[table_name].layout.position
        if (
            len(group) < 2
            or not role_plan.ordered
            or position_column is None
        ):
            return group
        return [group[index] for index in position_order([row.get(position_column) for row in group])]

    def variant_members(
        program: VariantProgram,
        parent_table: str,
        parent_role: str | None,
        parent_key: _KeyIdentity,
    ) -> list[tuple[Mapping[str, Cell], str]]:
        result: list[tuple[Mapping[str, Cell], str]] = []
        for branch in program.alternatives:
            if branch.table is not None:
                result.extend(
                    (row, branch.table)
                    for row in groups.get(
                        (
                            branch.table,
                            branch.role,
                            parent_table,
                            parent_role,
                            parent_key,
                        ),
                        [],
                    )
                )
        if program.null_table is not None:
            result.extend(
                (row, program.null_table)
                for row in groups.get(
                    (
                        program.null_table,
                        program.null_role,
                        parent_table,
                        parent_role,
                        parent_key,
                    ),
                    [],
                )
            )

        ordered = all(
            prepared.tables[table].roles[row_role(row)].ordered
            for row, table in result
        )
        if result and ordered and any(
            prepared.tables[table].layout.position is not None
            for _, table in result
        ):
            positions = [
                row.get(cast(str, prepared.tables[table].layout.position))
                if prepared.tables[table].layout.position is not None else None
                for row, table in result
            ]
            return [result[index] for index in position_order(positions)]
        return result

    def descendants(
        table_name: str, role_name: str | None, key: _KeyIdentity
    ) -> bool:
        return any(
            parent_table == table_name
            and parent_role == role_name
            and parent_key == key
            and group
            for (
                _,
                _,
                parent_table,
                parent_role,
                parent_key,
            ), group in groups.items()
        )

    def no_content(
        program_id: int,
        row: Mapping[str, Cell],
        table_name: str,
        key: _KeyIdentity,
        ignored_columns: frozenset[str] = frozenset(),
    ) -> bool:
        parent_role = row_role(row)
        pending = [program_id]
        while pending:
            program = prepared.programs[pending.pop()]
            if isinstance(program, ScalarProgram):
                if any(row[field.name] is not None for field in program.codec.fields
                       if field.name not in ignored_columns):
                    return False
            elif isinstance(program, RecordProgram):
                pending.extend(child for _, child in reversed(program.members))
            elif isinstance(program, TupleProgram):
                pending.extend(reversed(program.items))
            elif isinstance(program, VariantProgram):
                if program.repeated and program.layout == "split":
                    if variant_members(program, table_name, parent_role, key):
                        return False
                    continue
                if program.payload_null_column is not None:
                    payload_marker = row[program.payload_null_column]
                    if payload_marker is True or (
                        payload_marker is not None and type(payload_marker) is not bool
                    ):
                        return False
                if (not program.source_discriminator and program.discriminator_column not in ignored_columns
                        and row[program.discriminator_column] is not None):
                    return False
                pending.extend(branch.program for branch in reversed(program.alternatives))
            elif isinstance(program, (SequenceProgram, MapProgram)):
                if groups.get(
                    (
                        program.table,
                        program.role,
                        table_name,
                        parent_role,
                        key,
                    )
                ):
                    return False
            elif isinstance(program, LinkProgram):
                if groups.get(
                    (
                        program.table,
                        program.role,
                        table_name,
                        parent_role,
                        key,
                    )
                ):
                    return False
        return True

    def matches_tag(branch: VariantBranch, value: Cell) -> bool:
        return any(type(value) is type(tag) and value == tag for tag in branch.tags)

    def selected_payload_is_null(
        program: VariantProgram,
        branch: VariantBranch,
        row: Mapping[str, Cell],
        table_name: str,
        key: _KeyIdentity,
        row_number: int,
    ) -> bool:
        column = program.payload_null_column
        if column is None:
            return False
        marker = row[column]
        if type(marker) is not bool:
            raise _error(
                "invalid_variant_payload",
                "Selected alternative null flag must be a boolean",
                path=program.path,
                remedy="Supply false for a payload or true for a declared nil payload",
                table=table_name,
                row=row_number,
                column=column,
            )
        if marker and not branch.nullable_payload:
            raise _error(
                "invalid_variant_payload",
                f"Alternative {branch.key!r} does not declare a nullable payload",
                path=program.path,
                remedy="Clear the payload null flag or select a nillable alternative",
                table=table_name,
                row=row_number,
                column=column,
            )
        if marker and not no_content(branch.program, row, table_name, key):
            raise _error(
                "invalid_variant_payload",
                "Nil selected payload has populated content",
                path=program.path,
                remedy="Clear payload fields and descendants or clear the payload null flag",
                table=table_name,
                row=row_number,
                column=column,
            )
        return marker

    # Null markers are structural assertions: no business projection or child occurrence may coexist.
    for table_name, plan in prepared.tables.items():
        business_columns = set(plan.columns)
        business_columns.difference_update(plan.layout.key)
        business_columns.difference_update(plan.layout.parent)
        business_columns.difference_update(
            column
            for column in (
                plan.layout.position,
                plan.layout.map_key,
                plan.layout.parent_table,
                plan.layout.role_column,
                plan.layout.null_column,
            )
            if column is not None
        )
        for index, row in enumerate(ordered_rows[table_name]):
            role_name = row_role(row)
            role_plan = plan.roles[role_name]
            if not role_plan.nullable_member:
                continue
            marker = row[cast(str, plan.layout.null_column)]
            if type(marker) is not bool:
                raise _error(
                    "invalid_null_marker",
                    "Null marker must be a boolean",
                    path=role_plan.source_path,
                    remedy="Supply true or false",
                    table=table_name,
                    row=index,
                    column=plan.layout.null_column,
                )
            if marker:
                if any(row[column] is not None for column in business_columns):
                    raise _error(
                        "contradictory_null_member",
                        "Null member has populated business cells",
                        path=role_plan.source_path,
                        remedy="Clear the payload or set the null marker false",
                        table=table_name,
                        row=index,
                    )
                key = row_key(row, table_name)
                if descendants(table_name, role_name, key):
                    raise _error(
                        "contradictory_null_member",
                        "Null member has descendant rows",
                        path=role_plan.source_path,
                        remedy="Remove descendants or set the null marker false",
                        table=table_name,
                        row=index,
                    )
    # This walk uses row references, not a materialized native-value tree.
    consumed: set[tuple[str, str | None, _KeyIdentity]] = set()
    checked_scalars: dict[tuple[int, str, str | None, _KeyIdentity], object] = {}
    checked_variants: dict[tuple[int, str, str | None, _KeyIdentity], str | None] = {}
    checked_variant_nulls: set[tuple[int, str, str | None, _KeyIdentity]] = set()
    scalar_batches: dict[int, list[tuple[Mapping[str, Cell], str, int, str | None, _KeyIdentity]]] = defaultdict(list)
    validation: list[tuple[int, Mapping[str, Cell], str, int]] = [
        (prepared.root, root_row, prepared.root_table, 0)
    ]
    while validation:
        value_id, value_row, value_table, value_number = validation.pop()
        value_program = prepared.programs[value_id]
        value_key = row_key(value_row, value_table)
        value_role_name = row_role(value_row)
        consumed.add((value_table, value_role_name, value_key))
        value_plan = prepared.tables[value_table]
        value_role = value_plan.roles[value_role_name]
        if value_role.nullable_member and value_row[cast(str, value_plan.layout.null_column)] is True:
            continue
        if isinstance(value_program, ScalarProgram):
            scalar_batches[value_id].append((value_row, value_table, value_number, value_role_name, value_key))
        elif isinstance(value_program, VariantProgram):
            if value_program.repeated and value_program.layout == "split":
                variant_rows = variant_members(
                    value_program, value_table, value_role_name, value_key
                )
                branches_by_table = {
                    (branch.table, branch.role): branch
                    for branch in value_program.alternatives
                    if branch.table is not None
                }
                for child, child_table in variant_rows:
                    child_role_name = row_role(child)
                    child_key = row_key(child, child_table)
                    child_number = occurrence_number(child, child_table)
                    child_identity = (
                        value_id,
                        child_table,
                        child_role_name,
                        child_key,
                    )
                    if (
                        child_table,
                        child_role_name,
                    ) == (value_program.null_table, value_program.null_role):
                        child_role = prepared.tables[child_table].roles[
                            child_role_name
                        ]
                        if (
                            not child_role.nullable_member
                            or child[cast(str, prepared.tables[child_table].layout.null_column)]
                            is not True
                        ):
                            raise _error(
                                "invalid_variant_payload",
                                "Null-role row must have a true null marker",
                                path=value_program.path,
                                remedy="Use the prepared $null role only for a null occurrence",
                                table=child_table,
                                row=child_number,
                                column=prepared.tables[child_table].layout.null_column,
                            )
                        if not value_program.source_discriminator and child[value_program.discriminator_column] is not None:
                            raise _error(
                                "invalid_discriminator",
                                "Null-role row must have a null discriminator",
                                path=value_program.path,
                                remedy="Clear the discriminator for a null occurrence",
                                table=child_table,
                                row=child_number,
                                column=value_program.discriminator_column,
                            )
                        consumed.add((child_table, child_role_name, child_key))
                        checked_variants[child_identity] = None
                        continue
                    branch = branches_by_table.get((child_table, child_role_name))
                    if (
                        branch is None
                        or not matches_tag(branch, child[
                            cast(str, branch.source_column) if value_program.source_discriminator
                            else value_program.discriminator_column])
                    ):
                        raise _error(
                            "invalid_discriminator",
                            "Discriminator does not agree with the selected Split alternative table",
                            path=value_program.path,
                            remedy="Place the occurrence in its declared alternative table and use its tag",
                            table=child_table,
                            row=child_number,
                            column=value_program.discriminator_column,
                        )
                    payload_is_null = selected_payload_is_null(
                        value_program, branch, child, child_table, child_key, child_number
                    )
                    checked_variants[child_identity] = branch.key
                    if payload_is_null:
                        checked_variant_nulls.add(child_identity)
                        consumed.add((child_table, child_role_name, child_key))
                    else:
                        branch_role = prepared.tables[child_table].roles[
                            child_role_name
                        ]
                        validation.append(
                            (branch_role.program, child, child_table, child_number)
                        )
            else:
                tag = value_row.get(value_program.discriminator_column)
                candidates = [
                    item for item in value_program.alternatives
                    if matches_tag(item, value_row[
                        cast(str, item.source_column) if value_program.source_discriminator
                        else value_program.discriminator_column])
                ]
                branch = candidates[0] if len(candidates) == 1 else None
                null_tag = (
                    all(value_row[cast(str, item.source_column)] is None for item in value_program.alternatives)
                    if value_program.source_discriminator else tag is None
                )
                if null_tag and value_program.nullable:
                    if value_program.payload_null_column is not None:
                        null_payload = value_row[value_program.payload_null_column]
                        if type(null_payload) is not bool or null_payload:
                            raise _error(
                                "invalid_variant_payload",
                                "A null variant cannot carry a selected-payload null flag",
                                path=value_program.path,
                                remedy="Set the selected-payload null flag false or select its discriminator",
                                table=value_table,
                                row=value_number,
                                column=value_program.payload_null_column,
                            )
                    if any(
                        not no_content(item.program, value_row, value_table, value_key)
                        for item in value_program.alternatives
                    ):
                        raise _error(
                            "invalid_variant_payload",
                            "A null variant has populated alternative payload",
                            path=value_program.path,
                            remedy="Clear all alternative fields and descendants or select a discriminator",
                            table=value_table,
                            row=value_number,
                            column=value_program.discriminator_column,
                        )
                    checked_variants[
                        (value_id, value_table, value_role_name, value_key)
                    ] = None
                    continue
                if branch is None:
                    raise _error(
                        "invalid_discriminator",
                        f"Unknown variant discriminator {tag!r}",
                        path=value_program.path,
                        remedy="Use the tag for one prepared alternative",
                        table=value_table,
                        row=value_number,
                        column=value_program.discriminator_column,
                    )
                payload_is_null = selected_payload_is_null(
                    value_program, branch, value_row, value_table, value_key, value_number
                )
                shared_columns = payload_columns(branch.program) if value_program.layout == "shared" else frozenset()
                for inactive in value_program.alternatives:
                    if inactive.key != branch.key and not no_content(
                        inactive.program, value_row, value_table, value_key, shared_columns
                    ):
                        raise _error(
                            "invalid_variant_payload",
                            f"Inactive alternative {inactive.key!r} has populated payload",
                            path=value_program.path,
                            remedy="Clear fields and descendant rows for inactive alternatives",
                            table=value_table,
                            row=value_number,
                            column=value_program.discriminator_column,
                        )
                checked_variants[
                    (value_id, value_table, value_role_name, value_key)
                ] = branch.key
                if payload_is_null:
                    checked_variant_nulls.add(
                        (value_id, value_table, value_role_name, value_key)
                    )
                else:
                    validation.append(
                        (branch.program, value_row, value_table, value_number)
                    )
        elif isinstance(value_program, (RecordProgram, TupleProgram)):
            if value_program.nullable and no_content(value_id, value_row, value_table, value_key):
                continue
            child_ids = (
                tuple(child for _, child in value_program.members)
                if isinstance(value_program, RecordProgram)
                else value_program.items
            )
            validation.extend((child, value_row, value_table, value_number) for child in reversed(child_ids))
        elif isinstance(value_program, (SequenceProgram, MapProgram, LinkProgram)):
            collection_rows = members(
                value_program.table,
                value_program.role,
                value_table,
                value_role_name,
                value_key,
            )
            if (
                isinstance(value_program, LinkProgram)
                and not collection_rows
                and not value_program.nullable
            ):
                raise _error(
                    "missing_required_link",
                    "Required recursive link has no row",
                    path=value_program.path,
                    remedy="Supply the linked occurrence",
                    table=value_table,
                    row=value_number,
                )
            child_role = prepared.tables[value_program.table].roles[
                value_program.role
            ]
            validation.extend(
                (
                    child_role.program,
                    child,
                    value_program.table,
                    occurrence_number(child, value_program.table),
                )
                for child in reversed(collection_rows)
            )
        elif isinstance(value_program, NullProgram):
            continue
        else:
            raise _error(
                "invalid_prepared_mapping",
                f"Unsupported program {type(value_program).__name__}",
                path=value_program.path,
                remedy="Prepare a supported value shape",
                table=value_table,
                row=value_number,
            )
    for table_name, rows in ordered_rows.items():
        if table_name == prepared.root_table:
            continue
        for index, row in enumerate(rows):
            role_name = row_role(row)
            key = row_key(row, table_name)
            if (table_name, role_name, key) not in consumed:
                raise _error(
                    "unattached_row",
                    "Occurrence row is not reachable from the root payload",
                    path=prepared.tables[table_name].roles[role_name].source_path,
                    remedy="Attach the row or remove it",
                    table=table_name,
                    row=index,
                )

    # Project only selected scalar payloads, once per prepared use. Physical
    # keys/links remain untouched, and all checks precede native construction.
    for value_id, batch in scalar_batches.items():
        scalar = cast(ScalarProgram, prepared.programs[value_id])
        restored_rows = (
            scalar.projection.apply([row for row, _, _, _, _ in batch], reverse=True)
            if scalar.projection is not None else None
        )
        for index, (row, table, number, role, key) in enumerate(batch):
            checked_scalars[(value_id, table, role, key)] = _scalar_cell(
                restored_rows[index] if restored_rows is not None else row,
                scalar, table=table, row_number=number,
            )

    # Postorder evaluation tasks append results; no Python recursion is used for native depth.
    results: list[object] = []

    def push_eval(
        stack: list[_EvalTask], program_id: int, row: Mapping[str, Cell], table_name: str, row_number: int
    ) -> None:
        stack.append(("eval", program_id, row, table_name, row_number, None))

    stack: list[_EvalTask] = []
    push_eval(stack, prepared.root, root_row, prepared.root_table, 0)
    while stack:
        op, program_id, row, row_table, row_number, extra = stack.pop()
        if op == "finish_variant_member":
            variant = cast(VariantProgram, prepared.programs[program_id])
            alternative_key, payload_is_null = cast(tuple[str | None, bool], extra)
            if alternative_key is None:
                results.append(None)
            elif payload_is_null:
                results.append(variant.adapter.construct(alternative_key, None))
            else:
                payload = results.pop()
                results.append(variant.adapter.construct(alternative_key, payload))
            continue
        if op == "finish_variant_single":
            variant = cast(VariantProgram, prepared.programs[program_id])
            payload = results.pop()
            results.append(variant.adapter.construct(cast(str, extra), payload))
            continue
        if op == "finish_variant_sequence":
            count = cast(int, extra)
            values = results[-count:] if count else []
            if count:
                del results[-count:]
            variant = cast(VariantProgram, prepared.programs[program_id])
            results.append(tuple(values) if variant.container == "tuple" else values)
            continue
        if op == "finish_variant_map":
            count, map_keys_for_values = cast(tuple[int, tuple[str, ...]], extra)
            values = results[-count:] if count else []
            if count:
                del results[-count:]
            results.append(dict(zip(map_keys_for_values, values, strict=True)))
            continue
        if op == "finish_record":
            count = cast(int, extra)
            values = results[-count:] if count else []
            if count:
                del results[-count:]
            names = [name for name, _ in cast(RecordProgram, prepared.programs[program_id]).members]
            native = prepared.native_types.get(cast(RecordProgram, prepared.programs[program_id]).definition)
            if native is None:
                raise _error(
                    "invalid_prepared_mapping",
                    "No native class for record definition",
                    path=cast(RecordProgram, prepared.programs[program_id]).path,
                    remedy="Prepare with the derived native type association",
                    table=row_table,
                    row=row_number,
                )
            try:
                record_values = dict(zip(names, values, strict=True))
            except ValueError as exc:
                raise _error(
                    "invalid_prepared_mapping",
                    "Record construction values do not match members",
                    path=cast(RecordProgram, prepared.programs[program_id]).path,
                    remedy="Prepare a complete record program",
                    table=row_table,
                    row=row_number,
                ) from exc
            # Constructor argument order is irrelevant; child evaluation order remains declared order.
            try:
                constructed = prepared.record_adapters[cast(RecordProgram, prepared.programs[program_id]).definition].construct(
                    record_values, path=cast(RecordProgram, prepared.programs[program_id]).path
                )
            except StructureMapError as exc:
                if exc.code == "native_construct_failed":
                    exc.table = row_table
                    exc.row = row_number
                raise
            results.append(constructed)
            continue
        if op == "finish_tuple":
            count = cast(int, extra)
            values = results[-count:] if count else []
            if count:
                del results[-count:]
            results.append(tuple(values))
            continue
        if op == "finish_sequence":
            count = cast(int, extra)
            values = results[-count:] if count else []
            if count:
                del results[-count:]
            sequence = cast(SequenceProgram, prepared.programs[program_id])
            results.append(tuple(values) if sequence.container == "tuple" else values)
            continue
        if op == "finish_map":
            count, keys = cast(tuple[int, tuple[str, ...]], extra)
            values = results[-count:] if count else []
            if count:
                del results[-count:]
            results.append(dict(zip(keys, values, strict=True)))
            continue
        key = row_key(row, row_table)
        row_role_name = row_role(row)
        program = prepared.programs[program_id]
        if row_table != prepared.root_table:
            row_plan = prepared.tables[row_table]
            if (
                row_plan.roles[row_role_name].nullable_member
                and row[cast(str, row_plan.layout.null_column)] is True
            ):
                results.append(None)
                continue
        if isinstance(program, VariantProgram):
            if program.repeated and program.layout == "split":
                variant_rows = variant_members(
                    program, row_table, row_role_name, key
                )
                if not variant_rows and program.nullable:
                    results.append(None)
                    continue
                if program.map_values:
                    map_keys_for_values = tuple(
                        native_map_keys[
                            (child_table, row_role(child), row_key(child, child_table))
                        ]
                        for child, child_table in variant_rows
                    )
                    finish_op = "finish_variant_map"
                    finish_extra: object = (len(variant_rows), map_keys_for_values)
                else:
                    finish_op = "finish_variant_sequence"
                    finish_extra = len(variant_rows)
                stack.append((finish_op, program_id, row, row_table, row_number, finish_extra))
                for child, child_table in reversed(variant_rows):
                    child_role_name = row_role(child)
                    child_key = row_key(child, child_table)
                    child_number = occurrence_number(child, child_table)
                    child_identity = (
                        program_id,
                        child_table,
                        child_role_name,
                        child_key,
                    )
                    if (
                        child_table,
                        child_role_name,
                    ) == (program.null_table, program.null_role):
                        stack.append(
                            (
                                "finish_variant_member",
                                program_id,
                                child,
                                child_table,
                                child_number,
                                (None, False),
                            )
                        )
                        continue
                    alternative_key = checked_variants[child_identity]
                    branch = next(
                        item
                        for item in program.alternatives
                        if item.key == alternative_key
                    )
                    is_nil_payload = child_identity in checked_variant_nulls
                    stack.append(
                        (
                            "finish_variant_member",
                            program_id,
                            child,
                            child_table,
                            child_number,
                            (alternative_key, is_nil_payload),
                        )
                    )
                    if not is_nil_payload:
                        child_program = prepared.tables[child_table].roles[
                            child_role_name
                        ].program
                        stack.append(
                            ("eval", child_program, child, child_table, child_number, None)
                        )
                continue
            selected_identity = (program_id, row_table, row_role_name, key)
            alternative_key = checked_variants[selected_identity]
            if alternative_key is None:
                results.append(None)
                continue
            if selected_identity in checked_variant_nulls:
                results.append(program.adapter.construct(alternative_key, None))
                continue
            branch = next(item for item in program.alternatives if item.key == alternative_key)
            stack.append(
                ("finish_variant_single", program_id, row, row_table, row_number, alternative_key)
            )
            stack.append(("eval", branch.program, row, row_table, row_number, None))
            continue
        if isinstance(program, NullProgram):
            results.append(None)
            continue
        if isinstance(program, ScalarProgram):
            checked = checked_scalars[(program_id, row_table, row_role_name, key)]
            results.append(None if checked is None else cast(DecodingCodec, program.codec).decode(checked))
            continue
        if isinstance(program, RecordProgram):
            if program.nullable and no_content(program_id, row, row_table, key):
                results.append(None)
                continue
            member_tasks = [(child_id, row, row_table, row_number) for _, child_id in program.members]
            stack.append(("finish_record", program_id, row, row_table, row_number, len(member_tasks)))
            for child_id, child_row, child_table, child_number in reversed(member_tasks):
                stack.append(("eval", child_id, child_row, child_table, child_number, None))
            continue
        if isinstance(program, TupleProgram):
            if program.nullable and no_content(program_id, row, row_table, key):
                results.append(None)
                continue
            stack.append(("finish_tuple", program_id, row, row_table, row_number, len(program.items)))
            for child_id in reversed(program.items):
                stack.append(("eval", child_id, row, row_table, row_number, None))
            continue
        if isinstance(program, (SequenceProgram, MapProgram)):
            collection_rows = members(
                program.table, program.role, row_table, row_role_name, key
            )
            if program.nullable and not collection_rows:
                results.append(None)
                continue
            child_role = prepared.tables[program.table].roles[program.role]
            child_program = child_role.program
            is_map = isinstance(program, MapProgram)
            if is_map:
                map_key_values = tuple(
                    native_map_keys[
                        (program.table, program.role, row_key(child, program.table))
                    ]
                    for child in collection_rows
                )
                finish_extra = (len(collection_rows), map_key_values)
            else:
                finish_extra = len(collection_rows)
            stack.append(
                (
                    "finish_map" if is_map else "finish_sequence",
                    program_id,
                    row,
                    row_table,
                    row_number,
                    finish_extra,
                )
            )
            for child in reversed(collection_rows):
                stack.append(
                    (
                        "eval",
                        child_program,
                        child,
                        program.table,
                        occurrence_number(child, program.table),
                        None,
                    )
                )
            continue
        if isinstance(program, LinkProgram):
            linked_rows = members(
                program.table, program.role, row_table, row_role_name, key
            )
            if not linked_rows:
                if program.nullable:
                    results.append(None)
                    continue
                raise _error(
                    "missing_required_link",
                    "Required recursive link has no row",
                    path=program.path,
                    remedy="Supply the linked occurrence",
                    table=row_table,
                    row=row_number,
                )
            if len(linked_rows) != 1:
                raise _error(
                    "singular_cardinality",
                    f"Expected one linked row, got {len(linked_rows)}",
                    path=program.path,
                    remedy="Supply at most one linked occurrence",
                    table=program.table,
                    row=occurrence_number(linked_rows[1], program.table),
                )
            linked_row = linked_rows[0]
            linked_role = prepared.tables[program.table].roles[program.role]
            stack.append(
                (
                    "eval",
                    linked_role.program,
                    linked_row,
                    program.table,
                    occurrence_number(linked_row, program.table),
                    None,
                )
            )
            continue
        raise _error(
            "invalid_prepared_mapping",
            f"Unsupported program {type(program).__name__}",
            path=program.path,
            remedy="Prepare a supported value shape",
            table=row_table,
            row=row_number,
        )

    if len(results) != 1:
        raise _error(
            "invalid_prepared_mapping",
            "Reconstruction did not produce exactly one root value",
            path="",
            remedy="Prepare a valid finite value graph",
            table=prepared.root_table,
        )
    return cast(T, results[0])
