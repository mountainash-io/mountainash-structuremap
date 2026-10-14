from dataclasses import InitVar, dataclass, field, make_dataclass

import pytest
from pydantic import ValidationError

from mountainash_structuremap import (
    MappingSpec,
    Naming,
    StructureMapError,
    UseOverride,
    decompose,
    derive,
    prepare,
    reconstruct,
)
from tests.fixtures.models import Address, Document, Person
from tests.fixtures.topology import Nested, Node


def test_two_mappings_and_later_configuration_edits_do_not_change_prepared_outputs():
    model = derive(Document)
    ordinary = prepare(model)
    mapping = MappingSpec(
        defaults={"naming": Naming(metadata_namespace="recon_")},
        overrides={
            "people": UseOverride(table="persons"),
            "people/name": UseOverride(column="display_name"),
            "people/home": UseOverride(prefix="residence"),
        },
    )
    configured = prepare(model, mapping)
    mapping.overrides["people/name"] = UseOverride(column="changed")
    value = Document([Person("Ada", Address("Oslo"), [""])])
    rows = decompose(value, configured)
    assert rows["persons"][0]["display_name"] == "Ada"
    assert rows["persons"][0]["residence_city"] == "Oslo"
    assert rows["persons"][0]["recon_parent"] == rows["root"][0]["recon_key"]
    assert reconstruct(rows, configured) == value
    assert decompose(value, ordinary)["people"][0]["name"] == "Ada"


def test_anonymous_descendant_tables_follow_the_explicit_collection_name():
    prepared = prepare(derive(Nested), MappingSpec(overrides={"grid": UseOverride(table="matrix")}))
    value = Nested([[1], []], {}, ("x", 2))
    tables = decompose(value, prepared)
    assert [row["value"] for row in tables["matrix__item"]] == [1]
    assert reconstruct(tables, prepared) == value


def test_finite_recursive_override_applies_only_at_its_named_use():
    value = Node("zero", [Node("one", [Node("two", [Node("three", [Node("four")])])])])
    mapping = MappingSpec(overrides={"children/children/label": UseOverride(column="special")})
    prepared = prepare(derive(Node), mapping)
    tables = decompose(value, prepared)
    special = [row["special"] for rows in tables.values() for row in rows if "special" in row]
    ordinary = [row["label"] for rows in tables.values() for row in rows if "label" in row]
    assert special == ["two"]
    assert sorted(ordinary) == ["four", "one", "three", "zero"]
    assert reconstruct(tables, prepare(derive(Node), mapping)) == value


@pytest.mark.parametrize(
    "overrides,code,path",
    [
        ({"people/misspelled": {"column": "x"}}, "unknown_path", "people/misspelled"),
        ({"people/name": {"table": "x"}}, "inapplicable_override", "people/name"),
        ({"people/name": {"column": "_sm_key"}}, "column_collision", "people/name"),
        (
            {"people/name": {"column": "same"}, "people/home/city": {"column": "same"}},
            "column_collision",
            "people/home/city",
        ),
        ({"people": {"table": "same"}, "people/tags": {"table": "same"}}, "table_collision", "people/tags"),
    ],
)
def test_invalid_mapping_paths_and_targets_fail_before_execution(overrides, code, path):
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Document), MappingSpec(overrides=overrides))
    assert caught.value.code == code
    assert caught.value.path == path


@pytest.mark.parametrize(
    "defaults",
    [
        {"presence": "preserve"},
        {"positions": "lenient"},
        {"construction": "unvalidated"},
        {"variant_layout": "eav"},
        {"role_discriminator": "combined"},
        {"shared_types": "widen"},
        {"value_space": "lexical"},
        {"json_numbers": "exact"},
        {"duplicate_members": "preserve"},
        {"open_content": "structured"},
    ],
)
def test_declared_unavailable_global_policies_fail_during_preparation(defaults):
    mapping = MappingSpec(defaults=defaults)
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Document), mapping)
    assert caught.value.code == "unavailable_capability"


@pytest.mark.parametrize(
    "path,policy",
    [
        ("people", {"key": {"strategy": "source", "columns": ["name"]}}),
        ("people", {"ordered": False}),
        ("people", {"collection_table": {"strategy": "shared", "table": "shared", "role_column": "role"}}),
        ("people/name", {"cast": {"strategy": "type", "to": "date"}}),
        ("people/age", {"cast": {"strategy": "type", "to": "i64"}}),
        ("people/name", {"cast": {"strategy": "custom", "forward": {"column": "name"}}}),
        ("people/name", {"presence": "preserve"}),
    ],
)
def test_declared_unavailable_use_policies_identify_their_path(path, policy):
    mapping = MappingSpec(overrides={path: policy})
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Document), mapping)
    assert caught.value.code == "unavailable_capability"
    assert caught.value.path == path


