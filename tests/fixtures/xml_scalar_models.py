from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import ClassVar
from xml.etree.ElementTree import QName

from xsdata.models.datatype import (
    XmlDate,
    XmlDateTime,
    XmlDuration,
    XmlPeriod,
    XmlTime,
)


@dataclass
class UnboundedAmount:
    amount: Decimal = field(metadata={"type": "Element"})


@dataclass
class FacetedAmount:
    amount: Decimal = field(metadata={"type": "Element", "total_digits": 3, "fraction_digits": 2})


@dataclass
class Timed:
    when: XmlDateTime | None = field(default=None, metadata={"type": "Element", "nillable": True})


@dataclass
class Calendar:
    date: XmlDate = field(metadata={"type": "Element"})
    time: XmlTime = field(metadata={"type": "Element"})
    period: XmlPeriod = field(metadata={"type": "Element"})
    duration: XmlDuration = field(metadata={"type": "Element"})


@dataclass
class IntegerValue:
    value: int = field(metadata={"type": "Element"})


@dataclass
class NumericBoundaries:
    amount: Decimal = field(
        metadata={
            "type": "Element",
            "total_digits": 3,
            "fraction_digits": 2,
            "min_inclusive": "-9.99",
            "max_inclusive": "9.99",
        }
    )


@dataclass
class TemporalValues:
    date: XmlDate = field(metadata={"type": "Element"})
    time: XmlTime = field(metadata={"type": "Element"})
    period: XmlPeriod = field(metadata={"type": "Element"})
    duration: XmlDuration = field(metadata={"type": "Element"})


@dataclass
class NativeAtomicValues:
    bounded: int = field(
        metadata={
            "type": "Element",
            "min_inclusive": "-100",
            "max_inclusive": "100",
        }
    )
    hex_binary: bytes = field(metadata={"type": "Element", "format": "base16"})
    base64_binary: bytes = field(metadata={"type": "Element", "format": "base64"})
    qname: QName = field(metadata={"type": "Element"})


class DecimalState(Enum):
    SMALL = Decimal("1.25")
    LARGE = Decimal("9007199254740993.125")


@dataclass
class ConstructedScalarSibling:
    constructions: ClassVar[int] = 0
    value: str = field(metadata={"type": "Element"})

    def __post_init__(self) -> None:
        type(self).constructions += 1


@dataclass
class LaterScalarValidation:
    constructions: ClassVar[int] = 0
    first: ConstructedScalarSibling = field(metadata={"type": "Element"})
    later: int = field(metadata={"type": "Element"})

    def __post_init__(self) -> None:
        type(self).constructions += 1


@dataclass
class EnumScalar:
    value: DecimalState = field(metadata={"type": "Element"})


@dataclass
class PhysicalValues:
    amount: Decimal | None = field(
        metadata={
            "type": "Element",
            "total_digits": 20,
            "fraction_digits": 3,
        }
    )
    stamp: XmlDateTime | None = field(metadata={"type": "Element"})
    clock: XmlTime | None = field(metadata={"type": "Element"})


@dataclass
class PhysicalDocument:
    values: list[PhysicalValues] = field(default_factory=list, metadata={"type": "Element"})


@dataclass
class PrecisionBoundary:
    wide: Decimal = field(metadata={"type": "Element", "total_digits": 38, "fraction_digits": 0})
    wider: Decimal = field(metadata={"type": "Element", "total_digits": 38, "fraction_digits": 1})
    bounded: Decimal = field(
        metadata={
            "type": "Element",
            "total_digits": 38,
            "fraction_digits": 1,
            "min_inclusive": "-9",
            "max_inclusive": "9",
        }
    )


@dataclass
class LaterTemporalValidation:
    first: ConstructedScalarSibling = field(metadata={"type": "Element"})
    later: XmlDateTime = field(metadata={"type": "Element"})


class Count(Enum):
    ONE = 1


class HugeCount(Enum):
    BIG = 10**30


@dataclass
class CountValue:
    value: Count = field(metadata={"type": "Element"})


@dataclass
class HugeCountValue:
    value: HugeCount = field(metadata={"type": "Element"})


@dataclass
class BooleanFloat:
    flag: bool = field(metadata={"type": "Element"})
    number: float = field(metadata={"type": "Element"})
