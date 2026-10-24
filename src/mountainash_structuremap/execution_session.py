from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

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

_Task = tuple[str, int, object, dict[str, Cell], str, int, object | None]


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


class RowSink(Protocol):
    def accept(self, table: str, rows: Sequence[Mapping[str, Cell]]) -> None: ...


class RowCollector:
    """The row API's in-memory sink; every prepared table exists even when empty."""

    def __init__(self, tables: Mapping[str, object]):
        self.tables: dict[str, list[dict[str, Cell]]] = {name: [] for name in tables}

    def accept(self, table: str, rows: Sequence[Mapping[str, Cell]]) -> None:
        self.tables[table].extend(dict(row) for row in rows)


@dataclass(eq=False)
class ScopeHandle:
    """Internal live envelope context, never a native object or row identifier."""

    use: int
    row: dict[str, Cell]
    parent: ScopeHandle | None
    members: set[str] = field(default_factory=set)
    positions: dict[int, int] = field(default_factory=dict)


class DecompositionSession:
    """One document's traversal, projections, keys, and finalized-row delivery.

    Only live scopes retain row contexts between records. Source-key identity
    sets grow with document cardinality; generated keys need only a counter.
    """

    def __init__(self, prepared: PreparedMapping[Any], sink: RowSink) -> None:
        self.prepared = prepared
        self.sink = sink
        if "decompose" not in self.prepared.directions:
            raise StructureMapError(
                "unavailable_direction",
                "Prepared mapping does not support decomposition",
                remedy="Prepare with the decompose direction",
            )
        self.created_rows: list[tuple[str, str | None, dict[str, Cell]]] = []
        self.parent_rows: dict[int, tuple[dict[str, Cell], str, str | None]] = {}
        self.pending_metadata: dict[int, dict[str, Cell]] = {}
        self.written_columns: dict[int, set[str]] = {}
        self.row_handles: dict[int, str] = {}
        self.row_tables: dict[int, str] = {}
        self.row_roles: dict[int, str | None] = {}
        self.source_contexts: dict[int, dict[tuple[str, str | None], dict[str, Cell]]] = {}
        self.source_domains: dict[int, dict[str, tuple[Cell, ...]]] = {}
        self.projection_batches: dict[int, list[dict[str, Cell]]] = {}
        self.counter = 0
        self.ancestry: set[int] = set()
        self.row_keys: dict[int, tuple[Cell, ...]] = {}
        self.dirty_rows: set[int] = set()
        self.unique_keys: dict[tuple[str, str | None], set[tuple[tuple[type, Cell], ...]]] = {}
        self.scopes: set[ScopeHandle] = set()
        self.started = False
        self.finished = False

    def _active(self) -> None:
        if self.finished:
            raise _data_error(
                "invalid_session",
                "The document session is already finished",
                path="",
                remedy="Create a new session for another document",
            )

    def _scope(self, scope: ScopeHandle) -> RecordProgram:
        self._active()
        if scope not in self.scopes:
            raise _data_error(
                "invalid_scope",
                "Scope is closed or belongs to another session",
                path="",
                remedy="Use a live scope from this document session",
            )
        return cast(RecordProgram, self.prepared.programs[scope.use])

    def _task(
        self,
        operation: str,
        use: int,
        value: object,
        row: dict[str, Cell],
        position: int = -1,
        map_key: object | None = None,
    ) -> _Task:
        return (
            operation,
            use,
            value,
            row,
            self.row_tables[id(row)],
            position,
            map_key,
        )

    def consume(self, value: object) -> None:
        """Traverse a complete native document with the same walker as records."""
        self._active()
        if self.started:
            raise _data_error(
                "invalid_session",
                "A session consumes exactly one document",
                path="",
                remedy="Create a new session for another document",
            )
        self.started = True
        row = self.empty_row(self.prepared.root_table, self.new_key())
        self._walk([self._task("value", self.prepared.root, value, row)])
        self._flush()

    def open_scope(
        self,
        use: int,
        parent: ScopeHandle | None,
        position: int | None,
        map_key: object | None,
    ) -> ScopeHandle:
        """Open a root, inline record, or linked/collection record envelope.

        ``use`` is a prepared program ID. For a collection envelope it names
        the collection edge; its role selects the record program.
        """
        self._active()
        program = self.prepared.programs[use]
        if parent is None:
            if self.started or use != self.prepared.root:
                raise _data_error(
                    "invalid_scope",
                    "A root scope must start the prepared document",
                    path=program.path,
                    remedy="Open the prepared root once with no parent",
                )
            if not isinstance(program, RecordProgram):
                raise _data_error(
                    "unavailable_scope",
                    "An envelope scope requires a record",
                    path=program.path,
                    remedy="Consume this complete native value instead",
                )
            self.started = True
            row = self.empty_row(self.prepared.root_table, self.new_key())
        else:
            parent_program = self._scope(parent)
            name = next((name for name, child in parent_program.members if child == use), None)
            if name is None:
                raise _data_error(
                    "invalid_scope",
                    "Envelope is not a member of its parent scope",
                    path=program.path,
                    remedy="Follow the prepared record ancestry",
                )
            if isinstance(program, (SequenceProgram, MapProgram)):
                self._position(parent, use, position)
                row, use = self._collection_row(program, parent.row, position, map_key, False)
                program = self.prepared.programs[use]
            elif isinstance(program, LinkProgram):
                if name in parent.members:
                    self._duplicate_member(program.path)
                plan = self.prepared.tables[program.table]
                row = self.empty_row(program.table, self.new_key(), program.role, parent.row)
                self.attach(row, parent.row, self.row_tables[id(parent.row)])
                self.queue_metadata(row, plan.layout.parent_table, self.row_tables[id(parent.row)])
                self.queue_metadata(row, plan.layout.role_column, program.role)
                use = plan.roles[program.role].program
                program = self.prepared.programs[use]
            else:
                if name in parent.members:
                    self._duplicate_member(program.path)
                row = parent.row
            if not isinstance(program, RecordProgram):
                raise _data_error(
                    "unavailable_scope",
                    "An envelope scope requires a record",
                    path=program.path,
                    remedy="Schedule a complete member or collection occurrence instead",
                )
            parent.members.add(name)
        scope = ScopeHandle(use, row, parent)
        self.scopes.add(scope)
        return scope

    def _duplicate_member(self, path: str) -> None:
        raise _data_error(
            "duplicate_member",
            "A complete envelope member was already supplied",
            path=path,
            remedy="Supply each member once; use occurrence for incremental collections",
        )

    def member(self, scope: ScopeHandle, member: str, value: object) -> None:
        program = self._scope(scope)
        use = dict(program.members).get(member)
        if use is None:
            raise _data_error(
                "invalid_member",
                f"Unknown envelope member {member!r}",
                path=program.path,
                remedy="Supply a member declared by the prepared record",
            )
        if member in scope.members:
            self._duplicate_member(self.prepared.programs[use].path)
        scope.members.add(member)
        self._walk([self._task("value", use, value, scope.row)])
        self._flush()

    def _position(self, scope: ScopeHandle, collection: int, position: int | None) -> None:
        expected = scope.positions.get(collection, 0)
        if type(position) is not int or position != expected:
            raise _data_error(
                "invalid_position",
                f"Expected collection occurrence position {expected}, got {position!r}",
                path=self.prepared.programs[collection].path,
                remedy="Schedule each collection occurrence in order starting at zero",
            )
        scope.positions[collection] = expected + 1

    def occurrence(
        self,
        scope: ScopeHandle,
        collection: int,
        value: object,
        position: int,
        map_key: object | None = None,
    ) -> None:
        program = self._scope(scope)
        name = next((name for name, child in program.members if child == collection), None)
        edge = self.prepared.programs[collection]
        if name is None or not (
            isinstance(edge, (SequenceProgram, MapProgram))
            or isinstance(edge, VariantProgram)
            and edge.repeated
            and edge.layout == "split"
        ):
            raise _data_error(
                "invalid_collection",
                "Occurrence must name a collection in this scope",
                path=edge.path,
                remedy="Use the collection member's prepared program ID",
            )
        if name in scope.members and collection not in scope.positions:
            self._duplicate_member(edge.path)
        self._position(scope, collection, position)
        scope.members.add(name)
        operation = "variant_member" if isinstance(edge, VariantProgram) else "member"
        self._walk([self._task(operation, collection, value, scope.row, position, map_key)])
        self._flush()

    def close_scope(self, scope: ScopeHandle) -> None:
        program = self._scope(scope)
        if any(item.parent is scope for item in self.scopes):
            raise _data_error(
                "invalid_scope",
                "An envelope still has open child scopes",
                path=program.path,
                remedy="Close nested scopes before their parent",
            )
        adapter = self.prepared.record_adapters[program.definition]
        for name, use in program.members:
            if name in scope.members:
                continue
            child = self.prepared.programs[use]
            omitted = getattr(adapter, "omitted", None)
            supplied, value = omitted(name, path=child.path) if omitted is not None else (False, None)
            if not supplied:
                if isinstance(child, MapProgram) or isinstance(child, VariantProgram) and child.map_values:
                    value = {}
                elif isinstance(child, SequenceProgram) or isinstance(child, VariantProgram) and child.repeated:
                    value = () if child.container == "tuple" else []
                elif getattr(child, "nullable", False):
                    value = None
                else:
                    raise _data_error(
                        "missing_member",
                        f"Required envelope member {name!r} was not supplied",
                        path=child.path,
                        remedy="Supply the member or a native binding default before closing the scope",
                    )
            self.member(scope, name, value)
        self.scopes.remove(scope)
        self._flush()

    def finish(self) -> None:
        self._active()
        if not self.started or self.scopes:
            raise _data_error(
                "invalid_session",
                "The document is absent or has open envelope scopes",
                path="",
                remedy="Consume a document or close every envelope scope before finishing",
            )
        self._flush()
        self.finished = True
        self.unique_keys.clear()

    def _ready_row(self, row: dict[str, Cell]) -> None:
        """Resolve parent keys independently of whether their rows can be emitted."""
        pending = []
        current = row
        while True:
            if id(current) in self.row_keys and id(current) not in self.dirty_rows:
                break
            pending.append(current)
            parent = self.parent_rows.get(id(current))
            if parent is None:
                break
            current = parent[0]
        for current in reversed(pending):
            identity = id(current)
            self._finalize_row(self.row_tables[identity], self.row_roles[identity], current)
            self.dirty_rows.discard(identity)

    def _flush(self) -> None:
        self._project()
        retained = {id(scope.row) for scope in self.scopes}
        batches: dict[str, list[dict[str, Cell]]] = {}
        for table, role, row in self.created_rows:
            if id(row) not in retained:
                self._ready_row(row)
                batches.setdefault(table, []).append(row)
        # Finalize the entire record before crossing the sink boundary.
        for table, rows in batches.items():
            self.sink.accept(table, rows)
        live = []
        for item in self.created_rows:
            identity = id(item[2])
            if identity in retained:
                live.append(item)
                continue
            self.dirty_rows.discard(identity)
            for contexts in (
                self.parent_rows,
                self.pending_metadata,
                self.written_columns,
                self.row_handles,
                self.row_tables,
                self.row_roles,
                self.source_contexts,
                self.source_domains,
                self.row_keys,
            ):
                contexts.pop(identity, None)
        self.created_rows = live

    def _collection_row(
        self,
        edge: SequenceProgram | MapProgram,
        parent: dict[str, Cell],
        position: int | None,
        map_key: object | None,
        is_null: bool,
    ) -> tuple[dict[str, Cell], int]:
        plan = self.prepared.tables[edge.table]
        role = plan.roles.get(edge.role)
        if role is None:
            raise _data_error(
                "invalid_role",
                f"Unknown relationship role {edge.role!r} for table {edge.table!r}",
                path=edge.path,
                remedy=f"Use one of the declared roles {tuple(plan.roles)!r}",
            )
        row = self.empty_row(edge.table, self.new_key(), edge.role, parent)
        parent_table = self.row_tables[id(parent)]
        self.attach(row, parent, parent_table)
        layout = plan.layout
        self.queue_metadata(row, layout.parent_table, parent_table)
        self.queue_metadata(row, layout.role_column, edge.role)
        if role.ordered:
            self.queue_metadata(row, layout.position, position)
        if isinstance(edge, MapProgram):
            self.queue_metadata(row, layout.map_key, _encode_map_key(map_key, role, edge.path))
        if role.nullable_member:
            self.queue_metadata(row, layout.null_column, is_null)
        return row, role.program

    def new_key(self) -> str:
        key = f"r{self.counter}"
        self.counter += 1
        return key

    def empty_row(
        self, table_name: str, handle: str, role: str | None = None, parent: dict[str, Cell] | None = None
    ) -> dict[str, Cell]:
        plan = self.prepared.tables[table_name]
        if role not in plan.roles:
            raise _data_error(
                "invalid_role",
                f"Unknown relationship role {role!r} for table {table_name!r}",
                path=plan.source_path,
                remedy=f"Use one of the declared roles {tuple(plan.roles)!r}",
            )
        result: dict[str, Cell] = {column: None for column in plan.columns}
        self.created_rows.append((table_name, role, result))
        self.dirty_rows.add(id(result))
        self.row_tables[id(result)] = table_name
        self.row_roles[id(result)] = role
        self.row_handles[id(result)] = handle
        self.pending_metadata[id(result)] = {}
        self.source_domains[id(result)] = {}
        self.written_columns[id(result)] = set()
        sources = dict(self.source_contexts[id(parent)]) if parent is not None else {}
        sources[table_name, role] = result
        self.source_contexts[id(result)] = sources
        return result

    def attach(self, row: dict[str, Cell], parent: dict[str, Cell], parent_table: str) -> None:
        self.parent_rows[id(row)] = (parent, parent_table, self.row_roles[id(parent)])

    def expect_source_domain(self, row: dict[str, Cell], branch: VariantBranch) -> None:
        if branch.source_column is None or not branch.tags:
            role_plan = self.prepared.tables[self.row_tables[id(row)]].roles[self.row_roles[id(row)]]
            raise _data_error(
                "invalid_prepared_mapping",
                "Source-discriminated alternative has no bound source column or tags",
                path=role_plan.source_path,
                remedy="Bind each source discriminator to its scalar source field and finite tag domain",
            )
        self.source_domains[id(row)][branch.source_column] = branch.tags

    def write_cell(self, row: dict[str, Cell], column: str, value: Cell) -> None:
        tags = self.source_domains[id(row)].get(column)
        if tags is not None and (not any((_same_cell(value, tag) for tag in tags))):
            role_plan = self.prepared.tables[self.row_tables[id(row)]].roles[self.row_roles[id(row)]]
            raise _data_error(
                "invalid_discriminator",
                f"Source discriminator value {value!r} is not declared for the selected alternative",
                path=role_plan.source_path,
                remedy="Supply a source field value from the selected alternative's declared Literal domain",
            )
        row[column] = value
        self.written_columns[id(row)].add(column)
        self.dirty_rows.add(id(row))

    def queue_metadata(self, row: dict[str, Cell], column: str | None, value: Cell) -> None:
        if column is None:
            return
        queued = self.pending_metadata[id(row)]
        if column in queued and (not _same_cell(queued[column], value)):
            raise _data_error(
                "invalid_prepared_mapping",
                f"Conflicting metadata values for column {column!r}",
                path=self.prepared.tables[self.row_tables[id(row)]].source_path,
                remedy="Prepare distinct metadata columns for each row role",
            )
        queued[column] = value
        self.dirty_rows.add(id(row))

    def record_type(self, instance: object, program: RecordProgram) -> None:
        expected = self.prepared.native_types.get(program.definition)
        if expected is None or type(instance) is not expected:
            name = expected.__qualname__ if expected is not None else program.definition
            raise _data_error(
                "native_type_mismatch",
                f"Expected record {name}, got {type(instance).__qualname__}",
                path=program.path,
                remedy="Supply an instance of the declared native record",
            )

    def begin(self, instance: object, path: str) -> None:
        identity = id(instance)
        if identity in self.ancestry:
            raise _data_error(
                "containment_cycle",
                "Native containment graph contains a cycle",
                path=path,
                remedy="Remove the containment cycle; Foundation provides no reference strategy",
            )
        self.ancestry.add(identity)

    def null_inline(self, program_id: int, row: dict[str, Cell]) -> None:
        pending = [program_id]
        while pending:
            current = self.prepared.programs[pending.pop()]
            if isinstance(current, ScalarProgram):
                for name in current.codec.fields:
                    self.write_cell(row, name, None)
            elif isinstance(current, RecordProgram):
                pending.extend((child for _, child in reversed(current.members)))
            elif isinstance(current, TupleProgram):
                pending.extend(reversed(current.items))
            elif isinstance(current, VariantProgram):
                if current.repeated and current.layout == "split":
                    continue
                self.queue_metadata(row, current.discriminator_column, None)
                if current.payload_null_column is not None:
                    self.queue_metadata(row, current.payload_null_column, False)
                pending.extend((branch.program for branch in reversed(current.alternatives)))
            elif isinstance(current, NullProgram):
                continue

    def _walk(self, stack: list[_Task]) -> None:
        while stack:
            operation, program_id, native_value, row, row_table, position, map_key = stack.pop()
            program = self.prepared.programs[program_id]
            if operation == "exit":
                self.ancestry.remove(id(native_value))
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
                child_row, child_use = self._collection_row(
                    edge,
                    row,
                    position,
                    map_key,
                    native_value is None,
                )
                member_role = self.prepared.tables[edge.table].roles[edge.role]
                if native_value is None and member_role.nullable_member:
                    continue
                stack.append(self._task("value", child_use, native_value, child_row))
                continue
            if isinstance(program, VariantProgram):
                if program.repeated and program.layout == "split":
                    if operation == "variant_member":
                        entries = [(map_key, native_value)]
                        start_position = position
                    else:
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
                            entries = [(map_key, item) for map_key, item in native_value.items()]
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
                        self.begin(native_value, program.path)
                        stack.append(("exit", program_id, native_value, row, row_table, -1, None))
                        start_position = 0
                    jobs: list[tuple[int, object, dict[str, Cell], str]] = []
                    branch_by_key = {branch.key: branch for branch in program.alternatives}
                    for item_position, (map_key, item) in enumerate(entries, start=start_position):
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
                            child_key = self.new_key()
                            child_row = self.empty_row(table, child_key, role, row)
                            self.attach(child_row, row, row_table)
                            plan = self.prepared.tables[table]
                            table_role = plan.roles[role]
                            layout = plan.layout
                            if layout.parent_table is not None:
                                self.queue_metadata(child_row, layout.parent_table, row_table)
                            if layout.role_column is not None:
                                self.queue_metadata(child_row, layout.role_column, role)
                            if layout.position is not None and table_role.ordered:
                                self.queue_metadata(child_row, layout.position, item_position)
                            if program.map_values:
                                self.queue_metadata(
                                    child_row, layout.map_key, _encode_map_key(map_key, table_role, program.path)
                                )
                            self.queue_metadata(child_row, layout.null_column, True)
                            if not program.source_discriminator:
                                self.queue_metadata(child_row, program.discriminator_column, None)
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
                        if payload is None and (not branch.nullable_payload):
                            raise _data_error(
                                "invalid_variant_value",
                                f"Alternative {alternative_key!r} does not allow a null payload",
                                path=program.path,
                                remedy="Supply a non-null payload or declare the alternative nullable",
                            )
                        table = branch.table
                        child_key = self.new_key()
                        child_row = self.empty_row(table, child_key, branch.role, row)
                        self.attach(child_row, row, row_table)
                        plan = self.prepared.tables[table]
                        table_role = plan.roles[branch.role]
                        layout = plan.layout
                        if layout.parent_table is not None:
                            self.queue_metadata(child_row, layout.parent_table, row_table)
                        if layout.role_column is not None:
                            self.queue_metadata(child_row, layout.role_column, branch.role)
                        if layout.position is not None and table_role.ordered:
                            self.queue_metadata(child_row, layout.position, item_position)
                        if program.map_values:
                            self.queue_metadata(
                                child_row, layout.map_key, _encode_map_key(map_key, table_role, program.path)
                            )
                        if program.source_discriminator:
                            self.expect_source_domain(child_row, branch)
                        else:
                            self.queue_metadata(child_row, program.discriminator_column, branch.tag)
                        if program.payload_null_column is not None:
                            self.queue_metadata(child_row, program.payload_null_column, payload is None)
                        if payload is not None:
                            jobs.append((table_role.program, payload, child_row, table))
                    for branch_program, payload, child_row, table in reversed(jobs):
                        stack.append(("value", branch_program, payload, child_row, table, -1, None))
                    continue
                if native_value is None:
                    if program.nullable:
                        self.null_inline(program_id, row)
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
                if payload is None and (not branch.nullable_payload):
                    raise _data_error(
                        "invalid_variant_value",
                        f"Alternative {alternative_key!r} does not allow a null payload",
                        path=program.path,
                        remedy="Supply a non-null payload or declare the alternative nullable",
                    )
                if program.source_discriminator:
                    self.expect_source_domain(row, branch)
                else:
                    self.queue_metadata(row, program.discriminator_column, branch.tag)
                if program.payload_null_column is not None:
                    self.queue_metadata(row, program.payload_null_column, payload is None)
                if payload is not None:
                    stack.append(("value", branch.program, payload, row, row_table, -1, None))
                continue
            if isinstance(program, NullProgram):
                continue
            if isinstance(program, ScalarProgram):
                values = _native_scalar(native_value, program)
                codec = cast(EncodingCodec, program.codec)
                for name, cell in zip(codec.fields, values, strict=True):
                    self.write_cell(row, name, cell)
                if program.projection is not None:
                    self.projection_batches.setdefault(program_id, []).append(row)
                continue
            if isinstance(program, RecordProgram):
                if native_value is None:
                    if program.nullable:
                        self.null_inline(program_id, row)
                        continue
                    raise _data_error(
                        "null_not_allowed",
                        "Required inline record has a null value",
                        path=program.path,
                        remedy="Supply a record instance",
                    )
                self.record_type(native_value, program)
                self.begin(native_value, program.path)
                stack.append(("exit", program_id, native_value, row, row_table, -1, None))
                children: list[tuple[int, object]] = []
                for member, child_id in program.members:
                    child = self.prepared.programs[child_id]
                    children.append(
                        (
                            child_id,
                            self.prepared.record_adapters[program.definition].read(
                                native_value, member, path=child.path
                            ),
                        )
                    )
                for child_id, child_value in reversed(children):
                    stack.append(("value", child_id, child_value, row, row_table, -1, None))
                continue
            if isinstance(program, TupleProgram):
                if native_value is None:
                    if program.nullable:
                        self.null_inline(program_id, row)
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
                self.begin(native_value, program.path)
                stack.append(("exit", program_id, native_value, row, row_table, -1, None))
                for item_id, item_value in reversed(tuple(zip(program.items, native_value, strict=True))):
                    stack.append(("value", item_id, item_value, row, row_table, -1, None))
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
                            "native_shape_mismatch",
                            f"Expected a {program.container}",
                            path=program.path,
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
                self.begin(native_value, program.path)
                stack.append(("exit", program_id, native_value, row, row_table, -1, None))
                for index in range(len(entries) - 1, -1, -1):
                    item_key, item = entries[index]
                    stack.append(("member", program_id, item, row, row_table, index, item_key))
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
                child_plan = self.prepared.tables[program.table]
                child_role = child_plan.roles.get(program.role)
                if child_role is None:
                    raise _data_error(
                        "invalid_role",
                        f"Unknown relationship role {program.role!r} for table {program.table!r}",
                        path=program.path,
                        remedy=f"Use one of the declared roles {tuple(child_plan.roles)!r}",
                    )
                child_key = self.new_key()
                child_row = self.empty_row(program.table, child_key, program.role, row)
                self.attach(child_row, row, row_table)
                if child_plan.layout.parent_table is not None:
                    self.queue_metadata(child_row, child_plan.layout.parent_table, row_table)
                stack.append(("value", child_role.program, native_value, child_row, program.table, -1, None))
                continue
            raise _data_error(
                "invalid_prepared_mapping",
                f"Unsupported prepared program {type(program).__name__}",
                path=program.path,
                remedy="Prepare a supported value shape",
            )

    def _project(self) -> None:
        for program_id, rows in self.projection_batches.items():
            projection = cast(ScalarProgram, self.prepared.programs[program_id]).projection
            assert projection is not None
            projected = projection.apply(rows)
            for row, cells in zip(rows, projected, strict=True):
                row.update(cells)
        self.projection_batches.clear()

    def _finalize_row(self, table_name: str, role_name: str | None, row: dict[str, Cell]) -> None:
        plan = self.prepared.tables[table_name]
        role_plan = plan.roles[role_name]
        layout = plan.layout
        row_id = id(row)
        metadata = self.pending_metadata[row_id]
        for column in self.source_domains[row_id]:
            if column not in self.written_columns[row_id]:
                raise _data_error(
                    "invalid_discriminator",
                    f"Selected alternative did not emit its source discriminator column {column!r}",
                    path=role_plan.source_path,
                    remedy="Include the declared discriminator scalar in each selected alternative payload",
                )
        context = self.source_contexts[row_id]
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
            if source.column in self.written_columns[row_id] and (not _same_cell(row[source.column], expected)):
                raise _data_error(
                    "contradictory_parent_key",
                    f"Payload value for source key column {source.column!r} contradicts its declared source",
                    path=role_plan.source_path,
                    remedy="Use the declared source value or rename the payload column",
                )
            if source.column in metadata and (not _same_cell(metadata[source.column], expected)):
                raise _data_error(
                    "contradictory_parent_key",
                    f"Carried key column {source.column!r} contradicts its declared source",
                    path=role_plan.source_path,
                    remedy="Use the declared source value or choose a different source key",
                )
            row[source.column] = expected
            self.written_columns[row_id].add(source.column)
        for column, expected in metadata.items():
            if column in self.written_columns[row_id] and (not _same_cell(row[column], expected)):
                code = "contradictory_parent_key" if column in layout.parent else "contradictory_metadata"
                raise _data_error(
                    code,
                    f"Payload value for {column!r} contradicts its row-layout metadata",
                    path=role_plan.source_path,
                    remedy="Use the same value as the declared row metadata or rename the payload column",
                )
            row[column] = expected
        parent_info = self.parent_rows.get(row_id)
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
            parent_key = self.row_keys.get(id(parent_row))
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
                if column in self.written_columns[row_id] or column in metadata:
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
            key_cells: tuple[Cell, ...] = (self.row_handles[row_id],)
            column = layout.key[0]
            if column not in row:
                raise _data_error(
                    "invalid_prepared_mapping",
                    f"Generated key column {column!r} is absent from the table",
                    path=role_plan.source_path,
                    remedy="Include the generated key column in the table schema",
                )
            if column in self.written_columns[row_id] or column in metadata:
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
            if any((column not in row for column in layout.key)):
                raise _data_error(
                    "invalid_prepared_mapping",
                    "Source key column is absent from the table",
                    path=role_plan.source_path,
                    remedy="Include all source key columns in the table schema",
                )
            key_cells = tuple((row[column] for column in layout.key))
            if not key_cells or any((cell is None for cell in key_cells)):
                raise _data_error(
                    "invalid_key",
                    "Source occurrence key contains a null cell",
                    path=role_plan.source_path,
                    remedy="Supply non-null values for every source key column",
                )
        previous = self.row_keys.get(row_id)
        if previous is not None:
            if _key_identity(previous) != _key_identity(key_cells):
                raise _data_error(
                    "contradictory_key",
                    "A ready ancestor key changed after descendant delivery",
                    path=role_plan.source_path,
                    remedy="Supply key-defining members before their descendants",
                )
        elif not layout.generated:
            identity = _key_identity(key_cells)
            try:
                seen = self.unique_keys.setdefault((table_name, role_name), set())
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
        self.row_keys[row_id] = key_cells
