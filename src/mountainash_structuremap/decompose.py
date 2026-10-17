from __future__ import annotations

from typing import TypeVar

from mountainash_structuremap.codecs import Cell
from mountainash_structuremap.execution_session import DecompositionSession, RowCollector
from mountainash_structuremap.prepared import PreparedMapping

T = TypeVar("T")


def decompose(value: T, prepared: PreparedMapping[T]) -> dict[str, list[dict[str, Cell]]]:
    collector = RowCollector(prepared.tables)
    session = DecompositionSession(prepared, collector)
    session.consume(value)
    session.finish()
    return collector.tables
