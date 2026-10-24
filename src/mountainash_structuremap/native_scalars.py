from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import NoReturn

from mountainash import DecimalDtype
from mountainash.typespec.spec import FieldDefinition
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap.codecs import Cell, CellLocation, ScalarCodec
from mountainash_structuremap.definitions import NumericFacets, Scalar
from mountainash_structuremap.errors import StructureMapError


def decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("xs:decimal has only finite values")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("xs:decimal has a non-finite exponent")
    raw = "".join(str(digit) for digit in digits) or "0"
    if exponent >= 0:
        whole, fraction = raw + "0" * exponent, ""
    else:
        point = len(raw) + exponent
        whole, fraction = (raw[:point] or "0", raw[point:]) if point > 0 else ("0", "0" * -point + raw)
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    result = whole + ("." + fraction if fraction else "")
    return ("-" if sign and result != "0" else "") + result


def _bounded_decimal_precision(facets: NumericFacets | None, fraction_digits: int) -> int | None:
    if facets is None:
        return None
    if (facets.min_inclusive or facets.min_exclusive) is None or (facets.max_inclusive or facets.max_exclusive) is None:
        return None
    integral_digits = 0
    for name, bound in (
        ("min_inclusive", facets.min_inclusive),
        ("max_inclusive", facets.max_inclusive),
        ("min_exclusive", facets.min_exclusive),
        ("max_exclusive", facets.max_exclusive),
    ):
        if bound is None:
            continue
        value = Decimal(bound).copy_abs()
        digits = max(0, value.adjusted() + 1) if value else 0
        reduces_magnitude = (
            name == "min_exclusive" and Decimal(bound) < 0 or name == "max_exclusive" and Decimal(bound) > 0
        )
        coefficient = value.as_tuple().digits
        if reduces_magnitude and digits and coefficient[0] == 1 and not any(coefficient[1:]):
            digits -= 1
        integral_digits = max(integral_digits, digits)
    return max(1, integral_digits + fraction_digits)


