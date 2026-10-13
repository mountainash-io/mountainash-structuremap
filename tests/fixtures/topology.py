from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    label: str
    children: list[Node] = field(default_factory=list)


@dataclass
class Branch:
    label: str
    left: Branch | None = None
    right: Branch | None = None


@dataclass
class A:
    label: str
    other: B | None = None


@dataclass
class B:
    count: int
    others: list[A] = field(default_factory=list)


@dataclass
class Nested:
    grid: list[list[int | None] | None]
    lookup: dict[str, list[int | None]]
    coordinates: tuple[str, int]


@dataclass
class Detail:
    label: str | None
    tags: list[str] = field(default_factory=list)


@dataclass
class Members:
    entries: list[Detail | None]


@dataclass
class Address:
    city: str
    tags: list[str]


@dataclass
class Uses:
    home: Address
    work: Address


@dataclass
class Forest:
    home: Node
    work: Node


@dataclass
class Containers:
    maps: list[dict[str, int] | None]
    tuples: list[tuple[str | None, list[int]] | None]
    mapped: dict[str, Detail | None]
