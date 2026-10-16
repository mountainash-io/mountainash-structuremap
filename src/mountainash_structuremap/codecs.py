from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal, Protocol

from mountainash.typespec.spec import FieldSpec
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap.errors import StructureMapError

Cell = str | int | float | bool | Decimal | datetime | date | time | None


@dataclass(frozen=True)
class CellLocation:
    path: str
    table: str | None = None
    row: int | None = None
    column: str | None = None
class EncodingCodec(Protocol):
    fields: tuple[FieldSpec, ...]

    def encode(self, value: object) -> tuple[Cell, ...]: ...


class DecodingCodec(Protocol):
    fields: tuple[FieldSpec, ...]

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object: ...

    def decode(self, checked: object) -> object: ...


class ScalarCodec(EncodingCodec, DecodingCodec, Protocol):
    """Codec supporting both directions."""


class PrimitiveCodec:
    def __init__(
        self,
        scalar_type: Literal["str", "int", "float", "bool"],
        column: str,
        path: str,
        *,
        allowed_values: tuple[str | int | float | bool, ...] | None = None,
    ):
        self.scalar_type = scalar_type
        self.column = column
        self.path = path
        self.allowed_values = allowed_values
        expected_type = {"str": str, "int": int, "float": float, "bool": bool}[scalar_type]
        if allowed_values is not None and any(type(value) is not expected_type for value in allowed_values):
            raise StructureMapError(
                "invalid_literal_domain",
                f"Literal values must have exact type {scalar_type}",
                path=path,
                remedy="Declare homogeneous Literal values matching the primitive type",
            )
        physical = {
            "str": UniversalType.STRING,
            "int": UniversalType.INTEGER,
            "float": UniversalType.NUMBER,
            "bool": UniversalType.BOOLEAN,
        }[scalar_type]
        self.fields: tuple[FieldSpec, ...] = (FieldSpec(name=column, type=physical, missing_values=[]),)

    def _check_domain(self, value: object, path: str, location: CellLocation | None = None) -> None:
        if self.allowed_values is not None and not any(
            type(value) is type(candidate) and value == candidate for candidate in self.allowed_values
        ):
            raise StructureMapError(
                "literal_domain_violation",
                f"Value {value!r} is outside the declared Literal domain",
                path=path,
                table=location.table if location else None,
                row=location.row if location else None,
                column=self.column,
                owner="executor restriction",
                remedy=f"Supply one of the declared Literal values {self.allowed_values!r}",
            )

    def encode(self, value: object) -> tuple[Cell, ...]:
        if self.scalar_type == "str":
            valid = type(value) is str
        elif self.scalar_type == "int":
            valid = type(value) is int
        elif self.scalar_type == "float":
            valid = type(value) is float
        else:
            valid = type(value) is bool
        if not valid:
            raise StructureMapError(
                "native_type_mismatch", f"Expected {self.scalar_type}, got {type(value).__name__}",
                path=self.path, owner="executor restriction", remedy=f"Supply a native {self.scalar_type} value",
            )
        self._check_domain(value, self.path)
        if self.scalar_type == "int" and not -(2**63) <= value < 2**63:  # type: ignore[operator]
            raise StructureMapError(
                "value_out_of_range", "Integer does not fit signed i64", path=self.path,
                owner="executor restriction", remedy="Supply an integer in the signed i64 range",
            )
        return (value,)  # type: ignore[return-value]

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        valid = (
            type(value) is str if self.scalar_type == "str" else
            type(value) is int if self.scalar_type == "int" else
            type(value) is float if self.scalar_type == "float" else type(value) is bool
        )
        if not valid:
            raise StructureMapError(
                "cell_type_mismatch", f"Expected {self.scalar_type} cell, got {type(value).__name__}",
                path=location.path, table=location.table, row=location.row, column=self.column,
                owner="executor restriction", remedy=f"Supply a {self.scalar_type} cell in the table",
            )
        self._check_domain(value, location.path, location)
        if self.scalar_type == "int" and not -(2**63) <= value < 2**63:  # type: ignore[operator]
            raise StructureMapError(
                "value_out_of_range", "Integer does not fit signed i64", path=location.path,
                table=location.table, row=location.row, column=self.column,
                owner="executor restriction", remedy="Supply an integer in the signed i64 range",
            )
        return value


    def decode(self, checked: object) -> object:
        return checked


