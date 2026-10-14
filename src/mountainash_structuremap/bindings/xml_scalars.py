from __future__ import annotations

import re
from enum import Enum
from math import isnan
from typing import NoReturn, cast

from mountainash.typespec.spec import FieldSpec
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap.codecs import Cell, CellLocation, PrimitiveCodec, ScalarCodec
from mountainash_structuremap.definitions import Scalar
from mountainash_structuremap.errors import StructureMapError

_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")


class XmlIntegerCodec:
    """Use lexical storage to avoid silently imposing Mountainash's i64 bound."""

    def __init__(self, column: str, path: str):
        self.column = column
        self.path = path
        self.fields: tuple[FieldSpec, ...] = (FieldSpec(name=column, type=UniversalType.STRING, missing_values=[]),)

    def encode(self, value: object) -> tuple[Cell, ...]:
        if not isinstance(value, int) or isinstance(value, bool):
            raise StructureMapError(
                "invalid_scalar", f"Expected int, got {type(value).__name__}", path=self.path,
                owner="executor restriction", remedy="Provide a native integer value",
            )
        try:
            return (str(value),)
        except ValueError as exc:
            raise StructureMapError(
                "invalid_scalar", "Integer exceeds Python's configured decimal conversion limit", path=self.path,
                owner="executor restriction", remedy="Use a value within Python's configured integer conversion limit",
            ) from exc

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if not isinstance(value, str) or not _INTEGER.fullmatch(value):
            raise StructureMapError(
                "invalid_scalar", "Expected a canonical XML integer lexical value", path=location.path,
                table=location.table, row=location.row, column=self.column, owner="executor restriction",
                remedy=f"Supply 0 or a canonical signed decimal integer in column {self.column!r}",
            )
        try:
            return int(value)
        except ValueError as exc:
            raise StructureMapError(
                "invalid_scalar", "Integer exceeds Python's configured decimal conversion limit",
                path=location.path, table=location.table, row=location.row, column=self.column,
                owner="executor restriction",
                remedy="Use a value within Python's configured integer conversion limit",
            ) from exc

    def decode(self, checked: object) -> object:
        if type(checked) is not int:
            raise TypeError("validated XML integer is not an int")
        return checked


class XmlEnumCodec:
    def __init__(self, enum_type: type[Enum], column: str, path: str):
        self.enum_type = enum_type
        self.column = column
        self.path = path
        self.members = tuple(enum_type.__members__.values())
        values = tuple(object.__getattribute__(member, "_value_") for member in self.members)
        if not values or any(type(value) not in (str, int, float, bool) for value in values):
            raise StructureMapError(
                "unsupported_enum", f"Enum {enum_type.__qualname__} must have primitive values", path=path,
                owner="native limitation", remedy="Use a string, integer, float, or boolean-valued enum",
            )
        first_type = type(values[0])
        if any(type(value) is not first_type for value in values):
            raise StructureMapError(
                "unsupported_enum", f"Enum {enum_type.__qualname__} mixes primitive value types", path=path,
                owner="native limitation", remedy="Use one primitive value type for every enum member",
            )
        self.values = values
        field_type = {
            str: UniversalType.STRING,
            int: UniversalType.INTEGER,
            float: UniversalType.NUMBER,
            bool: UniversalType.BOOLEAN,
        }[first_type]
        if first_type is int and any(not -(2**63) <= value < 2**63 for value in values):
            field_type = UniversalType.STRING
            self.lexical_integer = True
        else:
            self.lexical_integer = False
        self.fields: tuple[FieldSpec, ...] = (FieldSpec(name=column, type=field_type, missing_values=[]),)

    def encode(self, value: object) -> tuple[Cell, ...]:
        if type(value) is not self.enum_type:
            raise StructureMapError(
                "invalid_scalar", f"Expected {self.enum_type.__qualname__}, got {type(value).__name__}",
                path=self.path, owner="executor restriction",
                remedy=f"Provide a {self.enum_type.__qualname__} member",
            )
        raw = object.__getattribute__(value, "_value_")
        try:
            return (str(raw) if self.lexical_integer else cast(Cell, raw),)
        except ValueError as exc:
            raise StructureMapError(
                "invalid_scalar", "Enum value exceeds Python's configured decimal conversion limit",
                path=self.path, owner="executor restriction",
                remedy="Use an enum value within Python's configured integer conversion limit",
            ) from exc

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if self.lexical_integer:
            if not isinstance(value, str) or not _INTEGER.fullmatch(value):
                self._invalid_value(location)
            try:
                parsed = int(value)
            except ValueError as exc:
                raise StructureMapError(
                    "invalid_scalar", "Enum integer exceeds Python's configured decimal conversion limit",
                    path=location.path, table=location.table, row=location.row, column=self.column,
                    owner="executor restriction",
                    remedy="Supply a declared enum value within Python's configured integer conversion limit",
                ) from exc
            value = parsed
        for index, candidate in enumerate(self.values):
            if type(value) is type(candidate) and (
                value == candidate or isinstance(value, float) and isinstance(candidate, float) and isnan(value) and isnan(candidate)
            ):
                return index
        self._invalid_value(location)

    def _invalid_value(self, location: CellLocation) -> NoReturn:
        raise StructureMapError(
            "invalid_scalar", f"Value is not a member of {self.enum_type.__qualname__}",
            path=location.path, table=location.table, row=location.row, column=self.column,
            owner="executor restriction",
            remedy=f"Supply one of the declared {self.enum_type.__qualname__} values",
        )

    def decode(self, checked: object) -> object:
        return self.members[cast(int, checked)]


def prepare_xml_scalar(
    declaration: Scalar, native_type: type | None, column: str, path: str
) -> ScalarCodec:
    if declaration.type == "enum":
        if native_type is None or not issubclass(native_type, Enum):
            raise StructureMapError(
                "invalid_native_scalar", "Enum scalar has no associated native enum type", path=path,
                owner="native limitation", remedy="Derive the enum through the xsdata binding",
            )
        return XmlEnumCodec(native_type, column, path)
    if declaration.type == "int":
        return XmlIntegerCodec(column, path)
    if declaration.type in ("str", "bool", "float"):
        return PrimitiveCodec(declaration.type, column, path)
    raise StructureMapError(
        "unavailable_scalar", f"XML scalar {declaration.type!r} is not available in this delivery", path=path,
        owner="binding gap", remedy="Use str, bool, float, int, or an enum-valued XML field",
    )
