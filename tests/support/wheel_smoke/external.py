from datetime import date

from mountainash.typespec.spec import FieldSpec
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap import StructureMapError
from mountainash_structuremap.codecs import Cell, CellLocation
from mountainash_structuremap.components import PreparationContext
from mountainash_structuremap.definitions import Nullable, Scalar
from mountainash_structuremap.mapping import MappingUse


class _DatePartsCodec:
    fields = {name: FieldSpec(type=UniversalType.INTEGER, missing_values=[]) for name in ("year", "month", "day")}

    def encode(self, value: object) -> tuple[Cell, ...]:
        if type(value) is not date:
            raise StructureMapError("native_type_mismatch", "Expected date")
        return value.year, value.month, value.day

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        if len(cells) != 3 or any(type(cell) is not int for cell in cells):
            raise StructureMapError("invalid_date_parts", "Date parts must be complete integers", path=location.path)
        try:
            return date(*cells)
        except (TypeError, ValueError) as exc:
            raise StructureMapError("invalid_date_parts", "Invalid calendar date", path=location.path) from exc

    def decode(self, checked: object) -> object:
        return checked


class DateParts:
    identity = "example.date-parts.v1"
    directions = frozenset({"decompose", "reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        shape = context.shape.value if isinstance(context.shape, Nullable) else context.shape
        names = use.config.get("columns")
        if not isinstance(shape, Scalar) or shape.type != "date" or names != ["year", "month", "day"]:
            raise StructureMapError("invalid_component", "DateParts requires date and year/month/day columns", path=context.path)
        context.value(codec=_DatePartsCodec())


class _DatePartsEncoder:
    fields = _DatePartsCodec.fields
    encode = _DatePartsCodec.encode


class _DatePartsDecoder:
    fields = _DatePartsCodec.fields
    check = _DatePartsCodec.check
    decode = _DatePartsCodec.decode


class DatePartsEncoder(DateParts):
    directions = frozenset({"decompose"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        context.value(codec=_DatePartsEncoder())


class DatePartsDecoder(DateParts):
    directions = frozenset({"reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        context.value(codec=_DatePartsDecoder())
