from __future__ import annotations

import io
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.parquet as pq
import pytest
from mountainash.typespec.converters import to_arrow_schema
from mountainash_files import FileSourceSpec, ZipArchive

from mountainash_structuremap import (
    MappingSpec,
    StructureMapError,
    UseOverride,
    decompose,
    derive,
    prepare,
    reconstruct,
)
from mountainash_structuremap.mapping import SourceKey, TypeCast
from mountainash_structuremap.table_io import ArrowCollector, FileTableSink, read_input_tables
from tests.test_mapping_components import order_case, prepared_order


@dataclass(frozen=True)
class Entry:
    value: str


@dataclass(frozen=True)
class Document:
    label: str
    entries: list[Entry]
    footer: str | None


def prepared_document():
    return prepare(
        derive(Document),
        MappingSpec(
            overrides={
                "entries/value": UseOverride(cast=TypeCast(to="i8")),
            }
        ),
    )


def independent_tables():
    return {
        "root": pa.Table.from_pylist(
            [
                {"_sm_key": "external", "label": "", "footer": None},
            ],
            schema=pa.schema([("_sm_key", pa.string()), ("label", pa.string()), ("footer", pa.string())]),
        ),
        "entries": pa.Table.from_pylist(
            [
                {"_sm_key": "later", "_sm_parent": "external", "_sm_position": 1, "value": 127},
                {"_sm_key": "earlier", "_sm_parent": "external", "_sm_position": 0, "value": -128},
            ],
            schema=pa.schema(
                [
                    ("_sm_key", pa.string()),
                    ("_sm_parent", pa.string()),
                    ("_sm_position", pa.int64()),
                    ("value", pa.int8()),
                ]
            ),
        ),
    }


def write_independent(path, table, format):
    if format == "parquet":
        pq.write_table(table, path)
    else:
        with ipc.new_file(path, table.schema) as writer:
            writer.write_table(table)


@pytest.mark.parametrize("format", ["memory", "parquet", "arrow"])
def test_independent_typed_input_reconstructs_without_manifest(tmp_path, format):
    sources = independent_tables()
    if format != "memory":
        path = tmp_path / f"unrelated-name.{format}"
        write_independent(path, sources["entries"], format)
        sources["entries"] = FileSourceSpec(path=str(path))
    rows = read_input_tables(sources, prepared_document())
    assert reconstruct(rows, prepared_document()) == Document("", [Entry("-128"), Entry("127")], None)


@pytest.mark.parametrize("format", ["parquet", "arrow"])
def test_fixed_schema_sink_preserves_empty_tables(tmp_path, format):
    prepared = prepared_document()
    source = Document("", [], None)
    paths = {name: tmp_path / f"{index:04d}.{format}" for index, name in enumerate(prepared.tables)}
    sink = FileTableSink(prepared.tables, paths, format=format)
    for name, rows in decompose(source, prepared).items():
        sink.accept(name, rows)
    sink.close()
    assert dict(sink.row_counts) == {"root": 1, "entries": 0}
    actual = read_input_tables({name: FileSourceSpec(path=str(path)) for name, path in paths.items()}, prepared)
    assert reconstruct(actual, prepared) == source
    reader = pq.read_table(paths["entries"]) if format == "parquet" else ipc.open_file(paths["entries"]).read_all()
    assert reader.num_rows == 0
    assert reader.schema.field("value").type == pa.int8()
    root = pq.read_table(paths["root"]) if format == "parquet" else ipc.open_file(paths["root"]).read_all()
    assert root.schema.field("footer").type == pa.string()
    assert root.column("label").to_pylist() == [""]


@pytest.mark.parametrize("format", ["parquet", "arrow"])
def test_synchronous_sink_splits_large_accept_into_bounded_batches(tmp_path, format):
    prepared = prepared_document()
    paths = {name: tmp_path / f"{index}.{format}" for index, name in enumerate(prepared.tables)}
    sink = FileTableSink(prepared.tables, paths, format=format, buffer_rows=2)
    source = Document("x", [Entry(str(n)) for n in range(5)], None)
    for name, rows in decompose(source, prepared).items():
        sink.accept(name, rows)
    assert sink.row_counts["entries"] == 4  # Full buffers are already written, before close.
    sink.close()
    if format == "parquet":
        stored = pq.ParquetFile(paths["entries"])
        sizes = [stored.metadata.row_group(n).num_rows for n in range(stored.num_row_groups)]
    else:
        stored = ipc.open_file(paths["entries"])
        sizes = [stored.get_batch(n).num_rows for n in range(stored.num_record_batches)]
    assert sizes == [2, 2, 1]
    assert (
        reconstruct(
            read_input_tables({name: FileSourceSpec(path=str(path)) for name, path in paths.items()}, prepared),
            prepared,
        )
        == source
    )


