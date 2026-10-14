from __future__ import annotations

from xml.etree import ElementTree

import pytest
from xsdata.formats.dataclass.models.generics import AnyElement, DerivedElement

from mountainash_structuremap import StructureMapError, decompose, derive, parse, prepare, reconstruct, serialize
from mountainash_structuremap.mapping import MappingSpec
from tests.fixtures.xml_fragments import (
    AttributeWildcard,
    CompoundWildcard,
    FragmentChoice,
    Mixed,
    MixedSibling,
    NamedMixed,
    ObjectMixed,
    OptionalWildcard,
    TypedFragment,
    WrappedWildcard,
)
from tests.fixtures.xml_models import NS, CountedDocument


def _envelope(payload):
    return '<sm:fragment xmlns:sm="urn:mountainash:structuremap:fragment">' + payload + "</sm:fragment>"


@pytest.mark.parametrize("policy", [None, "fragment"])
def test_fragment_keeps_nested_text_tails_and_expanded_names(policy):
    mapping = MappingSpec(defaults={"open_content": policy})
    prepared = prepare(derive(Mixed), mapping)
    xml = f'<mixed xmlns="{NS}" xmlns:q="urn:outside">lead<q:item q:flag="yes">inside<q:child/>inner-tail</q:item>outer-tail</mixed>'
    source = parse(xml, prepared)

    rows = decompose(source, prepared)
    rebuilt = reconstruct(rows, prepare(derive(Mixed), mapping))

    expected = [
        "lead", AnyElement(
            qname="{urn:outside}item", attributes={"{urn:outside}flag": "yes"},
            text="inside", tail="outer-tail",
            children=[AnyElement(qname="{urn:outside}child", text="", tail="inner-tail")],
        ),
    ]
    assert rebuilt.content == expected
    assert ElementTree.fromstring(rows["content"][1]["value"])[0].tag == "{urn:outside}item"
    reparsed = parse(serialize(rebuilt, prepared), prepared)
    assert reparsed.content == expected


def test_independently_authored_fragment_retains_namespace_and_empty_occurrences():
    mapping = MappingSpec(overrides={"content": {"table": "fragments", "column": "fragment_xml"}})
    prepared = prepare(derive(Mixed), mapping)
    rows = {
        "root": [{"_sm_key": "root-1"}],
        "fragments": [
            {"_sm_key": "text-1", "_sm_parent": "root-1", "_sm_position": 0, "fragment_xml": _envelope("text")},
            {
                "_sm_key": "empty-text", "_sm_parent": "root-1", "_sm_position": 1,
                "fragment_xml": _envelope(""),
            },
            {
                "_sm_key": "plain", "_sm_parent": "root-1", "_sm_position": 2,
                "fragment_xml": _envelope("<item/>"),
            },
            {
                "_sm_key": "item-1", "_sm_parent": "root-1", "_sm_position": 3,
                "fragment_xml": _envelope('<q:item xmlns:q="urn:outside" q:flag="yes"/>'),
            },
            {
                "_sm_key": "item-2", "_sm_parent": "root-1", "_sm_position": 4,
                "fragment_xml": _envelope('<q:item xmlns:q="urn:outside" q:flag="yes"/>'),
            },
        ],
    }

    rebuilt = reconstruct(rows, prepared)

    assert len(rebuilt.content) == 5
    assert rebuilt.content[0] == "text"
    assert rebuilt.content[1] == ""
    assert rebuilt.content[2].qname == "item"
    assert rebuilt.content[3].qname == "{urn:outside}item"
    assert rebuilt.content[3].attributes == {"{urn:outside}flag": "yes"}
    assert rebuilt.content[4].qname == "{urn:outside}item"
    assert rebuilt.content[3].tail in (None, "")


def test_typed_fragment_constructs_during_valid_reconstruction():
    prepared = prepare(derive(Mixed))
    xml = (
        f'<mixed xmlns="{NS}" xmlns:q="urn:fragment:typed" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<q:item xsi:type="q:typedFragment"><q:value>ready</q:value></q:item></mixed>'
    )
    TypedFragment.constructions = 0
    source = parse(xml, prepared)
    rows = decompose(source, prepared)

    rebuilt = reconstruct(rows, prepare(derive(Mixed)))

    result = rebuilt.content[0]
    assert isinstance(result, DerivedElement)
    assert result.qname == "{urn:fragment:typed}item"
    assert isinstance(result.value, TypedFragment)
    assert result.value.value == "ready"
    assert TypedFragment.constructions == 2


