from dataclasses import dataclass

from mountainash_structuremap import (
    MappingRef,
    MappingSpec,
    MappingUse,
    UseOverride,
    decompose,
    derive,
    prepare,
    reconstruct,
)
from mountainash_structuremap.errors import StructureMapError
from tests.fixtures.topology import A, Address, B, Branch, Uses


def test_mapping_reference_overlay_is_local_and_reusable_in_two_roles():
    shared = MappingUse(component="core.record", children={"city": MappingUse(component="core.value")})
    mapping = MappingSpec(
        use=MappingUse(component="core.record", children={
            "home": MappingRef(ref="Address", config={"prefix": "home"}),
            "work": MappingRef(ref="Address", config={"prefix": "work"}),
        }),
        definitions={"Address": shared},
    )
    value = Uses(Address("Oslo", ["h"]), Address("Kyoto", ["w"]))
    prepared = prepare(derive(Uses), mapping)
    emitted = decompose(value, prepared)["root"][0]
    assert emitted["home_city"] == "Oslo"
    assert emitted["work_city"] == "Kyoto"
    assert mapping.definitions["Address"] == shared


def test_reused_mapping_reconstructs_shuffled_independent_memberships():
    mapping = MappingSpec(
        use=MappingUse(component="core.record", children={
            "home": MappingRef(ref="address", config={"prefix": "home"}),
            "work": MappingRef(ref="address", config={"prefix": "work"}),
        }),
        definitions={"address": MappingUse(component="core.record")},
    )
    rows = {
        "root": [{"_sm_key": "document", "home_city": "Oslo", "work_city": "Kyoto"}],
        "home__tags": [
            {"_sm_key": "h2", "_sm_parent": "document", "_sm_position": 9, "value": "duplicate"},
            {"_sm_key": "h1", "_sm_parent": "document", "_sm_position": 3, "value": "duplicate"},
        ],
        "work__tags": [
            {"_sm_key": "w2", "_sm_parent": "document", "_sm_position": 7, "value": "second"},
            {"_sm_key": "w1", "_sm_parent": "document", "_sm_position": 1, "value": "first"},
        ],
    }
    expected = Uses(Address("Oslo", ["duplicate", "duplicate"]), Address("Kyoto", ["first", "second"]))
    assert reconstruct(rows, prepare(derive(Uses), mapping)) == expected


def test_mapping_reference_alias_cycle_fails_with_resolution_error():
    mapping = MappingSpec(use=MappingRef(ref="A"), definitions={"A": MappingRef(ref="B"), "B": MappingRef(ref="A")})
    try:
        prepare(derive(Uses), mapping)
    except StructureMapError as error:
        assert error.code == "mapping_cycle"
        assert "A" in str(error) and "B" in str(error)
    else:
        raise AssertionError("expected alias-only mapping cycle to fail")


def test_missing_mapping_reference_names_target_and_path():
    mapping = MappingSpec(use=MappingRef(ref="missing"))
    try:
        prepare(derive(Uses), mapping)
    except StructureMapError as error:
        assert error.code == "missing_mapping"
        assert error.path == ""
        assert "missing" in str(error)
        assert error.remedy
    else:
        raise AssertionError("expected an unresolved mapping reference to fail")


def test_recursive_definition_mapping_reference_is_finite_with_deep_path_override():
    mapping = MappingSpec(
        use=MappingRef(ref="Branch"),
        definitions={"Branch": MappingUse(component="core.record", children={
            "left": MappingRef(ref="Branch"), "right": MappingRef(ref="Branch"),
        })},
        overrides={"left/left/label": UseOverride(column="deep_label")},
    )
    node = None
    for index in reversed(range(40)):
        node = Branch(str(index), left=node)
    prepared = prepare(derive(Branch), mapping)
    rows = decompose(node, prepared)
    assert any(row.get("deep_label") == "2" for table_rows in rows.values() for row in table_rows)
    assert reconstruct(rows, prepared) == node


def test_mutually_recursive_mapping_references_retain_distinct_roles():
    mapping = MappingSpec(
        use=MappingRef(ref="A"),
        definitions={
            "A": MappingUse(component="core.record", children={"other": MappingRef(ref="B")}),
            "B": MappingUse(component="core.record", children={"others": MappingUse(component="core.sequence", children={"item": MappingRef(ref="A")})}),
        },
    )
    value = A("root", B(2, [A("leaf", B(3)), A("other")]))
    prepared = prepare(derive(A), mapping)
    assert reconstruct(decompose(value, prepared), prepared) == value


def test_definition_selector_precedence_is_path_specific():
    value = Uses(Address("Oslo", ["h"]), Address("Kyoto", ["w"]))
    mapping = MappingSpec(overrides={
        "@Address": UseOverride(prefix="shared"),
        "home/city": UseOverride(column="home_city"),
    })
    prepared = prepare(derive(Uses), mapping)
    emitted = decompose(value, prepared)["root"][0]
    assert emitted["home_city"] == "Oslo"
    assert emitted["shared_city"] == "Kyoto"


def test_qualified_definition_selector_resolves_exact_definition():
    @dataclass
    class Single:
        address: Address

    selector = f"@{Address.__module__}.{Address.__qualname__}"
    mapping = MappingSpec(overrides={selector: UseOverride(prefix="qualified")})
    value = Single(Address("Oslo", ["h"]))
    prepared = prepare(derive(Single), mapping)
    emitted = decompose(value, prepared)["root"][0]
    assert emitted["qualified_city"] == "Oslo"


def test_missing_definition_selector_lists_qualified_candidates():
    mapping = MappingSpec(overrides={"@Missing": UseOverride(prefix="missing")})
    try:
        prepare(derive(Uses), mapping)
    except StructureMapError as error:
        assert error.code == "unknown_selector"
        assert "Address" in str(error)
        assert error.remedy
    else:
        raise AssertionError("expected an unknown definition selector")


def test_conflicting_definition_selectors_report_both_origins():
    qualified = f"@{Address.__module__}.{Address.__qualname__}"
    mapping = MappingSpec(overrides={
        "@Address": UseOverride(prefix="short"),
        qualified: UseOverride(prefix="qualified"),
    })
    try:
        prepare(derive(Uses), mapping)
    except StructureMapError as error:
        assert error.code == "conflicting_override"
        assert "@Address" in str(error) and qualified in str(error)
        assert error.remedy
    else:
        raise AssertionError("expected equally specific selectors to conflict")


def test_ambiguous_definition_selector_reports_qualified_candidates():
    class North:
        @dataclass
        class Address:
            city: str

    class South:
        @dataclass
        class Address:
            city: str

    @dataclass
    class Pair:
        north: North.Address
        south: South.Address

    mapping = MappingSpec(overrides={"@Address": UseOverride(prefix="address")})
    try:
        prepare(derive(Pair), mapping)
    except StructureMapError as error:
        assert error.code == "ambiguous_selector"
        assert "North.Address" in str(error) and "South.Address" in str(error)
        assert error.remedy
    else:
        raise AssertionError("expected the short selector to be ambiguous")