def test_arrow_collector_preserves_schema_and_takes_ownership_of_rows():
    prepared = prepared_document()
    collector = ArrowCollector(prepared.tables)
    row = {"_sm_key": "external", "label": "original", "footer": None}
    collector.accept("root", [row])
    row["label"] = "changed"
    tables = collector.finish()
    assert tables["root"].column("label").to_pylist() == ["original"]
    assert tables["entries"].schema.field("value").type == pa.int8()
    assert tables["entries"].num_rows == 0


@pytest.mark.parametrize("format", ["parquet", "arrow"])
def test_external_multicolumn_components_and_lexical_decimal_survive_files(tmp_path, format):
    schemas, mapping, rows, expected = order_case()
    prepared = prepared_order(schemas, mapping)
    sources = {}
    for index, (name, table) in enumerate(prepared.tables.items()):
        path = tmp_path / f"{index}.{format}"
        write_independent(path, pa.Table.from_pylist(rows[name], schema=to_arrow_schema(table.table_schema)), format)
        sources[name] = FileSourceSpec(path=str(path))
    assert reconstruct(read_input_tables(sources, prepared), prepared) == expected


@pytest.mark.parametrize("format", ["parquet", "arrow"])
def test_typed_decimal_temporal_companions_survive_actual_storage(tmp_path, format):
    from xsdata.models.datatype import XmlDateTime, XmlTime

    from tests.fixtures.xml_scalar_models import PhysicalDocument, PhysicalValues

    prepared = prepare(derive(PhysicalDocument))
    source = PhysicalDocument(
        [
            PhysicalValues(
                Decimal("9007199254740993.125"),
                XmlDateTime.from_string("2024-02-03T04:05:06.123456+10:00"),
                XmlTime.from_string("12:34:56.654321-05:30"),
            ),
            PhysicalValues(None, None, None),
        ]
    )
    paths = {name: tmp_path / f"{index}.{format}" for index, name in enumerate(prepared.tables)}
    sink = FileTableSink(prepared.tables, paths, format=format, buffer_rows=1)
    for name, rows in decompose(source, prepared).items():
        sink.accept(name, rows)
    sink.close()
    actual = read_input_tables({name: FileSourceSpec(path=str(path)) for name, path in paths.items()}, prepared)
    assert actual["values"][0]["amount"] == Decimal("9007199254740993.125")
    assert actual["values"][0]["stamp"] == datetime(2024, 2, 2, 18, 5, 6, 123456)
    assert actual["values"][0]["_sm_stamp_offset"] == 600
    assert actual["values"][0]["_sm_clock_offset"] == -330
    assert reconstruct(actual, prepared) == source


@pytest.mark.parametrize("format", ["parquet", "arrow"])
def test_cast_source_keys_and_arbitrary_logical_names_use_supplied_paths(tmp_path, format):
    from tests.test_mapping_casts import Item, Items

    name = "../logical/name: not a path"
    prepared = prepare(
        derive(Items),
        MappingSpec(
            overrides={
                "items": UseOverride(table=name, key=SourceKey(columns=("id",))),
                "items/id": UseOverride(cast=TypeCast(to="u64")),
            }
        ),
    )
    source = Items([Item("18446744073709551615", "")])
    paths = {table: tmp_path / f"{index:04d}.{format}" for index, table in enumerate(prepared.tables)}
    sink = FileTableSink(prepared.tables, paths, format=format, buffer_rows=1)
    for table, rows in decompose(source, prepared).items():
        sink.accept(table, rows)
    sink.close()
    assert (
        reconstruct(
            read_input_tables({table: FileSourceSpec(path=str(path)) for table, path in paths.items()}, prepared),
            prepared,
        )
        == source
    )
    assert paths[name].is_file()


@pytest.mark.parametrize("mutation", ["width", "order", "extra", "missing"])
def test_physical_schema_mismatch_is_rejected_without_coercion(mutation):
    sources = independent_tables()
    table = sources["entries"]
    if mutation == "width":
        table = table.set_column(3, "value", table.column("value").cast(pa.int64()))
    elif mutation == "order":
        table = table.select(list(reversed(table.column_names)))
    elif mutation == "extra":
        table = table.append_column("extra", pa.array([1, 2]))
    else:
        table = table.drop(["value"])
    sources["entries"] = table
    with pytest.raises(StructureMapError) as caught:
        read_input_tables(sources, prepared_document())
    assert caught.value.table == "entries"


