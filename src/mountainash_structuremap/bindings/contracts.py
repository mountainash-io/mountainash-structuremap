from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

from mountainash_structuremap.codecs import ScalarCodec as _ScalarCodec
from mountainash_structuremap.definitions import Alternative, RecordDefinition, Scalar

T = TypeVar("T")


@dataclass(frozen=True)
class CapabilityReport:
    Introspect: bool = True
    Read: bool = True
    Construct: bool = True
    Parse: bool = False
    Serialize: bool = False
    Validate: bool = False


class RecordAdapter(Protocol):
    native_type: type

    def read(self, instance: object, member: str, *, path: str) -> object: ...

    def construct(self, values: dict[str, object], *, path: str) -> object: ...




class VariantAdapter(Protocol):
    def select(self, value: object) -> tuple[str, object]: ...

    def construct(self, key: str, payload: object) -> object: ...


class NativeBinding(Protocol):
    capabilities: CapabilityReport

    def describe_record(
        self, cls: type, register: Callable[[type, str], str], *, path: str
    ) -> RecordDefinition: ...

    def prepare_record(self, cls: type, definition: RecordDefinition, *, path: str) -> RecordAdapter: ...

    def prepare_scalar(
        self, declaration: Scalar, *, native_type: type | None, format: str | None,
        column: str, policy: str | None, metadata_namespace: str = "_sm_", path: str,
        field_context: tuple[type, str, str | None] | None = None,
    ) -> _ScalarCodec: ...

    def prepare_variant(
        self,
        alternatives: tuple[Alternative, ...],
        *,
        native_types: Mapping[str, type],
        namespace: str | None = None,
        path: str,
    ) -> VariantAdapter: ...


@runtime_checkable
class DocumentBinding(Protocol):
    capabilities: CapabilityReport

    def parse(self, source: str | bytes, root_type: type[T]) -> T: ...

    def serialize(self, value: object) -> str: ...




__all__ = ["CapabilityReport", "DocumentBinding", "NativeBinding", "RecordAdapter", "VariantAdapter"]