def test_late_invalid_fragment_precedes_sibling_and_xsi_type_construction():
    mapping = MappingSpec(overrides={"content": {"table": "fragments", "column": "fragment_xml"}})
    prepared = prepare(derive(MixedSibling), mapping)
    xml = (
        f'<mixedSibling xmlns="{NS}" xmlns:q="urn:fragment:typed" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<counted><value>native</value></counted>'
        '<q:item xsi:type="q:typedFragment"><q:value>ready</q:value></q:item>'
        "<q:later/>"
        "</mixedSibling>"
    )
    TypedFragment.constructions = CountedDocument.constructions = CountedDocument.factories = 0
    source = parse(xml, prepared)
    rows = decompose(source, prepared)
    bad = {name: [dict(row) for row in values] for name, values in rows.items()}
    bad["fragments"][-1]["fragment_xml"] = "<broken"
    TypedFragment.constructions = CountedDocument.constructions = CountedDocument.factories = 0

    with pytest.raises(StructureMapError):
        reconstruct(bad, prepared)
    assert TypedFragment.constructions == CountedDocument.constructions == CountedDocument.factories == 0

    rebuilt = reconstruct(rows, prepare(derive(MixedSibling), mapping))
    assert rebuilt.sibling.value == "native"
    assert TypedFragment.constructions == CountedDocument.constructions == 1


def test_wildcard_attribute_namespace_and_values_roundtrip():
    prepared = prepare(derive(AttributeWildcard))
    source = parse(f'<attributed xmlns="{NS}" xmlns:q="urn:outside" q:flag="yes"/>', prepared)
    rows = decompose(source, prepared)
    rebuilt = reconstruct(rows, prepare(derive(AttributeWildcard)))

    assert rebuilt.attributes == {"{urn:outside}flag": "yes"}


@pytest.mark.parametrize("strategy", ["drop", "structured", "reject"])
def test_unsupported_open_content_strategies_fail_during_preparation(strategy):
    with pytest.raises(StructureMapError):
        prepare(derive(Mixed), MappingSpec(defaults={"open_content": strategy}))


def test_native_typed_fragment_constructor_failure_keeps_its_cause():
    prepared = prepare(derive(Mixed))
    source = parse(
        f'<mixed xmlns="{NS}" xmlns:q="urn:fragment:typed" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<q:item xsi:type="q:typedFragment"><q:value>ready</q:value></q:item></mixed>',
        prepared,
    )
    rows = decompose(source, prepared)
    TypedFragment.should_fail = True
    try:
        with pytest.raises(StructureMapError) as caught:
            reconstruct(rows, prepare(derive(Mixed)))
    finally:
        TypedFragment.should_fail = False

    assert caught.value.code == "fragment_bind_failed"
    assert isinstance(caught.value.__cause__, RuntimeError)




def test_detached_fragment_occurrence_fails_before_sibling_construction():
    mapping = MappingSpec(overrides={"content": {"table": "fragments", "column": "fragment_xml"}})
    prepared = prepare(derive(MixedSibling), mapping)
    source = parse(
        f'<mixedSibling xmlns="{NS}"><counted><value>native</value></counted>'
        "<q:item xmlns:q='urn:outside'/></mixedSibling>",
        prepared,
    )
    rows = decompose(source, prepared)
    rows["fragments"].append({
        "_sm_key": "detached",
        "_sm_parent": "missing-parent",
        "_sm_position": 7,
        "fragment_xml": _envelope("<q:child xmlns:q='urn:outside'/>"),
    })
    CountedDocument.constructions = CountedDocument.factories = 0

    with pytest.raises(StructureMapError):
        reconstruct(rows, prepared)

    assert CountedDocument.constructions == CountedDocument.factories == 0


def test_fragment_cell_rejects_multiple_top_level_occurrences():
    mapping = MappingSpec(overrides={"content": {"table": "fragments", "column": "fragment_xml"}})
    prepared = prepare(derive(Mixed), mapping)
    tables = {
        "root": [{"_sm_key": "root-1"}],
        "fragments": [{
            "_sm_key": "pair",
            "_sm_parent": "root-1",
            "_sm_position": 0,
            "fragment_xml": _envelope("<one/><two/>"),
        }],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)

    assert caught.value.code == "invalid_xml_fragment"


