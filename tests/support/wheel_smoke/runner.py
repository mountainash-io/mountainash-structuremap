from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from importlib.metadata import distribution
from pathlib import Path
from xml.etree.ElementTree import fromstring

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mountainash.typespec.spec import FieldDefinition, TableSchema  # noqa: E402
from mountainash.typespec.universal_types import UniversalType as U  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from wheel_external import DateParts  # noqa: E402
from wheel_models import (  # noqa: E402
    CATALOG_XML,
    AuthoringAddress,
    AuthoringDirectory,
    AuthoringItem,
    AuthoringLedger,
    Catalog,
    Entry,
    Note,
    PydanticCatalog,
    PydanticEntry,
    load_xml_types,
)

from mountainash_structuremap import (  # noqa: E402
    MappingSpec,
    StructureMapError,
    decompose,
    derive,
    dump_mapping,
    load_mapping,
    parse,
    prepare,
    reconstruct,
    serialize,
)
from mountainash_structuremap.definitions import DerivedModel, StructureSpec  # noqa: E402
from mountainash_structuremap.mapping import AlternativeLayout, MappingUse, UseOverride  # noqa: E402


def _portable_mapping() -> MappingSpec:
    return load_mapping(Path("authoring-mapping.json").read_bytes(), format="json")


def _authoring_types():
    return {
        name: TableSchema.from_frictionless(schema)
        for name, schema in json.loads(Path("authoring-schema.json").read_text(encoding="utf-8")).items()
    }


def _authoring_rows():
    return {
        "root": [{"_sm_key": "ledger-1"}],
        "items": [
            {"_sm_key": "second", "_sm_parent": "ledger-1", "_sm_position": 8,
             "country": "NZ", "amount": "1000000", "year": 2024, "month": 2, "day": 29},
            {"_sm_key": "first-a", "_sm_parent": "ledger-1", "_sm_position": 2,
             "country": "AU", "amount": "0.01", "year": None, "month": None, "day": None},
            {"_sm_key": "first-b", "_sm_parent": "ledger-1", "_sm_position": 5,
             "country": "AU", "amount": "0.01", "year": None, "month": None, "day": None},
        ],
    }


def _authoring_prepare(mapping, relations):
    return prepare(derive(AuthoringLedger), mapping, relations=relations, components=(DateParts(),))


def _assert_authoring_value(value) -> None:
    assert type(value) is AuthoringLedger
    assert type(value.items) is tuple
    assert value.items == (
        AuthoringItem("AU", Decimal("0.01"), None),
        AuthoringItem("AU", Decimal("0.01"), None),
        AuthoringItem("NZ", Decimal("1000000"), date(2024, 2, 29)),
    )
    assert all(type(item.amount) is Decimal for item in value.items)
    assert type(value.items[-1].effective) is date


def _authoring_persist() -> None:
    mapping = MappingSpec(use=MappingUse(component="core.record", children={
        "items": MappingUse(component="core.sequence", children={
            "item": MappingUse(component="core.record", children={
                "country": MappingUse(component="core.value"),
                "amount": MappingUse(component="core.value"),
                "effective": MappingUse(component="example.date-parts.v1",
                                        config={"columns": ["year", "month", "day"]}),
            }),
        }),
    }))
    relations = {
        name: TableSchema(fields={
            column: FieldDefinition(type=kind, missing_values=[]) for column, kind in fields
        }, missing_values=[])
        for name, fields in {
            "root": [("_sm_key", U.STRING)],
            "items": [
                ("_sm_key", U.STRING), ("_sm_parent", U.STRING), ("_sm_position", U.INTEGER),
                ("country", U.STRING), ("amount", U.STRING),
                ("year", U.INTEGER), ("month", U.INTEGER), ("day", U.INTEGER),
            ],
        }.items()
    }
    prepared = _authoring_prepare(mapping, relations)
    Path("authoring-mapping.json").write_text(dump_mapping(mapping, format="json"), encoding="utf-8")
    Path("authoring-schema.json").write_text(json.dumps({
        name: schema.to_frictionless() for name, schema in relations.items()
    }), encoding="utf-8")
    source = AuthoringLedger((
        AuthoringItem("AU", Decimal("0.01"), None),
        AuthoringItem("AU", Decimal("0.01"), None),
        AuthoringItem("NZ", Decimal("1000000"), date(2024, 2, 29)),
    ))
    Path("authoring-emitted.json").write_text(json.dumps(decompose(source, prepared)), encoding="utf-8")


