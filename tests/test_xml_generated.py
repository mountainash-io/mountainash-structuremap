"""Consume authored xsdata 26.2 output; generation is not a runtime dependency."""

from decimal import Decimal
from xml.etree.ElementTree import fromstring

import pytest
from xsdata.formats.dataclass.models.generics import AnyElement
from xsdata.models.datatype import XmlDateTime

from mountainash_structuremap import MappingSpec, decompose, derive, parse, prepare, reconstruct, serialize
from tests.fixtures.generated_catalog import Catalog, Entry, Note

CATALOG_XML = """<catalog xmlns="urn:structuremap:catalog" xmlns:q="urn:external">
  <entry code="parent"><amount>9007199254740993.125</amount>
    <serial>1234567890123456789012345678901234567890</serial>
    <when>2024-02-03T04:05:06+10:00</when>
    <child code="child"><amount>0.001</amount><serial>-7</serial>
      <when>2024-02-03T04:05:06</when></child>
  </entry>
  <note>lead<q:item q:flag="yes">inside<q:child/>inner-tail</q:item>outer-tail</note>
  <entry code="last"><amount>2.000</amount><serial>42</serial>
    <when>2024-02-03T04:05:06Z</when></entry>
</catalog>"""


@pytest.mark.parametrize("layout", ["split", "prefixed"])
def test_generated_choices_recursion_scalars_and_mixed_content_compose(layout):
    mapping = MappingSpec(defaults={"variant_layout": layout})
    prepared = prepare(derive(Catalog), mapping)
    rows = decompose(parse(CATALOG_XML, prepared), prepared)
    supplied = {name: list(reversed(values)) for name, values in rows.items()}
    rebuilt = reconstruct(supplied, prepare(derive(Catalog), mapping))

    assert [type(item) for item in rebuilt.entry_or_note] == [Entry, Note, Entry]
    parent, note, last = rebuilt.entry_or_note
    assert parent.code == "parent"
    assert parent.amount == Decimal("9007199254740993.125")
    assert parent.serial == 1234567890123456789012345678901234567890
    assert parent.when == XmlDateTime.from_string("2024-02-03T04:05:06+10:00")
    assert parent.child == [Entry(
        amount=Decimal("0.001"), serial=-7,
        when=XmlDateTime.from_string("2024-02-03T04:05:06"), code="child",
    )]
    assert last.code == "last"
    assert last.amount == Decimal("2")
    assert last.serial == 42
    assert last.when.offset == 0
    assert note.content == [
        "lead",
        AnyElement(
            qname="{urn:external}item", text="inside", tail="outer-tail",
            attributes={"{urn:external}flag": "yes"},
            children=[AnyElement(qname="{urn:external}child", text="", tail="inner-tail")],
        ),
    ]
    wire = serialize(rebuilt, prepare(derive(Catalog), mapping))
    xml = fromstring(wire)
    assert [child.tag for child in xml] == [
        "{urn:structuremap:catalog}entry", "{urn:structuremap:catalog}note",
        "{urn:structuremap:catalog}entry",
    ]
    assert xml[1].text == "lead"
    assert xml[1][0].tag == "{urn:external}item"
    assert xml[1][0].tail == "outer-tail"
    assert xml[1][0][0].tail == "inner-tail"
    reparsed = parse(wire, prepare(derive(Catalog), mapping))
    assert reparsed.entry_or_note == rebuilt.entry_or_note