def test_inactive_choice_fragment_row_fails_before_native_construction():
    mapping = MappingSpec(overrides={
        "branches": {"table": "branch_rows", "variant_layout": "prefixed"},
        "branches/content": {"table": "inactive_fragments", "column": "fragment_xml"},
    })
    prepared = prepare(derive(FragmentChoice), mapping)
    source = FragmentChoice(branches=[CountedDocument(value="active")])
    rows = decompose(source, prepared)
    branch = rows["branch_rows"][0]
    rows["inactive_fragments"].append({
        "_sm_key": "inactive-fragment",
        "_sm_parent": branch["_sm_key"],
        "_sm_position": 9,
        "fragment_xml": _envelope("<item/>"),
    })
    CountedDocument.constructions = CountedDocument.factories = 0

    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)

    assert caught.value.code == "invalid_variant_payload"
    assert CountedDocument.constructions == CountedDocument.factories == 0


def test_fragment_preserves_wildcard_choice_name_for_bare_typed_value():
    prepared = prepare(derive(NamedMixed))
    source = NamedMixed(content=[TypedFragment(value="selected value")])
    rows = decompose(source, prepared)

    rebuilt = reconstruct(rows, prepare(derive(NamedMixed)))
    result = rebuilt.content[0]

    assert type(result) is TypedFragment
    assert result.value == "selected value"
    wire = serialize(rebuilt, prepared)
    assert ElementTree.fromstring(wire)[0].tag == f"{{{NS}}}selected"
    reparsed = parse(wire, prepared)
    assert type(reparsed.content[0]) is TypedFragment
    assert reparsed.content[0].value == "selected value"


@pytest.mark.parametrize("content", [None, AnyElement(qname="item", text="value")])
def test_optional_singular_wildcard_preserves_absence_and_value(content):
    prepared = prepare(derive(OptionalWildcard))
    rows = decompose(OptionalWildcard(content=content), prepared)
    rebuilt = reconstruct(rows, prepare(derive(OptionalWildcard)))
    assert rebuilt.content == content


def test_wrapper_stays_outside_individual_fragment_occurrences():
    prepared = prepare(derive(WrappedWildcard))
    source = parse(
        f'<wrapped xmlns="{NS}"><items xmlns=""><item>one</item><item>two</item></items></wrapped>',
        prepared,
    )
    rebuilt = reconstruct(decompose(source, prepared), prepare(derive(WrappedWildcard)))
    # xsdata retains this wildcard wrapper as generic content. The fragment
    # codec must not add a second wrapper to that retained native occurrence.
    assert rebuilt.content == [AnyElement(
        qname="items", text="", children=[
            AnyElement(qname="item", text="one"), AnyElement(qname="item", text="two"),
        ],
    )]


def test_scalar_mixed_annotation_requires_supported_native_collection():
    with pytest.raises(StructureMapError) as caught:
        derive(ObjectMixed)
    assert caught.value.code == "unsupported_native_shape"
    assert caught.value.path == "content"
    assert "list[object]" in caught.value.remedy


def test_compound_wildcard_keeps_declared_typed_and_open_occurrences():
    prepared = prepare(derive(CompoundWildcard))
    source = parse(
        f'<compound xmlns="{NS}" xmlns:t="urn:fragment:typed" xmlns:q="urn:outside">'
        '<known><t:value>typed</t:value></known><q:item>other</q:item></compound>',
        prepared,
    )
    rebuilt = reconstruct(decompose(source, prepared), prepare(derive(CompoundWildcard)))
    assert type(rebuilt.content[0]) is TypedFragment
    assert rebuilt.content[0].value == "typed"
    assert rebuilt.content[1] == AnyElement(qname="{urn:outside}item", text="other")
    root = ElementTree.fromstring(serialize(rebuilt, prepared))
    assert [child.tag for child in root] == [f"{{{NS}}}known", "{urn:outside}item"]
    assert root[0][0].text == "typed"
    assert root[1].text == "other"


def test_compound_fragment_rejects_undeclared_namespace_before_construction():
    mapping = MappingSpec(overrides={"content": {"table": "fragments", "column": "fragment_xml"}})
    prepared = prepare(derive(CompoundWildcard), mapping)
    tables = {
        "root": [{"_sm_key": "root"}],
        "fragments": [
            {"_sm_key": "first", "_sm_parent": "root", "_sm_position": 1,
             "fragment_xml": _envelope(
                 f'<known xmlns="{NS}" xmlns:t="urn:fragment:typed"><t:value>valid</t:value></known>'
             )},
            {"_sm_key": "later", "_sm_parent": "root", "_sm_position": 5,
             "fragment_xml": _envelope('<wrong xmlns="urn:undeclared"/>')},
        ],
    }
    TypedFragment.constructions = 0
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "invalid_xml_fragment"
    assert caught.value.row == 1
    assert TypedFragment.constructions == 0