def _authoring_independent() -> None:
    relations = _authoring_types()
    prepared = _authoring_prepare(_portable_mapping(), relations)
    _assert_authoring_value(reconstruct(_authoring_rows(), prepared))
    _assert_authoring_value(reconstruct(json.loads(Path("authoring-emitted.json").read_text()), prepared))
    empty = {"root": [{"_sm_key": "empty"}], "items": []}
    assert reconstruct(empty, prepared) == AuthoringLedger(())
    assert prepared.tables["items"].table_schema == relations["items"]
    print("Portable mapping and independent schemas reconstruct exact tuples, duplicates, Decimal/date and empty data")

def _role_mapping() -> MappingSpec:
    return MappingSpec.model_validate({"overrides": {
        "home": {"key": {"strategy": "source", "columns": ["city"]},
                 "collection_table": {"strategy": "shared", "table": "addresses",
                                      "role_column": "address_role", "role": "home"}},
        "work": {"key": {"strategy": "source", "columns": ["city"]},
                 "collection_table": {"strategy": "shared", "table": "addresses",
                                      "role_column": "address_role", "role": "work"}},
    }})


def _role_rows():
    return {"root": [{"_sm_key": "directory"}], "addresses": [
        {"city": "Oslo", "_sm_parent": "directory", "_sm_position": 9,
         "address_role": "home", "country": "NO"},
        {"city": "Kyoto", "_sm_parent": "directory", "_sm_position": 3,
         "address_role": "home", "country": "JP"},
        {"city": "Oslo", "_sm_parent": "directory", "_sm_position": 0,
         "address_role": "work", "country": "NO"},
    ]}


def _persist_roles() -> None:
    mapping = _role_mapping()
    prepared = prepare(derive(AuthoringDirectory), mapping)
    Path("authoring-role-mapping.json").write_text(dump_mapping(mapping, format="json"), encoding="utf-8")
    Path("authoring-role-schema.json").write_text(json.dumps({
        name: table.table_schema.to_frictionless() for name, table in prepared.tables.items()
    }), encoding="utf-8")


def _read_roles() -> None:
    mapping = load_mapping(Path("authoring-role-mapping.json").read_bytes(), format="json")
    relations = {name: TableSchema.from_frictionless(schema) for name, schema in
                 json.loads(Path("authoring-role-schema.json").read_text(encoding="utf-8")).items()}
    prepared = prepare(derive(AuthoringDirectory), mapping, relations=relations)
    assert reconstruct(_role_rows(), prepared) == AuthoringDirectory(
        [AuthoringAddress("Kyoto", "JP"), AuthoringAddress("Oslo", "NO")],
        [AuthoringAddress("Oslo", "NO")],
    )
    print("Portable shared roles keep equal source keys and independent position domains")


@dataclass
class BaseEntry:
    label: str


@dataclass
class BaseLedger:
    entries: list[BaseEntry]


def installed_path() -> None:
    import mountainash_structuremap

    package = Path(mountainash_structuremap.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    requirements = distribution("mountainash-structuremap").requires or []
    dependency = next(
        (item for item in requirements if item.partition("@")[0].strip().lower() == "mountainash"),
        None,
    )
    assert dependency is not None, requirements
    expected_commit = dependency.rsplit("@", 1)[1].strip()
    direct_url = distribution("mountainash").read_text("direct_url.json")
    assert direct_url is not None
    installed_commit = json.loads(direct_url)["vcs_info"]["commit_id"]
    assert installed_commit == expected_commit, (installed_commit, expected_commit)
    print(f"Installed package: {package}; dependency commit agrees with wheel metadata: {installed_commit}")


def _wire_value(value):
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, time):
        return {"$time": value.isoformat()}
    raise TypeError(type(value).__name__)


def _wire_hook(value):
    if "$decimal" in value:
        return Decimal(value["$decimal"])
    if "$datetime" in value:
        return datetime.fromisoformat(value["$datetime"])
    if "$date" in value:
        return date.fromisoformat(value["$date"])
    if "$time" in value:
        return time.fromisoformat(value["$time"])
    return value


def _table_schemas(prepared):
    return {name: plan.table_schema.to_frictionless() for name, plan in prepared.tables.items()}


def _portable_artifacts(derived, prepared, mapping):
    return {
        "declaration": derived.structure.model_dump(mode="json"),
        "mapping": mapping.model_dump(mode="json"),
        "table_schemas": _table_schemas(prepared),
    }


