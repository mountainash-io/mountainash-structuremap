from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Generic, Literal, Mapping, TypeVar

from mountainash.typespec.spec import TypeSpec

from mountainash_structuremap.bindings.contracts import CapabilityReport, DataclassBinding

Cell = str | int | float | bool | None
T = TypeVar("T")


@dataclass(frozen=True)
class ScalarProgram:
    path: str
    column: str
    scalar_type: Literal["str", "int", "float", "bool"]
    nullable: bool = False


@dataclass(frozen=True)
class RecordProgram:
    path: str
    definition: str
    members: tuple[tuple[str, ValueProgram], ...]
    nullable: bool = False


@dataclass(frozen=True)
class SequenceProgram:
    path: str
    table: str
    item: ValueProgram
    nullable: bool = False


ValueProgram = ScalarProgram | RecordProgram | SequenceProgram


@dataclass(frozen=True)
class TablePlan:
    name: str
    source_path: str
    parent_tables: tuple[str, ...]
    columns: tuple[str, ...]
    _typespec: TypeSpec = field(repr=False, compare=False)

    @property
    def typespec(self) -> TypeSpec:
        return deepcopy(self._typespec)


@dataclass(frozen=True)
class PreparedMapping(Generic[T]):
    root: RecordProgram
    root_type: type[T]
    tables: Mapping[str, TablePlan]
    native_types: Mapping[str, type]
    binding: DataclassBinding
    capabilities: CapabilityReport
    key_column: str
    parent_column: str
    position_column: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", MappingProxyType(dict(self.tables)))
        object.__setattr__(self, "native_types", MappingProxyType(dict(self.native_types)))
