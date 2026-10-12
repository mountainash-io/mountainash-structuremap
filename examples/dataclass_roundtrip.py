"""Run with `hatch run test:python examples/dataclass_roundtrip.py`."""

import json
from dataclasses import dataclass, field

from mountainash_structuremap import decompose, derive, prepare, reconstruct


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


if __name__ == "__main__":
    main()
