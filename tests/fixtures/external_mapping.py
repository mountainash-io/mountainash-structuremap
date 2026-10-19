"""Consumer-owned components using documented public submodules only."""
from datetime import date

from mountainash.typespec.spec import FieldSpec
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap import StructureMapError
from mountainash_structuremap.codecs import Cell, CellLocation
from mountainash_structuremap.components import PreparationContext
from mountainash_structuremap.definitions import Nullable, RecordRef, Scalar
from mountainash_structuremap.mapping import MappingUse, UseOverride


class AddressColumns:
    """Record city/country projection; both directions, no extra buffering."""

    identity = "example.address.v1"
    directions = frozenset({"decompose", "reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        shape = context.shape.value if isinstance(context.shape, Nullable) else context.shape
        if not isinstance(shape, RecordRef) or set(use.config) != {"prefix"} or not isinstance(use.config["prefix"], str):
            raise StructureMapError("invalid_component", "Address needs a record and string prefix", path=context.path,
                                    remedy="Apply to an Address record with config={'prefix': 'home'}")
        context.record(use.children, options=UseOverride(prefix=use.config["prefix"]))


class DatePartsCodec:
    def __init__(self, names: tuple[str, ...]):
        self.fields = {name: FieldSpec(type=UniversalType.INTEGER, missing_values=[]) for name in names}

    def encode(self, value: object) -> tuple[Cell, ...]:
        if type(value) is not date:
            raise StructureMapError("native_type_mismatch", "Date parts require a date", remedy="Supply a native date")
        return value.year, value.month, value.day

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        if len(cells) != 3 or any(type(cell) is not int for cell in cells):
            raise StructureMapError("invalid_date_parts", "Date parts must be three non-null integers", path=location.path,
                                    table=location.table, row=location.row, remedy="Supply all year/month/day cells or all null")
        try:
            return date(*cells)
        except (TypeError, ValueError) as exc:
            raise StructureMapError("invalid_date_parts", "Invalid calendar date", path=location.path,
                                    remedy="Supply a valid year/month/day") from exc

    def decode(self, checked: object) -> object:
        return checked


class DateParts:
    """Native date to three INTEGER fields; nullable wrapper stays core-owned."""

    identity = "example.date-parts.v1"
    directions = frozenset({"decompose", "reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        shape = context.shape.value if isinstance(context.shape, Nullable) else context.shape
        names = use.config.get("columns")
        if (not isinstance(shape, Scalar) or shape.type != "date" or set(use.config) != {"columns"}
                or not isinstance(names, list) or len(names) != 3 or any(not isinstance(name, str) for name in names)
                or use.children):
            raise StructureMapError("invalid_component", "Date parts need a date and three column names", path=context.path,
                                    remedy="Apply to a date with config={'columns': ['year', 'month', 'day']}")
        for name in names:
            if context.field(name).type != UniversalType.INTEGER:
                raise StructureMapError("incompatible_schema", "Date part is not INTEGER", path=context.path,
                                        remedy="Declare each date part column as INTEGER")
        context.value(codec=DatePartsCodec(tuple(names)))
