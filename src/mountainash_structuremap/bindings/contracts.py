from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CapabilityReport:
    Introspect: bool = True
    Read: bool = True
    Construct: bool = True
    Parse: bool = False
    Serialize: bool = False
    Validate: bool = False


class DataclassBinding(Protocol):
    capabilities: CapabilityReport

    def read(self, instance: object, member: str, *, path: str) -> object: ...

    def construct(self, cls: type, values: dict[str, object], *, path: str) -> object: ...
