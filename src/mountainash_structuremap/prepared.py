from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Generic, Literal, Mapping, TypeVar

from mountainash.typespec.spec import TypeSpec

from mountainash_structuremap.bindings.contracts import CapabilityReport, NativeBinding, RecordAdapter, VariantAdapter
from mountainash_structuremap.codecs import ScalarCodec

T = TypeVar("T")


@dataclass(frozen=True)
class ScalarProgram:
    path: str
    codec: ScalarCodec
    nullable: bool = False


@dataclass(frozen=True)
class RecordProgram:
    path: str
    definition: str
    members: tuple[tuple[str, int], ...]
    nullable: bool = False


@dataclass(frozen=True)
class VariantBranch:
    key: str
    tag: str
    program: int
    table: str | None = None
    nullable_payload: bool = False


@dataclass(frozen=True)
class SequenceProgram:
    path: str
    table: str
    nullable: bool = False
    container: Literal["list", "tuple"] = "list"


@dataclass(frozen=True)
class VariantProgram:
    path: str
    adapter: VariantAdapter
    discriminator_column: str
    alternatives: tuple[VariantBranch, ...]
    layout: Literal["split", "prefixed"]
    nullable: bool = False
    nullable_member: bool = False
    repeated: bool = False
    container: Literal["list", "tuple"] = "list"
    null_table: str | None = None
    map_values: bool = False
    payload_null_column: str | None = None




@dataclass(frozen=True)
class NullProgram:
    path: str
@dataclass(frozen=True)
class MapProgram:
    path: str
    table: str
    nullable: bool = False


@dataclass(frozen=True)
class TupleProgram:
    path: str
    items: tuple[int, ...]
    nullable: bool = False


@dataclass(frozen=True)
class LinkProgram:
    path: str
    table: str
    nullable: bool = False


ValueProgram = ScalarProgram | RecordProgram | SequenceProgram | MapProgram | TupleProgram | LinkProgram | VariantProgram | NullProgram


@dataclass(frozen=True)
class TablePlan:
    name: str
    source_path: str
    parent_tables: tuple[str, ...]
    columns: tuple[str, ...]
    _typespec: TypeSpec = field(repr=False, compare=False)
    _program: int = 0
    _kind: Literal["root", "single", "sequence", "map"] = "root"
    _nullable_member: bool = False
    _position_group: str | None = None

    @property
    def typespec(self) -> TypeSpec:
        return deepcopy(self._typespec)


@dataclass(frozen=True)
class PreparedMapping(Generic[T]):
    root: int
    programs: Mapping[int, ValueProgram]
    root_table: str
    root_type: type[T]
    tables: Mapping[str, TablePlan]
    native_types: Mapping[str, type]
    binding: NativeBinding
    capabilities: CapabilityReport
    record_adapters: Mapping[str, RecordAdapter]
    key_column: str
    parent_column: str
    position_column: str
    map_key_column: str
    null_column: str
    parent_table_column: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "programs", MappingProxyType(dict(self.programs)))
        object.__setattr__(self, "tables", MappingProxyType(dict(self.tables)))
        object.__setattr__(self, "native_types", MappingProxyType(dict(self.native_types)))
        object.__setattr__(self, "record_adapters", MappingProxyType(dict(self.record_adapters)))
