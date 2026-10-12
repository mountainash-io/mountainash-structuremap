from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NestedDocument:
    @dataclass
    class Address:
        city: str

    Count = int

    home: Address
    count: Count
