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

from mountainash.typespec import TypeSpec  # noqa: E402
from wheel_models import (  # noqa: E402
    CATALOG_XML,
    Catalog,
    Entry,
    Note,
    load_xml_types,
)

from mountainash_structuremap import (  # noqa: E402
    MappingSpec,
    StructureMapError,
    decompose,
    derive,
    parse,
    prepare,
    reconstruct,
    serialize,
)
from mountainash_structuremap.definitions import DerivedModel, StructureSpec  # noqa: E402
from mountainash_structuremap.mapping import AlternativeLayout, UseOverride  # noqa: E402


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


def _type_specs(prepared):
    return {name: plan.typespec.to_frictionless() for name, plan in prepared.tables.items()}


def _portable_artifacts(derived, prepared, mapping):
    return {
        "declaration": derived.structure.model_dump(mode="json"),
        "mapping": mapping.model_dump(mode="json"),
        "typespecs": _type_specs(prepared),
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
    root_key = tables[prepared.root_table][0][prepared.key_column]
    root_children = [
        row
        for table_name, rows in tables.items()
        if table_name != prepared.root_table
        for row in rows
        if row.get(prepared.parent_column) == root_key
    ]
    assert sorted(row[prepared.position_column] for row in root_children) == [0, 7, 14]


def main(mode: str) -> None:
    if mode in {"independent", "write", "read", "invalid"}:
        load_xml_types()
    installed_path()

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
        print("Writer emitted actual prepared table carriers, declaration, mapping and TypeSpecs")
    elif mode == "read":
        data = json.loads(Path("complete-input.json").read_text(encoding="utf-8"), object_hook=_wire_hook)
        mapping = MappingSpec.model_validate(data["artifacts"]["mapping"])
        native = derive(Catalog)
        derived = DerivedModel(
            structure=StructureSpec.model_validate(data["artifacts"]["declaration"]),
            native_types=native.native_types, binding=native.binding,
        )
        prepared = prepare(derived, mapping)
        for name, serialized in data["artifacts"]["typespecs"].items():
            restored = TypeSpec.from_frictionless(serialized)
            assert restored.to_frictionless() == prepared.tables[name].typespec.to_frictionless()
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
        tables["notes"][0][prepared.position_column] = tables["entries"][0][prepared.position_column]
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
        assert reconstruct(supplied, plain) == BaseLedger([BaseEntry("base-only")])
        print("Base-only dataclass execution passed")
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
