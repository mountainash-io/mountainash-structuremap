"""Run with `hatch run test:python examples/dataclass_roundtrip.py`."""

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

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


@dataclass(frozen=True)
class Line:
    country: Literal["AU", "NZ"]
    amount: Decimal
    effective: date | None
    source_date: str


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

    lines = (
        Line("AU", Decimal("0.01"), date(2023, 9, 30), "2023-09"),
        Line("NZ", Decimal("1000000"), None, "2023"),
        Line("AU", Decimal("0.01"), date(2023, 9, 30), "2023-09"),
    )
    line_plan = prepare(derive(tuple[Line, ...], binding="dataclass"))
    line_rows = decompose(lines, line_plan)
    print("Typed sequence-root rows:", line_rows)
    restored = reconstruct(
        {name: list(reversed(rows)) for name, rows in line_rows.items()},
        prepare(derive(tuple[Line, ...], binding="dataclass")),
    )
    assert restored == lines
    assert type(restored) is tuple
    assert type(restored[0].amount) is Decimal
    assert type(restored[0].effective) is date
    assert reconstruct(decompose((), line_plan), line_plan) == ()
    print("Reconstructed typed sequence:", restored)


if __name__ == "__main__":
    main()
