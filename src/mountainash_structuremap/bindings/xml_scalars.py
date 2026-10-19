from __future__ import annotations

import base64
import binascii
import re
from decimal import Decimal, InvalidOperation
from enum import Enum
from math import copysign, isinf, isnan
from typing import Literal, NoReturn, cast
from xml.etree.ElementTree import QName

from mountainash.typespec.spec import FieldSpec
from mountainash.typespec.universal_types import UniversalType
from xsdata.exceptions import ConverterError
from xsdata.formats.converter import QNameConverter
from xsdata.models.datatype import (
    XmlBase64Binary,
    XmlDate,
    XmlDateTime,
    XmlDuration,
    XmlHexBinary,
    XmlPeriod,
    XmlTime,
)

from mountainash_structuremap.bindings.xml_temporals import XmlTemporalCodec
from mountainash_structuremap.codecs import Cell, CellLocation, PrimitiveCodec, ScalarCodec
from mountainash_structuremap.definitions import NumericFacets, Scalar
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.native_scalars import DecimalCodec


def _float_text(value: float) -> str:
    if isnan(value):
        return "NaN"
    if isinf(value):
        return "-INF" if value < 0 else "INF"
    if value == 0:
        return "-0.0E0" if copysign(1, value) < 0 else "0.0E0"
    sign, digits, exponent = Decimal(repr(value)).as_tuple()
    assert isinstance(exponent, int)
    coefficient = "".join(str(digit) for digit in digits)
    scientific_exponent = exponent + len(coefficient) - 1
    coefficient = coefficient.rstrip("0")
    return ("-" if sign else "") + coefficient[0] + "." + (coefficient[1:] or "0") + "E" + str(scientific_exponent)


class XmlLexicalPrimitiveCodec:
    """Canonical XML boolean/double text with exact native inverse."""

    def __init__(self, family: Literal["bool", "float"], column: str, path: str):
        self.family, self.column, self.path = family, column, path
        self.primitive = PrimitiveCodec(family, column, path)
        self.fields = {column: FieldSpec(type=UniversalType.STRING, missing_values=[])}

    def encode(self, value: object) -> tuple[Cell, ...]:
        self.primitive.encode(value)
        return (self._text(value),)

    def _text(self, value: object) -> str:
        if self.family == "bool":
            return "true" if value else "false"
        return _float_text(cast(float, value))

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        try:
            if not isinstance(value, str):
                raise ValueError
            if self.family == "bool":
                if value not in ("true", "false"):
                    raise ValueError
                parsed: bool | float = value == "true"
            else:
                parsed = float(value)
            if self._text(parsed) != value:
                raise ValueError
        except ValueError:
            raise StructureMapError(
                "noncanonical_scalar",
                f"Expected canonical XML {self.family} text",
                path=location.path,
                table=location.table,
                row=location.row,
                column=self.column,
                remedy=f"Supply canonical lexical {self.family} text or select typed value_space",
            ) from None
        return parsed

    def decode(self, checked: object) -> object:
        return checked



class XmlBinaryCodec:
    fields: dict[str, FieldSpec]

    def __init__(self, native_type: type | None, column: str, path: str, format: str | None):
        self.native_type = native_type or bytes
        if not isinstance(self.native_type, type) or not issubclass(self.native_type, bytes):
            raise StructureMapError(
                "unsupported_native_scalar",
                "Unsupported XML binary native class",
                path=path,
                owner="binding limitation",
                remedy="Use a bytes subclass, XmlHexBinary or XmlBase64Binary",
            )
        if format is None:
            if issubclass(self.native_type, XmlHexBinary):
                format = "base16"
            elif issubclass(self.native_type, XmlBase64Binary):
                format = "base64"
        if format not in ("base16", "base64"):
            raise StructureMapError(
                "invalid_scalar_format",
                f"XML binary requires a declared native encoding, got {format!r}",
                path=path,
                owner="binding limitation",
                remedy="Set native field metadata format='base16' or format='base64'",
            )
        self.hex = format == "base16"
        if (
            issubclass(self.native_type, XmlHexBinary)
            and not self.hex
            or (issubclass(self.native_type, XmlBase64Binary) and self.hex)
        ):
            raise StructureMapError(
                "invalid_scalar_format",
                "Binary format conflicts with native binary class",
                path=path,
                owner="binding limitation",
                remedy="Align format with the declared native binary class",
            )
        self.column, self.path = column, path
        self.fields = {column: FieldSpec(type=UniversalType.STRING, missing_values=[])}

    def encode(self, value: object) -> tuple[Cell, ...]:
        if value is None:
            return (None,)
        if not isinstance(value, bytes):
            raise StructureMapError(
                "invalid_scalar",
                "Expected XML binary bytes",
                path=self.path,
                owner="executor restriction",
                remedy="Supply the declared binary value",
            )
        return ((value.hex().upper() if self.hex else base64.b64encode(value).decode("ascii")),)

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if value is None:
            return None
        if type(value) is not str:
            raise StructureMapError(
                "cell_type_mismatch",
                "Expected binary lexical text",
                path=location.path,
                table=location.table,
                row=location.row,
                column=self.column,
                owner="executor restriction",
                remedy="Supply canonical hex/base64 text",
            )
        try:
            if self.hex:
                if len(value) % 2 or not re.fullmatch(r"(?:[0-9A-F]{2})*", value):
                    raise ValueError
                return bytes.fromhex(value)
            decoded = base64.b64decode(value, validate=True)
            if base64.b64encode(decoded).decode("ascii") != value:
                raise ValueError
            return decoded
        except (ValueError, binascii.Error):
            raise StructureMapError(
                "noncanonical_scalar",
                "Malformed or noncanonical XML binary text",
                path=location.path,
                table=location.table,
                row=location.row,
                column=self.column,
                owner="executor restriction",
                remedy="Supply canonical base64 or uppercase hexadecimal text",
            ) from None

    def decode(self, checked: object) -> object:
        if checked is None:
            return None
        return self.native_type(cast(bytes, checked))


