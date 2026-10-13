from dataclasses import dataclass


@dataclass
class Entry:
    label: str | None


@dataclass
class Ledger:
    entries: list[Entry]


@dataclass
class Node:
    label: str
    children: list["Node"]
