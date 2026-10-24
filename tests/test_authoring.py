from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from jsonschema import Draft202012Validator
from mountainash.typespec.spec import FieldDefinition, TableSchema
from mountainash.typespec.universal_types import UniversalType as U

from mountainash_structuremap import (
    AuthoringBundle,
    MappingBuilder,
    MappingSpec,
    MappingUse,
    StructureDocument,
    assemble_structure,
    decompose,
    derive,
    dump_bundle,
    dump_mapping,
    dump_structure,
    load_bundle,
    load_mapping,
    load_structure,
    mapping_schema,
    prepare,
    reconstruct,
    structure_schema,
)
from mountainash_structuremap.definitions import Member, RecordDefinition, RecordRef, Scalar, Sequence, StructureSpec
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import SharedCollection, SourceKey, TypeCast, UseOverride
from tests.fixtures.external_mapping import AddressColumns, DateParts
from tests.test_mapping_components import Order, order_case


def test_json_yaml_canonical_and_builder_routes_reconstruct_same_independent_rows() -> None:
    schemas, mapping, rows, expected = order_case()
    components = (AddressColumns(), DateParts())
    routes = [
        load_mapping(dump_mapping(mapping, format="json"), format="json"),
        load_mapping(dump_mapping(mapping, format="yaml"), format="yaml"),
        mapping,
        MappingBuilder(mapping).build(),
    ]
    for route in routes:
        prepared = prepare(derive(Order), route, relations=schemas, components=components)
        assert reconstruct(rows, prepared) == expected
        assert decompose(expected, prepared) == {
            "orders": rows["orders"],
            "lines": [dict(rows["lines"][i], line_no=n) for n, i in enumerate((1, 0, 2))],
        }


def test_json_schema_independently_accepts_and_rejects_documents() -> None:
    schema = mapping_schema()
    validator = Draft202012Validator(schema)
    assert validator.is_valid(json.loads(dump_mapping(MappingSpec(), format="json")))
    assert not validator.is_valid({"not_a_mapping_key": True})
    structure_validator = Draft202012Validator(structure_schema())
    doc = StructureDocument(namespace="orders", structure=StructureSpec(root=Scalar(type="str"), definitions={}))
    assert structure_validator.is_valid(json.loads(dump_structure(doc, format="json")))


def test_structure_documents_assemble_qualified_and_ambiguous_names() -> None:
    from mountainash_structuremap.definitions import DerivedModel

    address = RecordDefinition(name="Address", members=(Member(name="city", shape=Scalar(type="str")),))
    orders = StructureDocument(namespace="orders", structure=StructureSpec(root="Address", definitions={"Address": address}))
    unique = DerivedModel(structure=assemble_structure([orders], root="Address"),
                          native_types={"orders.Address": City}, binding="dataclass")
    selected = MappingSpec(overrides={"@Address/city": UseOverride(column="selected")})
    assert reconstruct({"root": [{"_sm_key": "r", "selected": "Oslo"}]}, prepare(unique, selected)) == City("Oslo")
    geo = StructureDocument(namespace="geo", structure=StructureSpec(root="Address", definitions={"Address": address}))
    pair = StructureDocument(namespace="app", structure=StructureSpec(root="Pair", definitions={
        "Pair": RecordDefinition(name="Pair", members=(
            Member(name="own", shape=RecordRef(definition="orders.Address")),
            Member(name="other", shape=RecordRef(definition="geo.Address")),
        )),
    }))
    model = DerivedModel(
        structure=assemble_structure([orders, geo, pair], root="app.Pair"),
        native_types={"app.Pair": CityPair, "orders.Address": City, "geo.Address": City}, binding="dataclass",
    )
    with pytest.raises(StructureMapError) as ambiguous:
        prepare(model, selected)
    assert ambiguous.value.code == "ambiguous_selector"
    assert "orders.Address" in str(ambiguous.value) and "geo.Address" in str(ambiguous.value)
    qualified = MappingSpec(overrides={"@orders.Address/city": UseOverride(column="selected")})
    assert reconstruct({"root": [{"_sm_key": "r", "selected": "Oslo", "other_city": "Kyoto"}]},
                       prepare(model, qualified)) == CityPair(City("Oslo"), City("Kyoto"))
    with pytest.raises(StructureMapError) as duplicate:
        assemble_structure([orders, orders], root="orders.Address")
    assert duplicate.value.code == "duplicate_definition"


def test_unresolved_component_document_loads_before_explicit_implementation_binding() -> None:
    unresolved = MappingSpec(use=MappingUse(component="external.implementation"))
    assert load_mapping(dump_mapping(unresolved, format="json"), format="json") == unresolved
    with pytest.raises(StructureMapError, match="external.implementation") as caught:
        prepare(derive(Order), unresolved)
    assert caught.value.code == "missing_component"
    assert caught.value.path == ""

