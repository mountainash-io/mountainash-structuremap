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
    TableRole,
    TupleProgram,
    VariantBranch,
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



def _encode_map_key(value: object, role: TableRole, path: str) -> str:
    if type(value) is not str:
        raise _data_error(
            "native_type_mismatch",
            "Map keys must be strings",
            path=path,
            remedy="Supply string map keys",
        )
    codec = role.map_key_codec
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
            "null_not_allowed",
            "Non-nullable scalar has a null value",
            path=program.path,
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
    created_rows: list[tuple[str, str | None, dict[str, Cell]]] = []
    parent_rows: dict[int, tuple[dict[str, Cell], str, str | None]] = {}
    pending_metadata: dict[int, dict[str, Cell]] = {}
    written_columns: dict[int, set[str]] = {}
    row_handles: dict[int, str] = {}
    row_tables: dict[int, str] = {}
    row_roles: dict[int, str | None] = {}
    source_contexts: dict[int, dict[tuple[str, str | None], dict[str, Cell]]] = {}
    source_domains: dict[int, dict[str, tuple[Cell, ...]]] = {}
    projection_batches: dict[int, list[dict[str, Cell]]] = {}
    counter = 0
    ancestry: set[int] = set()

    def new_key() -> str:
        nonlocal counter
        key = f"r{counter}"
        counter += 1
        return key

    def empty_row(
        table_name: str,
        handle: str,
        role: str | None = None,
        parent: dict[str, Cell] | None = None,
    ) -> dict[str, Cell]:
        plan = prepared.tables[table_name]
        if role not in plan.roles:
            raise _data_error(
                "invalid_role",
                f"Unknown relationship role {role!r} for table {table_name!r}",
                path=plan.source_path,
                remedy=f"Use one of the declared roles {tuple(plan.roles)!r}",
            )
        result: dict[str, Cell] = {column: None for column in plan.columns}
        tables[table_name].append(result)
        created_rows.append((table_name, role, result))
        row_tables[id(result)] = table_name
        row_roles[id(result)] = role
        row_handles[id(result)] = handle
        pending_metadata[id(result)] = {}
        source_domains[id(result)] = {}
        written_columns[id(result)] = set()
        sources = dict(source_contexts[id(parent)]) if parent is not None else {}
        sources[(table_name, role)] = result
        source_contexts[id(result)] = sources
        return result

    def attach(row: dict[str, Cell], parent: dict[str, Cell], parent_table: str) -> None:
        parent_rows[id(row)] = (parent, parent_table, row_roles[id(parent)])

    def expect_source_domain(row: dict[str, Cell], branch: VariantBranch) -> None:
        if branch.source_column is None or not branch.tags:
            role_plan = prepared.tables[row_tables[id(row)]].roles[row_roles[id(row)]]
            raise _data_error(
                "invalid_prepared_mapping",
                "Source-discriminated alternative has no bound source column or tags",
                path=role_plan.source_path,
                remedy="Bind each source discriminator to its scalar source field and finite tag domain",
            )
        source_domains[id(row)][branch.source_column] = branch.tags

    def write_cell(row: dict[str, Cell], column: str, value: Cell) -> None:
        tags = source_domains[id(row)].get(column)
        if tags is not None and not any(_same_cell(value, tag) for tag in tags):
            role_plan = prepared.tables[row_tables[id(row)]].roles[row_roles[id(row)]]
            raise _data_error(
                "invalid_discriminator",
                f"Source discriminator value {value!r} is not declared for the selected alternative",
                path=role_plan.source_path,
                remedy="Supply a source field value from the selected alternative's declared Literal domain",
            )
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
            member_role = member_plan.roles.get(edge.role)
            if member_role is None:
                raise _data_error(
                    "invalid_role",
                    f"Unknown relationship role {edge.role!r} for table {edge.table!r}",
                    path=edge.path,
                    remedy=f"Use one of the declared roles {tuple(member_plan.roles)!r}",
                )
            child_key = new_key()
            child_row = empty_row(edge.table, child_key, edge.role, row)
            attach(child_row, row, row_table)
            layout = member_plan.layout
            if layout.parent_table is not None:
                queue_metadata(child_row, layout.parent_table, row_table)
            if layout.role_column is not None:
                queue_metadata(child_row, layout.role_column, edge.role)
            if layout.position is not None and member_role.ordered:
                queue_metadata(child_row, layout.position, position)
            if isinstance(edge, MapProgram):
                queue_metadata(
                    child_row,
                    layout.map_key,
                    _encode_map_key(map_key, member_role, edge.path),
                )
            if member_role.nullable_member:
                queue_metadata(child_row, layout.null_column, native_value is None)
            if native_value is None and member_role.nullable_member:
                continue
            stack.append(
                (
                    "value",
                    member_role.program,
                    native_value,
                    child_row,
                    child_key,
                    edge.table,
                    -1,
                    None,
                )
            )
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
                        role = program.null_role
                        child_key = new_key()
                        child_row = empty_row(table, child_key, role, row)
                        attach(child_row, row, row_table)
                        plan = prepared.tables[table]
                        table_role = plan.roles[role]
                        layout = plan.layout
                        if layout.parent_table is not None:
                            queue_metadata(child_row, layout.parent_table, row_table)
                        if layout.role_column is not None:
                            queue_metadata(child_row, layout.role_column, role)
                        if layout.position is not None and table_role.ordered:
                            queue_metadata(child_row, layout.position, item_position)
                        if program.map_values:
                            queue_metadata(
                                child_row,
                                layout.map_key,
                                _encode_map_key(map_key, table_role, program.path),
                            )
                        queue_metadata(child_row, layout.null_column, True)
                        if not program.source_discriminator:
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
                    child_row = empty_row(table, child_key, branch.role, row)
                    attach(child_row, row, row_table)
                    plan = prepared.tables[table]
                    table_role = plan.roles[branch.role]
                    layout = plan.layout
                    if layout.parent_table is not None:
                        queue_metadata(child_row, layout.parent_table, row_table)
                    if layout.role_column is not None:
                        queue_metadata(child_row, layout.role_column, branch.role)
                    if layout.position is not None and table_role.ordered:
                        queue_metadata(child_row, layout.position, item_position)
                    if program.map_values:
                        queue_metadata(
                            child_row,
                            layout.map_key,
                            _encode_map_key(map_key, table_role, program.path),
                        )
                    if program.source_discriminator:
                        expect_source_domain(child_row, branch)
                    else:
                        queue_metadata(child_row, program.discriminator_column, branch.tag)
                    if program.payload_null_column is not None:
                        queue_metadata(child_row, program.payload_null_column, payload is None)
                    if payload is not None:
                        jobs.append((table_role.program, payload, child_row, child_key, table))
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
            if program.source_discriminator:
                expect_source_domain(row, branch)
            else:
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
            if program.projection is not None:
                projection_batches.setdefault(program_id, []).append(row)
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
            child_role = child_plan.roles.get(program.role)
            if child_role is None:
                raise _data_error(
                    "invalid_role",
                    f"Unknown relationship role {program.role!r} for table {program.table!r}",
                    path=program.path,
                    remedy=f"Use one of the declared roles {tuple(child_plan.roles)!r}",
                )
            child_key = new_key()
            child_row = empty_row(program.table, child_key, program.role, row)
            attach(child_row, row, row_table)
            if child_plan.layout.parent_table is not None:
                queue_metadata(child_row, child_plan.layout.parent_table, row_table)
            stack.append(("value", child_role.program, native_value, child_row, child_key, program.table, -1, None))
            continue
        raise _data_error(
            "invalid_prepared_mapping",
            f"Unsupported prepared program {type(program).__name__}",
            path=program.path,
            remedy="Prepare a supported value shape",
        )
    # Batch each prepared scalar projection before source keys and parent carry
    # are finalized. Only its owned cells change; occurrence rows are not copied.
    for program_id, rows in projection_batches.items():
        projection = cast(ScalarProgram, prepared.programs[program_id]).projection
        assert projection is not None
        projected = projection.apply(rows)
        for row, cells in zip(rows, projected, strict=True):
            row.update(cells)
    row_keys: dict[int, tuple[Cell, ...]] = {}
    unique_keys: dict[tuple[str, str | None], set[tuple[tuple[type, Cell], ...]]] = {}
    for table_name, role_name, row in created_rows:
        plan = prepared.tables[table_name]
        role_plan = plan.roles[role_name]
        layout = plan.layout
        row_id = id(row)
        metadata = pending_metadata[row_id]
        for column in source_domains[row_id]:
            if column not in written_columns[row_id]:
                raise _data_error(
                    "invalid_discriminator",
                    f"Selected alternative did not emit its source discriminator column {column!r}",
                    path=role_plan.source_path,
                    remedy="Include the declared discriminator scalar in each selected alternative payload",
                )
        context = source_contexts[row_id]
        for source in role_plan.key_sources:
            source_row = context.get((source.source_table, source.source_role))
            if source_row is None:
                raise _data_error(
                    "invalid_prepared_mapping",
                    f"Source key origin {source.source_table!r}/{source.source_role!r} is not in this occurrence ancestry",
                    path=role_plan.source_path,
                    remedy="Declare an unambiguous source key from the current occurrence or an ancestor",
                )
            if source.source_column not in source_row:
                raise _data_error(
                    "invalid_prepared_mapping",
                    f"Source key origin column {source.source_column!r} is absent",
                    path=role_plan.source_path,
                    remedy="Map the source key to an encoded field in its declared origin table",
                )
            if source.column not in row:
                raise _data_error(
                    "invalid_prepared_mapping",
                    f"Source key column {source.column!r} is absent from the table",
                    path=role_plan.source_path,
                    remedy="Include every source key column in the table schema",
                )
            expected = source_row[source.source_column]
            if source.column in written_columns[row_id] and not _same_cell(
                row[source.column], expected
            ):
                raise _data_error(
                    "contradictory_parent_key",
                    f"Payload value for source key column {source.column!r} contradicts its declared source",
                    path=role_plan.source_path,
                    remedy="Use the declared source value or rename the payload column",
                )
            if source.column in metadata and not _same_cell(
                metadata[source.column], expected
            ):
                raise _data_error(
                    "contradictory_parent_key",
                    f"Carried key column {source.column!r} contradicts its declared source",
                    path=role_plan.source_path,
                    remedy="Use the declared source value or choose a different source key",
                )
            row[source.column] = expected
            written_columns[row_id].add(source.column)

        for column, expected in metadata.items():
            if column in written_columns[row_id] and not _same_cell(row[column], expected):
                code = "contradictory_parent_key" if column in layout.parent else "contradictory_metadata"
                raise _data_error(
                    code,
                    f"Payload value for {column!r} contradicts its row-layout metadata",
                    path=role_plan.source_path,
                    remedy="Use the same value as the declared row metadata or rename the payload column",
                )
            row[column] = expected

        parent_info = parent_rows.get(row_id)
        if parent_info is None:
            if layout.parent:
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Row layout declares parent fields without a parent occurrence",
                    path=role_plan.source_path,
                    remedy="Prepare parent fields only for linked row tables",
                )
        else:
            parent_row, parent_table, parent_role = parent_info
            if (parent_table, parent_role) not in role_plan.parent_roles:
                raise _data_error(
                    "invalid_prepared_mapping",
                    f"Parent role {parent_table!r}/{parent_role!r} is not allowed for this row",
                    path=role_plan.source_path,
                    remedy="Prepare a table relationship to the enclosing mapped-use role",
                )
            parent_key = row_keys.get(id(parent_row))
            if parent_key is None:
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Parent occurrence key was not finalized before its child",
                    path=role_plan.source_path,
                    remedy="Prepare a finite parent-before-child table graph",
                )
            if len(layout.parent) != len(parent_key):
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Parent layout fields do not match the parent key arity",
                    path=role_plan.source_path,
                    remedy="Declare one parent column for each parent key column",
                )
            parent_table_names = {table for table, _ in role_plan.parent_roles}
            if len(parent_table_names) > 1 and layout.parent_table is None:
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Multiple parent tables require a row-layout parent-table column",
                    path=role_plan.source_path,
                    remedy="Declare parent_table in the row layout",
                )
            for column, expected in zip(layout.parent, parent_key, strict=True):
                if column not in row:
                    raise _data_error(
                        "invalid_prepared_mapping",
                        f"Parent key column {column!r} is absent from the table",
                        path=role_plan.source_path,
                        remedy="Include each parent column in the table schema",
                    )
                if column in written_columns[row_id] or column in metadata:
                    if not _same_cell(row[column], expected):
                        raise _data_error(
                            "contradictory_parent_key",
                            f"Payload value for parent key column {column!r} contradicts its parent",
                            path=role_plan.source_path,
                            remedy="Use the parent key value or rename the payload column",
                        )
                else:
                    row[column] = expected
        if layout.generated:
            if len(layout.key) != 1:
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Generated occurrence keys require exactly one column",
                    path=role_plan.source_path,
                    remedy="Use one key column for a generated row layout",
                )
            key_cells: tuple[Cell, ...] = (row_handles[row_id],)
            column = layout.key[0]
            if column not in row:
                raise _data_error(
                    "invalid_prepared_mapping",
                    f"Generated key column {column!r} is absent from the table",
                    path=role_plan.source_path,
                    remedy="Include the generated key column in the table schema",
                )
            if column in written_columns[row_id] or column in metadata:
                if not _same_cell(row[column], key_cells[0]):
                    raise _data_error(
                        "contradictory_key",
                        f"Payload value for generated key column {column!r} contradicts the generated key",
                        path=role_plan.source_path,
                        remedy="Rename the payload column or use the generated key value",
                    )
            else:
                row[column] = key_cells[0]
        else:
            if any(column not in row for column in layout.key):
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Source key column is absent from the table",
                    path=role_plan.source_path,
                    remedy="Include all source key columns in the table schema",
                )
            key_cells = tuple(row[column] for column in layout.key)
            if not key_cells or any(cell is None for cell in key_cells):
                raise _data_error(
                    "invalid_key",
                    "Source occurrence key contains a null cell",
                    path=role_plan.source_path,
                    remedy="Supply non-null values for every source key column",
                )
        identity = _key_identity(key_cells)
        try:
            seen = unique_keys.setdefault((table_name, role_name), set())
            if identity in seen:
                raise _data_error(
                    "duplicate_key",
                    f"Duplicate occurrence key {key_cells!r}",
                    path=role_plan.source_path,
                    remedy="Supply unique key cells within each table role",
                )
            seen.add(identity)
        except TypeError as exc:
            raise _data_error(
                "invalid_key",
                "Occurrence key cells must be hashable",
                path=role_plan.source_path,
                remedy="Use supported scalar key cells",
            ) from exc
        row_keys[row_id] = key_cells
    return tables
