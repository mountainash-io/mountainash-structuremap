from dataclasses import dataclass, field


@dataclass
class Address:
    city: str


@dataclass
class Person:
    name: str
    home: Address
    tags: list[str | None] = field(default_factory=list)
    age: int = 0
    active: bool = True
    score: float = 0.0


@dataclass
class Document:
    people: list[Person]


@dataclass
class Empty:
    pass


@dataclass
class RequiredEmpty:
    empty: Empty
