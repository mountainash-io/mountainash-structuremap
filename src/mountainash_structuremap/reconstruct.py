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
    ValueProgram,
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

    indexed: dict[str, dict[_KeyIdentity, Mapping[str, Cell]]] = {}
    row_numbers: dict[str, dict[_KeyIdentity, int]] = {}
    ordered_rows: dict[str, list[Mapping[str, Cell]]] = {}
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
        by_key: dict[_KeyIdentity, Mapping[str, Cell]] = {}
        key_rows: dict[_KeyIdentity, int] = {}
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
            if columns != expected_columns:
                missing = sorted(expected_columns - columns)
                extra_columns = sorted(columns - expected_columns, key=repr)
                raise _error(
                    "row_shape_mismatch",
                    f"Missing columns {missing!r}; unexpected columns {extra_columns!r}",
                    path=plan.source_path,
                    remedy="Supply exactly the prepared table columns",
                    table=table_name,
                    row=index,
                )
            key_columns = plan.layout.key
            key_cells = tuple(row[column] for column in key_columns)
            if plan.layout.generated and (
                len(key_cells) != 1 or type(key_cells[0]) is not str or not key_cells[0]
            ):
                raise _error(
                    "invalid_key",
                    "Generated occurrence key must be one nonempty string cell",
                    path=plan.source_path,
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
                    path=plan.source_path,
                    remedy="Supply non-null values for every source key column",
                    table=table_name,
                    row=index,
                    column=next((column for column, cell in zip(key_columns, key_cells) if cell is None), None),
                )
            try:
                key = _key_identity(key_cells)
                duplicate = key in by_key
            except TypeError as exc:
                raise _error(
                    "invalid_key",
                    "Occurrence key cells must be hashable",
                    path=plan.source_path,
                    remedy="Use supported scalar key cells",
                    table=table_name,
                    row=index,
                ) from exc
            if duplicate:
                raise _error(
                    "duplicate_key",
                    f"Duplicate occurrence key {key_cells!r}",
                    path=plan.source_path,
                    remedy="Use unique key cells within this table",
                    table=table_name,
                    row=index,
                    column=key_columns[0],
                )
            rows.append(row)
            by_key[key] = row
            key_rows[key] = index
        indexed[table_name] = by_key
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
        return _key_identity(tuple(row[column] for column in prepared.tables[table].layout.key))

    native_map_keys: dict[tuple[str, _KeyIdentity], str] = {}

    # Rows are attached only after resolving both halves of their parent identity.
    groups: dict[tuple[str, str, _KeyIdentity], list[Mapping[str, Cell]]] = defaultdict(list)
    positions: dict[tuple[str, str, _KeyIdentity], set[int]] = defaultdict(set)
    map_keys: dict[tuple[str, str, _KeyIdentity], set[str]] = defaultdict(set)
    for table_name, plan in prepared.tables.items():
        if table_name == prepared.root_table:
            continue
        allowed_parents = set(plan.parent_tables)
        for index, row in enumerate(ordered_rows[table_name]):
            parent_cells = tuple(row[column] for column in plan.layout.parent)
            if any(cell is None for cell in parent_cells):
                raise _error("orphan_row", "Parent key contains a null cell", path=plan.source_path,
                             table=table_name, row=index, remedy="Supply every declared parent-key cell")
            try:
                parent_key = _key_identity(parent_cells)
                hash(parent_key)
            except TypeError as exc:
                raise _error("orphan_row", "Parent key cells must be scalar", path=plan.source_path,
                             table=table_name, row=index, remedy="Supply supported scalar parent-key cells") from exc
            if len(plan.parent_tables) > 1:
                parent_table = row[cast(str, plan.layout.parent_table)]
                if type(parent_table) is not str or parent_table not in allowed_parents:
                    raise _error(
                        "invalid_parent_table",
                        f"Parent table {parent_table!r} is not permitted",
                        path=plan.source_path,
                        remedy=f"Use one of {sorted(allowed_parents)!r}",
                        table=table_name,
                        row=index,
                        column=plan.layout.parent_table,
                    )
            else:
                if len(plan.parent_tables) != 1:
                    raise _error(
                        "invalid_prepared_mapping",
                        "Ambiguous parent table has no discriminator",
                        path=plan.source_path,
                        remedy="Prepare parent-table metadata for this role",
                        table=table_name,
                    )
                parent_table = plan.parent_tables[0]
            if parent_key not in indexed.get(parent_table, {}):
                raise _error(
                    "orphan_row",
                    f"Child parent key {parent_key!r} does not identify a row in {parent_table!r}",
                    path=plan.source_path,
                    remedy=f"Link the row to an enclosing occurrence in {parent_table!r}",
                    table=table_name,
                    row=index,
                    column=plan.layout.parent[0],
                )
            identity = (table_name, parent_table, parent_key)
            position_identity = (plan._position_group or table_name, parent_table, parent_key)
            if plan._kind in ("sequence", "map"):
                position = row[cast(str, plan.layout.position)]
                if type(position) is not int or not 0 <= position <= _I64_MAX:
                    raise _error(
                        "invalid_position",
                        "Position must be a nonnegative signed-i64 integer",
                        path=plan.source_path,
                        remedy="Supply an integer position between 0 and 2**63 - 1",
                        table=table_name,
                        row=index,
                        column=plan.layout.position,
                    )
                if position in positions[position_identity]:
                    raise _error(
                        "duplicate_position",
                        f"Duplicate position {position} for parent {parent_key!r}",
                        path=plan.source_path,
                        remedy="Use a unique position for each collection member",
                        table=table_name,
                        row=index,
                        column=plan.layout.position,
                    )
                positions[position_identity].add(position)
                if plan._kind == "map":
                    map_key = row[cast(str, plan.layout.map_key)]
                    if type(map_key) is not str:
                        raise _error(
                            "invalid_map_key",
                            "Map key must be a string cell",
                            path=plan.source_path,
                            remedy="Supply a string map key",
                            table=table_name,
                            row=index,
                            column=plan.layout.map_key,
                        )
                    if plan.map_key_codec is None:
                        raise _error(
                            "invalid_prepared_mapping", "Map table has no key codec",
                            path=plan.source_path, table=table_name,
                            remedy="Prepare the map with a string key component",
                        )
                    codec = cast(DecodingCodec, plan.map_key_codec)
                    checked = codec.check((map_key,), CellLocation(
                        plan.source_path, table_name, index, plan.layout.map_key))
                    decoded_key = codec.decode(checked)
                    if type(decoded_key) is not str:
                        raise _error("invalid_map_key", "Map key codec must reconstruct a string",
                                     path=plan.source_path, table=table_name, row=index,
                                     remedy="Use a codec whose decoded keys are strings")
                    map_key = decoded_key
                    native_map_keys[(table_name, row_key(row, table_name))] = map_key
                    map_identity = (plan._position_group or table_name, parent_table, parent_key)
                    if map_key in map_keys[map_identity]:
                        raise _error(
                            "duplicate_map_key",
                            f"Duplicate map key {map_key!r}",
                            path=plan.source_path,
                            remedy="Use unique keys within each map occurrence",
                            table=table_name,
                            row=index,
                            column=plan.layout.map_key,
                        )
                    map_keys[map_identity].add(map_key)
            elif plan._kind == "single":
                if groups[identity]:
                    raise _error(
                        "singular_cardinality",
                        f"Multiple rows for singular link from {parent_key!r}",
                        path=plan.source_path,
                        remedy="Supply at most one linked occurrence",
                        table=table_name,
                        row=index,
                    )
            else:
                raise _error(
                    "invalid_prepared_mapping",
                    f"Unknown table kind {plan._kind!r}",
                    path=plan.source_path,
                    remedy="Prepare a supported table role",
                    table=table_name,
                )
            groups[identity].append(row)

    def members(table_name: str, parent_table: str, parent_key: _KeyIdentity) -> list[Mapping[str, Cell]]:
        group = groups.get((table_name, parent_table, parent_key), [])
        if len(group) < 2:
            return group
        position_column = cast(str, prepared.tables[table_name].layout.position)
        metadata = [{position_column: row[position_column]} for row in group]
        sorted_metadata = ma.relation(metadata).sort(position_column).to_dicts()
        by_position = {row[position_column]: row for row in group}
        return [by_position[item[position_column]] for item in sorted_metadata]


    def variant_members(
        program: VariantProgram, parent_table: str, parent_key: _KeyIdentity
    ) -> list[tuple[Mapping[str, Cell], str]]:
        tables = [branch.table for branch in program.alternatives if branch.table is not None]
        if program.null_table is not None:
            tables.append(program.null_table)
        result = [
            (row, table_name)
            for table_name in tables
            for row in groups.get((table_name, parent_table, parent_key), [])
        ]
        result.sort(key=lambda item: cast(int, item[0][cast(str, prepared.tables[item[1]].layout.position)]))
        return result

    def descendants(table_name: str, key: _KeyIdentity) -> bool:
        return any(
            parent_table == table_name and parent_key == key and group
            for (_, parent_table, parent_key), group in groups.items()
        )

    def no_content(program_id: int, row: Mapping[str, Cell], table_name: str, key: _KeyIdentity) -> bool:
        pending = [program_id]
        while pending:
            program = prepared.programs[pending.pop()]
            if isinstance(program, ScalarProgram):
                if any(row[field.name] is not None for field in program.codec.fields):
                    return False
            elif isinstance(program, RecordProgram):
                pending.extend(child for _, child in reversed(program.members))
            elif isinstance(program, TupleProgram):
                pending.extend(reversed(program.items))
            elif isinstance(program, VariantProgram):
                if program.repeated and program.layout == "split":
                    if variant_members(program, table_name, key):
                        return False
                    continue
                if program.payload_null_column is not None:
                    payload_marker = row[program.payload_null_column]
                    if payload_marker is True or (payload_marker is not None and type(payload_marker) is not bool):
                        return False
                if row[program.discriminator_column] is not None:
                    return False
                pending.extend(branch.program for branch in reversed(program.alternatives))
            elif isinstance(program, (SequenceProgram, MapProgram)):
                if groups.get((program.table, table_name, key)):
                    return False
            elif isinstance(program, LinkProgram):
                if groups.get((program.table, table_name, key)):
                    return False
        return True

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
        if not plan._nullable_member:
            continue
        business_columns = set(plan.columns)
        business_columns.difference_update(plan.layout.key)
        business_columns.difference_update(plan.layout.parent)
        business_columns.difference_update(
            column for column in (plan.layout.position, plan.layout.map_key,
                                  plan.layout.parent_table, plan.layout.null_column) if column is not None)
        for index, row in enumerate(ordered_rows[table_name]):
            marker = row[cast(str, plan.layout.null_column)]
            if type(marker) is not bool:
                raise _error(
                    "invalid_null_marker",
                    "Null marker must be a boolean",
                    path=plan.source_path,
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
                        path=plan.source_path,
                        remedy="Clear the payload or set the null marker false",
                        table=table_name,
                        row=index,
                    )
                key = row_key(row, table_name)
                if descendants(table_name, key):
                    raise _error(
                        "contradictory_null_member",
                        "Null member has descendant rows",
                        path=plan.source_path,
                        remedy="Remove descendants or set the null marker false",
                        table=table_name,
                        row=index,
                    )

    # Validate logical activity and reachability before any native constructor.
    # This walk uses row references, not a materialized native-value tree.
    consumed: set[tuple[str, _KeyIdentity]] = set()
    checked_scalars: dict[tuple[int, str, _KeyIdentity], object] = {}
    checked_variants: dict[tuple[int, str, _KeyIdentity], str | None] = {}
    checked_variant_nulls: set[tuple[int, str, _KeyIdentity]] = set()
    validation: list[tuple[int, Mapping[str, Cell], str, int]] = [
        (prepared.root, root_row, prepared.root_table, 0)
    ]
    while validation:
        value_id, value_row, value_table, value_number = validation.pop()
        value_program = prepared.programs[value_id]
        value_key = row_key(value_row, value_table)
        consumed.add((value_table, value_key))
        value_plan = prepared.tables[value_table]
        if value_plan._nullable_member and value_row[cast(str, value_plan.layout.null_column)] is True:
            continue
        if isinstance(value_program, ScalarProgram):
            checked_scalars[(value_id, value_table, value_key)] = _scalar_cell(
                value_row, value_program, table=value_table, row_number=value_number
            )
        elif isinstance(value_program, VariantProgram):
            if value_program.repeated and value_program.layout == "split":
                variant_rows = variant_members(value_program, value_table, value_key)
                branches_by_table = {
                    branch.table: branch for branch in value_program.alternatives if branch.table is not None
                }
                for child, child_table in variant_rows:
                    child_key = row_key(child, child_table)
                    child_number = row_numbers[child_table][child_key]
                    if child_table == value_program.null_table:
                        if (
                            not prepared.tables[child_table]._nullable_member
                            or child[cast(str, prepared.tables[child_table].layout.null_column)] is not True
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
                        if child[value_program.discriminator_column] is not None:
                            raise _error(
                                "invalid_discriminator",
                                "Null-role row must have a null discriminator",
                                path=value_program.path,
                                remedy="Clear the discriminator for a null occurrence",
                                table=child_table,
                                row=child_number,
                                column=value_program.discriminator_column,
                            )
                        consumed.add((child_table, child_key))
                        checked_variants[(value_id, child_table, child_key)] = None
                        continue
                    branch = branches_by_table.get(child_table)
                    if branch is None or child[value_program.discriminator_column] != branch.tag:
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
                    checked_variants[(value_id, child_table, child_key)] = branch.key
                    if payload_is_null:
                        checked_variant_nulls.add((value_id, child_table, child_key))
                        consumed.add((child_table, child_key))
                    else:
                        validation.append((branch.program, child, child_table, child_number))
            else:
                tag = value_row[value_program.discriminator_column]
                branch = next(
                    (item for item in value_program.alternatives if item.tag == tag),
                    None,
                )
                if tag is None and value_program.nullable:
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
                    checked_variants[(value_id, value_table, value_key)] = None
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
                for inactive in value_program.alternatives:
                    if inactive.key != branch.key and not no_content(
                        inactive.program, value_row, value_table, value_key
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
                checked_variants[(value_id, value_table, value_key)] = branch.key
                if payload_is_null:
                    checked_variant_nulls.add((value_id, value_table, value_key))
                else:
                    validation.append((branch.program, value_row, value_table, value_number))
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
            collection_rows = groups.get((value_program.table, value_table, value_key), [])
            if isinstance(value_program, LinkProgram) and not collection_rows and not value_program.nullable:
                raise _error(
                    "missing_required_link",
                    "Required recursive link has no row",
                    path=value_program.path,
                    remedy="Supply the linked occurrence",
                    table=value_table,
                    row=value_number,
                )
            child_program = prepared.tables[value_program.table]._program
            validation.extend(
                (
                    child_program,
                    child,
                    value_program.table,
                    row_numbers[value_program.table][row_key(child, value_program.table)],
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
            key = row_key(row, table_name)
            if (table_name, key) not in consumed:
                raise _error(
                    "unattached_row",
                    "Occurrence row is not reachable from the root payload",
                    path=prepared.tables[table_name].source_path,
                    remedy="Attach the row or remove it",
                    table=table_name,
                    row=index,
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
        program: ValueProgram = prepared.programs[program_id]
        key = row_key(row, row_table)
        if row_table != prepared.root_table:
            row_plan = prepared.tables[row_table]
            if row_plan._nullable_member and row[cast(str, row_plan.layout.null_column)] is True:
                results.append(None)
                continue

        if isinstance(program, VariantProgram):
            if program.repeated and program.layout == "split":
                variant_rows = variant_members(program, row_table, key)
                if not variant_rows and program.nullable:
                    results.append(None)
                    continue
                if program.map_values:
                    map_keys_for_values = tuple(
                        native_map_keys[(child_table, row_key(child, child_table))] for child, child_table in variant_rows
                    )
                    finish_op = "finish_variant_map"
                    finish_extra: object = (len(variant_rows), map_keys_for_values)
                else:
                    finish_op = "finish_variant_sequence"
                    finish_extra = len(variant_rows)
                stack.append((finish_op, program_id, row, row_table, row_number, finish_extra))
                for child, child_table in reversed(variant_rows):
                    child_key = row_key(child, child_table)
                    child_number = row_numbers[child_table][child_key]
                    if child_table == program.null_table:
                        stack.append(
                            ("finish_variant_member", program_id, child, child_table, child_number, (None, False))
                        )
                        continue
                    alternative_key = checked_variants[(program_id, child_table, child_key)]
                    branch = next(item for item in program.alternatives if item.key == alternative_key)
                    is_nil_payload = (program_id, child_table, child_key) in checked_variant_nulls
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
                        stack.append(("eval", branch.program, child, child_table, child_number, None))
                continue
            selected_identity = (program_id, row_table, key)
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
            checked = checked_scalars[(program_id, row_table, key)]
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
            collection_rows = members(program.table, row_table, key)
            if program.nullable and not collection_rows:
                results.append(None)
                continue
            plan = prepared.tables[program.table]
            child_program = plan._program
            is_map = isinstance(program, MapProgram)
            if is_map:
                map_key_values = tuple(native_map_keys[(program.table, row_key(child, program.table))] for child in collection_rows)
                finish_extra = (len(collection_rows), map_key_values)
            else:
                finish_extra = len(collection_rows)
            stack.append(
                ("finish_map" if is_map else "finish_sequence", program_id, row, row_table, row_number, finish_extra)
            )
            for child in reversed(collection_rows):
                child_key = row_key(child, program.table)
                stack.append(("eval", child_program, child, program.table, row_numbers[program.table][child_key], None))
            continue
        if isinstance(program, LinkProgram):
            linked_rows = members(program.table, row_table, key)
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
                    row=row_numbers[program.table][row_key(linked_rows[1], program.table)],
                )
            linked_row = linked_rows[0]
            linked_key = row_key(linked_row, program.table)
            stack.append(
                (
                    "eval",
                    prepared.tables[program.table]._program,
                    linked_row,
                    program.table,
                    row_numbers[program.table][linked_key],
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