@pytest.mark.parametrize("source,fmt", [(b'{"use":{"component":"core.record"},"use":{"component":"core.record"}}', "json"), ("use: {component: core.record}\nuse: {component: core.record}\n", "yaml")])
def test_document_loaders_reject_duplicate_keys(source: bytes | str, fmt: str) -> None:
    with pytest.raises(StructureMapError):
        load_mapping(source, format=fmt)



def test_yaml_loader_rejects_tags_and_preserves_parser_cause(tmp_path) -> None:
    marker = tmp_path / "executed"
    expression = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
    with pytest.raises(StructureMapError) as unsafe:
        load_mapping(f"!!python/object/apply:builtins.eval [{json.dumps(expression)}]", format="yaml")
    assert not marker.exists()
    assert unsafe.value.__cause__ is not None
    with pytest.raises(StructureMapError) as malformed:
        load_mapping("use: [", format="yaml")
    assert malformed.value.__cause__ is not None


def test_bundle_preserves_decimal_and_missing_value_descriptor_details() -> None:
    from mountainash import DecimalDtype

    descriptor = TableSchema(fields={
        "amount": FieldDefinition(type=U.NUMBER, dtype=DecimalDtype(12, 2), missing_values=[""]),
        "optional": FieldDefinition(type=U.STRING, missing_values=["", "N/A"]),
        "text": FieldDefinition(type=U.STRING, missing_values=[]),
    }, missing_values=[""])
    bundle = AuthoringBundle(structure=None, mapping=MappingSpec(), relations={"records": descriptor})
    for fmt in ("json", "yaml"):
        loaded = load_bundle(dump_bundle(bundle, format=fmt), format=fmt)
        assert loaded.relations["records"] == descriptor
        assert loaded.relations["records"].fields["amount"].dtype == DecimalDtype(12, 2)
        assert loaded.relations["records"].fields["amount"].missing_values == ("",)
        assert loaded.relations["records"].missing_values == [""]

def test_builder_snapshots_input_and_each_built_mapping() -> None:
    initial = MappingSpec(overrides={"city": UseOverride(column="selected")})
    builder = MappingBuilder(initial)
    initial.overrides.clear()
    first = builder.build()
    first.overrides["city"] = UseOverride(column="mutated")
    second = builder.build()
    assert decompose(City("Oslo"), prepare(derive(City), second)) == {
        "root": [{"_sm_key": "r0", "selected": "Oslo"}],
    }