class XmlQNameCodec:
    fields: dict[str, FieldSpec]

    def __init__(self, native_type: type | None, column: str, path: str):
        self.native_type = native_type or QName
        if not issubclass(self.native_type, QName):
            raise StructureMapError(
                "unsupported_native_scalar",
                "Expected an XML QName native type",
                path=path,
                owner="binding limitation",
                remedy="Use xml.etree.ElementTree.QName",
            )
        self.column, self.path = column, path
        self.fields = {column: FieldSpec(type=UniversalType.STRING, missing_values=[])}

    def encode(self, value: object) -> tuple[Cell, ...]:

        if value is None:
            return (None,)
        if type(value) is not self.native_type:
            raise StructureMapError(
                "invalid_scalar",
                "Expected the declared QName native type",
                path=self.path,
                owner="executor restriction",
                remedy="Supply a QName value",
            )
        value = cast(QName, value)
        text = value.text[2:] if value.text.startswith("{}") else value.text
        if not self._valid(text):
            raise StructureMapError(
                "invalid_scalar",
                "QName needs expanded identity, not an unbound prefix",
                path=self.path,
                owner="executor restriction",
                remedy="Supply a QName with an expanded namespace",
            )
        return (text,)

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if value is None:
            return None
        if not isinstance(value, str) or not self._valid(value):
            raise StructureMapError(
                "invalid_scalar",
                "QName cell must be an expanded QName or local name",
                path=location.path,
                table=location.table,
                row=location.row,
                column=self.column,
                owner="executor restriction",
                remedy="Supply a Clark-notation QName such as {namespace}local",
            )
        return value

    @staticmethod
    def _valid(text: str) -> bool:
        try:
            namespace, local = QNameConverter.resolve(text)
        except ConverterError:
            return False
        return text == (f"{{{namespace}}}{local}" if namespace else local)

    def decode(self, checked: object) -> object:
        return None if checked is None else self.native_type(cast(str, checked))


