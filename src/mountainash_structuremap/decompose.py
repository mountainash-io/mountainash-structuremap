from __future__ import annotations

from typing import TypeVar, cast

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
)

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
T = TypeVar("T")
_PYTHON_TYPES = {"str": str, "int": int, "float": float, "bool": bool}
_Task = tuple[str, int, object, dict[str, Cell], str, str, int, str | None]


def _data_error(code: str, message: str, *, path: str, remedy: str) -> StructureMapError:
    return StructureMapError(code, message, path=path, owner="executor restriction", remedy=remedy)


def _native_scalar(value: object, program: ScalarProgram) -> Cell:
    if value is None:
        if program.nullable:
            return None
        raise _data_error(
            "null_not_allowed",
            "Non-nullable scalar has a null value",
            path=program.path,
            remedy="Supply a value or declare the member nullable",
        )
    expected = _PYTHON_TYPES[program.scalar_type]
    if type(value) is not expected:
        raise _data_error(
            "native_type_mismatch",
            f"Expected {expected.__name__}, got {type(value).__name__}",
            path=program.path,
            remedy=f"Supply a native {expected.__name__} value",
        )
    if isinstance(value, int) and not _I64_MIN <= value <= _I64_MAX:
        raise _data_error(
            "value_out_of_range",
            "Integer does not fit signed i64",
            path=program.path,
            remedy="Supply an integer in the signed i64 range",
        )
    return cast(Cell, value)


