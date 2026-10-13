from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import TypeVar, cast

import mountainash as ma

from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.prepared import (
    Cell,
    LinkProgram,
    MapProgram,
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
    TupleProgram,
    ValueProgram,
)

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
T = TypeVar("T")
_PYTHON_TYPES = {"str": str, "int": int, "float": float, "bool": bool}
_EvalTask = tuple[str, int, Mapping[str, Cell], str, int, object]


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
    value: object,
    program: ScalarProgram,
    *,
    table: str,
    row_number: int,
) -> Cell:
    if value is None:
        if program.nullable:
            return None
        raise _error(
            "null_not_allowed",
            "Non-nullable scalar cell is null",
            path=program.path,
            remedy="Supply a value or declare the member nullable",
            table=table,
            row=row_number,
            column=program.column,
        )
    expected = _PYTHON_TYPES[program.scalar_type]
    if type(value) is not expected:
        raise _error(
            "cell_type_mismatch",
            f"Expected {expected.__name__}, got {type(value).__name__}",
            path=program.path,
            remedy=f"Supply a {expected.__name__} cell",
            table=table,
            row=row_number,
            column=program.column,
        )
    if isinstance(value, int) and not _I64_MIN <= value <= _I64_MAX:
        raise _error(
            "value_out_of_range",
            "Integer does not fit signed i64",
            path=program.path,
            remedy="Supply an integer in the signed i64 range",
            table=table,
            row=row_number,
            column=program.column,
        )
    return cast(Cell, value)


