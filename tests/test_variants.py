import sys

import pytest

from mountainash_structuremap import StructureMapError, decompose, derive, parse, prepare, reconstruct, serialize
from mountainash_structuremap.codecs import Cell
from mountainash_structuremap.mapping import AlternativeLayout, MappingSpec, SourceDiscriminator, UseOverride
from tests.fixtures.variants import (
    CAT,
    DOG,
    AllNullCat,
    AllNullDog,
    AllNullPets,
    BooleanOrInteger,
    Branch,
    Cat,
    ChoiceEnvelope,
    Dog,
    Leaf,
    MapVariants,
    NullableDetailRoot,
    NullablePets,
    NullAlternativePets,
    OverrideA,
    OverrideB,
    OverrideRoot,
    OverrideRootReversed,
    OverrideX,
    OverrideY,
    Pets,
    RecursiveChoice,
    SharedA,
    SharedB,
    SharedRoot,
    SharedTags,
    VariantUses,
)


def pets_mapping(layout):
    return MappingSpec(overrides={"pets": UseOverride(
        variant_layout=layout,
        discriminator={"strategy": "recorded", "column": "kind", "values": {DOG: "DOG", CAT: "CAT"}},
        alternatives={
            DOG: AlternativeLayout(table="dogs" if layout == "split" else None, prefix="dog"),
            CAT: AlternativeLayout(table="cats" if layout == "split" else None, prefix="cat"),
        },
    )})


def nullable_pets_mapping():
    return MappingSpec(overrides={"pets": UseOverride(
        variant_layout="split",
        discriminator={"strategy": "recorded", "column": "kind",
                       "values": {"AllNullDog": "DOG", "AllNullCat": "CAT"}},
        alternatives={
            "AllNullDog": AlternativeLayout(table="dogs"),
            "AllNullCat": AlternativeLayout(table="cats"),
            "$null": AlternativeLayout(table="null_values"),
        },
    )})


def map_variants_mapping(layout):
    alternatives = {
        "Branch": AlternativeLayout(table="branch_values" if layout == "split" else None, prefix="branch"),
        "Leaf": AlternativeLayout(table="leaf_values" if layout == "split" else None, prefix="leaf"),
    }
    if layout == "split":
        alternatives["$null"] = AlternativeLayout(table="null_values")
    return MappingSpec(overrides={"values": UseOverride(
        variant_layout=layout,
        discriminator={
            "strategy": "recorded", "column": "kind",
            "values": {"Branch": "BRANCH", "Leaf": "LEAF"},
        },
        alternatives=alternatives,
    )})


@pytest.mark.parametrize("layout", ["split", "prefixed"])
def test_map_values_support_split_and_prefixed_variants(layout):
    prepared = prepare(derive(MapVariants), map_variants_mapping(layout))
    source = MapVariants({"branch": Branch("branch"), "leaf": Leaf("leaf"), "null": None})
    rebuilt = reconstruct(decompose(source, prepared), prepared)
    assert rebuilt == source


def test_split_map_alternatives_share_position_and_map_key_domains():
    prepared = prepare(derive(MapVariants), map_variants_mapping("split"))
    tables: dict[str, list[dict[str, Cell]]] = {name: [] for name in prepared.tables}
    tables["root"] = [{"_sm_key": "root"}]

    def row(table: str, **values: Cell) -> dict[str, Cell]:
        result = {column: None for column in prepared.tables[table].columns}
        result.update(values)
        return result

    tables["branch_values"] = [row(
        "branch_values", _sm_key="b", _sm_parent="root", _sm_position=9,
        _sm_map_key="branch", kind="BRANCH", branch_label="branch",
    )]
    tables["leaf_values"] = [row(
        "leaf_values", _sm_key="l", _sm_parent="root", _sm_position=2,
        _sm_map_key="leaf", kind="LEAF", leaf_label="leaf",
    )]
    tables["null_values"] = [row(
        "null_values", _sm_key="n", _sm_parent="root", _sm_position=7,
        _sm_map_key="null", _sm_null=True, kind=None,
    )]
    rebuilt = reconstruct(tables, prepared)
    assert list(rebuilt.values) == ["leaf", "null", "branch"]

    tables["branch_values"][0]["_sm_position"] = 2
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "duplicate_position"

    tables["branch_values"][0]["_sm_position"] = 9
    tables["branch_values"][0]["_sm_map_key"] = "same"
    tables["leaf_values"][0]["_sm_map_key"] = "same"
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "duplicate_map_key"


