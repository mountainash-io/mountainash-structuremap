import sys
from dataclasses import dataclass, field

import pytest

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct
from tests.fixtures.topology import A, Address, B, Branch, Containers, Detail, Forest, Members, Nested, Node, Uses


def test_ragged_members_and_map_insertion_order_survive_reordered_delivery():
    value = Nested([[], None, [None], [1, None, 1]], {"z": [2], "": [], "a/b": [None, 3]}, ("x", 2))
    tables = decompose(value, prepare(derive(Nested)))
    assert len(tables["grid"]) == 4
    assert [row["_sm_null"] for row in tables["grid"]] == [False, True, False, False]
    delivered = {name: list(reversed(rows)) for name, rows in reversed(list(tables.items()))}
    result = reconstruct(delivered, prepare(derive(Nested)))
    assert result == Nested([[], None, [None], [1, None, 1]], {"z": [2], "": [], "a/b": [None, 3]}, ("x", 2))
    assert list(result.lookup) == ["z", "", "a/b"]


def test_shared_instance_is_two_occurrences_but_ancestry_cycle_fails():
    leaf = Node("leaf")
    value = Node("root", [leaf, leaf])
    prepared = prepare(derive(Node))
    rebuilt = reconstruct(decompose(value, prepared), prepare(derive(Node)))
    assert rebuilt == Node("root", [Node("leaf"), Node("leaf")])
    assert rebuilt.children[0] is not rebuilt.children[1]
    value.children.append(value)
    with pytest.raises(StructureMapError) as caught:
        decompose(value, prepared)
    assert caught.value.code == "containment_cycle"
    assert caught.value.path


def test_mutual_recursion_retains_singular_and_repeated_links():
    value = A("root", B(2, [A("leaf", B(3)), A("other")]))
    tables = decompose(value, prepare(derive(A)))
    assert reconstruct(tables, prepare(derive(A))) == A("root", B(2, [A("leaf", B(3)), A("other")]))


def test_nullable_recursive_roles_do_not_mix_left_and_right():
    value = Branch("root", Branch("left", right=Branch("left-right")), Branch("right"))
    prepared = prepare(derive(Branch))
    tables = decompose(value, prepared)
    assert "_sm_position" not in prepared.tables["left"].columns
    assert "_sm_parent_table" in prepared.tables["left"].columns
    assert reconstruct(tables, prepare(derive(Branch))) == value


def test_null_record_member_is_distinct_from_present_all_null_member():
    prepared = prepare(derive(Members))
    tables = decompose(Members([Detail(None), None, Detail(None, ["child"])]), prepared)
    assert [row["_sm_null"] for row in tables["entries"]] == [False, True, False]
    assert reconstruct(tables, prepare(derive(Members))) == Members([Detail(None), None, Detail(None, ["child"])])
    tables["entries"][2]["_sm_null"] = True
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.table == "entries"
    assert caught.value.row == 2


def test_reused_definitions_keep_distinct_collection_uses():
    value = Uses(Address("Oslo", ["home"]), Address("Kyoto", ["work"]))
    prepared = prepare(derive(Uses))
    tables = decompose(value, prepared)
    assert [row["value"] for row in tables["home__tags"]] == ["home"]
    assert [row["value"] for row in tables["work__tags"]] == ["work"]
    assert reconstruct(tables, prepare(derive(Uses))) == value


def test_recursive_component_reuse_keeps_separate_families():
    value = Forest(Node("home", [Node("h1", [Node("h2")])]), Node("work", [Node("w1")]))
    tables = decompose(value, prepare(derive(Forest)))
    assert [row["label"] for row in tables["home__children"]] == ["h1", "h2"]
    assert [row["label"] for row in tables["work__children"]] == ["w1"]
    assert reconstruct(tables, prepare(derive(Forest))) == value


