from dataclasses import dataclass
from typing import Literal

import pytest

from mountainash_structuremap import MappingSpec, MappingUse, StructureMapError, decompose, derive, prepare, reconstruct
from mountainash_structuremap.mapping import SourceDiscriminator, TypeCast, UseOverride


@dataclass
class Counted:
    common: str
    count: int


@dataclass
class Flagged:
    common: str
    flag: bool


@dataclass
class SharedDocument:
    items: list[Counted | Flagged]


def shared_mapping():
    return MappingSpec(overrides={"items": UseOverride(variant_layout="shared", shared_types="exact")})


def test_shared_exact_reconstructs_independent_common_and_branch_columns():
    prepared = prepare(derive(SharedDocument), shared_mapping())
    rows = {
        "root": [{"_sm_key": "root"}],
        "items": [
            {"_sm_key": "b", "_sm_parent": "root", "_sm_position": 9,
             "_sm_variant": "Flagged", "common": "same", "count": None, "flag": False},
            {"_sm_key": "a", "_sm_parent": "root", "_sm_position": 3,
             "_sm_variant": "Counted", "common": "same", "count": 7, "flag": None},
        ],
    }
    expected = SharedDocument([Counted("same", 7), Flagged("same", False)])
    assert reconstruct(rows, prepared) == expected
    emitted = decompose(expected, prepared)
    assert emitted["items"] == [
        {"_sm_key": "r1", "_sm_parent": "r0", "_sm_position": 0,
         "_sm_variant": "Counted", "common": "same", "count": 7, "flag": None},
        {"_sm_key": "r2", "_sm_parent": "r0", "_sm_position": 1,
         "_sm_variant": "Flagged", "common": "same", "count": None, "flag": False},
    ]
    rows["items"][0]["count"] = 99
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code == "invalid_variant_payload"


@dataclass
class NumericTag:
    kind: Literal[1, 3]
    count: int


@dataclass
class OtherTag:
    kind: Literal[2]
    label: str


@dataclass
class TaggedDocument:
    items: list[NumericTag | OtherTag]


def tagged_mapping(layout="shared"):
    return MappingSpec(overrides={"items": UseOverride(
        variant_layout=layout, discriminator=SourceDiscriminator(field="kind"))})


def test_source_discriminator_retains_numeric_tags_and_finite_domain():
    prepared = prepare(derive(TaggedDocument), tagged_mapping())
    rows = {
        "root": [{"_sm_key": "root"}],
        "items": [
            {"_sm_key": "a", "_sm_parent": "root", "_sm_position": 5,
             "kind": 3, "count": 7, "label": None},
            {"_sm_key": "b", "_sm_parent": "root", "_sm_position": 1,
             "kind": 2, "count": None, "label": "first"},
        ],
    }
    expected = TaggedDocument([OtherTag(2, "first"), NumericTag(3, 7)])
    assert reconstruct(rows, prepared) == expected
    emitted = decompose(expected, prepared)
    assert [row["kind"] for row in emitted["items"]] == [2, 3]
    assert all(type(row["kind"]) is int and "_sm_variant" not in row for row in emitted["items"])
    for invalid in (True, 4, "1"):
        rows["items"][0]["kind"] = invalid
        with pytest.raises(StructureMapError) as caught:
            reconstruct(rows, prepared)
        assert caught.value.code in ("invalid_discriminator", "unknown_variant", "cell_type_mismatch")


@dataclass
class OverlapTag:
    kind: Literal[3]
    value: str


@dataclass
class AmbiguousDocument:
    items: list[NumericTag | OverlapTag]


def test_overlapping_source_discriminator_domains_fail_before_data():
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(AmbiguousDocument), tagged_mapping())
    assert caught.value.code == "ambiguous_discriminator"
    assert caught.value.path == "items"


def test_source_discriminator_cast_is_rejected_with_configuration_guidance():
    mapping = tagged_mapping()
    mapping.overrides["items/kind"] = UseOverride(cast=TypeCast(to="string"))
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(TaggedDocument), mapping)
    assert caught.value.path == "items/kind"
    assert caught.value.remedy


@dataclass
class TextAmount:
    amount: str
    text: str


@dataclass
class IntAmount:
    amount: int
    count: int


@dataclass
class CastSharedDocument:
    items: list[TextAmount | IntAmount]


def test_shared_exact_uses_postcast_types_and_branch_specific_inverse():
    mapping = MappingSpec(use=MappingUse(component="core.record", children={
        "items": MappingUse(component="core.sequence", children={
            "item": MappingUse(component="core.alternatives", config={"variant_layout": "shared"}, children={
                "TextAmount": MappingUse(component="core.record", children={
                    "amount": MappingUse(component="core.value", config={"cast": {"strategy": "type", "to": "i64"}}),
                }),
            }),
        }),
    }))
    prepared = prepare(derive(CastSharedDocument), mapping)
    value = CastSharedDocument([TextAmount("01", "text"), IntAmount(2, 7)])
    rows = decompose(value, prepared)
    assert [row["amount"] for row in rows["items"]] == [1, 2]
    actual = reconstruct(rows, prepared)
    assert actual == CastSharedDocument([TextAmount("1", "text"), IntAmount(2, 7)])
    assert type(actual.items[0].amount) is str
    assert type(actual.items[1].amount) is int


def test_shared_exact_rejects_incompatible_unprojected_field_types():
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(CastSharedDocument), shared_mapping())
    assert caught.value.code == "incompatible_schema"


@dataclass
class NullableTaggedDocument:
    items: list[NumericTag | OtherTag | None]


