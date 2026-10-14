from xml.etree import ElementTree

import pytest

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
from tests.fixtures.models import Address
from tests.fixtures.xml_models import (
    NS,
    AmbiguousDocument,
    BooleanDocument,
    CollisionDocument,
    Colour,
    CountedDocument,
    CountedEnumDocument,
    EmptyChild,
    EnumTokens,
    InheritedDocument,
    Measured,
    Measurement,
    NillableItems,
    PlainEnvelope,
    TextDocument,
    TextValue,
    TokenDocument,
    TupleTokens,
)


@pytest.mark.parametrize("values", [[], [""], ["same", "same", "last"]])
def test_native_xml_values_reconstruct_independently(values):
    prepared = prepare(derive(TextDocument))
    source = TextDocument(label="A", values=values)
    native = parse(serialize(source, prepared), prepared)
    supplied = decompose(native, prepared)
    rebuilt = reconstruct(supplied, prepare(derive(TextDocument)))
    assert rebuilt == TextDocument(label="A", values=values)
    assert parse(serialize(rebuilt, prepared), prepared).values == values


def test_independent_rows_restore_wrapper_and_position_order():
    prepared = prepare(derive(TextDocument), MappingSpec(overrides={"values": {"table": "entries", "column": "text"}}))
    rebuilt = reconstruct({
        "root": [{"_sm_key": "doc", "label": "external"}],
        "entries": [
            {"_sm_key": "a", "_sm_parent": "doc", "_sm_position": 9, "text": "last"},
            {"_sm_key": "b", "_sm_parent": "doc", "_sm_position": 2, "text": ""},
        ],
    }, prepared)
    assert rebuilt == TextDocument("external", ["", "last"])
    xml = ElementTree.fromstring(serialize(rebuilt, prepared))
    assert xml.tag == f"{{{NS}}}document"
    assert xml.attrib == {"label": "external"}
    assert [node.text or "" for node in xml.findall(f"{{{NS}}}values/{{{NS}}}value")] == ["", "last"]


def test_ambiguous_native_metadata_fails_with_configuration_guidance():
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(AmbiguousDocument))
    assert caught.value.owner == "native limitation"
    assert caught.value.path == "choices"
    assert "left" in str(caught.value) and "right" in str(caught.value)
    assert caught.value.remedy
    assert caught.value.__cause__ is not None


def test_xml_local_name_collisions_require_overrides_without_changing_xml():
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(CollisionDocument))
    assert caught.value.code == "column_collision"
    mapping = MappingSpec(overrides={
        "attribute": {"column": "attr"},
        "element": {"column": "local"},
        "foreign": {"column": "foreign"},
    })
    prepared = prepare(derive(CollisionDocument), mapping)
    source = f'<collision xmlns="{NS}" xmlns:f="urn:foreign" same="A"><same>B</same><f:same>C</f:same><empty/></collision>'
    native = parse(source, prepared)
    assert native == CollisionDocument(attribute="A", element="B", foreign="C", empty=EmptyChild())
    rows = decompose(native, prepared)
    assert {key: rows["root"][0][key] for key in ("attr", "local", "foreign")} == {"attr": "A", "local": "B", "foreign": "C"}
    rebuilt = reconstruct(rows, prepare(derive(CollisionDocument), mapping))
    xml = ElementTree.fromstring(serialize(rebuilt, prepared))
    assert xml.attrib == {"same": "A"}
    assert [(child.tag, child.text) for child in xml] == [
        (f"{{{NS}}}same", "B"), ("{urn:foreign}same", "C"), (f"{{{NS}}}empty", None),
    ]


def test_token_order_and_enum_identity_survive_native_documents():
    prepared = prepare(derive(TokenDocument))
    native = parse('<tokens colour="blue"><words>second first second</words></tokens>', prepared)
    tables = decompose(native, prepared)
    rebuilt = reconstruct(tables, prepare(derive(TokenDocument)))
    assert rebuilt.words == ["second", "first", "second"]
    assert rebuilt.colour is Colour.BLUE
    assert parse(serialize(rebuilt, prepared), prepared) == TokenDocument(["second", "first", "second"], Colour.BLUE)


def test_preparation_never_constructs_or_runs_factories_and_reconstruction_restores_capture():
    CountedDocument.constructions = CountedDocument.factories = 0
    prepared = prepare(derive(CountedDocument))
    assert (CountedDocument.constructions, CountedDocument.factories) == (0, 0)
    rebuilt = reconstruct({"root": [{"_sm_key": "external", "value": "captured"}]}, prepared)
    assert rebuilt.value == "captured"
    assert (CountedDocument.constructions, CountedDocument.factories) == (1, 0)


@pytest.mark.parametrize("operation", ["parse", "serialize", "validate"])
def test_missing_capability_is_rejected_at_the_operation_boundary(operation):
    prepared = prepare(derive(Address))
    with pytest.raises(StructureMapError) as caught:
        if operation == "parse":
            parse("<anything/>", prepared)
        elif operation == "serialize":
            serialize(Address("x"), prepared)
        else:
            prepare(derive(TextDocument), validate_native=True)
    assert caught.value.code == "unavailable_capability"