def test_assembled_namespace_with_explicit_associations_reconstructs_shared_roles() -> None:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Address:
        city: str
        country: str

    @dataclass(frozen=True)
    class Directory:
        home: list[Address]
        work: list[Address]

    document = load_structure(
        """namespace: addressbook
structure:
  root: Directory
  definitions:
    Directory:
      name: Directory
      members:
        - name: home
          shape: {kind: sequence, item: {kind: record, definition: addressbook.Address}}
        - name: work
          shape: {kind: sequence, item: {kind: record, definition: addressbook.Address}}
    Address:
      name: Address
      members:
        - {name: city, shape: {kind: scalar, type: str}}
        - {name: country, shape: {kind: scalar, type: str}}
""",
        format="yaml",
    )
    from mountainash_structuremap.definitions import DerivedModel

    geo_document = StructureDocument(
        namespace="geo",
        structure=StructureSpec(root=RecordRef(definition="Address"), definitions={"Address": document.structure.definitions["Address"]}),
    )
    model = DerivedModel(
        structure=assemble_structure([document, geo_document], root="addressbook.Directory"),
        native_types={"addressbook.Directory": Directory, "addressbook.Address": Address, "geo.Address": Address},
        binding="dataclass",
    )
    mappings = (
        load_mapping(
            '{"overrides":{"home":{"key":{"strategy":"source","columns":["city"]},'
            '"collection_table":{"strategy":"shared","table":"addresses","role_column":"address_role","role":"home"}},'
            '"work":{"key":{"strategy":"source","columns":["city"]},'
            '"collection_table":{"strategy":"shared","table":"addresses","role_column":"address_role","role":"work"}}}}',
            format="json",
        ),
        load_mapping(
            "overrides:\n  home:\n    key: {strategy: source, columns: [city]}\n"
            "    collection_table: {strategy: shared, table: addresses, role_column: address_role, role: home}\n"
            "  work:\n    key: {strategy: source, columns: [city]}\n"
            "    collection_table: {strategy: shared, table: addresses, role_column: address_role, role: work}\n",
            format="yaml",
        ),
        MappingBuilder().override("home", key={"strategy": "source", "columns": ["city"]},
                                  collection_table={"strategy": "shared", "table": "addresses", "role_column": "address_role", "role": "home"})
        .override("work", key={"strategy": "source", "columns": ["city"]},
                  collection_table={"strategy": "shared", "table": "addresses", "role_column": "address_role", "role": "work"})
        .build(),
        MappingSpec(overrides={
            role: UseOverride(key=SourceKey(columns=("city",)),
                              collection_table=SharedCollection(table="addresses", role_column="address_role", role=role))
            for role in ("home", "work")
        }),
    )
    relations = {
        "root": TableSchema(fields={"_sm_key": FieldDefinition(type=U.STRING, missing_values=[])}, missing_values=[]),
        "addresses": TableSchema(fields={name: FieldDefinition(type=kind, missing_values=[]) for name, kind in (
            ("city", U.STRING), ("_sm_parent", U.STRING), ("_sm_position", U.INTEGER),
            ("address_role", U.STRING), ("country", U.STRING),
        )}, missing_values=[]),
    }
    rows = {
        "root": [{"_sm_key": "document"}],
        "addresses": [
            {"city": "Kyoto", "_sm_parent": "document", "_sm_position": 9, "address_role": "home", "country": "JP"},
            {"city": "Oslo", "_sm_parent": "document", "_sm_position": 3, "address_role": "home", "country": "NO"},
            {"city": "Oslo", "_sm_parent": "document", "_sm_position": 0, "address_role": "work", "country": "NO"},
        ],
    }
    expected = Directory([Address("Oslo", "NO"), Address("Kyoto", "JP")], [Address("Oslo", "NO")])
    for mapping in mappings:
        bundle = load_bundle(dump_bundle(AuthoringBundle(document, mapping, relations), format="json"), format="json")
        plan = prepare(model, bundle.mapping, relations=bundle.relations)
        assert reconstruct(rows, plan) == expected
        assert {name: table.table_schema for name, table in plan.tables.items()} == relations
        assert decompose(expected, plan) == {
            "root": [{"_sm_key": "r0"}],
            "addresses": [
                {"city": "Oslo", "_sm_parent": "r0", "_sm_position": 0, "address_role": "home", "country": "NO"},
                {"city": "Kyoto", "_sm_parent": "r0", "_sm_position": 1, "address_role": "home", "country": "JP"},
                {"city": "Oslo", "_sm_parent": "r0", "_sm_position": 0, "address_role": "work", "country": "NO"},
            ],
        }
    with pytest.raises(StructureMapError, match="addressbook.Address"):
        prepare(model, MappingSpec(overrides={"@Address": {"prefix": "x"}}), relations=relations)
    qualified = mappings[0].model_copy(update={"overrides": {
        **mappings[0].overrides, "@addressbook.Address/country": UseOverride(column="country"),
    }})
    assert reconstruct(rows, prepare(model, qualified, relations=relations)) == expected


@dataclass
class City:
    city: str


@dataclass
class CityPair:
    own: City
    other: City


@dataclass
class AuthoredNode:
    label: str
    children: list[AuthoredNode]


@pytest.mark.parametrize("format", ["json", "yaml"])
@pytest.mark.parametrize("bundle", [False, True])
def test_portable_mapping_preserves_explicit_null_override(format, bundle):
    mapping = MappingSpec(overrides={
        "@City/city": UseOverride(cast=TypeCast(to="i64")),
        "city": UseOverride(cast=None),
    })
    if bundle:
        restored = load_bundle(dump_bundle(AuthoringBundle(None, mapping, {}), format=format), format=format).mapping
    else:
        restored = load_mapping(dump_mapping(mapping, format=format), format=format)
    assert decompose(City("01"), prepare(derive(City), restored)) == {
        "root": [{"_sm_key": "r0", "city": "01"}],
    }


def test_namespace_qualifies_nested_names_without_double_prefix():
    document = StructureDocument(namespace="orders", structure=StructureSpec(
        root="nested.City", definitions={"nested.City": RecordDefinition(
            name="nested.City", members=(Member(name="city", shape=Scalar(type="str")),),
        )},
    ))
    from mountainash_structuremap.definitions import DerivedModel
    model = DerivedModel(
        structure=assemble_structure([document], root="orders.nested.City"),
        native_types={"orders.nested.City": City}, binding="dataclass",
    )
    assert reconstruct({"root": [{"_sm_key": "r", "city": "Oslo"}]}, prepare(model)) == City("Oslo")


