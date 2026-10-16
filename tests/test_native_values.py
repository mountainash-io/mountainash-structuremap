from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

import pytest

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct


@dataclass(frozen=True)
class Line:
    country: Literal["AU", "NZ"]
    amount: Decimal
    effective: date | None
    source_date: str


def _line_values() -> tuple[Line, ...]:
    return (
        Line("AU", Decimal("0.01"), date(2023, 9, 30), "2023-09"),
        Line("NZ", Decimal("1000000"), None, "2023-09"),
    )


def test_explicit_dataclass_sequence_root_preserves_native_values():
    root = tuple[Line, ...]
    original = _line_values()
    encoded = decompose(original, prepare(derive(root, binding="dataclass")))

    rebuilt = reconstruct(encoded, prepare(derive(root, binding="dataclass")))

    assert rebuilt == original
    assert type(rebuilt) is tuple
    assert type(rebuilt[0]) is Line
    assert type(rebuilt[0].amount) is Decimal
    assert type(rebuilt[0].effective) is date
    assert rebuilt[0].source_date == "2023-09"
    assert rebuilt[1].effective is None


def test_explicit_dataclass_sequence_root_reconstructs_independent_native_rows():
    root = tuple[Line, ...]
    prepared = prepare(derive(root, binding="dataclass"))
    rows = {
        "root": [{"_sm_key": "doc"}],
        "root__item": [
            {
                "_sm_key": "row-late",
                "_sm_parent": "doc",
                "_sm_position": 9,
                "country": "NZ",
                "amount": "1000000",
                "effective": None,
                "source_date": "2023-09",
            },
            {
                "_sm_key": "row-early",
                "_sm_parent": "doc",
                "_sm_position": 3,
                "country": "AU",
                "amount": "0.01",
                "effective": date(2023, 9, 30),
                "source_date": "2023-09",
            },
            {
                "_sm_key": "row-duplicate",
                "_sm_parent": "doc",
                "_sm_position": 12,
                "country": "AU",
                "amount": "0.01",
                "effective": date(2023, 9, 30),
                "source_date": "2023-09",
            },
        ],
    }

    rebuilt = reconstruct(rows, prepared)

    assert rebuilt == (
        Line("AU", Decimal("0.01"), date(2023, 9, 30), "2023-09"),
        Line("NZ", Decimal("1000000"), None, "2023-09"),
        Line("AU", Decimal("0.01"), date(2023, 9, 30), "2023-09"),
    )
    assert type(rebuilt) is tuple
    assert type(rebuilt[0].amount) is Decimal
    assert rebuilt[1].effective is None
    assert type(rebuilt[2].effective) is date


def test_explicit_dataclass_sequence_root_reconstructs_empty_tuple():
    root = tuple[Line, ...]
    prepared = prepare(derive(root, binding="dataclass"))

    rebuilt = reconstruct({"root": [{"_sm_key": "doc"}], "root__item": []}, prepared)

    assert rebuilt == ()
    assert type(rebuilt) is tuple


def test_literal_domain_is_enforced_for_dataclass_values_and_independent_rows():
    @dataclass
    class Country:
        value: Literal["AU", "NZ"]

    prepared = prepare(derive(Country, binding="dataclass"))

    with pytest.raises(StructureMapError, match="AU|NZ"):
        decompose(Country("GB"), prepared)
    with pytest.raises(StructureMapError, match="AU|NZ"):
        reconstruct({"root": [{"_sm_key": "row-1", "value": "GB"}]}, prepared)


def test_heterogeneous_literal_domain_is_rejected_instead_of_erased():
    @dataclass
    class Heterogeneous:
        value: Literal["one", 1]

    with pytest.raises(StructureMapError) as caught:
        derive(Heterogeneous, binding="dataclass")

    assert caught.value.code == "unsupported_annotation"
    assert caught.value.path == "value"
    assert caught.value.remedy


def test_sequence_root_requires_an_explicitly_capable_binding():
    with pytest.raises(StructureMapError) as caught:
        derive(tuple[Line, ...])

    assert caught.value.code == "unsupported_root"
    assert "binding" in caught.value.remedy.lower()


def test_pydantic_binding_rejects_a_non_record_root_with_actionable_error():
    with pytest.raises(StructureMapError) as caught:
        derive(tuple[Line, ...], binding="pydantic")

    assert caught.value.code == "unsupported_root"
    assert caught.value.remedy
