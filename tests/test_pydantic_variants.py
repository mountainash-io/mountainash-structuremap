from __future__ import annotations

from typing import Annotated, ClassVar, Literal

import pytest
from pydantic import BaseModel, Field

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct
from mountainash_structuremap.mapping import AlternativeLayout, MappingSpec, UseOverride


class Dog(BaseModel):
    kind: Literal["DOG"]
    name: str
    constructions: ClassVar[int] = 0

    def model_post_init(self, context: object) -> None:
        type(self).constructions += 1


class Cat(BaseModel):
    kind: Literal["CAT"]
    name: str


Pet = Annotated[Dog | Cat, Field(discriminator="kind")]


class Pets(BaseModel):
    pets: list[Pet]


class Zoo(Pets):
    featured: Pet


def _mapping(layout: str) -> MappingSpec:
    def override(path: str) -> UseOverride:
        stem = "pet" if path == "pets" else "featured"
        alternatives = {
            "Dog": AlternativeLayout(table=f"{stem}_dogs" if layout == "split" and path == "pets" else None, prefix=f"{stem}_dog"),
            "Cat": AlternativeLayout(table=f"{stem}_cats" if layout == "split" and path == "pets" else None, prefix=f"{stem}_cat"),
        }
        return UseOverride(
            variant_layout=layout,
            discriminator={
                "strategy": "recorded",
                "column": "kind",
                "values": {"Dog": "DOG", "Cat": "CAT"},
            },
            alternatives=alternatives,
        )

    return MappingSpec(overrides={"pets": override("pets"), "featured": override("featured")})


@pytest.mark.parametrize("layout", ["split", "prefixed"])
def test_pydantic_repeated_and_singular_alternatives_preserve_branch_data_and_order(layout: str) -> None:
    mapping = _mapping(layout)
    prepared = prepare(derive(Zoo), mapping)
    native = Zoo(
        pets=[Dog(kind="DOG", name="first"), Cat(kind="CAT", name="second"), Dog(kind="DOG", name="third")],
        featured=Cat(kind="CAT", name="featured"),
    )

    rows = decompose(native, prepared)
    rebuilt = reconstruct({name: list(reversed(values)) for name, values in rows.items()}, prepare(derive(Zoo), mapping))

    assert [type(pet) for pet in rebuilt.pets] == [Dog, Cat, Dog]
    assert [(pet.kind, pet.name) for pet in rebuilt.pets] == [
        ("DOG", "first"), ("CAT", "second"), ("DOG", "third")
    ]
    assert type(rebuilt.featured) is Cat
    assert (rebuilt.featured.kind, rebuilt.featured.name) == ("CAT", "featured")


def _pets_mapping(layout: str) -> MappingSpec:
    alternatives = {
        "Dog": AlternativeLayout(table="dogs" if layout == "split" else None, prefix="dog"),
        "Cat": AlternativeLayout(table="cats" if layout == "split" else None, prefix="cat"),
    }
    return MappingSpec(overrides={"pets": UseOverride(
        variant_layout=layout,
        discriminator={"strategy": "recorded", "column": "kind", "values": {"Dog": "DOG", "Cat": "CAT"}},
        alternatives=alternatives,
    )})


def test_bad_recorded_tag_is_rejected_before_any_native_model_constructor() -> None:
    prepared = prepare(derive(Pets), _pets_mapping("split"))
    Dog.constructions = 0
    tables = {
        "root": [{"_sm_key": "z"}],
        "dogs": [{"_sm_key": "d", "_sm_parent": "z", "_sm_position": 0, "kind": "ALIEN", "dog_kind": "DOG", "dog_name": "x"}],
        "cats": [],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)

    assert caught.value.code == "invalid_discriminator"
    assert Dog.constructions == 0


def test_split_recorded_alternative_table_and_tag_must_agree_before_construction() -> None:
    prepared = prepare(derive(Pets), _pets_mapping("split"))
    Dog.constructions = 0
    tables = {
        "root": [{"_sm_key": "z"}],
        "dogs": [{"_sm_key": "d", "_sm_parent": "z", "_sm_position": 0, "kind": "CAT", "dog_kind": "DOG", "dog_name": "wrong"}],
        "cats": [],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)

    assert caught.value.code == "invalid_discriminator"
    assert Dog.constructions == 0


