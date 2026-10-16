import pytest
from xsdata.formats.dataclass.models.generics import DerivedElement

from mountainash_structuremap import (
    MappingSpec,
    StructureMapError,
    decompose,
    derive,
    parse,
    prepare,
    reconstruct,
    serialize,
)
from tests.fixtures.xml_variants import (
    EmptyA,
    EmptyB,
    Hound,
    InheritedChoices,
    LocalChoices,
    LocalUnion,
    LocalUnionA,
    SelectedNil,
    SiblingCat,
    SiblingType,
    TokenChoices,
    TypedAnimal,
)


@pytest.mark.parametrize("layout", ["split", "prefixed", "shared"])
def test_native_xsi_type_subclass_is_recorded_not_inferred_from_payload(layout):
    prepared = prepare(derive(TypedAnimal), MappingSpec(defaults={"variant_layout": layout}))
    native = parse(
        '<TypedAnimal xmlns:a="urn:animals" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<a:animal xsi:type="a:HoundType" name="hound" scent="7"/>'
        '</TypedAnimal>', prepared,
    )
    assert type(native.animal) is Hound
    supplied = decompose(native, prepared)
    rebuilt = reconstruct(supplied, prepare(derive(TypedAnimal), MappingSpec(defaults={"variant_layout": layout})))
    assert type(rebuilt.animal) is Hound
    assert rebuilt.animal.scent == 7
    assert parse(serialize(rebuilt, prepared), prepared).animal == Hound("hound", 7)


@pytest.mark.parametrize("layout", ["split", "prefixed", "shared"])
def test_selected_nil_qnames_are_distinct_from_all_null_records(layout):
    prepared = prepare(derive(SelectedNil), MappingSpec(defaults={"variant_layout": layout}))
    source = SelectedNil([
        DerivedElement("{urn:a}a", None), EmptyA(),
        DerivedElement("{urn:b}b", None), EmptyB(),
    ])
    rows = decompose(source, prepared)
    rebuilt = reconstruct({name: list(reversed(values)) for name, values in rows.items()}, prepared)
    assert rebuilt.values == [DerivedElement("{urn:a}a", None), EmptyA(), DerivedElement("{urn:b}b", None), EmptyB()]


@pytest.mark.parametrize("layout", ["split", "prefixed", "shared"])
def test_equal_local_names_have_distinct_declared_namespace_alternatives(layout):
    mapping = MappingSpec(overrides={"values": {
        "variant_layout": layout,
        "alternatives": {
            "{urn:a}item": {"prefix": "a", "table": "a_rows" if layout == "split" else None},
            "{urn:b}item": {"prefix": "b", "table": "b_rows" if layout == "split" else None},
        },
    }})
    prepared = prepare(derive(LocalChoices), mapping)
    source = parse('<LocalChoices xmlns:a="urn:a" xmlns:b="urn:b"><b:item/><a:item/></LocalChoices>', prepared)
    rebuilt = reconstruct(decompose(source, prepared), prepare(derive(LocalChoices), mapping))
    assert [type(value) for value in rebuilt.values] == [EmptyB, EmptyA]


def test_inherited_choice_qname_is_frozen_at_preparation():
    prepared = prepare(derive(InheritedChoices))
    known = InheritedChoices([DerivedElement("{urn:inherited}a", EmptyA("known"))])
    result = reconstruct(decompose(known, prepared), prepared)
    assert result.values == [EmptyA("known")]
    with pytest.raises(StructureMapError) as caught:
        decompose(InheritedChoices([DerivedElement("{urn:unknown}a", EmptyA("unknown"))]), prepared)
    assert caught.value.path == "values"


