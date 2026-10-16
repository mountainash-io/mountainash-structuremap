from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

import pytest
from mountainash.typespec.universal_types import UniversalType as U

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
    field = next(field for field in prepared.tables["items"].typespec.fields if field.name == "id")
    assert field.type is U.INTEGER
    assert field.backend_type == "i64"


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