class XmlBoundedIntegerCodec:
    fields: dict[str, FieldSpec]

    def __init__(self, facets: NumericFacets, column: str, path: str):
        self.column, self.path = column, path
        self.facets = facets
        bounds = _integer_bounds(facets, path)
        if bounds is None or bounds[0] < -(2**63) or bounds[1] >= 2**63:
            raise StructureMapError(
                "unrepresentable_scalar",
                "Integer facets do not prove signed-i64 fit",
                path=path,
                owner="binding limitation",
                remedy="Declare signed-i64-proving bounds or use lexical value_space",
            )
        self.bounds = bounds
        self.fields = {column: FieldSpec(type=UniversalType.INTEGER, missing_values=[])}

    def encode(self, value: object) -> tuple[Cell, ...]:
        if type(value) is not int:
            raise StructureMapError(
                "invalid_scalar",
                "Expected XML integer",
                path=self.path,
                owner="executor restriction",
                remedy="Provide an integer satisfying the declared XML facets",
            )
        self._check_bound(value, self.path)
        return (value,)

    def decode(self, checked: object) -> object:
        if type(checked) is not int:
            raise TypeError("validated bounded XML integer is not an int")
        return checked

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if type(value) is not int:
            raise StructureMapError(
                "cell_type_mismatch",
                "Expected bounded integer cell",
                path=location.path,
                table=location.table,
                row=location.row,
                column=self.column,
                owner="executor restriction",
                remedy="Supply an integer in the declared range",
            )
        self._check_bound(value, location.path, location)
        return value

    def _check_bound(self, value: int, path: str, location: CellLocation | None = None) -> None:
        lo, hi = self.bounds
        digits = len(_integer_text(abs(value)))
        if self.facets.total_digits is not None and digits > self.facets.total_digits:
            raise StructureMapError(
                "facet_violation",
                "Integer exceeds total_digits facet",
                path=path,
                table=location.table if location else None,
                row=location.row if location else None,
                column=self.column if location else None,
                owner="executor restriction",
                remedy="Supply an integer satisfying the declared XML facets",
            )
        if not lo <= value <= hi:
            raise StructureMapError(
                "facet_violation",
                "Integer violates declared bounds",
                path=path,
                table=location.table if location else None,
                row=location.row if location else None,
                column=self.column if location else None,
                owner="executor restriction",
                remedy="Supply an integer satisfying the declared XML facets",
            )


def _integer_bounds(facets: NumericFacets, path: str) -> tuple[int, int] | None:
    lower: int | None = None
    upper: int | None = None
    if facets.total_digits is not None and facets.total_digits <= 19:
        upper = 10**facets.total_digits - 1
        lower = -upper
    for name, text in (
        ("min_inclusive", facets.min_inclusive),
        ("max_inclusive", facets.max_inclusive),
        ("min_exclusive", facets.min_exclusive),
        ("max_exclusive", facets.max_exclusive),
    ):
        if text is None:
            continue
        try:
            value = Decimal(text)
            if not value.is_finite() or value != value.to_integral_value():
                raise ValueError
            bound = int(value)
        except (ValueError, InvalidOperation) as exc:
            raise StructureMapError(
                "invalid_numeric_facets",
                f"{name} must be an exact finite integer bound",
                path=path,
                remedy="Declare an integral numeric bound for this integer field",
            ) from exc
        if name == "min_exclusive":
            bound += 1
        elif name == "max_exclusive":
            bound -= 1
        if name.startswith("min"):
            lower = bound if lower is None else max(lower, bound)
        else:
            upper = bound if upper is None else min(upper, bound)
    return (lower, upper) if lower is not None and upper is not None else None


def _integer_text(value: int) -> str:
    if value == 0:
        return "0"
    negative = value < 0
    value = abs(value)
    chunks: list[str] = []
    base = 10**500
    while value:
        value, remainder = divmod(value, base)
        chunks.append(str(remainder) if not value else f"{remainder:0500d}")
    return ("-" if negative else "") + "".join(reversed(chunks))


def _parse_integer(value: str) -> int:
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    result = 0
    for index in range(0, len(digits), 500):
        chunk = digits[index : index + 500]
        result = result * 10 ** len(chunk) + int(chunk)
    return -result if negative else result


_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")


class XmlIntegerCodec:
    """Use lexical storage to avoid silently imposing Mountainash's i64 bound."""

    def __init__(self, column: str, path: str, facets: NumericFacets | None = None):
        self.column = column
        self.path = path
        self.facets = facets
        self.fields: dict[str, FieldSpec] = {column: FieldSpec(type=UniversalType.STRING, missing_values=[])}

    def encode(self, value: object) -> tuple[Cell, ...]:
        if not isinstance(value, int) or isinstance(value, bool):
            raise StructureMapError(
                "invalid_scalar",
                f"Expected int, got {type(value).__name__}",
                path=self.path,
                owner="executor restriction",
                remedy="Provide a native integer value",
            )
        self._validate_facets(value, self.path)
        return (_integer_text(value),)

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if not isinstance(value, str) or not _INTEGER.fullmatch(value):
            raise StructureMapError(
                "invalid_scalar",
                "Expected a canonical XML integer lexical value",
                path=location.path,
                table=location.table,
                row=location.row,
                column=self.column,
                owner="executor restriction",
                remedy=f"Supply 0 or a canonical signed decimal integer in column {self.column!r}",
            )
        result = _parse_integer(value)
        self._validate_facets(result, location.path, location)
        return result

    def decode(self, checked: object) -> object:
        if type(checked) is not int:
            raise TypeError("validated XML integer is not an int")
        return checked

    def _validate_facets(
        self,
        value: int,
        path: str,
        location: CellLocation | None = None,
    ) -> None:
        if self.facets is None:
            return
        digits = len(_integer_text(abs(value)))
        if self.facets.total_digits is not None and digits > self.facets.total_digits:
            self._facet_error(path, location)
        for name, bound in (
            ("min_inclusive", self.facets.min_inclusive),
            ("max_inclusive", self.facets.max_inclusive),
            ("min_exclusive", self.facets.min_exclusive),
            ("max_exclusive", self.facets.max_exclusive),
        ):
            if bound is None:
                continue
            edge = int(Decimal(bound))
            if (
                name == "min_inclusive"
                and value < edge
                or name == "max_inclusive"
                and value > edge
                or name == "min_exclusive"
                and value <= edge
                or name == "max_exclusive"
                and value >= edge
            ):
                self._facet_error(path, location)

    def _facet_error(self, path: str, location: CellLocation | None) -> NoReturn:
        raise StructureMapError(
            "facet_violation",
            "Integer violates declared numeric facets",
            path=path,
            table=location.table if location else None,
            row=location.row if location else None,
            column=self.column if location else None,
            owner="executor restriction",
            remedy="Supply an integer satisfying the declared XML facets",
        )


