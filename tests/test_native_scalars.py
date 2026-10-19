from datetime import date, datetime
from decimal import Decimal

import pytest
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap.codecs import CellLocation, PrimitiveCodec
from mountainash_structuremap.definitions import NumericFacets, Scalar
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.native_scalars import prepare_native_scalar


def test_native_decimal_uses_exact_lexical_text_and_round_trips():
    codec = prepare_native_scalar(Scalar(type="decimal"), "amount", None, "amount")

    assert codec.fields["amount"].type == UniversalType.STRING
    assert codec.encode(Decimal("0.01")) == ("0.01",)
    assert codec.encode(Decimal("1000000")) == ("1000000",)
    assert codec.encode(None) == (None,)
    assert codec.check(("0.01",), CellLocation("amount")) == Decimal("0.01")
    assert codec.check((None,), CellLocation("amount")) is None
    assert codec.decode(Decimal("1000000")) == Decimal("1000000")


def test_native_decimal_facets_select_exact_typed_storage():
    codec = prepare_native_scalar(
        Scalar(
            type="decimal",
            facets=NumericFacets(total_digits=6, fraction_digits=2, min_inclusive="0", max_inclusive="1000"),
        ),
        "amount",
        None,
        "amount",
    )

    assert codec.fields["amount"].type == UniversalType.NUMBER
    assert codec.encode(Decimal("0.01")) == (Decimal("0.01"),)
    assert codec.check((Decimal("1.25"),), CellLocation("amount")) == Decimal("1.25")
    with pytest.raises(StructureMapError):
        codec.encode(Decimal("1000.01"))


def test_native_decimal_rejects_float_and_noncanonical_text():
    codec = prepare_native_scalar(Scalar(type="decimal"), "amount", None, "amount")

    with pytest.raises(StructureMapError):
        codec.encode(0.01)
    with pytest.raises(StructureMapError):
        codec.check(("01.0",), CellLocation("amount"))


def test_native_date_uses_date_cells_and_rejects_datetime():
    codec = prepare_native_scalar(Scalar(type="date"), "effective", None, "effective")
    value = date(2023, 9, 30)

    assert codec.fields["effective"].type == UniversalType.DATE
    assert codec.encode(value) == (value,)
    assert codec.encode(None) == (None,)
    assert codec.check((value,), CellLocation("effective")) == value
    assert codec.check((None,), CellLocation("effective")) is None
    assert codec.decode(value) == value
    with pytest.raises(StructureMapError):
        codec.encode(datetime(2023, 9, 30))
    with pytest.raises(StructureMapError):
        codec.check((datetime(2023, 9, 30),), CellLocation("effective"))


def test_primitive_literal_domain_is_enforced_in_both_directions():
    codec = PrimitiveCodec("str", "country", "country", allowed_values=("AU", "NZ"))

    assert codec.encode("AU") == ("AU",)
    assert codec.check(("NZ",), CellLocation("country")) == "NZ"
    with pytest.raises(StructureMapError):
        codec.encode("US")
    with pytest.raises(StructureMapError):
        codec.check(("US",), CellLocation("country"))
