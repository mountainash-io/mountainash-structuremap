from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

import pytest
from mountainash.typespec.spec import FieldDefinition, TableSchema
from mountainash.typespec.universal_types import UniversalType as U

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct
from mountainash_structuremap.mapping import MappingSpec, MappingUse, RowLayout
from tests.fixtures.external_mapping import AddressColumns, DateParts


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


def order_case():
    schemas = {
        "orders": TableSchema(fields={n: FieldDefinition(type=t, missing_values=[]) for n, t in (
            ("order_id", U.INTEGER), ("home_city", U.STRING), ("home_country", U.STRING),
            ("work_city", U.STRING), ("work_country", U.STRING),
            ("year", U.INTEGER), ("month", U.INTEGER), ("day", U.INTEGER),
        )}, missing_values=[]),
        "lines": TableSchema(fields={n: FieldDefinition(type=t, missing_values=[]) for n, t in (
            ("order_id", U.INTEGER), ("line_no", U.INTEGER), ("country", U.STRING),
            ("amount", U.STRING), ("effective", U.DATE), ("source_date", U.STRING),
        )}, missing_values=[]),
    }
    mapping = MappingSpec(
        use=MappingUse(component="core.record", config={"table": "orders"}, children={
            "home": MappingUse(component="example.address.v1", config={"prefix": "home"}),
            "work": MappingUse(component="example.address.v1", config={"prefix": "work"}),
            "issued": MappingUse(component="example.date-parts.v1", config={"columns": ["year", "month", "day"]}),
        }),
        layouts={"orders": RowLayout(key=("order_id",)),
                 "lines": RowLayout(key=("order_id", "line_no"), parent=("order_id",), position="line_no")},
    )
    rows = {
        "orders": [{"order_id": 42, "home_city": "Sydney", "home_country": "AU",
                    "work_city": "Wellington", "work_country": "NZ", "year": 2023, "month": 9, "day": 30}],
        "lines": [
            {"order_id": 42, "line_no": 9, "country": "NZ", "amount": "1000000", "effective": None, "source_date": "2023"},
            {"order_id": 42, "line_no": 3, "country": "AU", "amount": "0.01", "effective": date(2023, 9, 30), "source_date": "2023-09"},
            {"order_id": 42, "line_no": 12, "country": "AU", "amount": "0.01", "effective": date(2023, 9, 30), "source_date": "2023-09"},
        ],
    }
    expected = Order(42, Address("Sydney", "AU"), Address("Wellington", "NZ"), date(2023, 9, 30), (
        Line("AU", Decimal("0.01"), date(2023, 9, 30), "2023-09"),
        Line("NZ", Decimal("1000000"), None, "2023"),
        Line("AU", Decimal("0.01"), date(2023, 9, 30), "2023-09"),
    ))
    return schemas, mapping, rows, expected


def prepared_order(schemas, mapping, **kwargs):
    return prepare(derive(Order), mapping, relations=schemas, components=(AddressColumns(), DateParts()), **kwargs)


def test_external_components_reconstruct_independent_source_key_rows():
    schemas, mapping, rows, expected = order_case()
    p = prepared_order(schemas, mapping)
    assert p.tables["lines"].columns == ("order_id", "line_no", "country", "amount", "effective", "source_date")
    actual = reconstruct(rows, p)
    assert actual == expected
    assert type(actual.lines) is tuple
    assert type(actual.lines[0].amount) is Decimal
    assert type(actual.issued) is date
    emitted = decompose(expected, p)
    assert emitted["orders"] == rows["orders"]
    assert emitted["lines"] == [dict(rows["lines"][i], line_no=n) for n, i in enumerate((1, 0, 2))]


def test_empty_and_all_null_inputs_keep_independent_schema():
    schemas, mapping, rows, _ = order_case()
    p = prepared_order(schemas, mapping)
    rows["orders"][0].update(year=None, month=None, day=None)
    rows["lines"] = []
    expected = Order(42, Address("Sydney", "AU"), Address("Wellington", "NZ"), None, ())
    assert reconstruct(rows, p) == expected
    assert decompose(expected, p) == rows
    assert list(p.tables["orders"].table_schema.fields.values())[-1].type == U.INTEGER


def test_preparation_isolates_caller_owned_schema():
    schemas, mapping, rows, expected = order_case()
    p = prepared_order(schemas, mapping)
    fields = schemas["orders"].fields
    fields["changed"] = fields.pop("order_id")
    assert reconstruct(rows, p) == expected
    assert p.tables["orders"].columns[0] == "order_id"


@pytest.mark.parametrize("change", ["missing", "wrong_type", "extra"])
def test_incompatible_physical_schema_fails_before_data(change):
    schemas, mapping, _, _ = order_case()
    if change == "missing":
        schemas["orders"].fields.popitem()
    elif change == "wrong_type":
        fields = schemas["orders"].fields
        last = list(fields)[-1]
        fields[last] = fields[last].variant(type=U.STRING)
    else:
        schemas["orders"].fields["unused"] = FieldDefinition(type=U.STRING)
    with pytest.raises(StructureMapError) as caught:
        prepared_order(schemas, mapping)
    assert caught.value.remedy


def test_missing_and_duplicate_component_implementations_fail():
    schemas, mapping, _, _ = order_case()
    for implementations in ((), (AddressColumns(), AddressColumns(), DateParts())):
        with pytest.raises(StructureMapError) as caught:
            prepare(derive(Order), mapping, relations=schemas, components=implementations)
        assert caught.value.remedy


@pytest.mark.parametrize("patch", [{"day": None}, {"month": 2, "day": 30}])
def test_multicolumn_date_rejects_partial_null_and_invalid_calendar(patch):
    schemas, mapping, rows, _ = order_case()
    rows["orders"][0].update(patch)
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared_order(schemas, mapping))
    assert caught.value.code == "invalid_date_parts"


def test_component_shape_and_children_are_checked_during_preparation():
    schemas, mapping, _, _ = order_case()
    for children in (
        {"order_id": MappingUse(component="example.date-parts.v1", config={"columns": ["year", "month", "day"]})},
        {"typo": MappingUse(component="core.value")},
    ):
        invalid = mapping.model_copy(update={"use": MappingUse(component="core.record", config={"table": "orders"}, children=children)})
        with pytest.raises(StructureMapError) as caught:
            prepared_order(deepcopy(schemas), invalid)
        assert caught.value.path
        assert caught.value.remedy