def test_nested_maps_and_tuples_preserve_empty_and_null_members():
    value = Containers([{}, None, {"x/y": 2}], [(None, []), None, ("", [1, 2])], {"": None, "a/b": Detail(None)})
    tables = decompose(value, prepare(derive(Containers)))
    assert reconstruct(tables, prepare(derive(Containers))) == value


def test_frozen_slotted_keyword_only_inheritance_keeps_captured_factory_output():
    calls = 0

    def token():
        nonlocal calls
        calls += 1
        return f"token-{calls}"

    @dataclass(frozen=True, slots=True, kw_only=True)
    class Base:
        name: str

    @dataclass(frozen=True, slots=True, kw_only=True)
    class Child(Base):
        captured: str = field(default_factory=token)

    prepared = prepare(derive(Child))
    assert calls == 0
    source = Child(name="Ada")
    rebuilt = reconstruct(decompose(source, prepared), prepare(derive(Child)))
    assert rebuilt == Child(name="Ada", captured="token-1")
    assert calls == 1


def test_recursive_spine_exceeds_python_recursion_depth():
    depth = sys.getrecursionlimit() + 100
    value = Node(str(depth))
    for index in reversed(range(depth)):
        value = Node(str(index), [value])
    tables = decompose(value, prepare(derive(Node)))
    rebuilt = reconstruct(tables, prepare(derive(Node)))
    for index in range(depth):
        assert rebuilt.label == str(index)
        assert len(rebuilt.children) == 1
        rebuilt = rebuilt.children[0]
    assert rebuilt.label == str(depth)
    assert rebuilt.children == []


@pytest.mark.parametrize(
    "value,path",
    [
        (Nested([], {1: []}, ("x", 2)), "lookup"),
        (Nested([], {}, ("x",)), "coordinates"),
    ],
)
def test_native_map_keys_and_tuple_arity_fail_at_the_source_path(value, path):
    with pytest.raises(StructureMapError) as caught:
        decompose(value, prepare(derive(Nested)))
    assert caught.value.path == path
    assert caught.value.owner == "executor restriction"


def test_inherited_annotations_keep_the_declaring_class_namespace():
    from tests.fixtures.postponed_models import DerivedAlias

    prepared = prepare(derive(DerivedAlias))
    supplied = {"root": [{"_sm_key": "external", "value": 7, "label": "seven"}]}
    rebuilt = reconstruct(supplied, prepared)
    assert rebuilt == DerivedAlias(7, "seven")
    row = decompose(rebuilt, prepared)["root"][0]
    assert (row["value"], row["label"]) == (7, "seven")


def test_empty_fixed_tuple_is_preserved_inline_and_as_a_member():
    @dataclass
    class EmptyTuples:
        inline: tuple[()]
        entries: list[tuple[()] | None]

    prepared = prepare(derive(EmptyTuples))
    tables = decompose(EmptyTuples((), [(), None, ()]), prepared)
    assert reconstruct(tables, prepared) == EmptyTuples((), [(), None, ()])


def test_one_detached_recursive_row_cannot_hide_behind_the_root():
    prepared = prepare(derive(Node))
    tables = {
        "root": [{"_sm_key": "root", "label": "root"}],
        "children": [
            {
                "_sm_key": "detached",
                "_sm_parent": "detached",
                "_sm_parent_table": "children",
                "_sm_position": 0,
                "label": "detached",
            }
        ],
    }
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "unattached_row"
    assert caught.value.table == "children"
    assert caught.value.row == 0


def test_inactive_metadata_names_remain_business_columns_on_null_members():
    @dataclass
    class Payload:
        _sm_map_key: str | None

    @dataclass
    class Root:
        entries: list[Payload | None]

    prepared = prepare(derive(Root))
    tables = decompose(Root([Payload("business"), None]), prepared)
    assert tables["entries"][1]["_sm_map_key"] is None
    assert reconstruct(tables, prepared) == Root([Payload("business"), None])
    tables["entries"][0]["_sm_null"] = True
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "contradictory_null_member"
    assert caught.value.table == "entries"
