from __future__ import annotations

from typing import TypeVar

from mountainash_structuremap.bindings.contracts import DocumentBinding
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.prepared import PreparedMapping

T = TypeVar("T")


def parse(source: str | bytes, prepared: PreparedMapping[T]) -> T:
    binding = prepared.binding
    if not isinstance(binding, DocumentBinding) or not prepared.capabilities.Parse:
        raise StructureMapError(
            "unavailable_capability", "The prepared binding does not provide document parsing",
            path="", owner="binding gap", remedy="Prepare a model with a binding that supports Parse",
        )
    return binding.parse(source, prepared.root_type)


def serialize(value: T, prepared: PreparedMapping[T]) -> str:
    binding = prepared.binding
    if not isinstance(binding, DocumentBinding) or not prepared.capabilities.Serialize:
        raise StructureMapError(
            "unavailable_capability", "The prepared binding does not provide document serialization",
            path="", owner="binding gap", remedy="Prepare a model with a binding that supports Serialize",
        )
    return binding.serialize(value)
