from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, TypeVar, runtime_checkable

from mountainash_structuremap.codecs import ScalarCodec as _ScalarCodec
from mountainash_structuremap.definitions import RecordDefinition, Scalar

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




class NativeBinding(Protocol):
    capabilities: CapabilityReport

    def describe_record(
        self, cls: type, register: Callable[[type, str], str], *, path: str
    ) -> RecordDefinition: ...

    def prepare_record(self, cls: type, definition: RecordDefinition, *, path: str) -> RecordAdapter: ...

    def prepare_scalar(
        self, declaration: Scalar, *, native_type: type | None, format: str | None,
        column: str, policy: str | None, path: str,
    ) -> _ScalarCodec: ...


@runtime_checkable
class DocumentBinding(Protocol):
    capabilities: CapabilityReport

    def parse(self, source: str | bytes, root_type: type[T]) -> T: ...

    def serialize(self, value: object) -> str: ...




__all__ = ["CapabilityReport", "DocumentBinding", "NativeBinding", "RecordAdapter"]