def test_source_discriminator_split_null_rows_need_no_generated_tag():
    mapping = tagged_mapping("split")
    mapping.overrides["items"] = UseOverride(
        variant_layout="split", discriminator=SourceDiscriminator(field="kind"),
        alternatives={
            "NumericTag": {"table": "numbers", "prefix": ""},
            "OtherTag": {"table": "labels", "prefix": ""},
            "$null": {"table": "nulls"},
        },
    )
    prepared = prepare(derive(NullableTaggedDocument), mapping)
    rows = {
        "root": [{"_sm_key": "root"}],
        "numbers": [{"_sm_key": "n", "_sm_parent": "root", "_sm_position": 9, "kind": 3, "count": 7}],
        "labels": [{"_sm_key": "l", "_sm_parent": "root", "_sm_position": 1, "kind": 2, "label": "first"}],
        "nulls": [{"_sm_key": "z", "_sm_parent": "root", "_sm_position": 4, "_sm_null": True}],
    }
    expected = NullableTaggedDocument([OtherTag(2, "first"), None, NumericTag(3, 7)])
    assert reconstruct(rows, prepared) == expected
    assert decompose(expected, prepared)["nulls"] == [
        {"_sm_key": "r2", "_sm_parent": "r0", "_sm_position": 1, "_sm_null": True},
    ]


@dataclass
class TaggedGroupA:
    common: str
    tags: list[str]


@dataclass
class TaggedGroupB:
    common: str
    tags: list[str]


@dataclass
class GroupDocument:
    groups: list[TaggedGroupA | TaggedGroupB]


def test_shared_alternatives_route_nested_shared_collections_by_branch_role():
    branches = {
        name: MappingUse(component="core.record", children={
            "tags": MappingUse(component="core.sequence", config={"collection_table": {
                "strategy": "shared", "table": "tags", "role_column": "branch", "role": role,
            }}),
        })
        for name, role in (("TaggedGroupA", "a"), ("TaggedGroupB", "b"))
    }
    mapping = MappingSpec(use=MappingUse(component="core.record", children={
        "groups": MappingUse(component="core.sequence", children={
            "item": MappingUse(component="core.alternatives", config={"variant_layout": "shared"}, children=branches),
        }),
    }))
    plan = prepare(derive(GroupDocument), mapping)
    rows = {
        "root": [{"_sm_key": "r"}],
        "groups": [
            {"_sm_key": "a", "_sm_parent": "r", "_sm_position": 4, "_sm_variant": "TaggedGroupA", "common": "one"},
            {"_sm_key": "b", "_sm_parent": "r", "_sm_position": 9, "_sm_variant": "TaggedGroupB", "common": "two"},
        ],
        "tags": [
            {"_sm_key": "x", "_sm_parent": "b", "_sm_position": 0, "branch": "b", "value": "second"},
            {"_sm_key": "x", "_sm_parent": "a", "_sm_position": 0, "branch": "a", "value": "first"},
        ],
    }
    expected = GroupDocument([TaggedGroupA("one", ["first"]), TaggedGroupB("two", ["second"])])
    assert reconstruct(rows, plan) == expected
    assert reconstruct(decompose(expected, plan), plan) == expected
    rows["tags"][0]["branch"] = "a"
    rows["tags"][0]["_sm_key"] = "not-duplicate"
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, plan)
    assert caught.value.code == "invalid_variant_payload"


@dataclass
class TaggedDirectory:
    home: list[NumericTag | OtherTag | None]
    work: list[NumericTag | OtherTag | None]


def test_shared_split_roles_keep_source_tags_and_lenient_order_separate():
    mapping = MappingSpec(overrides={
        role: UseOverride(
            variant_layout="split", positions="lenient",
            discriminator=SourceDiscriminator(field="kind"),
            collection_table={"strategy": "shared", "table": "entries", "role_column": "owner", "role": role},
            alternatives={"NumericTag": {"prefix": ""}, "OtherTag": {"prefix": ""}},
        )
        for role in ("home", "work")
    })
    prepared = prepare(derive(TaggedDirectory), mapping)
    rows = {
        "root": [{"_sm_key": "root"}],
        "entries__numeric_tag": [
            {"_sm_key": "same", "_sm_parent": "root", "_sm_position": None, "owner": "work", "kind": 1, "count": 8},
            {"_sm_key": "same", "_sm_parent": "root", "_sm_position": None, "owner": "home", "kind": 3, "count": 7},
        ],
        "entries__other_tag": [
            {"_sm_key": "label", "_sm_parent": "root", "_sm_position": None, "owner": "home", "kind": 2, "label": "last"},
        ],
        "entries__null": [
            {"_sm_key": "null", "_sm_parent": "root", "_sm_position": None, "owner": "home", "_sm_null": True},
        ],
    }
    expected = TaggedDirectory([NumericTag(3, 7), OtherTag(2, "last"), None], [NumericTag(1, 8)])
    assert reconstruct(rows, prepared) == expected
    assert reconstruct(decompose(expected, prepared), prepared) == expected


def test_prefixed_source_discriminator_rejects_populated_inactive_tag():
    mapping = MappingSpec(overrides={"items": UseOverride(
        variant_layout="prefixed", discriminator=SourceDiscriminator(field="kind"),
        alternatives={"NumericTag": {"prefix": "number"}, "OtherTag": {"prefix": "text"}},
    )})
    prepared = prepare(derive(TaggedDocument), mapping)
    rows = {
        "root": [{"_sm_key": "root"}],
        "items": [{
            "_sm_key": "item", "_sm_parent": "root", "_sm_position": 0,
            "number_kind": 3, "number_count": 7, "text_kind": None, "text_label": None,
        }],
    }
    expected = TaggedDocument([NumericTag(3, 7)])
    assert reconstruct(rows, prepared) == expected
    assert decompose(expected, prepared)["items"][0]["number_kind"] == 3
    rows["items"][0]["text_kind"] = 2
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code == "invalid_discriminator"