def _mapping():
    return MappingSpec(
        defaults={"variant_layout": "split"},
        overrides={
            "entry_or_note": UseOverride(
                variant_layout="split",
                discriminator={"strategy": "recorded", "column": "kind", "values": {"entry": "ENTRY", "note": "NOTE"}},
                alternatives={
                    "entry": AlternativeLayout(table="entries", prefix="entry"),
                    "note": AlternativeLayout(table="notes", prefix="note"),
                },
            ),
            "entry_or_note/child": UseOverride(table="recursive_children", prefix="child"),
            "entry_or_note/content": UseOverride(table="fragments", column="fragment_xml"),
        },
    )


def _independent_catalog_tables(prepared):
    # Literal independent input: no table rows are copied from decompose().
    # The positions are shared across the explicit entries/notes Split tables.
    tables = {name: [] for name in prepared.tables}
    tables.update({
        "root": [{"_sm_key": "catalog-1"}],
        "entries": [
            {
                "_sm_key": "entry-parent", "_sm_parent": "catalog-1", "_sm_position": 0,
                "kind": "ENTRY", "entry_code": "parent",
                "entry_amount": Decimal("9007199254740993.125"),
                "entry_serial": "1234567890123456789012345678901234567890",
                "entry_day": date(2024, 2, 29), "_sm_entry_day_offset": 0,
                "entry_clock": time(23, 59, 59), "_sm_entry_clock_offset": 120,
                "entry_when": datetime(2024, 2, 2, 18, 5, 6), "_sm_entry_when_offset": 600,
            },
            {
                "_sm_key": "entry-last", "_sm_parent": "catalog-1", "_sm_position": 14,
                "kind": "ENTRY", "entry_code": "last",
                "entry_amount": Decimal("2.000"), "entry_serial": "42",
                "entry_day": date(2024, 1, 1), "_sm_entry_day_offset": None,
                "entry_clock": time(0), "_sm_entry_clock_offset": 0,
                "entry_when": datetime(2024, 2, 3, 4, 5, 6), "_sm_entry_when_offset": 0,
            },
        ],
        "notes": [{
            "_sm_key": "note-1", "_sm_parent": "catalog-1", "_sm_position": 7,
            "kind": "NOTE",
        }],
        "recursive_children": [{
            "_sm_key": "child-1", "_sm_parent": "entry-parent", "_sm_position": 0,
            "child_amount": Decimal("0.001"), "child_serial": "-7",
            "child_day": date(2024, 2, 28), "_sm_child_day_offset": None,
            "child_clock": time(12, 34, 56), "_sm_child_clock_offset": -240,
            "child_when": datetime(2024, 2, 3, 4, 5, 6), "_sm_child_when_offset": None,
            "child_code": "child",
        }],
        "fragments": [
            {"_sm_key": "fragment-text", "_sm_parent": "note-1", "_sm_position": 0,
             "fragment_xml": '<sm:fragment xmlns:sm="urn:mountainash:structuremap:fragment">lead</sm:fragment>'},
            {"_sm_key": "fragment-item", "_sm_parent": "note-1", "_sm_position": 1,
             "fragment_xml": (
                 '<sm:fragment xmlns:sm="urn:mountainash:structuremap:fragment">'
                 '<q:item xmlns:q="urn:external" q:flag="yes">inside<q:child/>inner-tail</q:item>outer-tail'
                 '</sm:fragment>'
             )},
        ],
    })
    for rows in tables.values():
        rows.reverse()
    return tables


def _assert_document(value) -> None:
    from xsdata.formats.dataclass.models.generics import AnyElement
    from xsdata.models.datatype import XmlDate, XmlDateTime, XmlTime

    assert [type(item) for item in value.entry_or_note] == [Entry, Note, Entry]
    parent, note, last = value.entry_or_note
    assert parent.code == "parent"
    assert parent.amount == Decimal("9007199254740993.125")
    assert parent.serial == 1234567890123456789012345678901234567890
    assert parent.day == XmlDate.from_string("2024-02-29Z")
    assert parent.clock == XmlTime.from_string("23:59:59+02:00")
    assert parent.when == XmlDateTime.from_string("2024-02-03T04:05:06+10:00")
    assert (parent.day.offset, parent.clock.offset, parent.when.offset) == (0, 120, 600)
    assert parent.child == [Entry(
        amount=Decimal("0.001"), serial=-7,
        day=XmlDate.from_string("2024-02-28"),
        clock=XmlTime.from_string("12:34:56-04:00"),
        when=XmlDateTime.from_string("2024-02-03T04:05:06"), code="child",
    )]
    child = parent.child[0]
    assert (child.day.offset, child.clock.offset, child.when.offset) == (None, -240, None)
    assert last.code == "last" and last.amount == Decimal("2.000")
    assert last.serial == 42
    assert last.day == XmlDate.from_string("2024-01-01")
    assert last.clock == XmlTime.from_string("00:00:00Z")
    assert last.when == XmlDateTime.from_string("2024-02-03T04:05:06Z")
    assert (last.day.offset, last.clock.offset, last.when.offset) == (None, 0, 0)
    assert note.content == [
        "lead",
        AnyElement(
            qname="{urn:external}item", text="inside", tail="outer-tail",
            attributes={"{urn:external}flag": "yes"},
            children=[AnyElement(qname="{urn:external}child", text="", tail="inner-tail")],
        ),
    ]


