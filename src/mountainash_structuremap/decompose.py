from __future__ import annotations

from typing import TypeVar

from mountainash_structuremap.codecs import Cell
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
    VariantProgram,
)

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
T = TypeVar("T")
_Task = tuple[str, int, object, dict[str, Cell], str, str, int, str | None]


def _data_error(code: str, message: str, *, path: str, remedy: str) -> StructureMapError:
    return StructureMapError(code, message, path=path, owner="executor restriction", remedy=remedy)


def _native_scalar(value: object, program: ScalarProgram) -> tuple[Cell, ...]:
    if value is None:
        if program.nullable:
            return tuple(None for _ in program.codec.fields)
        raise _data_error(
            "null_not_allowed", "Non-nullable scalar has a null value", path=program.path,
            remedy="Supply a value or declare the member nullable",
        )
    return program.codec.encode(value)


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
            if len(plan.parent_tables) > 1:
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
                for field in current.codec.fields:
                    row[field.name] = None
            elif isinstance(current, RecordProgram):
                pending.extend(child for _, child in reversed(current.members))
            elif isinstance(current, TupleProgram):
                pending.extend(reversed(current.items))
            elif isinstance(current, VariantProgram):
                if current.repeated and current.layout == "split":
                    continue
                row[current.discriminator_column] = None
                if current.payload_null_column is not None:
                    row[current.payload_null_column] = False
                pending.extend(branch.program for branch in reversed(current.alternatives))
            elif isinstance(current, NullProgram):
                continue
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
            if len(member_plan.parent_tables) > 1:
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
        if isinstance(program, VariantProgram):
            if program.repeated and program.layout == "split":
                if native_value is None:
                    if program.nullable:
                        continue
                    raise _data_error(
                        "null_not_allowed",
                        "Non-nullable variant collection has a null value",
                        path=program.path,
                        remedy="Supply a collection or declare the member nullable",
                    )
                if program.map_values:
                    if not isinstance(native_value, dict):
                        raise _data_error(
                            "native_shape_mismatch",
                            "Expected a dictionary",
                            path=program.path,
                            remedy="Supply a dictionary",
                        )
                    entries: list[tuple[str | None, object]] = []
                    for map_key, item in native_value.items():
                        if type(map_key) is not str:
                            raise _data_error(
                                "native_type_mismatch",
                                "Map keys must be strings",
                                path=program.path,
                                remedy="Supply string map keys",
                            )
                        entries.append((map_key, item))
                else:
                    container_type = tuple if program.container == "tuple" else list
                    if not isinstance(native_value, container_type):
                        raise _data_error(
                            "native_shape_mismatch",
                            f"Expected a {program.container}",
                            path=program.path,
                            remedy=f"Supply a {program.container}",
                        )
                    entries = [(None, item) for item in native_value]
                begin(native_value, program.path)
                stack.append(("exit", program_id, native_value, row, row_key, row_table, -1, None))
                jobs: list[tuple[int, object, dict[str, Cell], str, str]] = []
                branch_by_key = {branch.key: branch for branch in program.alternatives}
                for item_position, (map_key, item) in enumerate(entries):
                    if item is None:
                        if not program.nullable_member or program.null_table is None:
                            raise _data_error(
                                "null_not_allowed",
                                "Null variant collection member is not declared",
                                path=program.path,
                                remedy="Declare a nullable alternative member",
                            )
                        table = program.null_table
                        child_key = new_key()
                        child_row = empty_row(table, child_key, row_key)
                        plan = prepared.tables[table]
                        if len(plan.parent_tables) > 1:
                            child_row[prepared.parent_table_column] = row_table
                        child_row[prepared.position_column] = item_position
                        if program.map_values:
                            child_row[prepared.map_key_column] = map_key
                        child_row[prepared.null_column] = True
                        child_row[program.discriminator_column] = None
                        tables[table].append(child_row)
                        continue
                    alternative_key, payload = program.adapter.select(item)
                    branch = branch_by_key.get(alternative_key)
                    if branch is None or branch.table is None:
                        raise _data_error(
                            "unknown_variant",
                            f"Adapter selected undeclared alternative {alternative_key!r}",
                            path=program.path,
                            remedy="Supply a value matching one prepared alternative",
                        )
                    if payload is None and not branch.nullable_payload:
                        raise _data_error(
                            "invalid_variant_value",
                            f"Alternative {alternative_key!r} does not allow a null payload",
                            path=program.path,
                            remedy="Supply a non-null payload or declare the alternative nullable",
                        )
                    table = branch.table
                    child_key = new_key()
                    child_row = empty_row(table, child_key, row_key)
                    plan = prepared.tables[table]
                    if len(plan.parent_tables) > 1:
                        child_row[prepared.parent_table_column] = row_table
                    child_row[prepared.position_column] = item_position
                    if program.map_values:
                        child_row[prepared.map_key_column] = map_key
                    child_row[program.discriminator_column] = branch.tag
                    if program.payload_null_column is not None:
                        child_row[program.payload_null_column] = payload is None
                    tables[table].append(child_row)
                    if payload is not None:
                        jobs.append((branch.program, payload, child_row, child_key, table))
                for branch_program, payload, child_row, child_key, table in reversed(jobs):
                    stack.append(("value", branch_program, payload, child_row, child_key, table, -1, None))
                continue
            if native_value is None:
                if program.nullable:
                    null_inline(program_id, row)
                    continue
                raise _data_error(
                    "null_not_allowed",
                    "Non-nullable variant has a null value",
                    path=program.path,
                    remedy="Supply a declared alternative or declare the variant nullable",
                )
            alternative_key, payload = program.adapter.select(native_value)
            branch = next((item for item in program.alternatives if item.key == alternative_key), None)
            if branch is None or branch.table is not None:
                raise _data_error(
                    "unknown_variant",
                    f"Adapter selected undeclared alternative {alternative_key!r}",
                    path=program.path,
                    remedy="Supply a value matching one prepared alternative",
                )
            if payload is None and not branch.nullable_payload:
                raise _data_error(
                    "invalid_variant_value",
                    f"Alternative {alternative_key!r} does not allow a null payload",
                    path=program.path,
                    remedy="Supply a non-null payload or declare the alternative nullable",
                )
            row[program.discriminator_column] = branch.tag
            if program.payload_null_column is not None:
                row[program.payload_null_column] = payload is None
            if payload is not None:
                stack.append(("value", branch.program, payload, row, row_key, row_table, -1, None))
            continue
        if isinstance(program, NullProgram):
            continue
        if isinstance(program, ScalarProgram):
            values = _native_scalar(native_value, program)
            row.update((field.name, cell) for field, cell in zip(program.codec.fields, values, strict=True))
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
                children.append((child_id, prepared.record_adapters[program.definition].read(native_value, member, path=child.path)))
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
            if isinstance(program, SequenceProgram):
                container_type = tuple if program.container == "tuple" else list
                if not isinstance(native_value, container_type):
                    raise _data_error(
                        "native_shape_mismatch", f"Expected a {program.container}", path=program.path,
                        remedy=f"Supply a {program.container}",
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
            if len(child_plan.parent_tables) > 1:
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
