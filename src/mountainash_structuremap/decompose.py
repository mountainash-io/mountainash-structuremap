from __future__ import annotations

from typing import TypeVar, cast

from mountainash_structuremap.codecs import Cell, EncodingCodec
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.prepared import (
    LinkProgram,
    MapProgram,
    NullProgram,
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
    TablePlan,
    TupleProgram,
    VariantProgram,
)

T = TypeVar("T")

_Task = tuple[str, int, object, dict[str, Cell], str, str, int, object | None]


def _same_cell(left: Cell, right: Cell) -> bool:
    return type(left) is type(right) and left == right


def _key_identity(cells: tuple[Cell, ...]) -> tuple[tuple[type, Cell], ...]:
    return tuple((type(cell), cell) for cell in cells)


def _data_error(code: str, message: str, *, path: str, remedy: str) -> StructureMapError:
    return StructureMapError(code, message, path=path, owner="executor restriction", remedy=remedy)



def _encode_map_key(value: object, plan: TablePlan, path: str) -> str:
    if type(value) is not str:
        raise _data_error(
            "native_type_mismatch",
            "Map keys must be strings",
            path=path,
            remedy="Supply string map keys",
        )
    codec = plan.map_key_codec
    if codec is None:
        raise _data_error(
            "invalid_prepared_mapping",
            "Map table has no prepared map-key codec",
            path=path,
            remedy="Prepare the map key through its table component",
        )
    cells = cast(EncodingCodec, codec).encode(value)
    if len(cells) != 1 or type(cells[0]) is not str:
        raise _data_error(
            "invalid_map_key",
            "Map key codec must produce one STRING cell",
            path=path,
            remedy="Prepare a map key codec with one STRING field",
        )
    return cells[0]

def _native_scalar(value: object, program: ScalarProgram) -> tuple[Cell, ...]:
    codec = cast(EncodingCodec, program.codec)
    if value is None:
        if program.nullable:
            return tuple(None for _ in codec.fields)
        raise _data_error(
            "null_not_allowed", "Non-nullable scalar has a null value", path=program.path,
            remedy="Supply a value or declare the member nullable",
        )
    return codec.encode(value)


