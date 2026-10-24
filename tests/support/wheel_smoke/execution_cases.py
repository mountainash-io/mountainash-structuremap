"""Real installed-wheel storage cases, driven by the existing isolated runner."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.parquet as pq
from mountainash.typespec.converters import to_arrow_schema
from mountainash.typespec.spec import TableSchema
from mountainash_files import FileSourceSpec
from wheel_external import DatePartsDecoder, DatePartsEncoder
from wheel_models import (
    CATALOG_XML,
    Alternatives,
    AuthoringAddress,
    AuthoringDirectory,
    AuthoringItem,
    AuthoringLedger,
    Catalog,
    Counted,
    ExecutionBatch,
    ExecutionLine,
    ExecutionStudent,
    Flagged,
    PydanticCatalog,
    PydanticEntry,
    load_xml_types,
)

from mountainash_structuremap import MappingSpec, derive, parse, prepare, reconstruct
from mountainash_structuremap.definitions import DerivedModel, StructureSpec
from mountainash_structuremap.execution import (
    OutputTarget,
    decompose_to,
    decompose_xml_to,
    reconstruct_from,
    reconstruct_to,
)
from mountainash_structuremap.mapping import RowLayout, SourceKey, UseOverride


def cases():
    # Import only the copied installed fixture, never the repository test suite.
    import runner as fixture

    load_xml_types()
    yield "xml-split", Catalog, fixture._mapping(), None, fixture._independent_catalog_tables, fixture._assert_document
    yield "external", AuthoringLedger, fixture._portable_mapping(), fixture._authoring_types(), lambda _: fixture._authoring_rows(), fixture._assert_authoring_value
    yield (
        "external-empty", AuthoringLedger, fixture._portable_mapping(), fixture._authoring_types(),
        lambda _: {"root": [{"_sm_key": "empty"}], "items": []},
        lambda value: equal(value, AuthoringLedger(())),
    )
    yield (
        "external-null", AuthoringLedger, fixture._portable_mapping(), fixture._authoring_types(),
        lambda _: {"root": [{"_sm_key": "null"}], "items": [
            {"_sm_key": "item", "_sm_parent": "null", "_sm_position": 4,
             "country": "AU", "amount": "0.01", "year": None, "month": None, "day": None},
        ]},
        lambda value: equal(value, AuthoringLedger((AuthoringItem("AU", Decimal("0.01"), None),))),
    )

    expected_roles = AuthoringDirectory(
        [AuthoringAddress("Kyoto", "JP"), AuthoringAddress("Oslo", "NO")],
        [AuthoringAddress("Oslo", "NO")],
    )
    yield "roles", AuthoringDirectory, fixture._role_mapping(), None, lambda _: fixture._role_rows(), lambda value: equal(value, expected_roles)

    mapping = MappingSpec(overrides={
        "": UseOverride(table="batches"),
        "id": UseOverride(column="batch_id"),
        "students": UseOverride(table="students", ordered=False),
        "students/id": UseOverride(column="student_id"),
        "students/lines/id": UseOverride(column="line_id"),
        "students/lines": UseOverride(table="lines", ordered=False, key=SourceKey(columns=("batch_id", "student_id", "line_id"))),
    }, layouts={
        "batches": RowLayout(key=("batch_id",), generated=False),
        "students": RowLayout(key=("batch_id", "student_id"), generated=False, parent=("batch_id",)),
        "lines": RowLayout(key=("batch_id", "student_id", "line_id"), generated=False, parent=("batch_id", "student_id")),
    })
    rows = {
        "batches": [{"batch_id": 7}],
        "students": [{"batch_id": 7, "student_id": 11}, {"batch_id": 7, "student_id": 12}],
        "lines": [{"batch_id": 7, "student_id": 12, "line_id": 4, "label": "second"},
                  {"batch_id": 7, "student_id": 11, "line_id": 3, "label": "first"}],
    }
    expected = ExecutionBatch(7, [ExecutionStudent(11, [ExecutionLine(3, "first", 7, 11)]), ExecutionStudent(12, [ExecutionLine(4, "second", 7, 12)])])
    yield "ancestor", ExecutionBatch, mapping, None, lambda _: rows, lambda value: equal(value, expected)

    for layout in ("prefixed", "shared"):
        mapping = MappingSpec(overrides={"items": UseOverride(
            variant_layout=layout, shared_types="exact",
            alternatives=({"Counted": {"prefix": "Counted"}, "Flagged": {"prefix": "Flagged"}} if layout == "prefixed" else {}),
        )})
        def alternative_rows(_, layout=layout):
            common = {"_sm_key": "a", "_sm_parent": "root", "_sm_position": 3, "_sm_variant": "Counted"}
            other = {"_sm_key": "b", "_sm_parent": "root", "_sm_position": 9, "_sm_variant": "Flagged"}
            if layout == "shared":
                common.update(common="same", count=7, flag=None)
                other.update(common="same", count=None, flag=False)
            else:
                common.update(Counted_common="same", Counted_count=7, Flagged_common=None, Flagged_flag=None)
                other.update(Counted_common=None, Counted_count=None, Flagged_common="same", Flagged_flag=False)
            return {"root": [{"_sm_key": "root"}], "items": [other, common]}
        yield layout, Alternatives, mapping, None, alternative_rows, lambda value: equal(value, Alternatives([Counted("same", 7), Flagged("same", False)]))

    pydantic_rows = {
        "root": [{"_sm_key": "root"}],
        "entries": [{"_sm_key": "second", "_sm_parent": "root", "_sm_position": 8, "code": "second", "amount": 2},
                    {"_sm_key": "first", "_sm_parent": "root", "_sm_position": 2, "code": "first", "amount": 1}],
    }
    def check_pydantic(value):
        assert type(value) is PydanticCatalog
        assert all(type(item) is PydanticEntry for item in value.entries)
        assert [(item.code, item.amount) for item in value.entries] == [("first", 1), ("second", 2)]
    yield "pydantic", PydanticCatalog, MappingSpec(), None, lambda _: pydantic_rows, check_pydantic


def equal(actual, expected):
    assert type(actual) is type(expected)
    assert actual == expected, (actual, expected)


def run(operation: str, format: str) -> None:
    root = Path(f"execution-{format}")
    root.mkdir(exist_ok=True)
    for name, model, mapping, relations, independent, check in cases():
        artifact = root / f"{name}.json"
        native = derive(model)
        external = name.startswith("external")
        decode = (DatePartsDecoder(),) if external else ()
        encode = (DatePartsEncoder(),) if external else ()
        if operation == "write":
            reader = prepare(native, mapping, relations=relations, components=decode, directions=frozenset({"reconstruct"}))
            rows = independent(reader)
            value = reconstruct(rows, reader)
            check(value)
            writer = prepare(native, mapping, relations=relations, components=encode, directions=frozenset({"decompose"}))
            artifact.write_text(json.dumps({
                "declaration": native.structure.model_dump(mode="json"),
                "mapping": mapping.model_dump(mode="json"),
                "relations": {key: table.table_schema.to_frictionless() for key, table in reader.tables.items()},
            }), encoding="utf-8")
            decompose_to(value, writer, OutputTarget(root, name), format=format, buffer_rows=1)
            if name == "xml-split":
                xml_input = root / "catalog.xml"
                xml_input.write_text(CATALOG_XML, encoding="utf-8")
                decompose_xml_to(
                    FileSourceSpec(path=str(xml_input)), writer,
                    OutputTarget(root, "xml-streamed"), record_path="entry_or_note",
                    format=format, buffer_rows=1,
                )
            # These files are independent literal input, not copied from writer output.
            folder = root / f"{name}-independent"
            folder.mkdir()
            for index, (key, plan) in enumerate(reader.tables.items()):
                table = pa.Table.from_pylist(rows[key], schema=to_arrow_schema(plan.table_schema))
                path = folder / f"{index}.{format}"
                if format == "parquet":
                    pq.write_table(table, path)
                else:
                    with ipc.new_file(path, table.schema) as output:
                        output.write_table(table)
        elif operation == "read":
            data = json.loads(artifact.read_text(encoding="utf-8"))
            restored = DerivedModel(structure=StructureSpec.model_validate(data["declaration"]), native_types=native.native_types, binding=native.binding)
            reader = prepare(restored, MappingSpec.model_validate(data["mapping"]),
                             relations={key: TableSchema.from_frictionless(schema) for key, schema in data["relations"].items()},
                             components=decode, directions=frozenset({"reconstruct"}))
            check(reconstruct_from(root / name, reader))
            sources = {key: FileSourceSpec(path=str(root / f"{name}-independent" / f"{index}.{format}")) for index, key in enumerate(reader.tables)}
            check(reconstruct_from(sources, reader))
            if name == "xml-split":
                streamed = root / "xml-streamed"
                check(reconstruct_from(streamed, reader))
                for packaging in ("standalone", "managed"):
                    output = reconstruct_to(
                        streamed, reader, OutputTarget(root, f"xml-{packaging}"),
                        packaging=packaging,
                    )
                    document = output.path if packaging == "standalone" else output.path / "document"
                    wire = document.read_text(encoding="utf-8")
                    check(parse(wire, reader))
                    xml = fromstring(wire)
                    assert [child.tag for child in xml] == [
                        "{urn:structuremap:catalog}entry", "{urn:structuremap:catalog}note",
                        "{urn:structuremap:catalog}entry",
                    ]
                    assert xml[1].text == "lead"
                    assert xml[1][0].tag == "{urn:external}item"
                    assert xml[1][0].tail == "outer-tail"
                    assert xml[1][0][0].tail == "inner-tail"
            elif name == "pydantic":
                output = reconstruct_to(sources, reader, OutputTarget(root, "pydantic-json"))
                wire = output.path.read_text(encoding="utf-8")
                assert json.loads(wire) == {"records": [
                    {"entry-code": "first", "total": 1},
                    {"entry-code": "second", "total": 2},
                ]}
                check(parse(wire, reader))
        else:
            raise ValueError(operation)
        print(f"Installed {operation} {format}: {name} managed + independent typed input")