def reconstruct(tables: Mapping[str, Sequence[Mapping[str, Cell]]], prepared: PreparedMapping[T]) -> T:
    """Validate and iteratively construct native values from finite occurrence tables."""
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
        extra_names = sorted(actual_names - expected_names)
        raise _error(
            "table_set_mismatch",
            f"Missing tables {missing!r}; unexpected tables {extra_names!r}",
            path="",
            remedy="Supply exactly the tables in the prepared mapping",
        )

    indexed: dict[str, dict[str, Mapping[str, Cell]]] = {}
    row_numbers: dict[str, dict[str, int]] = {}
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
        by_key: dict[str, Mapping[str, Cell]] = {}
        key_rows: dict[str, int] = {}
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
            key = row[prepared.key_column]
            if not isinstance(key, str) or not key:
                raise _error(
                    "invalid_key",
                    "Occurrence key must be a nonempty string",
                    path=plan.source_path,
                    remedy="Supply a nonempty string key",
                    table=table_name,
                    row=index,
                    column=prepared.key_column,
                )
            if key in by_key:
                raise _error(
                    "duplicate_key",
                    f"Duplicate occurrence key {key!r}",
                    path=plan.source_path,
                    remedy="Use a unique key within this table",
                    table=table_name,
                    row=index,
                    column=prepared.key_column,
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

    # Rows are attached only after resolving both halves of their parent identity.
    groups: dict[tuple[str, str, str], list[Mapping[str, Cell]]] = defaultdict(list)
    positions: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    map_keys: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for table_name, plan in prepared.tables.items():
        if table_name == prepared.root_table:
            continue
        allowed_parents = set(plan.parent_tables)
        for index, row in enumerate(ordered_rows[table_name]):
            parent_key = row[prepared.parent_column]
            if not isinstance(parent_key, str):
                raise _error(
                    "orphan_row",
                    f"Child parent key {parent_key!r} is not a string",
                    path=plan.source_path,
                    remedy="Link the row to an enclosing occurrence",
                    table=table_name,
                    row=index,
                    column=prepared.parent_column,
                )
            if prepared.parent_table_column in plan.columns:
                parent_table = row[prepared.parent_table_column]
                if type(parent_table) is not str or parent_table not in allowed_parents:
                    raise _error(
                        "invalid_parent_table",
                        f"Parent table {parent_table!r} is not permitted",
                        path=plan.source_path,
                        remedy=f"Use one of {sorted(allowed_parents)!r}",
                        table=table_name,
                        row=index,
                        column=prepared.parent_table_column,
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
                    column=prepared.parent_column,
                )
            identity = (table_name, parent_table, parent_key)
            if plan._kind in ("sequence", "map"):
                position = row[prepared.position_column]
                if type(position) is not int or not 0 <= position <= _I64_MAX:
                    raise _error(
                        "invalid_position",
                        "Position must be a nonnegative signed-i64 integer",
                        path=plan.source_path,
                        remedy="Supply an integer position between 0 and 2**63 - 1",
                        table=table_name,
                        row=index,
                        column=prepared.position_column,
                    )
                if position in positions[identity]:
                    raise _error(
                        "duplicate_position",
                        f"Duplicate position {position} for parent {parent_key!r}",
                        path=plan.source_path,
                        remedy="Use a unique position for each collection member",
                        table=table_name,
                        row=index,
                        column=prepared.position_column,
                    )
                positions[identity].add(position)
                if plan._kind == "map":
                    map_key = row[prepared.map_key_column]
                    if type(map_key) is not str:
                        raise _error(
                            "invalid_map_key",
                            "Map key must be a string cell",
                            path=plan.source_path,
                            remedy="Supply a string map key",
                            table=table_name,
                            row=index,
                            column=prepared.map_key_column,
                        )
                    if map_key in map_keys[identity]:
                        raise _error(
                            "duplicate_map_key",
                            f"Duplicate map key {map_key!r}",
                            path=plan.source_path,
                            remedy="Use unique keys within each map occurrence",
                            table=table_name,
                            row=index,
                            column=prepared.map_key_column,
                        )
                    map_keys[identity].add(map_key)
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

    def members(table_name: str, parent_table: str, parent_key: str) -> list[Mapping[str, Cell]]:
        group = groups.get((table_name, parent_table, parent_key), [])
        if len(group) < 2:
            return group
        position_column = prepared.position_column
        metadata = [{position_column: row[position_column]} for row in group]
        sorted_metadata = ma.relation(metadata).sort(position_column).to_dicts()
        by_position = {row[position_column]: row for row in group}
        return [by_position[item[position_column]] for item in sorted_metadata]

    def descendants(table_name: str, key: str) -> bool:
        return any(
            parent_table == table_name and parent_key == key and group
            for (_, parent_table, parent_key), group in groups.items()
        )

    def no_content(program_id: int, row: Mapping[str, Cell], table_name: str, key: str) -> bool:
        pending = [program_id]
        while pending:
            program = prepared.programs[pending.pop()]
            if isinstance(program, ScalarProgram):
                if row[program.column] is not None:
                    return False
            elif isinstance(program, RecordProgram):
                pending.extend(child for _, child in reversed(program.members))
            elif isinstance(program, TupleProgram):
                pending.extend(reversed(program.items))
            elif isinstance(program, (SequenceProgram, MapProgram)):
                if groups.get((program.table, table_name, key)):
                    return False
            elif isinstance(program, LinkProgram):
                if groups.get((program.table, table_name, key)):
                    return False
        return True

    # Null markers are structural assertions: no business projection or child occurrence may coexist.
    for table_name, plan in prepared.tables.items():
        if not plan._nullable_member:
            continue
        business_columns = set(plan.columns)
        business_columns.discard(prepared.key_column)
        if plan._kind != "root":
            business_columns.discard(prepared.parent_column)
        if plan._kind in ("sequence", "map"):
            business_columns.discard(prepared.position_column)
        if plan._kind == "map":
            business_columns.discard(prepared.map_key_column)
        if len(plan.parent_tables) > 1:
            business_columns.discard(prepared.parent_table_column)
        business_columns.discard(prepared.null_column)
        for index, row in enumerate(ordered_rows[table_name]):
            marker = row[prepared.null_column]
            if type(marker) is not bool:
                raise _error(
                    "invalid_null_marker",
                    "Null marker must be a boolean",
                    path=plan.source_path,
                    remedy="Supply true or false",
                    table=table_name,
                    row=index,
                    column=prepared.null_column,
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
                key = cast(str, row[prepared.key_column])
                if descendants(table_name, key):
                    raise _error(
                        "contradictory_null_member",
                        "Null member has descendant rows",
                        path=plan.source_path,
                        remedy="Remove descendants or set the null marker false",
                        table=table_name,
                        row=index,
                    )

    # Postorder evaluation tasks append results; no Python recursion is used for native depth.
    results: list[object] = []
    consumed: set[tuple[str, str]] = set()

    def push_eval(
        stack: list[_EvalTask], program_id: int, row: Mapping[str, Cell], table_name: str, row_number: int
    ) -> None:
        stack.append(("eval", program_id, row, table_name, row_number, None))

    stack: list[_EvalTask] = []
    push_eval(stack, prepared.root, root_row, prepared.root_table, 0)
    while stack:
        op, program_id, row, row_table, row_number, extra = stack.pop()
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
                constructed = prepared.binding.construct(
                    native, record_values, path=cast(RecordProgram, prepared.programs[program_id]).path
                )
            except StructureMapError as exc:
                if exc.code == "native_construct_failed":
                    exc.table = row_table
                    exc.row = row_number
                raise
            results.append(constructed)
            consumed.add((row_table, cast(str, row[prepared.key_column])))
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
            results.append(values)
            continue
        if op == "finish_map":
            count, keys = cast(tuple[int, tuple[str, ...]], extra)
            values = results[-count:] if count else []
            if count:
                del results[-count:]
            results.append(dict(zip(keys, values, strict=True)))
            continue
        program: ValueProgram = prepared.programs[program_id]
        key = cast(str, row[prepared.key_column])
        if row_table != prepared.root_table:
            consumed.add((row_table, key))
            row_plan = prepared.tables[row_table]
            if row_plan._nullable_member and row[prepared.null_column] is True:
                results.append(None)
                continue

        if isinstance(program, ScalarProgram):
            results.append(_scalar_cell(row[program.column], program, table=row_table, row_number=row_number))
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
            child_rows = members(program.table, row_table, key)
            if program.nullable and not child_rows:
                results.append(None)
                continue
            plan = prepared.tables[program.table]
            child_program = plan._program
            is_map = isinstance(program, MapProgram)
            if is_map:
                map_key_values = tuple(cast(str, child[prepared.map_key_column]) for child in child_rows)
                finish_extra: object = (len(child_rows), map_key_values)
            else:
                finish_extra = len(child_rows)
            stack.append(
                ("finish_map" if is_map else "finish_sequence", program_id, row, row_table, row_number, finish_extra)
            )
            for child in reversed(child_rows):
                child_key = cast(str, child[prepared.key_column])
                stack.append(("eval", child_program, child, program.table, row_numbers[program.table][child_key], None))
            continue
        if isinstance(program, LinkProgram):
            child_rows = members(program.table, row_table, key)
            if not child_rows:
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
            if len(child_rows) != 1:
                raise _error(
                    "singular_cardinality",
                    f"Expected one linked row, got {len(child_rows)}",
                    path=program.path,
                    remedy="Supply at most one linked occurrence",
                    table=program.table,
                    row=row_numbers[program.table][cast(str, child_rows[1][prepared.key_column])],
                )
            child = child_rows[0]
            child_key = cast(str, child[prepared.key_column])
            stack.append(
                (
                    "eval",
                    prepared.tables[program.table]._program,
                    child,
                    program.table,
                    row_numbers[program.table][child_key],
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

    for table_name, rows in ordered_rows.items():
        if table_name == prepared.root_table:
            continue
        for index, row in enumerate(rows):
            key = cast(str, row[prepared.key_column])
            if (table_name, key) not in consumed:
                raise _error(
                    "unattached_row",
                    "Occurrence row is not reachable from the root payload",
                    path=prepared.tables[table_name].source_path,
                    remedy="Attach the row or remove it",
                    table=table_name,
                    row=index,
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
