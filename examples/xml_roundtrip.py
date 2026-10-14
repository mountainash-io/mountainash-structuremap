"""Run with `hatch run test:python examples/xml_roundtrip.py`."""

import json
from dataclasses import dataclass, field
from xml.etree import ElementTree

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


if __name__ == "__main__":
    main()
