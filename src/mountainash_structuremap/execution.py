"""Typed table execution and local output publication for prepared mappings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import pyarrow as pa

from mountainash_structuremap.bindings.contracts import DocumentBinding
from mountainash_structuremap.documents import serialize
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.execution_session import DecompositionSession
from mountainash_structuremap.prepared import PreparedMapping
from mountainash_structuremap.publication import Publication, execution_error, output_path, table_sources
from mountainash_structuremap.reconstruct import reconstruct
from mountainash_structuremap.table_io import ArrowCollector, FileTableSink, read_input_tables

if TYPE_CHECKING:
    from mountainash_files import FileSourceSpec

T = TypeVar("T")
type TableInput = Mapping[str, pa.Table | FileSourceSpec] | Path


@dataclass(frozen=True)
class OutputTarget:
    root: Path
    name: str
    namespace: str | None = None
    completion: Literal["atomic", "keep_partial"] = "atomic"
    replace: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    path: Path
    kind: Literal["tables", "document"]
    row_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "row_counts", MappingProxyType(dict(self.row_counts)))


def _direction(prepared: PreparedMapping[Any], direction: Literal["decompose", "reconstruct"]) -> None:
    if direction not in prepared.directions:
        raise StructureMapError(
            "unavailable_direction",
            f"Prepared mapping does not support {direction}",
            remedy=f"Prepare the mapping with the {direction!r} direction",
        )


def _publication(output: OutputTarget, *, managed: bool) -> Publication:
    target = Path(output.root) / (output.namespace or "") / output.name
    try:
        target = output_path(output.root, output.name, output.namespace)
        return Publication(target, managed=managed, replace=output.replace, completion=output.completion)
    except OSError as exc:
        raise execution_error(exc, target) from exc


def decompose_arrow(value: T, prepared: PreparedMapping[T]) -> dict[str, pa.Table]:
    """Map one native value into tables carrying the complete prepared schemas."""
    _direction(prepared, "decompose")
    sink = ArrowCollector(prepared.tables)
    session = DecompositionSession(prepared, sink)
    session.consume(value)
    session.finish()
    return sink.finish()


def decompose_to(
    value: T,
    prepared: PreparedMapping[T],
    output: OutputTarget,
    *,
    format: Literal["parquet", "arrow"] = "parquet",
    buffer_rows: int = 10_000,
) -> ExecutionResult:
    """Map one native value and publish a complete typed local dataset."""
    _direction(prepared, "decompose")
    if format not in {"parquet", "arrow"} or type(buffer_rows) is not int or buffer_rows <= 0:
        raise StructureMapError(
            "invalid_output",
            "Invalid table format or buffer_rows",
            remedy="Select 'parquet' or 'arrow' and a positive integer buffer_rows",
        )
    publication = _publication(output, managed=True)
    sink: FileTableSink | None = None
    entries: list[dict[str, Any]] = [
        {"name": name, "path": f"tables/{index:04d}.{format}", "rows": 0, "format": format}
        for index, name in enumerate(prepared.tables)
    ]
    manifest: dict[str, Any] = {"kind": "tables", "tables": entries}
    try:
        staging = publication.stage()
        (staging / "tables").mkdir()
        paths = {entry["name"]: staging / entry["path"] for entry in entries}
        sink = FileTableSink(prepared.tables, paths, format=format, buffer_rows=buffer_rows)
        session = DecompositionSession(prepared, sink)
        session.consume(value)
        session.finish()
        sink.close()
        for entry in entries:
            entry["rows"] = sink.row_counts[entry["name"]]
        publication.publish(manifest)
        return ExecutionResult(publication.target, "tables", sink.row_counts)
    except Exception as exc:
        error = execution_error(exc, publication.target)
        if sink is not None:
            sink.close(primary_error=error)
            for entry in entries:
                entry["rows"] = sink.row_counts[entry["name"]]
        publication.fail(error, manifest)
        if error is exc:
            raise
        raise error from exc


def reconstruct_from(source: TableInput, prepared: PreparedMapping[T]) -> T:
    """Reconstruct in memory from independent typed tables or a managed dataset."""
    _direction(prepared, "reconstruct")
    bindings = table_sources(source) if isinstance(source, Path) else source
    return reconstruct(read_input_tables(bindings, prepared), prepared)


def reconstruct_to(
    source: TableInput,
    prepared: PreparedMapping[T],
    output: OutputTarget,
    *,
    packaging: Literal["standalone", "managed"] = "standalone",
) -> ExecutionResult:
    """Reconstruct and serialize through the native binding before publication."""
    _direction(prepared, "reconstruct")
    if not isinstance(prepared.binding, DocumentBinding) or not prepared.capabilities.Serialize:
        raise StructureMapError(
            "unavailable_capability",
            "The prepared binding cannot serialize a native document",
            owner="binding gap",
            remedy="Prepare a model with a Serialize-capable native binding",
        )
    if packaging not in {"standalone", "managed"}:
        raise StructureMapError(
            "invalid_output", f"Unsupported document packaging {packaging!r}", remedy="Select 'standalone' or 'managed'"
        )
    publication = _publication(output, managed=packaging == "managed")
    manifest: dict[str, Any] = {"kind": "document", "document": {"path": "document"}}
    try:
        staging = publication.stage()
        value = reconstruct_from(source, prepared)
        (staging / "document").write_text(serialize(value, prepared), encoding="utf-8")
        publication.publish(manifest)
        return ExecutionResult(publication.target, "document", {})
    except Exception as exc:
        error = execution_error(exc, publication.target)
        publication.fail(error, manifest)
        if error is exc:
            raise
        raise error from exc