def decompose(value: T, prepared: PreparedMapping[T]) -> dict[str, list[dict[str, Cell]]]:
    if "decompose" not in prepared.directions:
        raise StructureMapError(
            "unavailable_direction",
            "Prepared mapping does not support decomposition",
            remedy="Prepare with the decompose direction",
        )
    tables: dict[str, list[dict[str, Cell]]] = {name: [] for name in prepared.tables}
    created_rows: list[tuple[str, dict[str, Cell]]] = []
    parent_rows: dict[int, tuple[dict[str, Cell], str]] = {}
    pending_metadata: dict[int, dict[str, Cell]] = {}
    written_columns: dict[int, set[str]] = {}
    row_handles: dict[int, str] = {}
    row_tables: dict[int, str] = {}
    counter = 0
    ancestry: set[int] = set()

    def new_key() -> str:
        nonlocal counter
        key = f"r{counter}"
        counter += 1
        return key

    def empty_row(table_name: str, handle: str) -> dict[str, Cell]:
        plan = prepared.tables[table_name]
        result: dict[str, Cell] = {column: None for column in plan.columns}
        tables[table_name].append(result)
        created_rows.append((table_name, result))
        row_tables[id(result)] = table_name
        row_handles[id(result)] = handle
        pending_metadata[id(result)] = {}
        written_columns[id(result)] = set()
        return result

    def attach(row: dict[str, Cell], parent: dict[str, Cell], parent_table: str) -> None:
        parent_rows[id(row)] = (parent, parent_table)

    def write_cell(row: dict[str, Cell], column: str, value: Cell) -> None:
        row[column] = value
        written_columns[id(row)].add(column)

    def queue_metadata(row: dict[str, Cell], column: str | None, value: Cell) -> None:
        if column is None:
            return
        queued = pending_metadata[id(row)]
        if column in queued and not _same_cell(queued[column], value):
            raise _data_error(
                "invalid_prepared_mapping",
                f"Conflicting metadata values for column {column!r}",
                path=prepared.tables[row_tables[id(row)]].source_path,
                remedy="Prepare distinct metadata columns for each row role",
            )
        queued[column] = value

    def record_type(instance: object, program: RecordProgram) -> None:
        expected = prepared.native_types.get(program.definition)
        if expected is None or type(instance) is not expected:
            name = expected.__qualname__ if expected is not None else program.definition
            raise _data_error(
                "native_type_mismatch",
                f"Expected record {name}, got {type(instance).__qualname__}",
                path=program.path,
                remedy="Supply an instance of the declared native record",
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
                    write_cell(row, field.name, None)
            elif isinstance(current, RecordProgram):
                pending.extend(child for _, child in reversed(current.members))
            elif isinstance(current, TupleProgram):
                pending.extend(reversed(current.items))
            elif isinstance(current, VariantProgram):
                if current.repeated and current.layout == "split":
                    continue
                queue_metadata(row, current.discriminator_column, None)
                if current.payload_null_column is not None:
                    queue_metadata(row, current.payload_null_column, False)
                pending.extend(branch.program for branch in reversed(current.alternatives))
            elif isinstance(current, NullProgram):
                continue

    root_key = new_key()
    root_row = empty_row(prepared.root_table, root_key)
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
            child_row = empty_row(edge.table, child_key)
            attach(child_row, row, row_table)
            layout = member_plan.layout
            if layout.parent_table is not None:
                queue_metadata(child_row, layout.parent_table, row_table)
            if layout.position is not None:
                queue_metadata(child_row, layout.position, position)
            if isinstance(edge, MapProgram):
                queue_metadata(child_row, layout.map_key, _encode_map_key(map_key, member_plan, edge.path))
            if member_plan._nullable_member:
                queue_metadata(child_row, layout.null_column, native_value is None)
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
                    entries: list[tuple[object | None, object]] = [
                        (map_key, item) for map_key, item in native_value.items()
                    ]
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
                        child_row = empty_row(table, child_key)
                        attach(child_row, row, row_table)
                        plan = prepared.tables[table]
                        layout = plan.layout
                        if layout.parent_table is not None:
                            queue_metadata(child_row, layout.parent_table, row_table)
                        queue_metadata(child_row, layout.position, item_position)
                        if program.map_values:
                            queue_metadata(child_row, layout.map_key, _encode_map_key(map_key, plan, program.path))
                        queue_metadata(child_row, layout.null_column, True)
                        queue_metadata(child_row, program.discriminator_column, None)
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
                    child_row = empty_row(table, child_key)
                    attach(child_row, row, row_table)
                    plan = prepared.tables[table]
                    layout = plan.layout
                    if layout.parent_table is not None:
                        queue_metadata(child_row, layout.parent_table, row_table)
                    queue_metadata(child_row, layout.position, item_position)
                    if program.map_values:
                        queue_metadata(child_row, layout.map_key, _encode_map_key(map_key, plan, program.path))
                    queue_metadata(child_row, program.discriminator_column, branch.tag)
                    if program.payload_null_column is not None:
                        queue_metadata(child_row, program.payload_null_column, payload is None)
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
            queue_metadata(row, program.discriminator_column, branch.tag)
            if program.payload_null_column is not None:
                queue_metadata(row, program.payload_null_column, payload is None)
            if payload is not None:
                stack.append(("value", branch.program, payload, row, row_key, row_table, -1, None))
            continue
        if isinstance(program, NullProgram):
            continue
        if isinstance(program, ScalarProgram):
            values = _native_scalar(native_value, program)
            codec = cast(EncodingCodec, program.codec)
            for field, cell in zip(codec.fields, values, strict=True):
                write_cell(row, field.name, cell)
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
            child_row = empty_row(program.table, child_key)
            attach(child_row, row, row_table)
            if child_plan.layout.parent_table is not None:
                queue_metadata(child_row, child_plan.layout.parent_table, row_table)
            stack.append(("value", child_plan._program, native_value, child_row, child_key, program.table, -1, None))
            continue
        raise _data_error(
            "invalid_prepared_mapping",
            f"Unsupported prepared program {type(program).__name__}",
            path=program.path,
            remedy="Prepare a supported value shape",
        )
    row_keys: dict[int, tuple[Cell, ...]] = {}
    unique_keys: dict[str, set[tuple[tuple[type, Cell], ...]]] = {}
    for table_name, row in created_rows:
        plan = prepared.tables[table_name]
        layout = plan.layout
        row_id = id(row)
        metadata = pending_metadata[row_id]
        for column, expected in metadata.items():
            if column in written_columns[row_id] and not _same_cell(row[column], expected):
                raise _data_error(
                    "contradictory_metadata",
                    f"Payload value for {column!r} contradicts its row-layout metadata",
                    path=plan.source_path,
                    remedy="Use the same value as the declared row metadata or rename the payload column",
                )
            row[column] = expected

        parent_info = parent_rows.get(row_id)
        if parent_info is None:
            if layout.parent:
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Row layout declares parent fields without a parent occurrence",
                    path=plan.source_path,
                    remedy="Prepare parent fields only for linked row tables",
                )
        else:
            parent_row, parent_table = parent_info
            if parent_table not in plan.parent_tables:
                raise _data_error(
                    "invalid_prepared_mapping",
                    f"Parent table {parent_table!r} is not allowed for this row",
                    path=plan.source_path,
                    remedy="Prepare a table relationship to the enclosing row",
                )
            parent_key = row_keys.get(id(parent_row))
            if parent_key is None:
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Parent occurrence key was not finalized before its child",
                    path=plan.source_path,
                    remedy="Prepare a finite parent-before-child table graph",
                )
            if len(layout.parent) != len(parent_key):
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Parent layout fields do not match the parent key arity",
                    path=plan.source_path,
                    remedy="Declare one parent column for each parent key column",
                )
            if len(plan.parent_tables) > 1 and layout.parent_table is None:
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Multiple parent tables require a row-layout parent-table column",
                    path=plan.source_path,
                    remedy="Declare parent_table in the row layout",
                )
            for column, expected in zip(layout.parent, parent_key, strict=True):
                if column not in row:
                    raise _data_error(
                        "invalid_prepared_mapping",
                        f"Parent key column {column!r} is absent from the table",
                        path=plan.source_path,
                        remedy="Include each parent column in the table schema",
                    )
                if column in written_columns[row_id] or column in metadata:
                    if not _same_cell(row[column], expected):
                        raise _data_error(
                            "contradictory_parent_key",
                            f"Payload value for parent key column {column!r} contradicts its parent",
                            path=plan.source_path,
                            remedy="Use the parent key value or rename the payload column",
                        )
                else:
                    row[column] = expected

        if layout.generated:
            if len(layout.key) != 1:
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Generated occurrence keys require exactly one column",
                    path=plan.source_path,
                    remedy="Use one key column for a generated row layout",
                )
            key_cells: tuple[Cell, ...] = (row_handles[row_id],)
            column = layout.key[0]
            if column not in row:
                raise _data_error(
                    "invalid_prepared_mapping",
                    f"Generated key column {column!r} is absent from the table",
                    path=plan.source_path,
                    remedy="Include the generated key column in the table schema",
                )
            if column in written_columns[row_id] or column in metadata:
                if not _same_cell(row[column], key_cells[0]):
                    raise _data_error(
                        "contradictory_key",
                        f"Payload value for generated key column {column!r} contradicts the generated key",
                        path=plan.source_path,
                        remedy="Rename the payload column or use the generated key value",
                    )
            else:
                row[column] = key_cells[0]
        else:
            if any(column not in row for column in layout.key):
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Source key column is absent from the table",
                    path=plan.source_path,
                    remedy="Include all source key columns in the table schema",
                )
            key_cells = tuple(row[column] for column in layout.key)
            if any(cell is None for cell in key_cells):
                raise _data_error(
                    "invalid_key",
                    "Source occurrence key contains a null cell",
                    path=plan.source_path,
                    remedy="Supply non-null values for every source key column",
                )
        identity = _key_identity(key_cells)
        try:
            seen = unique_keys.setdefault(table_name, set())
            if identity in seen:
                raise _data_error(
                    "duplicate_key",
                    f"Duplicate occurrence key {key_cells!r}",
                    path=plan.source_path,
                    remedy="Supply unique key cells within each table",
                )
            seen.add(identity)
        except TypeError as exc:
            raise _data_error(
                "invalid_key",
                "Occurrence key cells must be hashable",
                path=plan.source_path,
                remedy="Use supported scalar key cells",
            ) from exc
        row_keys[row_id] = key_cells
    return tables