def test_storage_metadata_and_carrier_nullability_are_not_mapping_identity():
    sources = independent_tables()
    table = sources["entries"]
    schema = pa.schema(
        [pa.field(field.name, field.type, nullable=False, metadata={"external": "field"}) for field in table.schema],
        metadata={"external": "storage"},
    )
    sources["entries"] = pa.Table.from_arrays(table.columns, schema=schema)
    assert reconstruct(read_input_tables(sources, prepared_document()), prepared_document()).entries == [
        Entry("-128"),
        Entry("127"),
    ]


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_logical_table_bindings_are_exact(change):
    sources = independent_tables()
    if change == "missing":
        del sources["entries"]
    else:
        sources["unexpected"] = sources["entries"]
    with pytest.raises(StructureMapError):
        read_input_tables(sources, prepared_document())


@pytest.mark.parametrize(
    "options",
    [
        {"glob": "*.arrow"},
        {"path": "{name}.arrow"},
        {"archive": ZipArchive()},
        {"on_missing": "skip"},
    ],
)
def test_nonconcrete_sources_fail_before_acquisition(tmp_path, monkeypatch, options):
    import mountainash_files

    def forbidden(*args, **kwargs):
        pytest.fail("nonconcrete source reached acquisition")

    monkeypatch.setattr(mountainash_files, "stream", forbidden)
    monkeypatch.setattr(mountainash_files, "inspect_schema", forbidden)
    sources = independent_tables()
    sources["entries"] = replace(FileSourceSpec(path=str(tmp_path / "missing.arrow")), **options)
    with pytest.raises(StructureMapError) as caught:
        read_input_tables(sources, prepared_document())
    assert caught.value.table == "entries"


@pytest.mark.parametrize("stage", ["inspect", "parse"])
@pytest.mark.parametrize("count", [0, 2])
def test_parquet_results_must_have_exactly_one_source(tmp_path, monkeypatch, stage, count):
    import mountainash_files

    sources = independent_tables()
    path = tmp_path / "entries.parquet"
    pq.write_table(sources["entries"], path)
    spec = FileSourceSpec(path=str(path))
    operation = "inspect_schema" if stage == "inspect" else "parse"
    results = getattr(mountainash_files, operation)(spec)
    monkeypatch.setattr(mountainash_files, operation, lambda *args, **kwargs: results * count)
    sources["entries"] = spec
    with pytest.raises(StructureMapError) as caught:
        read_input_tables(sources, prepared_document())
    assert caught.value.table == "entries"


def test_parquet_actual_read_schema_is_rechecked(tmp_path, monkeypatch):
    import mountainash_files

    sources = independent_tables()
    path = tmp_path / "entries.parquet"
    pq.write_table(sources["entries"], path)
    real_parse = mountainash_files.parse

    def changed(source, **kwargs):
        return [
            replace(result, data=result.data.set_column(3, "value", result.data.column("value").cast(pa.int64())))
            for result in real_parse(source, **kwargs)
        ]

    monkeypatch.setattr(mountainash_files, "parse", changed)
    sources["entries"] = FileSourceSpec(path=str(path))
    with pytest.raises(StructureMapError) as caught:
        read_input_tables(sources, prepared_document())
    assert caught.value.table == "entries"


@pytest.mark.parametrize("failure", ["nonseekable", "corrupt", "close", "corrupt-and-close", None])
def test_ipc_stream_is_closed_and_primary_failure_survives(tmp_path, monkeypatch, failure):
    import mountainash_files

    table = independent_tables()["entries"]
    path = tmp_path / "entries.arrow"
    write_independent(path, table, "arrow")
    handle = path.open("rb")
    close_error = OSError("closing controlled stream")

    @contextmanager
    def controlled_stream(source, *, access):
        assert source.path == str(path)
        assert access == "random"
        stream = io.BytesIO(b"not ipc") if failure in {"corrupt", "corrupt-and-close"} else handle
        if failure == "nonseekable":
            stream = io.BytesIO(b"not seekable")
            stream.seekable = lambda: False
        try:
            yield stream
        finally:
            stream.close()
            handle.close()
            if failure in {"close", "corrupt-and-close"}:
                raise close_error

    monkeypatch.setattr(mountainash_files, "stream", controlled_stream)
    sources = independent_tables()
    sources["entries"] = FileSourceSpec(path=str(path))
    if failure is None:
        assert reconstruct(read_input_tables(sources, prepared_document()), prepared_document()).entries == [
            Entry("-128"),
            Entry("127"),
        ]
    else:
        with pytest.raises(StructureMapError) as caught:
            read_input_tables(sources, prepared_document())
        assert caught.value.table == "entries"
        if failure == "corrupt-and-close":
            assert isinstance(caught.value.__cause__, pa.ArrowInvalid)
            assert any("closing controlled stream" in note for note in getattr(caught.value, "__notes__", []))
        elif failure == "close":
            assert caught.value.__cause__ is close_error
    assert handle.closed


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_buffer_size_fails_before_creating_files(tmp_path, value):
    prepared = prepared_document()
    paths = {name: tmp_path / f"{index}.parquet" for index, name in enumerate(prepared.tables)}
    with pytest.raises(StructureMapError):
        FileTableSink(prepared.tables, paths, buffer_rows=value)
    assert not any(path.exists() for path in paths.values())


