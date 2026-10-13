"""Run with `hatch run test:python examples/dataclass_roundtrip.py`."""

import json
from dataclasses import dataclass, field

from mountainash_structuremap import MappingSpec, Naming, UseOverride, decompose, derive, prepare, reconstruct


@dataclass
class Address:
    city: str


@dataclass
class Person:
    name: str
    home: Address
    tags: list[str | None] = field(default_factory=list)


@dataclass
class Document:
    people: list[Person]


@dataclass
class Node:
    label: str
    children: list["Node"] = field(default_factory=list)


def main() -> None:
    source = Document(
        [
            Person("Ada", Address("Oslo"), [None, "", "same", "same"]),
            Person("Lin", Address("Kyoto")),
        ]
    )
    tables = decompose(source, prepare(derive(Document)))
    print("Emitted tables:")
    print(json.dumps(tables, indent=2))
    delivery = {name: list(reversed(rows)) for name, rows in reversed(list(tables.items()))}
    rebuilt = reconstruct(delivery, prepare(derive(Document)))
    assert rebuilt == Document(
        [
            Person("Ada", Address("Oslo"), [None, "", "same", "same"]),
            Person("Lin", Address("Kyoto")),
        ]
    )
    print("Reconstructed:", rebuilt)

    tree = Node("root", [Node("branch", [Node("leaf")]), Node("sibling")])
    recursive_tables = decompose(tree, prepare(derive(Node)))
    print("Recursive tables:")
    print(json.dumps(recursive_tables, indent=2))
    recursive_delivery = {name: list(reversed(rows)) for name, rows in recursive_tables.items()}
    rebuilt_tree = reconstruct(recursive_delivery, prepare(derive(Node)))
    assert rebuilt_tree == Node("root", [Node("branch", [Node("leaf")]), Node("sibling")])
    print("Reconstructed recursive object:", rebuilt_tree)

    mapping = MappingSpec(
        defaults={"naming": Naming(metadata_namespace="recon_")},
        overrides={
            "people": UseOverride(table="persons"),
            "people/name": UseOverride(column="display_name"),
            "people/home": UseOverride(prefix="residence"),
        },
    )
    configured_rows = decompose(source, prepare(derive(Document), mapping))
    print("Configured tables:")
    print(json.dumps(configured_rows, indent=2))
    assert configured_rows["persons"][0]["display_name"] == "Ada"
    assert configured_rows["persons"][0]["residence_city"] == "Oslo"
    assert reconstruct(configured_rows, prepare(derive(Document), mapping)) == source


if __name__ == "__main__":
    main()