def decompose(value: T, prepared: PreparedMapping[T]) -> dict[str, list[dict[str, Cell]]]:
    """Project one native value into rows by iteratively walking its prepared graph."""
    tables: dict[str, list[dict[str, Cell]]] = {name: [] for name in prepared.tables}
    counter = 0
    ancestry: set[int] = set()

    def new_key() -> str:
        nonlocal counter
        key = f"r{counter}"
        counter += 1
        return key

    def empty_row(table_name: str, key: str, parent: str | None = None) -> dict[str, Cell]:
        plan = prepared.tables[table_name]
        result: dict[str, Cell] = {column: None for column in plan.columns}
        result[prepared.key_column] = key
        if parent is not None:
            result[prepared.parent_column] = parent
            if prepared.parent_table_column in plan.columns:
                result[prepared.parent_table_column] = ""
        return result

    def record_type(instance: object, program: RecordProgram) -> None:
        expected = prepared.native_types.get(program.definition)
        if expected is None or type(instance) is not expected:
            name = expected.__qualname__ if expected is not None else program.definition
            raise _data_error(
                "native_type_mismatch",
                f"Expected record {name}, got {type(instance).__qualname__}",
                path=program.path,
                remedy="Supply an instance of the declared dataclass",
            )

    def begin(instance: object, path: str) -> None:
        identity = id(instance)
        if identity in ancestry:
            raise _data_error(
                "containment_cycle",
                "Native containment graph contains a cycle",
                path=path,
                remedy="Remove the containment cycle; Foundation provides no reference strategy",
            )
        ancestry.add(identity)

    def null_inline(program_id: int, row: dict[str, Cell]) -> None:
        pending = [program_id]
        while pending:
            current = prepared.programs[pending.pop()]
            if isinstance(current, ScalarProgram):
                row[current.column] = None
            elif isinstance(current, RecordProgram):
                pending.extend(child for _, child in reversed(current.members))
            elif isinstance(current, TupleProgram):
                pending.extend(reversed(current.items))

    root_program = prepared.programs.get(prepared.root)
    if not isinstance(root_program, RecordProgram):
        raise _data_error(
            "invalid_prepared_mapping",
            "Prepared root is not a record",
            path="",
            remedy="Prepare a dataclass record root",
        )

    root_key = new_key()
    root_row = empty_row(prepared.root_table, root_key)
    tables[prepared.root_table].append(root_row)
    # (op, program id, value, containing row, containing key, containing table, position, map key)
    stack: list[_Task] = [("value", prepared.root, value, root_row, root_key, prepared.root_table, -1, None)]
    while stack:
        operation, program_id, native_value, row, row_key, row_table, position, map_key = stack.pop()
        program = prepared.programs[program_id]
        if operation == "exit":
            ancestry.remove(id(native_value))
            continue
        if operation == "member":
            edge = program
            if not isinstance(edge, (SequenceProgram, MapProgram)):
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Collection member has no collection edge",
                    path=edge.path,
                    remedy="Prepare a supported collection",
                )
            member_plan = prepared.tables[edge.table]
            child_key = new_key()
            child_row = empty_row(edge.table, child_key, row_key)
            if prepared.parent_table_column in member_plan.columns:
                child_row[prepared.parent_table_column] = row_table
            if prepared.position_column in member_plan.columns:
                child_row[prepared.position_column] = position
            if isinstance(edge, MapProgram):
                child_row[prepared.map_key_column] = map_key
            if member_plan._nullable_member:
                child_row[prepared.null_column] = native_value is None
            tables[edge.table].append(child_row)
            if native_value is None and member_plan._nullable_member:
                continue
            stack.append(("value", member_plan._program, native_value, child_row, child_key, edge.table, -1, None))
            continue
        if isinstance(program, ScalarProgram):
            row[program.column] = _native_scalar(native_value, program)
            continue
        if isinstance(program, RecordProgram):
            if native_value is None:
                if program.nullable:
                    null_inline(program_id, row)
                    continue
                raise _data_error(
                    "null_not_allowed",
                    "Required inline record has a null value",
                    path=program.path,
                    remedy="Supply a record instance",
                )
            record_type(native_value, program)
            begin(native_value, program.path)
            stack.append(("exit", program_id, native_value, row, row_key, row_table, -1, None))
            children: list[tuple[int, object]] = []
            for member, child_id in program.members:
                child = prepared.programs[child_id]
                children.append((child_id, prepared.binding.read(native_value, member, path=child.path)))
            for child_id, child_value in reversed(children):
                stack.append(("value", child_id, child_value, row, row_key, row_table, -1, None))
            continue
        if isinstance(program, TupleProgram):
            if native_value is None:
                if program.nullable:
                    null_inline(program_id, row)
                    continue
                raise _data_error(
                    "null_not_allowed",
                    "Required tuple has a null value",
                    path=program.path,
                    remedy="Supply a tuple value",
                )
            if type(native_value) is not tuple or len(native_value) != len(program.items):
                raise _data_error(
                    "native_shape_mismatch",
                    f"Expected tuple of length {len(program.items)}",
                    path=program.path,
                    remedy=f"Supply a tuple with exactly {len(program.items)} items",
                )
            begin(native_value, program.path)
            stack.append(("exit", program_id, native_value, row, row_key, row_table, -1, None))
            for item_id, item_value in reversed(tuple(zip(program.items, native_value, strict=True))):
                stack.append(("value", item_id, item_value, row, row_key, row_table, -1, None))
            continue
        if isinstance(program, (SequenceProgram, MapProgram)):
            if native_value is None:
                if program.nullable:
                    continue
                raise _data_error(
                    "null_not_allowed",
                    "Non-nullable collection has a null value",
                    path=program.path,
                    remedy="Supply a collection or declare the member nullable",
                )
            entries: list[tuple[str | None, object]]
            if isinstance(program, SequenceProgram):
                if not isinstance(native_value, list):
                    raise _data_error(
                        "native_shape_mismatch", "Expected a list", path=program.path, remedy="Supply a list"
                    )
                entries = [(None, item) for item in native_value]
            else:
                if not isinstance(native_value, dict):
                    raise _data_error(
                        "native_shape_mismatch",
                        "Expected a dictionary",
                        path=program.path,
                        remedy="Supply a dictionary",
                    )
                entries = []
                for key, item in native_value.items():
                    if type(key) is not str:
                        raise _data_error(
                            "native_type_mismatch",
                            "Map keys must be strings",
                            path=program.path,
                            remedy="Supply string map keys",
                        )
                    entries.append((key, item))
            begin(native_value, program.path)
            stack.append(("exit", program_id, native_value, row, row_key, row_table, -1, None))
            for index in range(len(entries) - 1, -1, -1):
                item_key, item = entries[index]
                stack.append(("member", program_id, item, row, row_key, row_table, index, item_key))
            continue
        if isinstance(program, LinkProgram):
            if native_value is None:
                if program.nullable:
                    continue
                raise _data_error(
                    "null_not_allowed",
                    "Required recursive link has a null value",
                    path=program.path,
                    remedy="Supply a linked record",
                )
            child_plan = prepared.tables[program.table]
            child_key = new_key()
            child_row = empty_row(program.table, child_key, row_key)
            if prepared.parent_table_column in child_plan.columns:
                child_row[prepared.parent_table_column] = row_table
            tables[program.table].append(child_row)
            stack.append(("value", child_plan._program, native_value, child_row, child_key, program.table, -1, None))
            continue
        raise _data_error(
            "invalid_prepared_mapping",
            f"Unsupported prepared program {type(program).__name__}",
            path=program.path,
            remedy="Prepare a supported value shape",
        )
    return tables
