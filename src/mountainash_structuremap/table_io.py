"""Fixed-schema table sinks and explicit typed input for execution.

PyArrow owns encoding; Files owns source acquisition and Parquet interpretation.
No logical table name is interpreted as a filesystem path here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from string import Formatter
from types import MappingProxyType, ModuleType
from typing import TYPE_CHECKING, Literal, cast

import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.parquet as pq
from mountainash.typespec.converters import to_arrow_schema

from mountainash_structuremap.codecs import Cell
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.prepared import PreparedMapping, TablePlan

if TYPE_CHECKING:
    from mountainash_files import FileSourceSpec


def _error(
    message: str, *, table: str | None = None, path: str = "", code: str = "table_io_error"
) -> StructureMapError:
    return StructureMapError(
        code,
        message,
        table=table,
        path=path,
        owner="table storage",
        remedy="Supply concrete readable sources with exactly the prepared schemas, or correct the output I/O failure",
    )


def _schemas(tables: Mapping[str, TablePlan]) -> dict[str, pa.Schema]:
    return {name: to_arrow_schema(table.typespec) for name, table in tables.items()}


class ArrowCollector:
    """Own typed batches for the explicitly in-memory execution operation."""

    def __init__(self, tables: Mapping[str, TablePlan]) -> None:
        self._schemas = _schemas(tables)
        self._tables: dict[str, list[pa.Table]] = {name: [] for name in tables}

    def accept(self, table: str, rows: Sequence[Mapping[str, Cell]]) -> None:
        if table not in self._schemas:
            raise _error("Sink received an unexpected logical table", table=table)
        if not rows:
            return
        try:
            batch = pa.Table.from_pylist(rows, schema=self._schemas[table])
        except Exception as exc:
            raise _error("Cannot encode finalized rows with the prepared schema", table=table) from exc
        self._tables[table].append(batch)

    def finish(self) -> dict[str, pa.Table]:
        return {
            name: pa.concat_tables(parts) if parts else pa.Table.from_batches([], schema=self._schemas[name])
            for name, parts in self._tables.items()
        }


class FileTableSink:
    """Synchronously write bounded row buffers to caller-supplied physical paths.

    ``row_counts`` counts successfully written rows, not pending rows. Call
    ``close()`` before publication. On an earlier failure, ``close(primary_error=)``
    discards pending rows and attaches close diagnostics to that original error.
    """

    def __init__(
        self,
        tables: Mapping[str, TablePlan],
        paths: Mapping[str, Path],
        *,
        format: Literal["parquet", "arrow"] = "parquet",
        buffer_rows: int = 10_000,
    ) -> None:
        if type(buffer_rows) is not int or buffer_rows < 1:
            raise _error("buffer_rows must be a positive integer", code="invalid_execution_options")
        if format not in ("parquet", "arrow"):
            raise _error("Table output format must be parquet or arrow", code="invalid_execution_options")
        if set(paths) != set(tables):
            raise _error("Output paths must bind exactly the prepared logical tables")
        self._paths = {name: Path(path) for name, path in paths.items()}
        if len(set(self._paths.values())) != len(self._paths):
            raise _error("Each logical table requires a distinct output path")
        self._schemas = _schemas(tables)
        self._buffers: dict[str, list[dict[str, Cell]]] = {name: [] for name in tables}
        self._counts = dict.fromkeys(tables, 0)
        self._writers: dict[str, pq.ParquetWriter | ipc.RecordBatchFileWriter] = {}
        self._streams: dict[str, pa.OSFile] = {}
        self._buffer_rows = buffer_rows
        self._closed = False
        for name, schema in self._schemas.items():
            try:
                if format == "parquet":
                    self._writers[name] = pq.ParquetWriter(self._paths[name], schema)
                else:
                    stream = pa.OSFile(str(self._paths[name]), "wb")
                    self._streams[name] = stream
                    self._writers[name] = ipc.new_file(stream, schema)
            except BaseException as exc:
                error = (
                    _error("Cannot open table writer", table=name, path=str(self._paths[name]))
                    if isinstance(exc, Exception)
                    else exc
                )
                self.close(primary_error=error)
                if error is exc:
                    raise
                raise error from exc

    @property
    def row_counts(self) -> Mapping[str, int]:
        return MappingProxyType(self._counts)

    def accept(self, table: str, rows: Sequence[Mapping[str, Cell]]) -> None:
        if self._closed:
            raise _error("Cannot accept rows after sink closure", table=table)
        if table not in self._buffers:
            raise _error("Sink received an unexpected logical table", table=table)
        buffer = self._buffers[table]
        for row in rows:
            buffer.append(dict(row))
            if len(buffer) == self._buffer_rows:
                self._flush(table)

    def _flush(self, table: str) -> None:
        buffer = self._buffers[table]
        if not buffer:
            return
        try:
            batch = pa.Table.from_pylist(buffer, schema=self._schemas[table])
            self._writers[table].write_table(batch)
        except Exception as exc:
            raise _error("Cannot write finalized rows", table=table, path=str(self._paths[table])) from exc
        self._counts[table] += len(buffer)
        buffer.clear()

    def close(self, *, primary_error: BaseException | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        failure = primary_error
        try:
            if failure is None:
                for name in self._buffers:
                    try:
                        self._flush(name)
                    except BaseException as exc:
                        failure = exc
                        break
        finally:
            for name, writer in self._writers.items():
                try:
                    writer.close()
                except BaseException as exc:
                    if failure is None:
                        failure = _error("Cannot close table writer", table=name, path=str(self._paths[name]))
                        failure.__cause__ = exc
                    else:
                        failure.add_note(f"Closing table {name!r} at {str(self._paths[name])!r} also failed: {exc}")
            for name, stream in self._streams.items():
                try:
                    stream.close()
                except BaseException as exc:
                    if failure is None:
                        failure = _error("Cannot close table stream", table=name, path=str(self._paths[name]))
                        failure.__cause__ = exc
                    else:
                        failure.add_note(f"Closing stream for table {name!r} also failed: {exc}")
            for buffer in self._buffers.values():
                buffer.clear()
        if primary_error is None and failure is not None:
            raise failure


def _check_schema(actual: pa.Schema, expected: pa.Schema, table: str, path: str = "") -> None:
    # Carrier nullability and unrelated Arrow metadata are not mapping identity.
    # DataType equality retains widths, decimal parameters, temporal units/timezones
    # and nested field structure; no cast is attempted to make a source fit.
    if actual.names != expected.names or actual.types != expected.types:
        raise _error(
            f"Physical schema does not match prepared schema: expected {expected}, got {actual}",
            table=table,
            path=path,
            code="table_schema_mismatch",
        )


def _check_source(source: FileSourceSpec, table: str) -> None:
    try:
        template = any(field is not None for _, field, _, _ in Formatter().parse(source.path))
    except ValueError:
        template = True
    if source.glob is not None or source.archive is not None or source.on_missing != "raise" or template:
        raise StructureMapError(
            "nonconcrete_source",
            "Execution requires one concrete source per logical table",
            table=table,
            path=source.path,
            owner="table storage",
            remedy="Resolve templates, globs and archive members first, and use on_missing='raise'",
        )


def _is_ipc(source: FileSourceSpec) -> bool:
    if source.format is not None:
        return isinstance(source.format, str) and source.format in {"arrow", "ipc", ".arrow", ".ipc"}
    return Path(source.path).suffix.lower() in {".arrow", ".ipc"}


def _read_ipc(source: FileSourceSpec, schema: pa.Schema, table: str, files: ModuleType) -> pa.Table:
    # A contextmanager's __exit__ can itself fail. Keep the read/schema failure
    # primary even when closing its owned transport stream raises another error.
    manager = files.stream(source, access="random")
    handle = manager.__enter__()
    failure: BaseException | None = None
    try:
        if not handle.seekable():
            raise _error("Arrow IPC input requires a seekable random-access stream", table=table, path=source.path)
        with ipc.open_file(handle) as reader:
            _check_schema(reader.schema, schema, table, source.path)
            result = reader.read_all()
            _check_schema(result.schema, schema, table, source.path)
    except BaseException as exc:
        failure = (
            exc
            if isinstance(exc, StructureMapError) or not isinstance(exc, Exception)
            else _error(
                "Cannot read Arrow IPC table",
                table=table,
                path=source.path,
            )
        )
        if failure is not exc:
            failure.__cause__ = exc
        raise failure
    finally:
        try:
            manager.__exit__(
                type(failure) if failure is not None else None, failure, failure.__traceback__ if failure else None
            )
        except BaseException as exc:
            if failure is not None:
                failure.add_note(f"Closing input stream at {source.path!r} also failed: {exc}")
            else:
                raise
    return result


def _read_parquet(source: FileSourceSpec, schema: pa.Schema, table: str, files: ModuleType) -> pa.Table:
    inspected = files.inspect_schema(source)
    if len(inspected) != 1:
        raise _error(
            f"Parquet schema inspection returned {len(inspected)} sources; expected exactly one",
            table=table,
            path=source.path,
        )
    observed = inspected[0]
    if observed.format_key != "parquet" or observed.basis != "metadata":
        raise _error(
            "Table input requires Parquet metadata schema inspection or Arrow IPC", table=table, path=source.path
        )
    _check_schema(observed.schema, schema, table, source.path)
    parsed = files.parse(source, output="arrow")
    if len(parsed) != 1:
        raise _error(
            f"Parquet parsing returned {len(parsed)} sources; expected exactly one", table=table, path=source.path
        )
    actual = parsed[0]
    if actual.format_key != "parquet" or not isinstance(actual.data, pa.Table):
        raise _error("Parquet reader did not return an Arrow table", table=table, path=source.path)
    _check_schema(actual.data.schema, schema, table, source.path)
    return actual.data


def read_input_tables[T](
    source: Mapping[str, pa.Table | FileSourceSpec],
    prepared: PreparedMapping[T],
) -> dict[str, list[dict[str, Cell]]]:
    """Read explicit bindings, checking schemas before constructing Python rows.

    Managed manifests are resolved by the execution layer. Files remains optional
    for in-memory Arrow inputs and is loaded only when a file binding is selected.
    Inspection and parsing use the same concrete spec; this is not a claim of
    source immutability between reads.
    """
    expected = set(prepared.tables)
    if set(source) != expected:
        raise _error(
            f"Logical table bindings differ: missing={sorted(expected - set(source))!r}, unexpected={sorted(set(source) - expected)!r}",
            code="table_bindings_mismatch",
        )
    files: ModuleType | None = None
    for name, binding in source.items():
        if isinstance(binding, pa.Table):
            continue
        if files is None:
            try:
                import mountainash_files

                files = mountainash_files
            except ImportError as exc:
                raise StructureMapError(
                    "unavailable_capability",
                    "File table input requires Mountainash Files",
                    table=name,
                    owner="table storage",
                    remedy="Install mountainash-structuremap[files] to read file table inputs",
                ) from exc
        if not isinstance(binding, files.FileSourceSpec):
            raise _error("Bind each logical table to an Arrow table or FileSourceSpec", table=name)
        _check_source(binding, name)
    schemas = _schemas(prepared.tables)
    result: dict[str, list[dict[str, Cell]]] = {}
    for name, binding in source.items():
        path = "" if isinstance(binding, pa.Table) else binding.path
        try:
            if isinstance(binding, pa.Table):
                table = binding
                _check_schema(table.schema, schemas[name], name)
            elif _is_ipc(binding):
                table = _read_ipc(binding, schemas[name], name, cast(ModuleType, files))
            else:
                table = _read_parquet(binding, schemas[name], name, cast(ModuleType, files))
            result[name] = table.to_pylist()
        except StructureMapError:
            raise
        except Exception as exc:
            raise _error("Cannot read typed input table", table=name, path=path) from exc
    return result
