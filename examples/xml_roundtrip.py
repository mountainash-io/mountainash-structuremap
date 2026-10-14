"""Run with `hatch run test:python examples/xml_roundtrip.py`."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from xml.etree import ElementTree

from xsdata.models.datatype import XmlDateTime

from mountainash_structuremap import MappingSpec, decompose, derive, parse, prepare, reconstruct, serialize

NS = "urn:structuremap:example"


@dataclass
class Document:
    class Meta:
        name = "document"
        namespace = NS

    label: str = field(metadata={"type": "Attribute"})
    values: list[str] = field(default_factory=list, metadata={
        "type": "Element", "name": "value", "wrapper": "values",
    })


@dataclass
class Dog:
    name: str = field(metadata={"type": "Attribute"})


@dataclass
class Cat:
    name: str = field(metadata={"type": "Attribute"})


@dataclass
class Pets:
    class Meta:
        name = "pets"
        namespace = NS

    pets: list[object] = field(default_factory=list, metadata={"type": "Elements", "choices": (
        {"name": "dog", "namespace": NS, "type": Dog},
        {"name": "cat", "namespace": NS, "type": Cat},
    )})


@dataclass
class Measurements:
    amount: Decimal = field(metadata={"type": "Element"})
    when: XmlDateTime = field(metadata={"type": "Element"})
    local: XmlDateTime = field(metadata={"type": "Element"})


@dataclass
class Commentary:
    content: list[object] = field(default_factory=list, metadata={
        "type": "Wildcard", "namespace": "##any", "mixed": True,
    })


@dataclass
class Report:
    class Meta:
        name = "report"
        namespace = NS

    measurements: Measurements = field(metadata={"type": "Element", "namespace": NS})
    commentary: Commentary = field(metadata={"type": "Element", "namespace": NS})
    pets: list[object] = field(default_factory=list, metadata={"type": "Elements", "choices": (
        {"name": "dog", "namespace": NS, "type": Dog},
        {"name": "cat", "namespace": NS, "type": Cat},
    )})


def variant_example() -> None:
    mapping = MappingSpec(overrides={"pets": {
        "variant_layout": "split",
        "discriminator": {"strategy": "recorded", "column": "kind", "values": {
            f"{{{NS}}}dog": "DOG", f"{{{NS}}}cat": "CAT",
        }},
        "alternatives": {
            f"{{{NS}}}dog": {"table": "dogs", "prefix": "dog"},
            f"{{{NS}}}cat": {"table": "cats", "prefix": "cat"},
        },
    }})
    prepared = prepare(derive(Pets), mapping)
    native = parse(f'<pets xmlns="{NS}"><dog name="same"/><cat name="same"/><dog name="last"/></pets>', prepared)
    rows = decompose(native, prepared)
    restored = reconstruct({name: list(reversed(values)) for name, values in rows.items()}, prepare(derive(Pets), mapping))
    assert [type(value) for value in restored.pets] == [Dog, Cat, Dog]
    assert parse(serialize(restored, prepared), prepared) == native
    authored = {
        "root": [{"_sm_key": "external"}],
        "dogs": [{"_sm_key": "d", "_sm_parent": "external", "_sm_position": 9, "kind": "DOG", "dog_name": "later"}],
        "cats": [{"_sm_key": "c", "_sm_parent": "external", "_sm_position": 2, "kind": "CAT", "cat_name": "earlier"}],
    }
    result = reconstruct(authored, prepare(derive(Pets), mapping))
    assert result.pets == [Cat("earlier"), Dog("later")]
    print("Independent Split alternatives, globally ordered across tables:")
    print(serialize(result, prepared))


def scalar_example() -> None:
    prepared = prepare(derive(Measurements))
    native = parse(
        "<Measurements><amount>9007199254740993.125</amount>"
        "<when>2024-02-03T04:05:06.123456+10:00</when>"
        "<local>2024-02-03T04:05:06</local></Measurements>", prepared,
    )
    with localcontext() as context:
        context.prec = 3
        rows = decompose(native, prepared)
        assert rows["root"][0]["amount"] == "9007199254740993.125"
        assert rows["root"][0]["when"] == datetime(2024, 2, 2, 18, 5, 6, 123456)
        assert rows["root"][0]["_sm_when_offset"] == 600
        assert rows["root"][0]["_sm_local_offset"] is None
        restored = reconstruct(rows, prepare(derive(Measurements)))
        assert restored == native
        assert tuple(restored.when) == tuple(native.when)
        assert tuple(restored.local) == tuple(native.local)
    print("Exact Decimal, UTC timestamp + offset, and timezone-absent wall time:")
    print(rows)
    print(serialize(restored, prepared))
    lexical = MappingSpec(overrides={"when": {"value_space": "lexical"}})
    submicroseconds = Measurements(
        Decimal("2.00"), XmlDateTime.from_string("2024-02-03T04:05:06.123456789+10:00"),
        XmlDateTime.from_string("2024-02-03T04:05:06"),
    )
    lexical_rows = decompose(submicroseconds, prepare(derive(Measurements), lexical))
    inverse = reconstruct(lexical_rows, prepare(derive(Measurements), lexical))
    assert inverse == submicroseconds
    assert tuple(inverse.when) == tuple(submicroseconds.when)
    assert tuple(inverse.local) == tuple(submicroseconds.local)
    print("Explicit Lexical policy preserves submicroseconds:")
    print(serialize(inverse, prepare(derive(Measurements), lexical)))


def combined_example() -> None:
    prepared = prepare(derive(Report))
    native = parse(
        f'<report xmlns="{NS}" xmlns:q="urn:outside">'
        '<measurements><amount>9007199254740993.125</amount>'
        '<when>2024-02-03T04:05:06+10:00</when>'
        '<local>2024-02-03T04:05:06</local></measurements>'
        '<commentary>lead<q:item q:flag="yes">inside<q:child/>inner-tail</q:item>outer-tail</commentary>'
        '<cat name="first"/><dog name="second"/></report>', prepared,
    )
    rows = decompose(native, prepared)
    rebuilt = reconstruct(
        {name: list(reversed(values)) for name, values in rows.items()},
        prepare(derive(Report)),
    )
    assert rebuilt.measurements.amount == Decimal("9007199254740993.125")
    assert rebuilt.measurements.when.offset == 600
    assert rebuilt.measurements.local.offset is None
    assert rebuilt.pets == [Cat("first"), Dog("second")]
    xml = serialize(rebuilt, prepare(derive(Report)))
    commentary = ElementTree.fromstring(xml).find(f"{{{NS}}}commentary")
    assert commentary is not None
    assert commentary.text == "lead"
    assert commentary[0].tag == "{urn:outside}item"
    assert commentary[0].attrib == {"{urn:outside}flag": "yes"}
    assert commentary[0].text == "inside"
    assert commentary[0][0].tail == "inner-tail"
    assert commentary[0].tail == "outer-tail"
    assert parse(xml, prepare(derive(Report))) == rebuilt
    print("One document: choices, exact scalars, offsets and standalone mixed fragments:")
    print(xml)


def main() -> None:
    mapping = MappingSpec(overrides={"values": {"table": "entries", "column": "text"}})
    prepared = prepare(derive(Document), mapping)
    source = f'<document xmlns="{NS}" label="native"><values><value/><value>same</value><value>same</value></values></document>'
    native = parse(source, prepared)
    assert native == Document("native", ["", "same", "same"])
    tables = decompose(native, prepared)
    print("Native XML tables:")
    print(json.dumps(tables, indent=2))
    delivered = {name: list(reversed(rows)) for name, rows in tables.items()}
    rebuilt = reconstruct(delivered, prepare(derive(Document), mapping))
    assert rebuilt == Document("native", ["", "same", "same"])
    print("Reconstructed XML:")
    print(serialize(rebuilt, prepared))

    independent = {
        "root": [{"_sm_key": "external", "label": "independent"}],
        "entries": [
            {"_sm_key": "later", "_sm_parent": "external", "_sm_position": 9, "text": "last"},
            {"_sm_key": "earlier", "_sm_parent": "external", "_sm_position": 2, "text": ""},
        ],
    }
    external = reconstruct(independent, prepare(derive(Document), mapping))
    assert external == Document("independent", ["", "last"])
    xml = serialize(external, prepared)
    parsed = ElementTree.fromstring(xml)
    assert parsed.attrib == {"label": "independent"}
    assert [node.text or "" for node in parsed.findall(f"{{{NS}}}values/{{{NS}}}value")] == ["", "last"]
    print("Independently authored tables -> XML:")
    print(xml)
    variant_example()
    scalar_example()
    combined_example()


if __name__ == "__main__":
    main()