@pytest.mark.parametrize("source", ["<boolean>", "<boolean><value>invalid</value></boolean>"])
def test_native_parse_errors_keep_their_cause(source):
    with pytest.raises(StructureMapError) as caught:
        parse(source, prepare(derive(BooleanDocument)))
    assert caught.value.owner == "native limitation"
    assert caught.value.__cause__ is not None


def test_bad_enum_cell_precedes_all_native_construction():
    prepared = prepare(derive(CountedEnumDocument))
    CountedDocument.constructions = CountedDocument.factories = 0
    with pytest.raises(StructureMapError) as caught:
        reconstruct({"root": [{"_sm_key": "external", "child_value": "valid", "colour": "not-a-colour"}]}, prepared)
    assert caught.value.table == "root"
    assert caught.value.column == "colour"
    assert (CountedDocument.constructions, CountedDocument.factories) == (0, 0)


@pytest.mark.parametrize("note", [None, ""])
def test_inherited_frozen_slotted_model_preserves_table_values_and_native_normalization(note):
    mapping = MappingSpec(overrides={
        "attribute": {"column": "attr"}, "element": {"column": "local"}, "foreign": {"column": "foreign"},
    })
    prepared = prepare(derive(InheritedDocument), mapping)
    value = InheritedDocument(attribute="A", element="B", foreign="C", empty=EmptyChild(), note=note)
    rebuilt = reconstruct(decompose(value, prepared), prepare(derive(InheritedDocument), mapping))
    assert isinstance(rebuilt, InheritedDocument)
    assert rebuilt.note == note
    assert parse(serialize(rebuilt, prepared), prepared) == InheritedDocument(
        attribute="A", element="B", foreign="C", empty=EmptyChild(), note=None,
    )


def test_text_content_and_attribute_survive_byte_document_input():
    prepared = prepare(derive(TextValue))
    value = parse('<text lang="fr">été &amp; hiver</text>'.encode(), prepared)
    assert value == TextValue("été & hiver", "fr")
    rebuilt = reconstruct(decompose(value, prepared), prepare(derive(TextValue)))
    xml = ElementTree.fromstring(serialize(rebuilt, prepared))
    assert xml.text == "été & hiver"
    assert xml.attrib == {"lang": "fr"}


def test_enum_token_members_keep_native_associations_in_collection_tables():
    prepared = prepare(derive(EnumTokens))
    native = parse("<enum-tokens><colours>blue red blue</colours></enum-tokens>", prepared)
    rebuilt = reconstruct(decompose(native, prepared), prepare(derive(EnumTokens)))
    assert rebuilt.colours == [Colour.BLUE, Colour.RED, Colour.BLUE]
    assert parse(serialize(rebuilt, prepared), prepared) == EnumTokens([Colour.BLUE, Colour.RED, Colour.BLUE])


def test_xml_nil_belongs_to_repeated_members_not_the_collection():
    prepared = prepare(derive(NillableItems))
    native = parse(
        '<nillable-items xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<value xsi:nil="true"/><value>x</value></nillable-items>', prepared,
    )
    assert native.values == [None, "x"]
    rows = decompose(native, prepared)
    rebuilt = reconstruct(rows, prepare(derive(NillableItems)))
    assert rebuilt.values == [None, "x"]
    assert parse(serialize(rebuilt, prepared), prepared).values == [None, "x"]
    empty = reconstruct(decompose(NillableItems([]), prepared), prepared)
    assert empty.values == []


def test_tuple_token_container_survives_independent_reconstruction():
    prepared = prepare(derive(TupleTokens))
    native = parse("<tuple-tokens><words>second first second</words></tuple-tokens>", prepared)
    assert native.words == ("second", "first", "second")
    rebuilt = reconstruct(decompose(native, prepared), prepare(derive(TupleTokens)))
    assert rebuilt.words == ("second", "first", "second")
    assert parse(serialize(rebuilt, prepared), prepared).words == ("second", "first", "second")


def test_nan_enum_selection_is_validated_before_native_construction():
    prepared = prepare(derive(Measured))
    CountedDocument.constructions = 0
    rebuilt = reconstruct(
        {"root": [{"_sm_key": "external", "child_value": "valid", "reading": float("nan")}]}, prepared,
    )
    assert rebuilt.reading is Measurement.UNKNOWN
    assert CountedDocument.constructions == 1
    assert reconstruct(decompose(rebuilt, prepared), prepared).reading is Measurement.UNKNOWN


def test_detected_plain_root_cannot_silently_strip_nested_xml_metadata():
    with pytest.raises(StructureMapError) as caught:
        derive(PlainEnvelope)
    assert caught.value.code == "unavailable_binding"
    assert caught.value.path == "child"
    explicit = prepare(derive(PlainEnvelope, binding="dataclass"))
    value = PlainEnvelope(TextValue("text", "en"))
    assert reconstruct(decompose(value, explicit), explicit) == value


def test_enum_root_fails_with_an_attributed_record_root_error():
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Colour))
    assert caught.value.code == "unsupported_root"


@pytest.mark.parametrize("as_bytes", [False, True])
def test_declared_non_utf8_encoding_preserves_unicode_text_and_encoded_bytes(as_bytes):
    text = '<?xml version="1.0" encoding="ISO-8859-1"?><text lang="fr">été</text>'
    source = text.encode("iso-8859-1") if as_bytes else text
    native = parse(source, prepare(derive(TextValue)))
    assert native == TextValue("été", "fr")
