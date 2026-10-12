from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import TypeVar, cast

import mountainash as ma

from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.prepared import (
    Cell,
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
)

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
T = TypeVar("T")
_PYTHON_TYPES = {"str": str, "int": int, "float": float, "bool": bool}


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
        remedy=remedy,
        table=table,
        row=row,
        column=column,
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
            "Non-nullable scalar has a null cell",
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
            f"Expected {expected.__name__} cell, got {type(value).__name__}",
            path=program.path,
            remedy=f"Supply a {expected.__name__} cell",
            table=table,
            row=row_number,
            column=program.column,
        )
    if isinstance(value, int) and not _I64_MIN <= value <= _I64_MAX:
        raise _error(
            "value_out_of_range",
            "Integer cell does not fit signed i64",
            path=program.path,
            remedy="Supply an integer in the signed i64 range",
            table=table,
            row=row_number,
            column=program.column,
        )
    return cast(Cell, value)


def reconstruct(tables: Mapping[str, Sequence[Mapping[str, Cell]]], prepared: PreparedMapping[T]) -> T:
    """Validate supplied table occurrences and construct a fresh native record."""
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
        extra = sorted(actual_names - expected_names)
        raise _error(
            "table_set_mismatch",
            f"Missing tables {missing!r}; unexpected tables {extra!r}",
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
                extra = sorted(columns - expected_columns, key=repr)
                raise _error(
                    "row_shape_mismatch",
                    f"Missing columns {missing!r}; unexpected columns {extra!r}",
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

    root_rows = ordered_rows.get("root", [])
    if len(root_rows) != 1:
        raise _error(
            "root_cardinality",
            f"Expected exactly one root row, got {len(root_rows)}",
            path="",
            remedy="Supply exactly one root occurrence",
            table="root",
        )

    # Attach each collection to its actual enclosing row owner, including inline descendants.
    groups: dict[str, dict[str, list[Mapping[str, Cell]]]] = defaultdict(lambda: defaultdict(list))
    for table_name, plan in prepared.tables.items():
        if table_name == "root":
            continue
        if len(plan.parent_tables) != 1:
            raise _error(
                "unsupported_prepared_topology",
                "Expected one enclosing parent table",
                path=plan.source_path,
                remedy="Prepare a nonrecursive collection layout",
                table=table_name,
            )
        parent_table = plan.parent_tables[0]
        positions: dict[str, set[int]] = defaultdict(set)
        for index, row in enumerate(ordered_rows[table_name]):
            parent = row[prepared.parent_column]
            if not isinstance(parent, str) or parent not in indexed[parent_table]:
                raise _error(
                    "orphan_row",
                    f"Child parent key {parent!r} does not identify a row in {parent_table!r}",
                    path=plan.source_path,
                    remedy=f"Link the row to its enclosing occurrence in {parent_table!r}",
                    table=table_name,
                    row=index,
                    column=prepared.parent_column,
                )
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
            if position in positions[parent]:
                raise _error(
                    "duplicate_position",
                    f"Duplicate position {position} for parent {parent!r}",
                    path=plan.source_path,
                    remedy="Use a unique position for each collection member",
                    table=table_name,
                    row=index,
                    column=prepared.position_column,
                )
            positions[parent].add(position)
            groups[table_name][parent].append(row)

    def ordered_members(table_name: str, parent_key: str) -> list[Mapping[str, Cell]]:
        members = groups[table_name].get(parent_key, [])
        if not members:
            return []
        position_column = prepared.position_column
        metadata = [{position_column: row[position_column]} for row in members]
        # Only position metadata enters Mountainash; business cells are never inferred or sorted.
        sorted_metadata = ma.relation(metadata).sort(position_column).to_dicts()
        by_position = {row[position_column]: row for row in members}
        return [by_position[item[position_column]] for item in sorted_metadata]

    def no_content(program: RecordProgram, row: Mapping[str, Cell]) -> bool:
        for _, member in program.members:
            if isinstance(member, ScalarProgram):
                if row[member.column] is not None:
                    return False
            elif isinstance(member, RecordProgram):
                if not no_content(member, row):
                    return False
            elif isinstance(member, SequenceProgram):
                if groups[member.table].get(cast(str, row[prepared.key_column])):
                    return False
        return True

    def build_record(program: RecordProgram, row: Mapping[str, Cell], table_name: str, row_number: int) -> object:
        native = prepared.native_types.get(program.definition)
        if native is None:
            raise _error(
                "invalid_prepared_mapping",
                f"No native class is associated with {program.definition!r}",
                path=program.path,
                remedy="Prepare with the derived native type association",
                table=table_name,
                row=row_number,
            )
        if program.nullable and no_content(program, row):
            return None
        values: dict[str, object] = {}
        for name, member in program.members:
            if isinstance(member, ScalarProgram):
                values[name] = _scalar_cell(row[member.column], member, table=table_name, row_number=row_number)
            elif isinstance(member, RecordProgram):
                values[name] = build_record(member, row, table_name, row_number)
            elif isinstance(member, SequenceProgram):
                members = ordered_members(member.table, cast(str, row[prepared.key_column]))
                if member.nullable and not members:
                    values[name] = None
                elif isinstance(member.item, ScalarProgram):
                    values[name] = [
                        _scalar_cell(
                            item[member.item.column],
                            member.item,
                            table=member.table,
                            row_number=row_numbers[member.table][cast(str, item[prepared.key_column])],
                        )
                        for item in members
                    ]
                elif isinstance(member.item, RecordProgram):
                    values[name] = [
                        build_record(
                            member.item,
                            item,
                            member.table,
                            row_numbers[member.table][cast(str, item[prepared.key_column])],
                        )
                        for item in members
                    ]
                else:
                    raise _error(
                        "unsupported_prepared_topology",
                        "Nested collection reconstruction is outside this checkpoint",
                        path=member.path,
                        remedy="Use scalar or record list items",
                        table=member.table,
                    )
        return prepared.binding.construct(native, values, path=program.path)

    root_plan = prepared.tables.get("root")
    if root_plan is None:
        raise _error(
            "invalid_prepared_mapping",
            "Prepared mapping has no root table",
            path="",
            remedy="Prepare a dataclass record root",
        )
    return cast(T, build_record(prepared.root, root_rows[0], "root", 0))