def test_parquet_reader_honors_declared_projection_options(tmp_path):
    from mountainash_files.formats.parquet import ParquetSpec

    sources = independent_tables()
    table = sources["entries"]
    path = tmp_path / "external.data"
    pq.write_table(table.append_column("unrelated", pa.array(["x", "y"])), path)
    sources["entries"] = FileSourceSpec(path=str(path), format=ParquetSpec(columns=tuple(table.column_names)))
    assert reconstruct(read_input_tables(sources, prepared_document()), prepared_document()).entries == [
        Entry("-128"),
        Entry("127"),
    ]


@pytest.mark.parametrize("primary", [False, True])
def test_sink_close_failure_closes_other_writers_without_masking_primary(tmp_path, monkeypatch, primary):
    real_writer = pq.ParquetWriter
    failure = OSError("controlled writer close failure")

    class ClosingWriter:
        def __init__(self, path, schema):
            self.writer = real_writer(path, schema)
            self.fail = path.name == "0.parquet"

        def write_table(self, table):
            self.writer.write_table(table)

        def close(self):
            self.writer.close()
            if self.fail:
                raise failure

    monkeypatch.setattr(pq, "ParquetWriter", ClosingWriter)
    prepared = prepared_document()
    paths = {name: tmp_path / f"{index}.parquet" for index, name in enumerate(prepared.tables)}
    sink = FileTableSink(prepared.tables, paths, buffer_rows=1)
    for name, rows in decompose(Document("x", [Entry("1")], None), prepared).items():
        sink.accept(name, rows)
    if primary:
        original = ValueError("primary mapping failure")
        sink.close(primary_error=original)
        assert any("controlled writer close failure" in note for note in original.__notes__)
    else:
        with pytest.raises(StructureMapError) as caught:
            sink.close()
        assert caught.value.__cause__ is failure
    assert pq.read_table(paths["entries"]).column("value").to_pylist() == [1]


def test_writer_constructor_failure_closes_already_created_files(tmp_path):
    prepared = prepared_document()
    paths = {"root": tmp_path / "root.parquet", "entries": tmp_path / "absent" / "entries.parquet"}
    with pytest.raises(StructureMapError) as caught:
        FileTableSink(prepared.tables, paths)
    assert caught.value.table == "entries"
    assert caught.value.__cause__ is not None
    assert pq.read_table(paths["root"]).num_rows == 0


def test_failure_close_discards_unwritten_rows_but_keeps_readable_empty_files(tmp_path):
    prepared = prepared_document()
    paths = {name: tmp_path / f"{index}.arrow" for index, name in enumerate(prepared.tables)}
    sink = FileTableSink(prepared.tables, paths, format="arrow", buffer_rows=10)
    for name, rows in decompose(Document("x", [Entry("1")], None), prepared).items():
        sink.accept(name, rows)
    sink.close(primary_error=ValueError("mapping stopped"))
    assert dict(sink.row_counts) == {"root": 0, "entries": 0}
    for path in paths.values():
        with ipc.open_file(path) as reader:
            assert reader.read_all().num_rows == 0


@pytest.mark.parametrize(
    ("column", "dtype"),
    [
        ("amount", pa.decimal128(24, 3)),
        ("amount", pa.decimal128(23, 4)),
        ("stamp", pa.timestamp("ms")),
        ("stamp", pa.timestamp("us", tz="UTC")),
    ],
)
def test_empty_input_still_checks_decimal_and_temporal_parameters(column, dtype):
    from tests.fixtures.xml_scalar_models import PhysicalDocument

    prepared = prepare(derive(PhysicalDocument))
    sources = {
        name: pa.Table.from_batches([], schema=to_arrow_schema(table.table_schema))
        for name, table in prepared.tables.items()
    }
    table = sources["values"]
    sources["values"] = table.set_column(table.schema.get_field_index(column), column, pa.array([], type=dtype))
    with pytest.raises(StructureMapError) as caught:
        read_input_tables(sources, prepared)
    assert caught.value.table == "values"