def test_prefixed_inactive_alternative_payload_is_rejected_before_construction() -> None:
    prepared = prepare(derive(Pets), _pets_mapping("prefixed"))
    tables = decompose(Pets(pets=[Dog(kind="DOG", name="active")]), prepared)
    Dog.constructions = 0
    tables["pets"][0]["cat_name"] = "inactive"

    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)

    assert caught.value.code == "invalid_variant_payload"
    assert Dog.constructions == 0


def test_split_cross_branch_duplicate_positions_fail_before_native_construction() -> None:
    prepared = prepare(derive(Pets), _pets_mapping("split"))
    Dog.constructions = 0
    tables = {
        "root": [{"_sm_key": "z"}],
        "dogs": [{"_sm_key": "d", "_sm_parent": "z", "_sm_position": 4, "kind": "DOG", "dog_kind": "DOG", "dog_name": "later"}],
        "cats": [{"_sm_key": "c", "_sm_parent": "z", "_sm_position": 4, "kind": "CAT", "cat_kind": "CAT", "cat_name": "earlier"}],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)

    assert caught.value.code == "duplicate_position"
    assert Dog.constructions == 0


def test_undeclared_runtime_subclass_is_not_admitted_to_frozen_alternative_association() -> None:
    class UnlistedDog(Dog):
        pass

    prepared = prepare(derive(Pets), _pets_mapping("split"))
    value = Pets(pets=[UnlistedDog(kind="DOG", name="unlisted")])

    with pytest.raises(StructureMapError) as caught:
        decompose(value, prepared)

    assert caught.value.code == "unknown_variant"


def test_recorded_primitive_string_branch_still_uses_native_left_to_right_union_validation() -> None:
    class PrimitiveChoice(BaseModel):
        value: int | str = Field(union_mode="left_to_right")

    prepared = prepare(derive(PrimitiveChoice), MappingSpec(overrides={
        "value": UseOverride(
            variant_layout="split",
            discriminator={"strategy": "recorded", "column": "kind", "values": {"int": "INTEGER", "str": "STRING"}},
        )
    }))
    rows = {"root": [{"_sm_key": "p", "kind": "STRING", "int_value": None, "str_value": "456"}]}

    rebuilt = reconstruct(rows, prepared)

    assert type(rebuilt.value) is int
    assert rebuilt.value == 456


def test_recorded_primitive_string_branch_keeps_non_numeric_input_as_string() -> None:
    class PrimitiveChoice(BaseModel):
        value: int | str = Field(union_mode="left_to_right")

    prepared = prepare(derive(PrimitiveChoice), MappingSpec(overrides={
        "value": UseOverride(
            variant_layout="split",
            discriminator={"strategy": "recorded", "column": "kind", "values": {"int": "INTEGER", "str": "STRING"}},
        )
    }))
    rebuilt = reconstruct({"root": [{"_sm_key": "p", "kind": "STRING", "int_value": None, "str_value": "not-a-number"}]}, prepared)

    assert type(rebuilt.value) is str
    assert rebuilt.value == "not-a-number"


@pytest.mark.parametrize("layout", ["split", "prefixed"])
def test_independent_recorded_variants_rebuild_native_classes_in_position_order(layout):
    prepared = prepare(derive(Pets), _pets_mapping(layout))
    dog = {"_sm_key": "d", "_sm_parent": "z", "_sm_position": 9,
           "kind": "DOG", "dog_kind": "DOG", "dog_name": "later"}
    cat = {"_sm_key": "c", "_sm_parent": "z", "_sm_position": 2,
           "kind": "CAT", "cat_kind": "CAT", "cat_name": "earlier"}
    if layout == "split":
        rows = {"root": [{"_sm_key": "z"}], "dogs": [dog], "cats": [cat]}
    else:
        dog.update(cat_kind=None, cat_name=None)
        cat.update(dog_kind=None, dog_name=None)
        rows = {"root": [{"_sm_key": "z"}], "pets": [dog, cat]}
    result = reconstruct(rows, prepared)
    assert [type(pet) for pet in result.pets] == [Cat, Dog]
    assert [(pet.kind, pet.name) for pet in result.pets] == [("CAT", "earlier"), ("DOG", "later")]