class DecimalCodec:
    fields: Mapping[str, FieldDefinition]

    def __init__(self, declaration: Scalar, column: str, path: str, policy: str | None):
        self.column, self.path = column, path
        self.facets = declaration.facets
        bounds = []
        if self.facets is not None:
            for name in ("min_inclusive", "max_inclusive", "min_exclusive", "max_exclusive"):
                text = getattr(self.facets, name)
                if text is None:
                    continue
                try:
                    bound = Decimal(text)
                    if not bound.is_finite():
                        raise ValueError
                except (ValueError, InvalidOperation) as exc:
                    raise StructureMapError(
                        "invalid_numeric_facets", f"{name} requires an exact finite Decimal bound", path=path,
                        remedy="Declare a finite numeric bound",
                    ) from exc
                bounds.append((name, bound))
        self.bounds = tuple(bounds)
        total = self.facets.total_digits if self.facets is not None else None
        fraction = self.facets.fraction_digits if self.facets is not None else None
        precision = None
        if total is not None and fraction is not None:
            declared_precision = total + fraction
            precision = declared_precision if declared_precision <= 38 else _bounded_decimal_precision(self.facets, fraction)
        if policy == "typed" and (total is None or fraction is None):
            raise StructureMapError(
                "unrepresentable_scalar", "Typed Decimal requires total_digits and fraction_digits facets",
                path=path, owner="binding limitation", remedy="Supply both facets or use lexical value_space",
            )
        if policy == "typed" and (precision is None or precision > 38):
            raise StructureMapError(
                "unrepresentable_scalar", "Typed Decimal domain exceeds portable precision 38",
                path=path, owner="binding limitation", remedy="Use lexical value_space or narrower facets",
            )
        self.typed = policy == "typed" or policy is None and precision is not None and precision <= 38
        if self.typed:
            assert precision is not None and fraction is not None
            self.fields = {column: FieldDefinition(type=UniversalType.NUMBER, dtype=DecimalDtype(precision, fraction), missing_values=[])}
        else:
            self.fields = {column: FieldDefinition(type=UniversalType.STRING, missing_values=[])}

    def _validate_facets(self, value: Decimal, path: str, location: CellLocation | None = None) -> None:
        if not value.is_finite():
            self._error("invalid_scalar", "xs:decimal must be finite", path, location)
        if self.facets is None:
            return
        _, digits, exponent = value.as_tuple()
        assert isinstance(exponent, int)
        length = len(digits)
        while length and digits[length - 1] == 0:
            length -= 1
            exponent += 1
        significant = length + max(exponent, 0) if length else 1
        fraction_digits = max(-exponent, 0) if length else 0
        if self.facets.total_digits is not None and significant > self.facets.total_digits:
            self._error("facet_violation", "Decimal exceeds total_digits facet", path, location)
        if self.facets.fraction_digits is not None and fraction_digits > self.facets.fraction_digits:
            self._error("facet_violation", "Decimal exceeds fraction_digits facet", path, location)
        for name, edge in self.bounds:
            if (
                name == "min_inclusive" and value < edge or name == "max_inclusive" and value > edge
                or name == "min_exclusive" and value <= edge or name == "max_exclusive" and value >= edge
            ):
                self._error("facet_violation", f"Decimal violates {name} facet", path, location)

    def encode(self, value: object) -> tuple[Cell, ...]:
        if value is None:
            return (None,)
        if not isinstance(value, Decimal):
            self._error("invalid_scalar", f"Expected Decimal, got {type(value).__name__}", self.path)
        self._validate_facets(value, self.path)
        if self.typed:
            return (value,)
        try:
            return (decimal_text(value),)
        except ValueError:
            self._error("invalid_scalar", "xs:decimal must be finite", self.path)

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if value is None:
            return None
        if self.typed:
            if not isinstance(value, Decimal):
                self._error("cell_type_mismatch", "Expected exact Decimal table cell", location.path, location)
            result = value
        else:
            if type(value) is not str:
                self._error("cell_type_mismatch", "Expected canonical decimal lexical text", location.path, location)
            try:
                result = Decimal(value)
            except InvalidOperation:
                self._error("invalid_scalar", "Malformed decimal text", location.path, location)
            try:
                canonical = decimal_text(result)
            except ValueError:
                self._error("invalid_scalar", "xs:decimal must be finite", location.path, location)
            if canonical != value:
                self._error("noncanonical_scalar", "Decimal text must use canonical xs:decimal form", location.path, location)
        self._validate_facets(result, location.path, location)
        return result

    def decode(self, checked: object) -> object:
        return checked

    @staticmethod
    def _error(code: str, message: str, path: str, location: CellLocation | None = None) -> NoReturn:
        raise StructureMapError(
            code, message, path=path, table=location.table if location else None,
            row=location.row if location else None, column=location.column if location else None,
            owner="executor restriction", remedy="Supply a canonical Decimal value satisfying its declared facets",
        )


class NativeDateCodec:
    def __init__(self, column: str, path: str):
        self.column, self.path = column, path
        self.fields: Mapping[str, FieldDefinition] = {column: FieldDefinition(type=UniversalType.DATE, missing_values=[])}

    def encode(self, value: object) -> tuple[Cell, ...]:
        if value is None:
            return (None,)
        if type(value) is not date:
            raise StructureMapError(
                "invalid_scalar", f"Expected date, got {type(value).__name__}", path=self.path,
                owner="executor restriction", remedy="Supply a native datetime.date value, not datetime",
            )
        return (value,)

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if value is None:
            return None
        if type(value) is not date:
            raise StructureMapError(
                "cell_type_mismatch", f"Expected date cell, got {type(value).__name__}", path=location.path,
                table=location.table, row=location.row, column=self.column,
                owner="executor restriction", remedy="Supply a datetime.date cell, not datetime",
            )
        return value

    def decode(self, checked: object) -> object:
        return checked


def prepare_native_scalar(declaration: Scalar, column: str, policy: str | None, path: str) -> ScalarCodec:
    if declaration.type == "decimal":
        return DecimalCodec(declaration, column, path, policy)
    if declaration.type == "date":
        return NativeDateCodec(column, path)
    raise StructureMapError(
        "unavailable_scalar", f"Native scalar {declaration.type!r} is unavailable", path=path,
        owner="binding gap", remedy="Use a supported native Decimal or date scalar",
    )
