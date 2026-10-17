from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

import polars as pl
import pyarrow as pa
import pytest
from mountainash.core.dtypes import MountainashDtype, NativeDtype, TypeTarget
from mountainash.typespec.converters import to_arrow_schema
from mountainash.typespec.spec import FieldSpec, TypeSpec

from mountainash_structuremap import (
    MappingSpec,
    StructureMapError,
    UseOverride,
    decompose,
    derive,
    prepare,
    reconstruct,
)
from mountainash_structuremap.mapping import (
    CustomCast,
    SourceDiscriminator,
    SourceKey,
    TypeCast,
)
from mountainash_structuremap.projections import prepare_value_projection


@dataclass(frozen=True)
class Item:
    id: str
    value: str


@dataclass(frozen=True)
class Items:
    items: list[Item]


@dataclass(frozen=True)
class TaggedOne:
    kind: Literal[1]
    value: str


@dataclass(frozen=True)
class TaggedTwo:
    kind: Literal[2]
    value: str


@dataclass(frozen=True)
class TaggedItems:
    items: list[TaggedOne | TaggedTwo]



def _mapping(cast: TypeCast, *, source_key: bool = False) -> MappingSpec:
    return MappingSpec(overrides={
        "items": UseOverride(key=SourceKey(columns=("id",))) if source_key else UseOverride(),
        "items/id": UseOverride(cast=cast),
    })


def test_type_cast_projects_string_values_to_integer_and_reconstructs_native_strings():
    prepared = prepare(derive(Items), _mapping(TypeCast(to="i64")))
    source = Items([Item("01", "first"), Item("2", "second")])

    rows = decompose(source, prepared)
    assert [row["id"] for row in rows["items"]] == [1, 2]
    assert type(rows["items"][0]["id"]) is int
    assert reconstruct(rows, prepared) == Items([Item("1", "first"), Item("2", "second")])
    table = pa.Table.from_pylist(rows["items"], schema=to_arrow_schema(prepared.tables["items"].typespec))
    assert table.schema.field("id").type == pa.int64()
    assert table.column("id").to_pylist() == [1, 2]


@pytest.mark.parametrize(("target", "arrow_type", "values"), [
    ("i8", pa.int8(), ["-128", "127"]),
    ("i16", pa.int16(), ["-32768", "32767"]),
    ("i32", pa.int32(), ["-2147483648", "2147483647"]),
    ("i64", pa.int64(), ["-9223372036854775808", "9223372036854775807"]),
    ("u8", pa.uint8(), ["0", "255"]),
    ("u16", pa.uint16(), ["0", "65535"]),
    ("u32", pa.uint32(), ["0", "4294967295"]),
    ("u64", pa.uint64(), ["0", "18446744073709551615"]),
    ("fp32", pa.float32(), ["-1.5", "1.5"]),
    ("fp64", pa.float64(), ["-1.5", "1.5"]),
])
def test_cast_physical_width_and_values_survive_schema_reload(target, arrow_type, values):
    prepared = prepare(derive(Items), _mapping(TypeCast(to=target)))
    source = Items([Item(value, "payload") for value in values])
    rows = decompose(source, prepared)
    reloaded = TypeSpec.from_frictionless(prepared.tables["items"].typespec.to_frictionless())
    table = pa.Table.from_pylist(rows["items"], schema=to_arrow_schema(reloaded))
    assert table.schema.field("id").type == arrow_type
    convert = float if pa.types.is_floating(arrow_type) else int
    assert table.column("id").to_pylist() == [convert(value) for value in values]
    assert reconstruct({**rows, "items": table.to_pylist()}, prepared) == source