@pytest.mark.parametrize("layout", ["split", "prefixed"])
def test_equal_shaped_native_alternatives_keep_selection(layout):
    mapping = pets_mapping(layout)
    prepared = prepare(derive(Pets), mapping)
    native = parse(serialize(Pets([Dog("same"), Cat("same"), Dog("")]), prepared), prepared)
    delivered = {name: list(reversed(rows)) for name, rows in decompose(native, prepared).items()}
    rebuilt = reconstruct(delivered, prepare(derive(Pets), mapping))
    assert [type(item) for item in rebuilt.pets] == [Dog, Cat, Dog]
    assert [item.name for item in rebuilt.pets] == ["same", "same", ""]
    assert parse(serialize(rebuilt, prepared), prepared) == Pets([Dog("same"), Cat("same"), Dog("")])


def test_split_positions_order_across_alternative_tables_and_reject_cross_table_duplicates():
    prepared = prepare(derive(Pets), pets_mapping("split"))
    tables = {
        "root": [{"_sm_key": "external"}],
        "dogs": [{"_sm_key": "d", "_sm_parent": "external", "_sm_position": 9, "kind": "DOG", "dog_name": "later"}],
        "cats": [{"_sm_key": "c", "_sm_parent": "external", "_sm_position": 2, "kind": "CAT", "cat_name": "earlier"}],
    }
    result = reconstruct(tables, prepared)
    assert result.pets == [Cat("earlier"), Dog("later")]
    Dog.calls = 0
    tables["cats"][0]["_sm_position"] = 9
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "duplicate_position"
    assert Dog.calls == 0


def test_split_table_and_discriminator_must_agree_before_construction():
    prepared = prepare(derive(Pets), pets_mapping("split"))
    Dog.calls = 0
    tables = {
        "root": [{"_sm_key": "external"}],
        "dogs": [{"_sm_key": "d", "_sm_parent": "external", "_sm_position": 0, "kind": "CAT", "dog_name": "wrong"}],
        "cats": [],
    }
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "invalid_discriminator"
    assert Dog.calls == 0


def test_prefixed_alternative_rejects_inactive_payload():
    prepared = prepare(derive(Pets), pets_mapping("prefixed"))
    tables = decompose(Pets([Dog("active")]), prepared)
    row = tables["pets"][0]
    row["cat_name"] = "inactive"
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "invalid_variant_payload"


def test_repeated_split_null_has_its_own_role_and_does_not_collapse_with_absence():
    prepared = prepare(derive(NullablePets), nullable_pets_mapping())
    supplied = {
        "root": [{"_sm_key": "root"}],
        "dogs": [],
        "cats": [],
        "null_values": [{
            "_sm_key": "n", "_sm_parent": "root", "_sm_position": 7,
            "kind": None, "_sm_null": True,
        }],
    }
    rebuilt = reconstruct(supplied, prepared)
    assert rebuilt.pets == [None]
    supplied["null_values"] = []
    assert reconstruct(supplied, prepared).pets == []


