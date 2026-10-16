"""Run with `hatch run test:python examples/composable_mapping.py`."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType as U

from mountainash_structuremap import (
    MappingSpec,
    MappingUse,
    RowLayout,
    StructureMapError,
    UseOverride,
    decompose,
    derive,
    prepare,
    reconstruct,
)
from mountainash_structuremap.codecs import CellLocation
from mountainash_structuremap.components import PreparationContext
from mountainash_structuremap.definitions import Nullable, RecordRef, Scalar


@dataclass(frozen=True)
class Address:
    city: str
    country: str


@dataclass(frozen=True)
class Line:
    country: Literal["AU", "NZ"]
    amount: Decimal
    effective: date | None
    source_date: str


@dataclass(frozen=True)
class Order:
    order_id: int
    home: Address
    work: Address
    issued: date | None
    lines: tuple[Line, ...]


class AddressMapping:
    identity = "example.address.v1"
    directions = frozenset({"decompose", "reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        if not isinstance(context.shape, RecordRef) or set(use.config) != {"prefix"}:
            raise StructureMapError("invalid_component", "Address mapping needs a record and prefix", path=context.path,
                                    remedy="Apply this component to an Address with a string prefix")
        context.record(use.children, options=UseOverride(prefix=use.config["prefix"]))


class DatePartsCodec:
    fields = tuple(FieldSpec(name=name, type=U.INTEGER, missing_values=[]) for name in ("year", "month", "day"))

    def encode(self, value):
        if type(value) is not date:
            raise StructureMapError("invalid_date", "Expected a date", remedy="Supply a native date, not datetime")
        return value.year, value.month, value.day

    def check(self, cells, location: CellLocation):
        if len(cells) != 3 or any(type(cell) is not int for cell in cells):
            raise StructureMapError("invalid_date", "Date needs three integer cells", path=location.path,
                                    remedy="Supply year/month/day together, or all null")
        try:
            return date(*cells)
        except ValueError as exc:
            raise StructureMapError("invalid_date", "Invalid calendar date", path=location.path,
                                    remedy="Supply a valid calendar date") from exc

    def decode(self, checked):
        return checked


class DateMapping:
    identity = "example.date.v1"
    directions = frozenset({"decompose", "reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        shape = context.shape.value if isinstance(context.shape, Nullable) else context.shape
        if not isinstance(shape, Scalar) or shape.type != "date" or use.config or use.children:
            raise StructureMapError("invalid_component", "Date mapping requires a date with no configuration",
                                    path=context.path, remedy="Apply this component to a date field")
        for name in ("year", "month", "day"):
            if context.field(name).type != U.INTEGER:
                raise StructureMapError("invalid_schema", "Date part must be INTEGER", path=context.path,
                                        remedy="Declare year, month and day as INTEGER")
        context.value(codec=DatePartsCodec())


def main():
    schemas = {
        "orders": TypeSpec(fields=[FieldSpec(name=n, type=t, missing_values=[]) for n, t in (
            ("order_id", U.INTEGER), ("home_city", U.STRING), ("home_country", U.STRING),
            ("work_city", U.STRING), ("work_country", U.STRING),
            ("year", U.INTEGER), ("month", U.INTEGER), ("day", U.INTEGER),
        )], missing_values=[]),
        "lines": TypeSpec(fields=[FieldSpec(name=n, type=t, missing_values=[]) for n, t in (
            ("order_id", U.INTEGER), ("line_no", U.INTEGER), ("country", U.STRING),
            ("amount", U.STRING), ("effective", U.DATE), ("source_date", U.STRING),
        )], missing_values=[]),
    }
    mapping = MappingSpec(use=MappingUse(component="core.record", config={"table": "orders"}, children={
        "home": MappingUse(component="example.address.v1", config={"prefix": "home"}),
        "work": MappingUse(component="example.address.v1", config={"prefix": "work"}),
        "issued": MappingUse(component="example.date.v1"),
    }), layouts={
        "orders": RowLayout(key=("order_id",)),
        "lines": RowLayout(key=("order_id", "line_no"), parent=("order_id",), position="line_no"),
    })
    plan = prepare(derive(Order), mapping, relations=schemas, components=(AddressMapping(), DateMapping()))
    rows = {
        "orders": [{"order_id": 42, "home_city": "Sydney", "home_country": "AU", "work_city": "Wellington",
                    "work_country": "NZ", "year": 2023, "month": 9, "day": 30}],
        "lines": [{"order_id": 42, "line_no": position, "country": "AU", "amount": "0.01",
                   "effective": None, "source_date": "2023-09"} for position in (9, 3, 12)],
    }
    expected = Order(42, Address("Sydney", "AU"), Address("Wellington", "NZ"), date(2023, 9, 30),
                     (Line("AU", Decimal("0.01"), None, "2023-09"),) * 3)
    restored = reconstruct(rows, plan)
    assert restored == expected
    assert type(restored.lines) is tuple and type(restored.lines[0].amount) is Decimal
    emitted = decompose(restored, plan)
    assert emitted["orders"] == rows["orders"]
    assert [row["line_no"] for row in emitted["lines"]] == [0, 1, 2]
    print("Independent schemas:", {name: table.columns for name, table in plan.tables.items()})
    print("Reconstructed native value:", restored)
    print("Emitted rows:", emitted)
    rows["orders"][0]["day"] = None
    try:
        reconstruct(rows, plan)
    except StructureMapError as exc:
        assert exc.code == "invalid_date"
        print("Partial-null date rejected:", exc)
    else:
        raise AssertionError("Partial-null date was accepted")


if __name__ == "__main__":
    main()