class XmlEnumCodec:
    fields: dict[str, FieldSpec]
    def __init__(
        self,
        enum_type: type[Enum],
        column: str,
        path: str,
        policy: str | None = None,
        metadata_namespace: str = "_sm_",
        format: str | None = None,
        facets: NumericFacets | None = None,
    ):
        self.enum_type = enum_type
        self.column = column
        self.path = path
        self.members = tuple(enum_type.__members__.values())
        values = tuple(object.__getattribute__(member, "_value_") for member in self.members)
        if not values:
            raise StructureMapError(
                "unsupported_enum",
                f"Enum {enum_type.__qualname__} has no values",
                path=path,
                owner="native limitation",
                remedy="Declare at least one enum value",
            )
        first_type = type(values[0])
        if any(type(value) is not first_type for value in values):
            raise StructureMapError(
                "unsupported_enum",
                f"Enum {enum_type.__qualname__} mixes native value types",
                path=path,
                owner="native limitation",
                remedy="Use one scalar type for every enum value",
            )
        self.values = values
        self.encoded_values: dict[tuple[Cell, ...], int] = {}
        if first_type is int:
            permitted = []
            source_codec = XmlIntegerCodec(column, path, facets)
            for candidate in values:
                try:
                    source_codec.encode(candidate)
                except StructureMapError as exc:
                    if exc.code == "facet_violation":
                        continue
                    raise
                permitted.append(candidate)
            if not permitted:
                raise StructureMapError(
                    "invalid_numeric_facets",
                    "No integer enum member satisfies the declared facets",
                    path=path,
                    remedy="Declare facets admitting a native enum member",
                )
            enum_facets = (facets or NumericFacets()).model_copy(
                update={
                    "min_inclusive": _integer_text(min(permitted)),
                    "max_inclusive": _integer_text(max(permitted)),
                }
            )
            self.delegate = prepare_xml_scalar(
                Scalar(type="int", facets=enum_facets),
                first_type,
                column,
                path,
                policy=policy,
                metadata_namespace=metadata_namespace,
            )
            self.fields = self.delegate.fields
            self._prepare_delegate()
            return
        families: dict[
            type,
            Literal[
                "str",
                "bool",
                "float",
                "decimal",
                "date",
                "time",
                "datetime",
                "period",
                "duration",
                "binary",
                "qname",
            ],
        ] = {
            str: "str",
            bool: "bool",
            float: "float",
            Decimal: "decimal",
            XmlDate: "date",
            XmlTime: "time",
            XmlDateTime: "datetime",
            XmlPeriod: "period",
            XmlDuration: "duration",
            bytes: "binary",
            XmlHexBinary: "binary",
            XmlBase64Binary: "binary",
        }
        family = families.get(first_type)
        if family is None and issubclass(first_type, QName):
            family = "qname"
        if family is None:
            raise StructureMapError(
                "unsupported_enum",
                f"Enum {enum_type.__qualname__} uses unsupported value type {first_type.__name__}",
                path=path,
                owner="native limitation",
                remedy="Use a supported XML scalar enum value",
            )
        self.delegate = prepare_xml_scalar(
            Scalar(type=family, format=format, facets=facets),
            first_type,
            column,
            path,
            policy=policy,
            metadata_namespace=metadata_namespace,
        )
        self.fields = self.delegate.fields
        self._prepare_delegate()

    def _prepare_delegate(self) -> None:
        for index, candidate in enumerate(self.values):
            try:
                encoded = self._key(self.delegate.encode(candidate))
            except StructureMapError as exc:
                if exc.code == "facet_violation":
                    continue
                raise
            previous = self.encoded_values.get(encoded)
            if previous is not None and self.members[previous] is not self.members[index]:
                raise StructureMapError(
                    "ambiguous_enum_value",
                    "Distinct enum members have the same canonical scalar representation",
                    path=self.path,
                    remedy="Use distinct enum values under the selected scalar representation",
                )
            self.encoded_values[encoded] = index
        if not self.encoded_values:
            raise StructureMapError(
                "invalid_numeric_facets",
                "No enum member satisfies the declared scalar constraints",
                path=self.path,
                remedy="Declare constraints that admit a member of the native enum",
            )

    def encode(self, value: object) -> tuple[Cell, ...]:
        if type(value) is not self.enum_type:
            raise StructureMapError(
                "invalid_scalar",
                f"Expected {self.enum_type.__qualname__}, got {type(value).__name__}",
                path=self.path,
                owner="executor restriction",
                remedy=f"Provide a {self.enum_type.__qualname__} member",
            )
        raw = object.__getattribute__(value, "_value_")
        return self.delegate.encode(raw)

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        self.delegate.check(cells, location)
        index = self.encoded_values.get(self._key(cells))
        if index is not None:
            return index
        self._invalid_value(location)

    @staticmethod
    def _key(cells: tuple[Cell, ...]) -> tuple[Cell, ...]:
        return tuple("NaN" if isinstance(value, float) and isnan(value) else value for value in cells)

    def _invalid_value(self, location: CellLocation) -> NoReturn:
        raise StructureMapError(
            "invalid_scalar",
            f"Value is not a member of {self.enum_type.__qualname__}",
            path=location.path,
            table=location.table,
            row=location.row,
            column=self.column,
            owner="executor restriction",
            remedy=f"Supply one of the declared {self.enum_type.__qualname__} values",
        )

    def decode(self, checked: object) -> object:
        return self.members[cast(int, checked)]