@pytest.mark.parametrize("text,format", [
    ('{"mapping": {}, "relations": {}, "mappnig": {}}', "json"),
    ("mapping: {}\nrelations: {}\nmappnig: {}", "yaml"),
    ("mapping: {}\nrelations: {1: {fields: []}}", "yaml"),
])
def test_bundle_rejects_unknown_fields_and_nonstring_table_names(text, format):
    with pytest.raises(StructureMapError) as error:
        load_bundle(text, format=format)
    assert error.value.code == "invalid_authoring_document"


@pytest.mark.parametrize("text,format", [
    ('{"use": {"component": "external", "config": {"value": NaN}}}', "json"),
    ("use: {component: external, config: {value: .nan}}", "yaml"),
])
def test_authoring_rejects_nonfinite_values_outside_json_data_model(text, format):
    with pytest.raises(StructureMapError) as error:
        load_mapping(text, format=format)
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("format", ["json", "yaml"])
def test_recursive_structural_document_reconstructs_independent_rows(format):
    from mountainash_structuremap.definitions import DerivedModel
    document = StructureDocument(namespace="forest", structure=StructureSpec(root="Node", definitions={
        "Node": RecordDefinition(name="Node", members=(
            Member(name="label", shape=Scalar(type="str")),
            Member(name="children", shape=Sequence(item=RecordRef(definition="Node"))),
        )),
    }))
    restored = load_structure(dump_structure(document, format=format), format=format)
    model = DerivedModel(structure=assemble_structure([restored], root="forest.Node"),
                         native_types={"forest.Node": AuthoredNode}, binding="dataclass")
    rows = {
        "root": [{"_sm_key": "r", "label": "root"}],
        "children": [
            {"_sm_key": "c", "_sm_parent": "b", "_sm_position": 0, "_sm_parent_table": "children", "label": "leaf"},
            {"_sm_key": "b", "_sm_parent": "r", "_sm_position": 0, "_sm_parent_table": "root", "label": "branch"},
        ],
    }
    assert reconstruct(rows, prepare(model)) == AuthoredNode("root", [AuthoredNode("branch", [AuthoredNode("leaf", [])])])


@pytest.mark.parametrize("format", ["json", "yaml"])
def test_dump_rejects_nonfinite_component_configuration(format):
    mapping = MappingSpec(use=MappingUse(component="example.number", config={"value": float("nan")}))
    with pytest.raises(ValueError):
        dump_mapping(mapping, format=format)


@pytest.mark.parametrize("format", ["json", "yaml"])
@pytest.mark.parametrize("bundle", [False, True])
def test_export_preserves_mutated_default_dictionaries(format, bundle):
    from mountainash_structuremap import MappingRef, RowLayout

    use = MappingUse(component="core.record")
    address = MappingUse(component=AddressColumns.identity)
    address.config["prefix"] = "selected"
    use.children["own"] = MappingRef(ref="address")
    mapping = MappingSpec(use=use)
    mapping.definitions["address"] = address
    mapping.layouts["root"] = RowLayout(key=("id",), generated=True)
    mapping.overrides["other/city"] = UseOverride(column="other_town")
    rows = {"root": [{"id": "r0", "selected_city": "Oslo", "other_town": "Kyoto"}]}
    expected = CityPair(City("Oslo"), City("Kyoto"))
    prepared = prepare(derive(CityPair), mapping, components=(AddressColumns(),))
    assert reconstruct(rows, prepared) == expected
    if bundle:
        restored = load_bundle(dump_bundle(AuthoringBundle(None, mapping, {}), format=format), format=format).mapping
    else:
        restored = load_mapping(dump_mapping(mapping, format=format), format=format)
    reloaded = prepare(derive(CityPair), restored, components=(AddressColumns(),))
    assert reconstruct(rows, reloaded) == expected
    assert decompose(expected, reloaded) == rows


@pytest.mark.parametrize("root", [
    {"type": "str"},
    {"kind": "sequence", "item": {"type": "str"}},
])
def test_structural_schema_requires_discriminators_used_by_loader(root):
    document = {"namespace": "n", "structure": {"root": root, "definitions": {}}}
    assert not Draft202012Validator(structure_schema()).is_valid(document)
    with pytest.raises(StructureMapError):
        load_structure(json.dumps(document), format="json")


@pytest.mark.parametrize("format", ["json", "yaml"])
@pytest.mark.parametrize("bundle", [False, True])
def test_document_policy_types_agree_with_generated_schema(format, bundle):
    document = {"overrides": {"items": {"ordered": "false"}}}
    assert not Draft202012Validator(mapping_schema()).is_valid(document)
    with pytest.raises(StructureMapError):
        if bundle:
            load_bundle(json.dumps({"mapping": document, "relations": {}}), format=format)
        else:
            load_mapping(json.dumps(document), format=format)
