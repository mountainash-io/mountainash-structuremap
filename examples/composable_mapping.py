"""Run with `hatch run test:python examples/composable_mapping.py`."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from mountainash.typespec.spec import FieldDefinition, TableSchema
from mountainash.typespec.universal_types import UniversalType as U

from mountainash_structuremap import (
    AuthoringBundle,
    MappingBuilder,
    MappingRef,
    MappingSpec,
    MappingUse,
    RowLayout,
    StructureMapError,
    UseOverride,
    assemble_structure,
    decompose,
    derive,
    dump_bundle,
    load_bundle,
    load_mapping,
    load_structure,
    prepare,
    reconstruct,
)
from mountainash_structuremap.codecs import CellLocation
from mountainash_structuremap.components import PreparationContext
from mountainash_structuremap.definitions import DerivedModel, Nullable, RecordRef, Scalar
from mountainash_structuremap.mapping import SharedCollection, TypeCast


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
    fields = {name: FieldDefinition(type=U.INTEGER, missing_values=[]) for name in ("year", "month", "day")}

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


@dataclass(frozen=True)
class Tree:
    label: str
    children: list["Tree"]


@dataclass(frozen=True)
class Directory:
    home: list[Address]
    work: list[Address]


@dataclass(frozen=True)
class TextAmount:
    amount: str
    text: str


@dataclass(frozen=True)
class CountAmount:
    amount: int
    count: int


@dataclass(frozen=True)
class Ledger:
    entries: list[TextAmount | CountAmount]


def checkpoint_b():
    recursive = MappingSpec(
        use=MappingRef(ref="node"),
        definitions={"node": MappingUse(component="core.record", children={
            "children": MappingUse(component="core.sequence", children={"item": MappingRef(ref="node")}),
        })},
        overrides={"@Tree/label": UseOverride(column="node_label")},
    )
    tree = Tree("root", [Tree("branch", [Tree("leaf", [])]), Tree("other", [])])
    plan = prepare(derive(Tree), recursive)
    tree_rows = decompose(tree, plan)
    assert reconstruct(tree_rows, prepare(derive(Tree), recursive)) == tree
    print("Reusable recursive mapping:", tree_rows)

    shared = MappingSpec(overrides={
        role: UseOverride(collection_table=SharedCollection(
            table="addresses", role_column="address_role", role=role), positions="lenient")
        for role in ("home", "work")
    })
    directory = Directory([Address("Oslo", "NO"), Address("Kyoto", "JP")], [Address("Lima", "PE")])
    plan = prepare(derive(Directory), shared)
    directory_rows = decompose(directory, plan)
    assert reconstruct(directory_rows, plan) == directory
    print("Shared collection roles:", directory_rows)

    ledger_mapping = MappingSpec(
        use=MappingUse(component="core.record", children={
            "entries": MappingUse(component="core.sequence", children={
                "item": MappingUse(component="core.alternatives",
                    config={"variant_layout": "shared"}, children={
                        "TextAmount": MappingUse(component="core.record", children={
                            "amount": MappingUse(component="core.value",
                                config={"cast": TypeCast(to="i64").model_dump()}),
                        }),
                    }),
            }),
        }),
    )
    plan = prepare(derive(Ledger), ledger_mapping)
    ledger = Ledger([TextAmount("01", "source"), CountAmount(2, 7)])
    ledger_rows = decompose(ledger, plan)
    restored = reconstruct(ledger_rows, plan)
    assert [row["amount"] for row in ledger_rows["entries"]] == [1, 2]
    assert restored == Ledger([TextAmount("1", "source"), CountAmount(2, 7)])
    print("Shared/Exact cast cells:", ledger_rows)
    print("Branch-specific native inverse:", restored)


def portable_authoring():
    document = load_structure("""
    namespace: addressbook
    structure:
      root: Directory
      definitions:
        Directory:
          name: Directory
          members:
            - name: home
              shape: {kind: sequence, item: {kind: record, definition: Address}}
            - name: work
              shape: {kind: sequence, item: {kind: record, definition: Address}}
        Address:
          name: Address
          members:
            - {name: city, shape: {kind: scalar, type: str}}
            - {name: country, shape: {kind: scalar, type: str}}
    """, format="yaml")
    model = DerivedModel(
        structure=assemble_structure([document], root="addressbook.Directory"),
        native_types={"addressbook.Directory": Directory, "addressbook.Address": Address},
        binding="dataclass",
    )
    json_mapping = """{
      "overrides": {
        "home": {"key": {"strategy": "source", "columns": ["city"]},
                 "collection_table": {"strategy": "shared", "table": "addresses",
                                      "role_column": "address_role", "role": "home"}},
        "work": {"key": {"strategy": "source", "columns": ["city"]},
                 "collection_table": {"strategy": "shared", "table": "addresses",
                                      "role_column": "address_role", "role": "work"}}
      }
    }"""
    yaml_mapping = """
    overrides:
      home:
        key: {strategy: source, columns: [city]}
        collection_table: {strategy: shared, table: addresses, role_column: address_role, role: home}
      work:
        key: {strategy: source, columns: [city]}
        collection_table: {strategy: shared, table: addresses, role_column: address_role, role: work}
    """
    builder = MappingBuilder()
    for role in ("home", "work"):
        builder.override(
            role, key={"strategy": "source", "columns": ["city"]},
            collection_table={"strategy": "shared", "table": "addresses",
                              "role_column": "address_role", "role": role},
        )
    schemas = {
        "root": TableSchema(fields={"_sm_key": FieldDefinition(type=U.STRING, missing_values=[])}, missing_values=[]),
        "addresses": TableSchema(fields={
            name: FieldDefinition(type=kind, missing_values=[]) for name, kind in (
                ("city", U.STRING), ("_sm_parent", U.STRING), ("_sm_position", U.INTEGER),
                ("address_role", U.STRING), ("country", U.STRING),
            )
        }, missing_values=[]),
    }
    rows = {
        "root": [{"_sm_key": "document"}],
        "addresses": [
            {"city": "Kyoto", "_sm_parent": "document", "_sm_position": 9, "address_role": "home", "country": "JP"},
            {"city": "Oslo", "_sm_parent": "document", "_sm_position": 3, "address_role": "home", "country": "NO"},
            {"city": "Oslo", "_sm_parent": "document", "_sm_position": 0, "address_role": "work", "country": "NO"},
        ],
    }
    expected = Directory([Address("Oslo", "NO"), Address("Kyoto", "JP")], [Address("Oslo", "NO")])
    for name, mapping in (
        ("JSON", load_mapping(json_mapping, format="json")),
        ("YAML", load_mapping(yaml_mapping, format="yaml")),
        ("fluent", builder.build()),
    ):
        bundle = load_bundle(dump_bundle(
            AuthoringBundle(structure=document, mapping=mapping, relations=schemas), format="json",
        ), format="json")
        plan = prepare(model, bundle.mapping, relations=bundle.relations)
        restored = reconstruct(rows, plan)
        assert restored == expected
        emitted = decompose(restored, plan)
        assert [row["address_role"] for row in emitted["addresses"]] == ["home", "home", "work"]
        assert [row["_sm_position"] for row in emitted["addresses"]] == [0, 1, 0]
        print(f"{name} authoring + retained bundle:", restored)


if __name__ == "__main__":
    main()
    checkpoint_b()
    portable_authoring()