def prepare_xml_scalar(
    declaration: Scalar,
    native_type: type | None,
    column: str,
    path: str,
    *,
    policy: str | None = None,
    metadata_namespace: str = "_sm_",
) -> ScalarCodec:
    if declaration.type == "enum":
        if native_type is None or not issubclass(native_type, Enum):
            raise StructureMapError(
                "invalid_native_scalar",
                "Enum scalar has no associated native enum type",
                path=path,
                owner="native limitation",
                remedy="Derive the enum through the xsdata binding",
            )
        return XmlEnumCodec(
            native_type,
            column,
            path,
            policy,
            metadata_namespace,
            declaration.format,
            declaration.facets,
        )
    if declaration.type == "int":
        facets = declaration.facets
        bounds = _integer_bounds(facets, path) if facets is not None else None
        fits = bounds is not None and -(2**63) <= bounds[0] <= bounds[1] < 2**63
        if policy == "typed" or policy is None and fits:
            if facets is None:
                raise StructureMapError(
                    "unrepresentable_scalar",
                    "Typed integer requires signed-i64-proving bounds",
                    path=path,
                    owner="binding limitation",
                    remedy="Add inclusive min/max bounds or use lexical value_space",
                )
            return XmlBoundedIntegerCodec(facets, column, path)
        return XmlIntegerCodec(column, path, facets)
    if declaration.type == "decimal":
        return DecimalCodec(declaration, column, path, policy)
    if declaration.type in ("date", "time", "datetime", "duration", "period"):
        return XmlTemporalCodec(
            declaration.type,
            native_type,
            column,
            path,
            policy,
            metadata_namespace,
            declaration.format,
        )
    if declaration.type in ("binary", "qname") and policy == "typed":
        raise StructureMapError(
            "unrepresentable_scalar",
            f"No portable typed {declaration.type} carrier is selected",
            path=path,
            remedy="Use lexical value_space with the declared native format",
        )
    if declaration.type == "binary":
        return XmlBinaryCodec(native_type, column, path, declaration.format)
    if declaration.type == "qname":
        return XmlQNameCodec(native_type, column, path)
    if declaration.type in ("str", "bool", "float"):
        if policy == "lexical" and declaration.type in ("bool", "float"):
            return XmlLexicalPrimitiveCodec(declaration.type, column, path)
        return PrimitiveCodec(declaration.type, column, path)
    raise StructureMapError(
        "unavailable_scalar",
        f"XML scalar {declaration.type!r} is not available in this delivery",
        path=path,
        owner="binding gap",
        remedy="Use a supported XML scalar representation",
    )