def test_all_null_selected_records_keep_their_independent_discriminators():
    mapping = MappingSpec(overrides={"pets": UseOverride(
        variant_layout="split",
        discriminator={
            "strategy": "recorded", "column": "kind",
            "values": {"AllNullDog": "DOG", "AllNullCat": "CAT"},
        },
        alternatives={
            "AllNullDog": AlternativeLayout(table="dog_nulls", prefix="dog"),
            "AllNullCat": AlternativeLayout(table="cat_nulls", prefix="cat"),
        },
    )})
    prepared = prepare(derive(AllNullPets), mapping)
    supplied = {
        "root": [{"_sm_key": "root"}],
        "dog_nulls": [{
            "_sm_key": "d", "_sm_parent": "root", "_sm_position": 4,
            "kind": "DOG", "dog_label": None,
        }],
        "cat_nulls": [{
            "_sm_key": "c", "_sm_parent": "root", "_sm_position": 1,
            "kind": "CAT", "cat_label": None,
        }],
    }
    rebuilt = reconstruct(supplied, prepared)
    assert rebuilt.pets == [AllNullCat(None), AllNullDog(None)]


def test_inactive_alternative_descendants_are_rejected_before_construction():
    mapping = MappingSpec(overrides={
        "items": UseOverride(
            variant_layout="prefixed",
            discriminator={
                "strategy": "recorded", "column": "kind",
                "values": {"DogWithChildren": "DOG", "Leaf": "LEAF"},
            },
            alternatives={
                "DogWithChildren": AlternativeLayout(prefix="dog"),
                "Leaf": AlternativeLayout(prefix="leaf"),
            },
        ),
        "items/children": UseOverride(table="dog_children"),
    })
    prepared = prepare(derive(ChoiceEnvelope), mapping)
    tables = decompose(ChoiceEnvelope([Leaf(None)]), prepared)
    item_key = tables["items"][0]["_sm_key"]
    tables["dog_children"].append({
        "_sm_key": "inactive-child",
        "_sm_parent": item_key,
        "_sm_position": 0,
        "label": "not selected",
    })
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "invalid_variant_payload"


def test_real_alternative_named_null_cannot_collide_with_metadata_null_role():
    mapping = MappingSpec(overrides={"pets": UseOverride(
        variant_layout="split",
        discriminator={
            "strategy": "recorded", "column": "kind",
            "values": {"Null": "NULL", "AllNullDog": "DOG"},
        },
    )})
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(NullAlternativePets), mapping)
    assert caught.value.code == "table_collision"


def test_source_discriminator_is_rejected_with_recorded_strategy_remedy():
    mapping = MappingSpec(overrides={"pets": UseOverride(
        discriminator=SourceDiscriminator(field="kind"),
    )})
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(NullAlternativePets), mapping)
    assert caught.value.code == "unavailable_capability"
    assert "strategy='recorded'" in caught.value.remedy


def test_unknown_native_runtime_types_cannot_extend_variant_schema():
    prepared = prepare(derive(NullAlternativePets), MappingSpec(overrides={
        "pets": UseOverride(variant_layout="split", alternatives={
            "$null": AlternativeLayout(table="null_values"),
        }),
    }))
    with pytest.raises(StructureMapError) as caught:
        decompose(NullAlternativePets([object()]), prepared)
    assert caught.value.code == "unknown_variant"


def test_explicit_alternative_table_collisions_fail_during_preparation():
    mapping = MappingSpec(overrides={"first/children": UseOverride(
        variant_layout="split",
        alternatives={
            "Branch": AlternativeLayout(table="shared_rows"),
            "Leaf": AlternativeLayout(table="shared_rows"),
        },
    )})
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(VariantUses), mapping)
    assert caught.value.code == "table_collision"