@pytest.mark.parametrize("model", [Mixed, OptionalWildcard, AttributeWildcard, CompoundWildcard])
def test_global_reject_covers_every_open_content_shape(model):
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(model), MappingSpec(defaults={"open_content": "reject"}))
    assert caught.value.code == "unsupported_open_content"
    assert caught.value.path == ("attributes" if model is AttributeWildcard else "content")


def test_fragment_override_permits_selected_use_under_global_reject():
    mapping = MappingSpec(
        defaults={"open_content": "reject"},
        overrides={"content": {"open_content": "fragment"}},
    )
    prepared = prepare(derive(Mixed), mapping)
    source = Mixed(content=["permitted"])
    rebuilt = reconstruct(decompose(source, prepared), prepared)
    assert rebuilt.content == ["permitted"]


def test_reject_override_refuses_selected_use_under_global_fragment():
    mapping = MappingSpec(
        defaults={"open_content": "fragment"},
        overrides={"content": {"open_content": "reject"}},
    )
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Mixed), mapping)
    assert caught.value.code == "unsupported_open_content"
    assert caught.value.path == "content"


def test_singular_wildcard_preserves_native_multi_element_aggregate():
    prepared = prepare(derive(OptionalWildcard))
    source = parse(
        f'<optional xmlns="{NS}"><one xmlns="">first</one><two xmlns="">second</two></optional>',
        prepared,
    )
    assert source.content == AnyElement(children=[
        AnyElement(qname="one", text="first"), AnyElement(qname="two", text="second"),
    ])
    rebuilt = reconstruct(decompose(source, prepared), prepare(derive(OptionalWildcard)))
    assert rebuilt.content == source.content
    root = ElementTree.fromstring(serialize(rebuilt, prepared))
    assert [(child.tag, child.text) for child in root] == [("one", "first"), ("two", "second")]


@pytest.mark.parametrize(("model", "payload"), [
    (Mixed, '<q:item xmlns:q="urn:fragment:typed" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
     'xsi:type="q:typedFragment"><q:value>ready</q:value></q:item>tail'),
    (Mixed, '<q:typedFragment xmlns:q="urn:fragment:typed"><q:value>ready</q:value></q:typedFragment>tail'),
    (Mixed, '<item xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
     'xmlns:xs="http://www.w3.org/2001/XMLSchema" xsi:type="xs:int">7</item>tail'),
    (NamedMixed, f'<selected xmlns="{NS}" xmlns:q="urn:fragment:typed"><q:value>ready</q:value></selected>tail'),
])
def test_typed_fragment_tail_cannot_be_silently_discarded(model, payload):
    mapping = MappingSpec(overrides={"content": {"table": "fragments", "column": "fragment_xml"}})
    prepared = prepare(derive(model), mapping)
    source = model(content=[])
    tables = decompose(source, prepared)
    tables["fragments"] = [{
        "_sm_key": "typed", "_sm_parent": "r0", "_sm_position": 0,
        "fragment_xml": _envelope(payload),
    }]
    TypedFragment.constructions = CountedDocument.constructions = CountedDocument.factories = 0
    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)
    assert caught.value.code == "invalid_xml_fragment"
    assert TypedFragment.constructions == CountedDocument.constructions == CountedDocument.factories == 0


def test_typed_mixed_tail_roundtrips_as_a_separate_native_occurrence():
    prepared = prepare(derive(Mixed))
    source = parse(
        f'<mixed xmlns="{NS}" xmlns:q="urn:fragment:typed" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<q:item xsi:type="q:typedFragment"><q:value>ready</q:value></q:item>tail</mixed>',
        prepared,
    )
    rebuilt = reconstruct(decompose(source, prepared), prepare(derive(Mixed)))
    assert type(rebuilt.content[0]) is DerivedElement
    assert type(rebuilt.content[0].value) is TypedFragment
    assert rebuilt.content[0].value.value == "ready"
    assert rebuilt.content[1] == "tail"
    root = ElementTree.fromstring(serialize(rebuilt, prepared))
    assert root[0].tag == "{urn:fragment:typed}item"
    assert root[0].tail == "tail"