@pytest.mark.parametrize("layout", ["split", "prefixed", "shared"])
def test_compound_choice_retains_its_xsi_type_subclass(layout):
    from tests.fixtures.xml_variants import AnimalChoices

    mapping = MappingSpec(defaults={"variant_layout": layout}, overrides={"values": {
        "alternatives": {"{urn:animals}animal": {"prefix": "kept"}},
        "discriminator": {"strategy": "recorded", "values": {
            "{urn:animals}animal": "A", "{urn:other}other": "B",
        }},
    }, "values/scent": {"column": "smell"}})
    prepared = prepare(derive(AnimalChoices), mapping)
    source = parse(
        '<AnimalChoices xmlns:a="urn:animals" xmlns:o="urn:other" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<a:animal xsi:type="a:HoundType" name="hound" scent="7"/><o:other/>'
        '</AnimalChoices>', prepared,
    )
    assert type(source.values[0]) is Hound
    rows = decompose(source, prepared)
    table = "values__animal" if layout == "split" else "values"
    assert rows[table][0]["smell"] == "7"
    name_column = "kept_name" if layout == "shared" else "kept_hound_type_name"
    assert rows[table][0][name_column] == "hound"
    rebuilt = reconstruct(rows, prepare(derive(AnimalChoices), mapping))
    assert type(rebuilt.values[0]) is Hound
    assert rebuilt.values[0].scent == 7
    assert type(rebuilt.values[1]) is EmptyB
    assert parse(serialize(rebuilt, prepared), prepared) == source


def test_token_choice_alternatives_use_item_type_not_shared_list_type():
    prepared = prepare(derive(TokenChoices))
    source = parse(
        "<TokenChoices><ints>1 2</ints><words>alpha beta</words></TokenChoices>",
        prepared,
    )
    assert source.values == [[1, 2], ["alpha", "beta"]]
    assert reconstruct(decompose(source, prepared), prepared) == source


def test_empty_token_choice_retains_explicit_qname():
    source = TokenChoices([
        DerivedElement(qname="ints", value=[]),
        DerivedElement(qname="words", value=[]),
    ])
    prepared = prepare(derive(TokenChoices))
    rebuilt = reconstruct(decompose(source, prepared), prepare(derive(TokenChoices)))
    assert [(item.qname, item.value) for item in rebuilt.values] == [("ints", []), ("words", [])]
    assert decompose(rebuilt, prepared) == decompose(source, prepared)


def test_local_union_types_are_declared_alternatives_without_global_type_qnames():
    prepared = prepare(derive(LocalUnion))
    source = parse('<LocalUnion><value a="one"/></LocalUnion>', prepared)
    assert type(source.value) is LocalUnionA
    assert reconstruct(decompose(source, prepared), prepared) == source


def test_native_sibling_xsi_type_substitution_is_frozen_during_preparation():
    prepared = prepare(derive(SiblingType))
    source = parse(
        '<SiblingType xmlns:a="urn:animals" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<a:animal xsi:type="a:SiblingCatType" lives="9"/>'
        '</SiblingType>',
        prepared,
    )
    assert isinstance(source.animal, DerivedElement)
    assert type(source.animal.value) is SiblingCat
    rebuilt = reconstruct(decompose(source, prepared), prepared)
    assert isinstance(rebuilt.animal, DerivedElement)
    assert type(rebuilt.animal.value) is SiblingCat
    assert parse(serialize(rebuilt, prepared), prepared) == source


@pytest.mark.parametrize("layout", ["split", "prefixed", "shared"])
def test_nillable_token_choice_distinguishes_nil_from_empty_tokens(layout):
    source = TokenChoices([
        DerivedElement(qname="ints", value=None),
        DerivedElement(qname="ints", value=[]),
    ])
    mapping = MappingSpec(defaults={"variant_layout": layout})
    prepared = prepare(derive(TokenChoices), mapping)
    rebuilt = reconstruct(decompose(source, prepared), prepare(derive(TokenChoices), mapping))
    assert [(item.qname, item.value) for item in rebuilt.values] == [("ints", None), ("ints", [])]
    assert decompose(rebuilt, prepared) == decompose(source, prepared)