def test_detached_cyclic_component_in_inactive_variant_is_rejected():
    mapping = MappingSpec(overrides={
        "first": UseOverride(variant_layout="split", alternatives={
            "Branch": AlternativeLayout(table="first_branches", prefix="branch"),
            "Leaf": AlternativeLayout(table="first_leaves", prefix="leaf"),
        }),
        "first/children": UseOverride(variant_layout="split", alternatives={
            "Branch": AlternativeLayout(table="branch_children", prefix="branch"),
            "Leaf": AlternativeLayout(table="leaf_children", prefix="leaf"),
        }),
    })
    prepared = prepare(derive(VariantUses), mapping)
    tables = decompose(VariantUses([Leaf("active")], []), prepared)
    cycle_table = next(
        name
        for name, plan in prepared.tables.items()
        if plan.source_path.startswith("first/children") and name in plan.parent_tables
    )
    cycle_plan = prepared.tables[cycle_table]
    variant_column = next(column for column in cycle_plan.columns if column.startswith("_sm_variant"))

    def cycle_row(key: str, parent: str) -> dict[str, Cell]:
        row = {column: None for column in cycle_plan.columns}
        row.update({
            "_sm_key": key,
            "_sm_parent": parent,
            "_sm_position": 0,
            "branch_label": key,
            variant_column: "Branch",
        })
        if prepared.parent_table_column in cycle_plan.columns:
            row[prepared.parent_table_column] = cycle_table
        return row

    tables[cycle_table] = [
        cycle_row("cycle-a", "cycle-b"),
        cycle_row("cycle-b", "cycle-a"),
    ]
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "unattached_row"


def test_variant_object_reuse_is_distinct_occurrences_but_ancestry_cycles_fail():
    prepared = prepare(derive(VariantUses))
    shared = Branch("shared")
    rebuilt = reconstruct(decompose(VariantUses([shared, shared], []), prepared), prepared)
    assert rebuilt.first == [Branch("shared"), Branch("shared")]
    assert rebuilt.first[0] is not rebuilt.first[1]

    shared.children.append(shared)
    with pytest.raises(StructureMapError) as caught:
        decompose(VariantUses([shared], []), prepared)
    assert caught.value.code == "containment_cycle"


@pytest.mark.parametrize("layout", ["split", "prefixed"])
def test_boolean_integer_and_null_occurrences_keep_native_selection(layout):
    prepared = prepare(derive(BooleanOrInteger), MappingSpec(defaults={"variant_layout": layout}))
    source = BooleanOrInteger([True, 1, None, False, 0, None])
    supplied = decompose(source, prepared)
    result = reconstruct({name: list(reversed(rows)) for name, rows in supplied.items()}, prepared)
    assert [type(item) for item in result.values] == [bool, int, type(None), bool, int, type(None)]
    assert result.values == [True, 1, None, False, 0, None]


def test_prefixed_recursive_variants_keep_finite_table_overrides():
    mapping = MappingSpec(defaults={"variant_layout": "prefixed"}, overrides={
        "children": UseOverride(table="recursive_children"),
        "children/children": UseOverride(table="deeper_children"),
    })
    prepared = prepare(derive(RecursiveChoice), mapping)
    source = RecursiveChoice([RecursiveChoice([Leaf("deep")])])
    assert {"recursive_children", "deeper_children"} <= set(prepared.tables)
    assert reconstruct(decompose(source, prepared), prepare(derive(RecursiveChoice), mapping)) == source


def test_same_recursive_choice_uses_can_have_distinct_and_finite_deeper_table_overrides():
    mapping = MappingSpec(overrides={
        "first": UseOverride(variant_layout="split", alternatives={
            "Branch": AlternativeLayout(table="first_branches"),
            "Leaf": AlternativeLayout(table="first_leaves"),
        }),
        "second": UseOverride(variant_layout="split", alternatives={
            "Branch": AlternativeLayout(table="second_branches"),
            "Leaf": AlternativeLayout(table="second_leaves"),
        }),
        "first/children": UseOverride(variant_layout="split", alternatives={
            "Branch": AlternativeLayout(table="deep_branches"),
            "Leaf": AlternativeLayout(table="deep_leaves"),
        }),
    })
    prepared = prepare(derive(VariantUses), mapping)
    assert {"first_branches", "first_leaves", "second_branches", "second_leaves",
            "deep_branches", "deep_leaves"} <= set(prepared.tables)
    native = VariantUses([Branch("outer", [Branch("nested")])], [])
    assert reconstruct(decompose(native, prepared), prepare(derive(VariantUses), mapping)) == native