def test_unknown_canonical_option_is_not_silently_ignored():
    with pytest.raises(ValidationError) as caught:
        MappingSpec(overrides={"people": {"invented": True}})
    assert any(
        error["type"] == "extra_forbidden" and error["loc"] == ("overrides", "people", "invented")
        for error in caught.value.errors()
    )


def test_plain_dataclass_validation_is_not_advertised_as_native_validation():
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Document), validate_native=True)
    assert caught.value.code == "unavailable_capability"



@pytest.mark.parametrize("constructor", ["init_false", "initvar", "custom"])
def test_unsupported_constructors_are_refused_during_preparation_not_derivation(constructor):
    if constructor == "init_false":

        @dataclass
        class Model:
            value: int = field(init=False, default=1)
    elif constructor == "initvar":

        @dataclass
        class Model:
            value: int
            context: InitVar[str]
    else:

        @dataclass(init=False)
        class Model:
            value: int

            def __init__(self, value, context):
                raise AssertionError("Preparation must not construct instances")

    model = derive(Model)
    with pytest.raises(StructureMapError) as caught:
        prepare(model)
    assert caught.value.code == "unsupported_constructor"


def test_optional_constructor_only_arguments_keep_their_native_defaults():
    @dataclass
    class Model:
        value: int
        context: InitVar[str] = "native-default"

        def __post_init__(self, context):
            if context != "native-default":
                raise ValueError("wrong native default")

    prepared = prepare(derive(Model))
    assert reconstruct({"root": [{"_sm_key": "external", "value": 7}]}, prepared) == Model(7)


def test_native_constructor_exception_is_preserved_as_the_cause():
    @dataclass
    class Model:
        value: int

        def __post_init__(self):
            if self.value < 0:
                raise ValueError("negative")

    with pytest.raises(StructureMapError) as caught:
        reconstruct({"root": [{"_sm_key": "external", "value": -1}]}, prepare(derive(Model)))
    assert caught.value.code == "native_construct_failed"
    assert isinstance(caught.value.__cause__, ValueError)


def test_equal_class_names_in_different_modules_reconstruct_their_actual_types():
    from tests.fixtures.models import Address as First
    from tests.fixtures.topology import Address as Second

    @dataclass
    class Model:
        first: First
        second: Second

    value = Model(First("Oslo"), Second("Kyoto", ["work"]))
    prepared = prepare(derive(Model))
    assert reconstruct(decompose(value, prepared), prepared) == value


def test_distinct_classes_with_one_qualified_name_fail_instead_of_aliasing():
    first = make_dataclass("Duplicate", [("value", int)], namespace={"__module__": __name__})
    second = make_dataclass("Duplicate", [("text", str)], namespace={"__module__": __name__})
    root = make_dataclass("Root", [("first", first), ("second", second)], namespace={"__module__": __name__})
    with pytest.raises(StructureMapError) as caught:
        derive(root)
    assert caught.value.code == "definition_collision"
    assert caught.value.path == "second"


def test_unrelated_metadata_and_explicit_plain_binding_do_not_claim_xml():
    @dataclass
    class Plain:
        class Meta:
            description = "not XML metadata"

        value: str

    @dataclass
    class Marked:
        value: str = field(metadata={"type": "Element"})

    plain = prepare(derive(Plain))
    assert reconstruct(decompose(Plain("x"), plain), plain) == Plain("x")
    explicit = prepare(derive(Marked, binding="dataclass"))
    assert reconstruct(decompose(Marked("x"), explicit), explicit) == Marked("x")


def test_literal_names_root_rename_and_scalar_collection_projection():
    mapping = MappingSpec(
        defaults={"binding": {"strategy": "declared", "name": "dataclass"}},
        overrides={
            "": {"table": "DocumentRows"},
            "people": {"key": "generated", "positions": "strict", "ordered": True, "collection_table": "separate"},
            "people/name": {"column": "DisplayName"},
            "people/home": {"prefix": "ResidenceX"},
            "people/tags": {"column": "TagText"},
        },
    )
    value = Document([Person("Ada", Address("Oslo"), ["x", None])])
    prepared = prepare(derive(Document), mapping)
    rows = decompose(value, prepared)
    assert rows["people"][0]["DisplayName"] == "Ada"
    assert rows["people"][0]["ResidenceX_city"] == "Oslo"
    assert rows["people"][0]["_sm_parent"] == rows["DocumentRows"][0]["_sm_key"]
    assert [row["TagText"] for row in rows["people__tags"]] == ["x", None]
    assert reconstruct(rows, prepared) == value