def _assert_gapped_root_positions(tables, prepared) -> None:
    root_layout = prepared.tables[prepared.root_table].layout
    root_key = tuple(tables[prepared.root_table][0][column] for column in root_layout.key)
    positions = [
        row[prepared.tables[table_name].layout.position]
        for table_name, rows in tables.items()
        if table_name != prepared.root_table
        for row in rows
        if tuple(row[column] for column in prepared.tables[table_name].layout.parent) == root_key
    ]
    assert sorted(positions) == [0, 7, 14]


def main(mode: str) -> None:
    if mode in {"independent", "write", "read", "invalid"}:
        load_xml_types()
    installed_path()
    if mode.startswith("execution-"):
        from execution_cases import run

        _, operation, format = mode.split("-")
        run(operation, format)
        return
    if mode == "authoring-persist":
        _authoring_persist()
        _persist_roles()
        return
    if mode == "authoring-independent":
        _authoring_independent()
        _read_roles()
        return
    if mode == "pydantic":
        import pydantic

        print(f"Pydantic version: {pydantic.__version__}")
        prepared = prepare(derive(PydanticCatalog), MappingSpec(overrides={
            "entries/code": {"column": "identifier"},
            "entries/amount": {"column": "quantity"},
        }))
        tables = {
            "root": [{"_sm_key": "catalog-1"}],
            "entries": [
                {"_sm_key": "second", "_sm_parent": "catalog-1", "_sm_position": 8,
                 "identifier": "second", "quantity": 2},
                {"_sm_key": "first", "_sm_parent": "catalog-1", "_sm_position": 2,
                 "identifier": "first", "quantity": 1},
            ],
        }
        document = reconstruct(tables, prepared)
        assert type(document) is PydanticCatalog
        assert [type(item) for item in document.entries] == [PydanticEntry, PydanticEntry]
        assert [(item.code, item.amount) for item in document.entries] == [("first", 1), ("second", 2)]
        decomposed = decompose(document, prepared)
        assert [(row["identifier"], row["quantity"]) for row in decomposed["entries"]] == [("first", 1), ("second", 2)]
        assert [row["_sm_position"] for row in decomposed["entries"]] == [0, 1]
        wire = serialize(document, prepared)
        assert json.loads(wire) == {"records": [
            {"entry-code": "first", "total": 1}, {"entry-code": "second", "total": 2},
        ]}
        parsed = parse(wire, prepared)
        assert type(parsed) is PydanticCatalog
        assert [(item.code, item.amount) for item in parsed.entries] == [("first", 1), ("second", 2)]
        invalid = '{"records": [{"entry-code": "bad", "total": -1}]}'
        try:
            parse(invalid, prepared)
        except StructureMapError as error:
            assert isinstance(error.__cause__, ValidationError)
            assert error.code == "native_parse_failed"
            assert error.__cause__.errors()[0]["type"] == "greater_than"
        else:
            raise AssertionError("invalid constrained Pydantic input was accepted")
        print("Pydantic native reconstruction, decomposition, alias JSON and validation passed")
        return

    if mode == "independent":
        mapping = _mapping()
        prepared = prepare(derive(Catalog), mapping)
        tables = _independent_catalog_tables(prepared)
        _assert_gapped_root_positions(tables, prepared)
        document = reconstruct(tables, prepared)
        _assert_document(document)
        print("Independent root/entry/note/recursive-child/fragment tables reconstructed with exact values and order")
    elif mode == "write":
        mapping = _mapping()
        derived = derive(Catalog)
        prepared = prepare(derived, mapping)
        parsed = parse(CATALOG_XML, prepared)
        _assert_document(parsed)
        tables = decompose(parsed, prepared)
        for rows in tables.values():
            rows.reverse()
        wire = {
            "tables": tables,
            "artifacts": _portable_artifacts(derived, prepared, mapping),
        }
        Path("complete-input.json").write_text(json.dumps(wire, default=_wire_value), encoding="utf-8")
        print("Writer emitted actual prepared table carriers, declaration, mapping and TableSchemas")
    elif mode == "read":
        data = json.loads(Path("complete-input.json").read_text(encoding="utf-8"), object_hook=_wire_hook)
        mapping = MappingSpec.model_validate(data["artifacts"]["mapping"])
        native = derive(Catalog)
        derived = DerivedModel(
            structure=StructureSpec.model_validate(data["artifacts"]["declaration"]),
            native_types=native.native_types, binding=native.binding,
        )
        relations = {
            name: TableSchema.from_frictionless(serialized)
            for name, serialized in data["artifacts"]["table_schemas"].items()
        }
        prepared = prepare(derived, mapping, relations=relations)
        tables = data["tables"]
        rebuilt = reconstruct(tables, prepared)
        _assert_document(rebuilt)
        xml_root = fromstring(serialize(rebuilt, prepared))
        assert [element.tag for element in xml_root] == [
            "{urn:structuremap:catalog}entry", "{urn:structuremap:catalog}note",
            "{urn:structuremap:catalog}entry",
        ]
        assert xml_root[1].text == "lead"
        assert xml_root[1][0].tag == "{urn:external}item"
        assert xml_root[1][0].tail == "outer-tail"
        assert xml_root[1][0][0].tail == "inner-tail"
        independent = _independent_catalog_tables(prepared)
        _assert_gapped_root_positions(independent, prepared)
        _assert_document(reconstruct(independent, prepare(derived, mapping)))
        print("Fresh reader reconstructed writer and independent tables, exact carriers, offsets and mixed content")
    elif mode == "invalid":
        mapping = _mapping()
        prepared = prepare(derive(Catalog), mapping)
        tables = _independent_catalog_tables(prepared)
        tables["notes"][0][prepared.tables["notes"].layout.position] = tables["entries"][0][prepared.tables["entries"].layout.position]
        try:
            reconstruct(tables, prepared)
        except StructureMapError as error:
            assert error.code == "duplicate_position", (error.code, error)
        else:
            raise AssertionError("duplicate cross-role Split positions were accepted")
        print("Duplicate cross-role Split positions rejected with the expected error")
    elif mode == "base":
        plain = prepare(derive(BaseLedger))
        supplied = {
            "root": [{"_sm_key": "r"}],
            "entries": [{"_sm_key": "e", "_sm_parent": "r", "_sm_position": 0, "label": "base-only"}],
        }
        from mountainash_structuremap.execution import (
            OutputTarget,
            decompose_arrow,
            decompose_to,
            reconstruct_from,
        )

        expected = BaseLedger([BaseEntry("base-only")])
        assert reconstruct(supplied, plain) == expected
        assert reconstruct_from(decompose_arrow(expected, plain), plain) == expected
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="base-output-") as directory:
            output = decompose_to(
                expected, plain, OutputTarget(Path(directory), "ledger"),
            )
            try:
                reconstruct_from(output.path, plain)
            except StructureMapError as error:
                assert error.code == "unavailable_capability", error
                assert "mountainash-structuremap[files]" in error.remedy
                assert isinstance(error.__cause__, ModuleNotFoundError)
            else:
                raise AssertionError("managed file input unexpectedly succeeded without Files")
        print("Base-only execution imported, Arrow roundtrip passed, managed input requests [files]")
    elif mode == "missing-xml":
        try:
            derive(Note)
        except StructureMapError as error:
            assert error.code == "unavailable_binding", error
            assert "mountainash-structuremap[xml]" in error.remedy
            assert isinstance(error.__cause__, ModuleNotFoundError)
        else:
            raise AssertionError("detected XML binding unexpectedly succeeded without extra")
        try:
            derive(Note, binding="xsdata")
        except StructureMapError as error:
            assert error.code == "unavailable_binding", error
            assert "mountainash-structuremap[xml]" in error.remedy
            assert isinstance(error.__cause__, ModuleNotFoundError)
        else:
            raise AssertionError("declared xsdata binding unexpectedly succeeded without extra")
        print("Missing XML extra produced actionable errors with native causes")
    else:
        raise ValueError(f"unknown mode: {mode}")


if __name__ == "__main__":
    main(sys.argv[1])