@pytest.mark.parametrize("root_type", [OverrideRoot, OverrideRootReversed])
def test_override_resolution_keeps_later_alternative_matches(root_type):
    mapping = MappingSpec(overrides={"items/detail/label": UseOverride(column="renamed")})
    prepared = prepare(derive(root_type), mapping)
    source = root_type([OverrideA(OverrideX("x")), OverrideB(OverrideY("y"))])
    rows = decompose(source, prepared)
    assert any(row.get("renamed") == "y" for table_rows in rows.values() for row in table_rows)
    assert reconstruct(rows, prepare(derive(root_type), mapping)) == source


def test_inline_null_does_not_emit_split_variant_metadata():
    prepared = prepare(derive(NullableDetailRoot))
    rows = decompose(NullableDetailRoot(None), prepared)
    assert all(
        set(row) == set(prepared.tables[name].columns)
        for name, table_rows in rows.items()
        for row in table_rows
    )
    supplied = {name: [] for name in prepared.tables}
    supplied[prepared.root_table] = [
        {column: None for column in prepared.tables[prepared.root_table].columns}
    ]
    supplied[prepared.root_table][0][prepared.key_column] = "root"
    assert reconstruct(supplied, prepared) == NullableDetailRoot(None)


@pytest.mark.parametrize("item_type", [SharedA, SharedB])
def test_nested_shared_definitions_keep_variant_branch_identity(item_type):
    prepared = prepare(derive(SharedRoot))
    source = SharedRoot(item_type(SharedTags(["one", "two"])))
    rows = decompose(source, prepared)
    assert reconstruct(rows, prepared) == source

def test_inactive_shared_variant_descendants_are_rejected():
    prepared = prepare(derive(SharedRoot))
    rows = decompose(SharedRoot(SharedA(SharedTags(["active"]))), prepared)
    tables = [
        name for name, plan in prepared.tables.items()
        if plan.source_path == "item/shared/tags"
    ]
    assert len(tables) == 2
    inactive = next(name for name in tables if not rows[name])
    plan = prepared.tables[inactive]
    injected = {column: None for column in plan.columns}
    injected[prepared.key_column] = "inactive"
    injected[prepared.parent_column] = rows[prepared.root_table][0][prepared.key_column]
    injected[prepared.position_column] = 0
    business = next(column for column in plan.columns if not column.startswith("_sm_"))
    injected[business] = "invalid"
    rows[inactive].append(injected)
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code == "invalid_variant_payload"


@pytest.mark.parametrize("layout", ["split", "prefixed"])
def test_recursive_variant_traversal_supports_both_layouts(layout):
    mapping = MappingSpec(defaults={"variant_layout": layout})
    prepared = prepare(derive(RecursiveChoice), mapping)
    root = RecursiveChoice([])
    current = root
    for _ in range(sys.getrecursionlimit() + 20):
        child = RecursiveChoice([])
        current.children.append(child)
        current = child
    tables = decompose(root, prepared)
    rebuilt = reconstruct(tables, prepare(derive(RecursiveChoice), mapping))
    count = 0
    current = rebuilt
    while current.children:
        child = current.children[0]
        assert type(child) is RecursiveChoice
        count += 1
        current = child
    assert count == sys.getrecursionlimit() + 20


@pytest.mark.parametrize("branch_name", ["NestedA", "NestedB"])
def test_nested_split_collections_keep_enclosing_alternative_identity(branch_name):
    from tests.fixtures.variants import NestedA, NestedB, NestedVariants, SharedChoices

    branch = NestedA if branch_name == "NestedA" else NestedB
    source = NestedVariants(branch(SharedChoices([1, "two", None])))
    prepared = prepare(derive(NestedVariants))
    rows = decompose(source, prepared)
    rebuilt = reconstruct(
        {name: list(reversed(values)) for name, values in rows.items()},
        prepare(derive(NestedVariants)),
    )
    assert type(rebuilt.item) is branch
    assert rebuilt.item.shared.values == [1, "two", None]