@pytest.mark.parametrize(("declared", "override", "compatible"), [
    (MountainashDtype.I64, "int8", True),
    (MountainashDtype.I8, "int64", False),
])
def test_explicit_schema_uses_native_override_for_physical_compatibility(declared, override, compatible):
    mapping = _mapping(TypeCast(to="i8"))
    inferred = prepare(derive(Items), mapping)
    schemas = {name: table.typespec for name, table in inferred.tables.items()}
    schemas["items"].fields = [
        FieldSpec(name="id", dtype=declared, backend_type=override, missing_values=[]) if field.name == "id" else field
        for field in schemas["items"].fields
    ]
    if not compatible:
        with pytest.raises(StructureMapError) as caught:
            prepare(derive(Items), mapping, relations=schemas)
        assert caught.value.code == "incompatible_schema"
        return
    prepared = prepare(derive(Items), mapping, relations=schemas)
    source = Items([Item("-128", "payload"), Item("127", "payload")])
    rows = decompose(source, prepared)
    table = pa.Table.from_pylist(rows["items"], schema=to_arrow_schema(prepared.tables["items"].typespec))
    assert table.schema.field("id").type == pa.int8()
    assert reconstruct({**rows, "items": table.to_pylist()}, prepared) == source


def test_inverse_projection_honors_native_override_before_canonical_dtype():
    source_field = FieldSpec(
        name="amount", dtype=MountainashDtype.I8,
        backend_type=NativeDtype(value=pl.Int64, target=TypeTarget.POLARS), missing_values=[],
    )
    projection = prepare_value_projection((source_field,), TypeCast(to="string"), path="amount")
    assert projection.apply([{"amount": "128"}], reverse=True) == [{"amount": 128}]


def test_source_keys_that_collide_after_cast_are_rejected():
    prepared = prepare(derive(Items), _mapping(TypeCast(to="i64"), source_key=True))

    with pytest.raises(StructureMapError) as caught:
        decompose(Items([Item("01", "first"), Item("1", "second")]), prepared)

    assert caught.value.code == "duplicate_key"


def test_cast_overflow_throws_during_decomposition():
    prepared = prepare(derive(Items), _mapping(TypeCast(to="i64")))

    with pytest.raises(StructureMapError):
        decompose(Items([Item("999999999999999999999999999999", "overflow")]), prepared)


def test_failed_cast_to_null_is_rejected_for_nonnullable_native_destination():
    prepared = prepare(derive(Items), _mapping(TypeCast(to="i64", on_failure="null")))
    rows = decompose(Items([Item("not-an-integer", "bad")]), prepared)
    assert rows["items"][0]["id"] is None

    with pytest.raises(StructureMapError):
        reconstruct(rows, prepared)


def test_custom_cast_is_an_explicit_unavailable_capability():
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Items), MappingSpec(overrides={
            "items/id": UseOverride(cast=CustomCast(forward="cast")),
        }))

    assert caught.value.code == "unavailable_capability"


def test_source_discriminator_cast_is_rejected_during_preparation():
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(TaggedItems), MappingSpec(overrides={
            "items": UseOverride(discriminator=SourceDiscriminator(field="kind")),
            "items/kind": UseOverride(cast=TypeCast(to="i64")),
        }))

    assert caught.value.code == "invalid_discriminator"


@dataclass
class CountingAmount:
    amount: int
    constructed: ClassVar[list[int]] = []

    def __post_init__(self):
        self.constructed.append(self.amount)


@dataclass
class CountingAmounts:
    items: list[CountingAmount]


def test_inverse_cast_failure_precedes_all_native_construction(monkeypatch):
    monkeypatch.setattr(CountingAmount, "constructed", [])
    prepared = prepare(derive(CountingAmounts), MappingSpec(overrides={
        "items/amount": UseOverride(cast=TypeCast(to="string")),
    }))
    rows = {
        "root": [{"_sm_key": "r"}],
        "items": [
            {"_sm_key": "a", "_sm_parent": "r", "_sm_position": 0, "amount": "1"},
            {"_sm_key": "b", "_sm_parent": "r", "_sm_position": 1, "amount": "not-an-integer"},
        ],
    }
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code == "cast_failed"
    assert caught.value.__cause__ is not None
    assert CountingAmount.constructed == []