def test_declared_mapping_binding_cannot_silently_reinterpret_the_model():
    mapping = MappingSpec(defaults={"binding": {"strategy": "declared", "name": "xsdata"}})
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Document), mapping)
    assert caught.value.code == "binding_mismatch"


def test_non_string_map_declaration_fails_with_a_configuration_error():
    @dataclass
    class Model:
        values: dict[int, str]

    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Model))
    assert caught.value.code == "unavailable_construct"
    assert caught.value.path == "values"


def test_named_overrides_cross_anonymous_collection_levels():
    @dataclass
    class Grid:
        cells: list[list[Address]]

    mapping = MappingSpec(
        overrides={
            "cells": {"table": "matrix"},
            "cells/city": {"column": "City"},
        }
    )
    value = Grid([[Address("Oslo")], []])
    prepared = prepare(derive(Grid), mapping)
    tables = decompose(value, prepared)
    assert tables["matrix__item"][0]["City"] == "Oslo"
    assert reconstruct(tables, prepared) == value


def test_explicit_normalization_and_root_prefix_are_supported():
    prepared = prepare(
        derive(Address),
        MappingSpec(
            overrides={
                "": {"prefix": "RootX", "presence": "normalize"},
            }
        ),
    )
    tables = decompose(Address("Oslo"), prepared)
    assert tables["root"][0]["RootX_city"] == "Oslo"
    assert reconstruct(tables, prepared) == Address("Oslo")


def test_singular_recursive_table_override_is_scoped_to_its_use():
    from tests.fixtures.topology import Branch

    value = Branch("root", Branch("one", Branch("two")))
    mapping = MappingSpec(overrides={"left": {"table": "first_left"}})
    prepared = prepare(derive(Branch), mapping)
    tables = decompose(value, prepared)
    assert [row["label"] for row in tables["first_left"]] == ["one"]
    assert reconstruct(tables, prepared) == value


def test_renaming_a_collection_keeps_named_descendant_use_names():
    prepared = prepare(derive(Document), MappingSpec(overrides={"people": {"table": "persons"}}))
    tables = decompose(Document([Person("Ada", Address("Oslo"), ["x"])]), prepared)
    assert tables["people__tags"][0]["value"] == "x"
    assert tables["people__tags"][0]["_sm_parent"] == tables["persons"][0]["_sm_key"]


@pytest.mark.parametrize("column", ["_sm_parent_table", "_sm_null", "_sm_map_key"])
def test_inactive_metadata_names_remain_business_columns(column):
    value = Document([Person("Ada", Address("Oslo"), [])])
    prepared = prepare(derive(Document), MappingSpec(overrides={"people/name": {"column": column}}))
    tables = decompose(value, prepared)
    assert tables["people"][0][column] == "Ada"
    assert reconstruct(tables, prepared) == value


@pytest.mark.parametrize("overrides", [{}, {"children/children/label": {"column": "special"}}])
def test_ordinary_recursive_role_names_do_not_depend_on_depth_first_discovery(overrides):
    @dataclass
    class TaggedNode:
        label: str
        children: list["TaggedNode"]
        tags: list[str]

    value = TaggedNode("root", [TaggedNode("child", [], ["child-tag"])], ["root-tag"])
    prepared = prepare(derive(TaggedNode), MappingSpec(overrides=overrides))
    tables = decompose(value, prepared)
    assert sorted(row["value"] for row in tables["tags"]) == ["child-tag", "root-tag"]
    assert reconstruct(tables, prepared) == value


def test_sibling_singular_recursive_roles_keep_their_own_names():
    from tests.fixtures.topology import Branch

    value = Branch("root", Branch("left"), Branch("right"))
    prepared = prepare(derive(Branch))
    tables = decompose(value, prepared)
    assert [row["label"] for row in tables["left"]] == ["left"]
    assert [row["label"] for row in tables["right"]] == ["right"]
    assert reconstruct(tables, prepared) == value
