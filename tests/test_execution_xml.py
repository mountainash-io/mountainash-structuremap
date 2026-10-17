import json

import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.parquet as pq
import pytest
from mountainash_files import FileSourceSpec

from mountainash_structuremap import derive, prepare
from tests.fixtures.execution_models import Document, Entry, execution_mapping
from tests.fixtures.execution_xml import WrappedDocument


def stored_tables(result):
    manifest = json.loads((result.path / "manifest.json").read_text())
    return {
        entry["name"]: (
            pq.read_table(result.path / entry["path"])
            if entry["path"].endswith(".parquet")
            else ipc.open_file(result.path / entry["path"]).read_all()
        )
        for entry in manifest["tables"]
    }


@pytest.mark.parametrize("model,wrapped", [(Document, False), (WrappedDocument, True)])
@pytest.mark.parametrize("format", ["parquet", "arrow"])
@pytest.mark.parametrize("buffer_rows", [1, 10000])
def test_direct_and_wrapped_records_preserve_casts_and_trailing_header(tmp_path, model, wrapped, format, buffer_rows):
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from

    records = "<entry><value>-128</value></entry><entry><value>127</value></entry>"
    if wrapped:
        records = f"<entries>{records}</entries>"
    source = tmp_path / "input.xml"
    source.write_text(f'<document label="native">{records}<footer>done</footer></document>')
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepare(derive(model), execution_mapping()),
        OutputTarget(tmp_path, "streamed"),
        record_path="entries",
        format=format,
        buffer_rows=buffer_rows,
    )
    tables = stored_tables(result)
    assert result.row_counts == {"root": 1, "entries": 2}
    assert tables["entries"].schema.field("value").type == pa.int8()
    assert tables["entries"].column("value").to_pylist() == [-128, 127]
    assert tables["entries"].column("_sm_position").to_pylist() == [0, 1]
    root = tables["root"].to_pylist()[0]
    assert (root["label"], root["footer"]) == ("native", "done")
    assert tables["entries"].column("_sm_parent").to_pylist() == [root["_sm_key"]] * 2
    assert reconstruct_from(result.path, prepare(derive(model), execution_mapping())) == model(
        "native",
        [Entry("-128"), Entry("127")],
        "done",
    )


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("footer", ["", "<footer>explicit</footer>"])
def test_scalar_and_record_headers_on_both_sides_and_native_defaults(tmp_path, count, footer):
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from
    from tests.fixtures.execution_xml import HeaderDocument
    from tests.fixtures.xml_models import TextValue

    source = tmp_path / "headers.xml"
    source.write_text(
        '<headers label="header"><before lang="en">first</before>'
        + "<entry><value>7</value></entry>" * count
        + '<after lang="fr">last</after>'
        + footer
        + "</headers>"
    )
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepare(derive(HeaderDocument), execution_mapping()),
        OutputTarget(tmp_path, "headers"),
        record_path="entries",
        buffer_rows=1,
    )
    expected = HeaderDocument(
        "header",
        TextValue("first", "en"),
        [Entry("7")] * count,
        TextValue("last", "fr"),
        "explicit" if footer else "native-default",
    )
    assert reconstruct_from(result.path, prepare(derive(HeaderDocument), execution_mapping())) == expected
    table = stored_tables(result)["entries"]
    assert table.num_rows == count
    assert table.schema.field("value").type == pa.int8()
    assert table.column("value").to_pylist() == [7] * count


@pytest.mark.parametrize("repeated", [False, True])
@pytest.mark.parametrize("buffer_rows", [1, 10000])
def test_nested_scopes_preserve_composite_ancestor_keys_and_position_domains(tmp_path, repeated, buffer_rows):
    from mountainash_structuremap import MappingSpec
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from
    from mountainash_structuremap.mapping import RowLayout, TypeCast
    from tests.fixtures.execution_xml import Group, GroupedDocument, NestedDocument

    model = GroupedDocument if repeated else NestedDocument
    root_tag = "grouped" if repeated else "nested"
    group_path = "groups" if repeated else "group"
    record_path = f"{group_path}/entries"
    mapping = MappingSpec(
        overrides={
            **({group_path: {"table": "groups"}} if repeated else {}),
            record_path: {"table": "entries"},
            f"{record_path}/value": {"cast": TypeCast(to="i8")},
        },
        layouts={
            "root": RowLayout(key=("label",), generated=False),
            "groups": RowLayout(
                key=("label", "code"),
                parent=("label",),
                generated=False,
                position="_sm_position" if repeated else None,
            ),
            "entries": RowLayout(
                key=("label", "code", "value"),
                parent=("label", "code"),
                generated=False,
                position="_sm_position",
            ),
        }
        if repeated
        else {},
    )
    first = '<group code="a"><entry><value>1</value></entry><entry><value>2</value></entry><footer>A</footer></group>'
    second = '<group code="b"><entry><value>1</value></entry></group>'
    source = tmp_path / "nested.xml"
    source.write_text(f'<{root_tag} label="root">{first}{second if repeated else ""}<footer>end</footer></{root_tag}>')
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepare(derive(model), mapping),
        OutputTarget(tmp_path, "nested"),
        record_path=record_path,
        buffer_rows=buffer_rows,
    )
    rows = stored_tables(result)["entries"].to_pylist()
    assert [(r["value"], r["_sm_position"]) for r in rows] == ([(1, 0), (2, 1)] + ([(1, 0)] if repeated else []))
    if repeated:
        assert [(r["label"], r["code"]) for r in rows] == [("root", "a"), ("root", "a"), ("root", "b")]
    a = Group("a", [Entry("1"), Entry("2")], "A")
    expected = model("root", [a, Group("b", [Entry("1")])] if repeated else a, "end")
    assert reconstruct_from(result.path, prepare(derive(model), mapping)) == expected


def test_record_path_distinguishes_namespace_and_structural_collisions(tmp_path):
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from
    from tests.fixtures.execution_xml import CollisionEnvelope, Group

    source = tmp_path / "collision.xml"
    source.write_text(
        '<collision xmlns:s="urn:selected" xmlns:f="urn:foreign">'
        '<section code="header"><entry><value>header</value></entry></section>'
        "<f:entry><value>foreign</value></f:entry>"
        "<s:entry><value>selected-1</value></s:entry>"
        "<s:entry><value>selected-2</value></s:entry></collision>"
    )
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepare(derive(CollisionEnvelope)),
        OutputTarget(tmp_path, "collision"),
        record_path="records",
        buffer_rows=1,
    )
    assert reconstruct_from(result.path, prepare(derive(CollisionEnvelope))) == CollisionEnvelope(
        Group("header", [Entry("header")]),
        [Entry("selected-1"), Entry("selected-2")],
        [Entry("foreign")],
    )
    assert stored_tables(result)["records"].column("value").to_pylist() == ["selected-1", "selected-2"]


@pytest.mark.parametrize("layout", ["split", "prefixed", "shared"])
@pytest.mark.parametrize("case", ["qname", "xsi-type", "nil"])
def test_repeated_native_choices_keep_selection_and_complete_values(tmp_path, layout, case):
    from mountainash_structuremap import MappingSpec, decompose, parse, reconstruct
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from
    from tests.fixtures.execution_xml import NullableSelectedNil
    from tests.fixtures.xml_variants import AnimalChoices, EmptyA, EmptyB, Hound, LocalChoices

    if case == "qname":
        model = LocalChoices
        payload = "<b:item/><a:item/><b:item/>"
        expected = LocalChoices([EmptyB(), EmptyA(), EmptyB()])
        override = {
            "values": {
                "alternatives": {
                    "{urn:a}item": {"prefix": "a", "table": "a_rows" if layout == "split" else None},
                    "{urn:b}item": {"prefix": "b", "table": "b_rows" if layout == "split" else None},
                }
            }
        }
    elif case == "xsi-type":
        model = AnimalChoices
        payload = '<an:animal xsi:type="an:HoundType" name="hound" scent="7"/><o:other/>'
        expected = AnimalChoices([Hound("hound", 7), EmptyB()])
        override = {}
    else:
        model = NullableSelectedNil
        payload = '<a:a xsi:nil="true"/><a:a/><b:b xsi:nil="true"/><b:b/>'
        expected = NullableSelectedNil([None, EmptyA(), None, EmptyB()])
        override = {}
    mapping = MappingSpec(defaults={"variant_layout": layout}, overrides=override)
    source = tmp_path / "choices.xml"
    source.write_text(
        f'<{model.__name__} xmlns:a="urn:a" xmlns:b="urn:b" xmlns:an="urn:animals" xmlns:o="urn:other" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">{payload}</{model.__name__}>'
    )
    prepared = prepare(derive(model), mapping)
    native = parse(source.read_text(), prepared)
    assert native == expected
    assert reconstruct(decompose(native, prepared), prepare(derive(model), mapping)) == expected
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepare(derive(model), mapping),
        OutputTarget(tmp_path, "choices"),
        record_path="values",
        buffer_rows=1,
    )
    assert reconstruct_from(result.path, prepare(derive(model), mapping)) == expected


@pytest.mark.parametrize("read_size", [None, 1], ids=["ordinary-source", "tail-split-source"])
def test_complete_records_keep_inline_children_nested_collections_and_mixed_fragments(tmp_path, monkeypatch, read_size):
    import io
    from contextlib import contextmanager

    import mountainash_files
    from xsdata.formats.dataclass.models.generics import AnyElement

    from mountainash_structuremap import MappingSpec
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from
    from tests.fixtures.execution_xml import CompleteDocument, CompleteRecord
    from tests.fixtures.xml_fragments import Mixed
    from tests.fixtures.xml_models import TextValue

    mapping = MappingSpec()
    source = tmp_path / "complete.xml"
    source.write_text(
        '<complete xmlns:q="urn:outside"><record code="a"><title lang="en">title</title>'
        "<entry><value>nested-1</value></entry><entry><value>nested-2</value></entry>"
        '<fragment>lead<q:item q:flag="yes">inside<q:child/>inner-tail</q:item>outer-tail</fragment>'
        '</record><record code="b"><title lang="fr">second</title></record></complete>'
    )
    expected = CompleteDocument(
        [
            CompleteRecord(
                "a",
                TextValue("title", "en"),
                [Entry("nested-1"), Entry("nested-2")],
                Mixed(
                    [
                        "lead",
                        AnyElement(
                            qname="{urn:outside}item",
                            attributes={"{urn:outside}flag": "yes"},
                            text="inside",
                            tail="outer-tail",
                            children=[AnyElement(qname="{urn:outside}child", text="", tail="inner-tail")],
                        ),
                    ]
                ),
            ),
            CompleteRecord("b", TextValue("second", "fr")),
        ]
    )

    class SmallChunks(io.BytesIO):
        def read(self, size=-1):
            return super().read(read_size if size < 0 else min(size, read_size))

    @contextmanager
    def chunked_source(spec, *, access):
        # One-byte reads guarantee a transport boundary immediately after
        # child closing tags, before their mixed-content tails are available.
        with SmallChunks(source.read_bytes()) as handle:
            yield handle

    with monkeypatch.context() as patch:
        if read_size is not None:
            patch.setattr(mountainash_files, "stream", chunked_source)
        result = decompose_xml_to(
            FileSourceSpec(path=str(source)),
            prepare(derive(CompleteDocument), mapping),
            OutputTarget(tmp_path, "complete"),
            record_path="records",
            buffer_rows=1,
        )
    assert reconstruct_from(result.path, prepare(derive(CompleteDocument), mapping)) == expected


def test_complete_record_multicolumn_codec_writes_independent_typed_cells(tmp_path):
    from mountainash.typespec.spec import FieldSpec, TypeSpec
    from mountainash.typespec.universal_types import UniversalType as U
    from xsdata.models.datatype import XmlDate

    from mountainash_structuremap import MappingSpec
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from
    from mountainash_structuremap.mapping import MappingUse
    from tests.fixtures.execution_xml import DatedDocument, DatedRecord, XmlDateParts

    mapping = MappingSpec(
        use=MappingUse(
            component="core.record",
            children={
                "records": MappingUse(
                    component="core.sequence",
                    children={
                        "item": MappingUse(
                            component="core.record",
                            children={
                                "issued": MappingUse(
                                    component="example.xml-date-parts.v1", config={"columns": ["year", "month", "day"]}
                                ),
                            },
                        ),
                    },
                ),
            },
        )
    )
    relations = {
        "records": TypeSpec(
            fields=[
                FieldSpec(name=name, type=kind, missing_values=[])
                for name, kind in (
                    ("_sm_key", U.STRING),
                    ("_sm_parent", U.STRING),
                    ("_sm_position", U.INTEGER),
                    ("year", U.INTEGER),
                    ("month", U.INTEGER),
                    ("day", U.INTEGER),
                )
            ],
            missing_values=[],
        )
    }
    relations["root"] = TypeSpec(
        fields=[FieldSpec(name="_sm_key", type=U.STRING, missing_values=[])],
        missing_values=[],
    )

    def prepared():
        return prepare(derive(DatedDocument), mapping, relations=relations, components=(XmlDateParts(),))

    source = tmp_path / "dated.xml"
    source.write_text(
        "<dated><record><issued>2024-02-29</issued></record><record><issued>2025-12-31</issued></record></dated>"
    )
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepared(),
        OutputTarget(tmp_path, "dated"),
        record_path="records",
        buffer_rows=1,
    )
    table = stored_tables(result)["records"]
    assert [(r["year"], r["month"], r["day"]) for r in table.to_pylist()] == [(2024, 2, 29), (2025, 12, 31)]
    assert all(pa.types.is_integer(table.schema.field(name).type) for name in ("year", "month", "day"))
    assert reconstruct_from(result.path, prepared()) == DatedDocument(
        [
            DatedRecord(XmlDate(2024, 2, 29)),
            DatedRecord(XmlDate(2025, 12, 31)),
        ]
    )


def test_typed_batches_are_persisted_before_source_releases_document_end(tmp_path, monkeypatch):
    import io
    from contextlib import contextmanager

    import mountainash_files

    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from

    observed = []

    class WithheldEnd(io.BytesIO):
        def __init__(self):
            super().__init__(
                b'<document label="native"><entry><value>-128</value></entry>'
                b"<entry><value>127</value></entry><entry><value>0</value></entry>"
            )
            self.released = False

        def read(self, size=-1):
            chunk = super().read(size)
            if chunk or self.released:
                return chunk
            # IPC files have an eight-byte magic/alignment prefix followed by
            # ordinary IPC messages. Read persisted messages without needing
            # the footer, which is legitimately absent until writer close.
            for path in tmp_path.rglob("*.arrow"):
                data = path.read_bytes()
                if len(data) <= 8:
                    continue
                reader = pa.BufferReader(data[8:])
                schema = ipc.read_schema(ipc.read_message(reader))
                if "value" not in schema.names:
                    continue
                assert schema.field("value").type == pa.int8()
                while reader.tell() < len(data) - 8:
                    batch = ipc.read_record_batch(ipc.read_message(reader), schema)
                    observed.extend(batch.column(schema.get_field_index("value")).to_pylist())
            assert observed == [-128, 127, 0], "record batches must reach real storage before document EOF"
            self.released = True
            return b"<footer>done</footer></document>"

    @contextmanager
    def controlled_source(source, *, access):
        with WithheldEnd() as handle:
            yield handle

    monkeypatch.setattr(mountainash_files, "stream", controlled_source)
    result = decompose_xml_to(
        FileSourceSpec(path=str(tmp_path / "controlled.xml")),
        prepare(derive(Document), execution_mapping()),
        OutputTarget(tmp_path, "streamed"),
        record_path="entries",
        format="arrow",
        buffer_rows=1,
    )
    assert observed == [-128, 127, 0]
    # Restore acquisition before reconstruction so the double only owns XML.
    monkeypatch.undo()
    assert reconstruct_from(result.path, prepare(derive(Document), execution_mapping())) == Document(
        "native",
        [Entry("-128"), Entry("127"), Entry("0")],
        "done",
    )


@pytest.mark.parametrize("record_path", ["missing", "footer", "entries/value"])
def test_invalid_record_boundary_is_rejected_before_opening_input(tmp_path, record_path):
    from mountainash_structuremap import StructureMapError
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to

    with pytest.raises(StructureMapError) as caught:
        decompose_xml_to(
            FileSourceSpec(path=str(tmp_path / "missing.xml")),
            prepare(derive(Document), execution_mapping()),
            OutputTarget(tmp_path, "rejected"),
            record_path=record_path,
        )
    assert caught.value.path == record_path
    assert caught.value.code == "unavailable_xml_stream"
    assert caught.value.remedy
    assert not isinstance(caught.value.__cause__, FileNotFoundError)


def test_trailing_ancestor_source_key_is_rejected_before_opening_input(tmp_path):
    from mountainash_structuremap import MappingSpec, StructureMapError
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to
    from mountainash_structuremap.mapping import RowLayout

    mapping = MappingSpec(
        layouts={
            "root": RowLayout(key=("footer",), generated=False),
            "entries": RowLayout(key=("_sm_key",), parent=("footer",), generated=True, position="_sm_position"),
        }
    )
    with pytest.raises(StructureMapError) as caught:
        decompose_xml_to(
            FileSourceSpec(path=str(tmp_path / "missing.xml")),
            prepare(derive(Document), mapping),
            OutputTarget(tmp_path, "rejected"),
            record_path="entries",
        )
    assert caught.value.code == "unavailable_xml_stream"
    assert caught.value.path == "footer"
    assert "in-memory" in caught.value.remedy
    assert not isinstance(caught.value.__cause__, FileNotFoundError)


def test_supplied_native_root_class_accepts_alias_root_name(tmp_path):
    from mountainash_structuremap import parse
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from

    xml = '<alias label="native"><entry><value>7</value></entry><footer>end</footer></alias>'
    expected = Document("native", [Entry("7")], "end")
    prepared = prepare(derive(Document), execution_mapping())
    assert parse(xml, prepared) == expected
    source = tmp_path / "alias.xml"
    source.write_text(xml)
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepared,
        OutputTarget(tmp_path, "alias"),
        record_path="entries",
        buffer_rows=1,
    )
    assert reconstruct_from(result.path, prepare(derive(Document), execution_mapping())) == expected


def test_nillable_repeated_ancestor_does_not_invent_required_members(tmp_path):
    from mountainash_structuremap import parse
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from
    from tests.fixtures.execution_xml import Group, NillableGroups

    xml = (
        '<nillable-groups xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<group xsi:nil="true"/><group code="a"><entry><value>7</value></entry></group>'
        '<group xsi:nil="true"/></nillable-groups>'
    )
    expected = NillableGroups([None, Group("a", [Entry("7")]), None])
    prepared = prepare(derive(NillableGroups))
    assert parse(xml, prepared) == expected
    source = tmp_path / "nil-ancestor.xml"
    source.write_text(xml)
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepared,
        OutputTarget(tmp_path, "nil-ancestor"),
        record_path="groups/entries",
        buffer_rows=1,
    )
    assert reconstruct_from(result.path, prepare(derive(NillableGroups))) == expected


def test_complete_header_collection_waits_for_nested_ancestor_source_key(tmp_path):
    from mountainash_structuremap import MappingSpec, parse
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from
    from mountainash_structuremap.mapping import RowLayout
    from tests.fixtures.execution_xml import CollectionHeader, Group, NestedKeyEnvelope

    xml = (
        "<envelope><before><entry><value>header</value></entry></before>"
        '<group code="key"><entry><value>record</value></entry></group></envelope>'
    )
    expected = NestedKeyEnvelope(CollectionHeader([Entry("header")]), Group("key", [Entry("record")]))
    mapping = MappingSpec(
        overrides={"before/entries": {"table": "headers"}, "group/entries": {"table": "records"}},
        layouts={
            "root": RowLayout(key=("group_code",), generated=False),
            "headers": RowLayout(
                key=("_sm_key",),
                parent=("group_code",),
                generated=True,
                position="_sm_position",
            ),
            "records": RowLayout(
                key=("_sm_key",),
                parent=("group_code",),
                generated=True,
                position="_sm_position",
            ),
        },
    )
    prepared = prepare(derive(NestedKeyEnvelope), mapping)
    assert parse(xml, prepared) == expected
    source = tmp_path / "nested-header-key.xml"
    source.write_text(xml)
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepared,
        OutputTarget(tmp_path, "nested-header-key"),
        record_path="group/entries",
        buffer_rows=1,
    )
    tables = stored_tables(result)
    assert tables["root"].column("group_code").to_pylist() == ["key"]
    for table, value in (("headers", "header"), ("records", "record")):
        assert [(row["group_code"], row["_sm_position"], row["value"]) for row in tables[table].to_pylist()] == [
            ("key", 0, value)
        ]
    assert reconstruct_from(result.path, prepare(derive(NestedKeyEnvelope), mapping)) == expected


def test_non_nillable_document_root_rejects_xsi_nil(tmp_path):
    from mountainash_structuremap import StructureMapError, parse
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to

    xml = '<document xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" label="native"/>'
    prepared = prepare(derive(Document), execution_mapping())
    with pytest.raises(StructureMapError):
        parse(xml, prepared)
    source = tmp_path / "nil-root.xml"
    source.write_text(xml)
    with pytest.raises(StructureMapError) as caught:
        decompose_xml_to(
            FileSourceSpec(path=str(source)),
            prepared,
            OutputTarget(tmp_path, "nil-root"),
            record_path="entries",
            buffer_rows=1,
        )
    assert caught.value.code == "native_parse_failed"
    assert not (tmp_path / "nil-root").exists()


def test_internal_entity_text_reaches_native_cast_without_truncation(tmp_path):
    from mountainash_structuremap import parse
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from

    xml = (
        '<!DOCTYPE document [<!ENTITY digit "2">]>'
        '<document label="native"><entry><value>1&digit;</value></entry></document>'
    )
    expected = Document("native", [Entry("12")], "")
    prepared = prepare(derive(Document), execution_mapping())
    assert parse(xml, prepared) == expected
    source = tmp_path / "internal-entity.xml"
    source.write_text(xml)
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepared,
        OutputTarget(tmp_path, "internal-entity"),
        record_path="entries",
        buffer_rows=1,
    )
    table = stored_tables(result)["entries"]
    assert table.schema.field("value").type == pa.int8()
    assert table.column("value").to_pylist() == [12]
    assert reconstruct_from(result.path, prepare(derive(Document), execution_mapping())) == expected


def test_external_entity_cannot_supply_document_values_or_publish_output(tmp_path):
    from mountainash_structuremap import StructureMapError
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to

    secret = tmp_path / "secret.txt"
    # Resolving this would produce a valid i8 value, not an unrelated cast failure.
    secret.write_text("2")
    source = tmp_path / "external-entity.xml"
    source.write_text(
        f'<!DOCTYPE document [<!ENTITY secret SYSTEM "{secret.as_uri()}">]>'
        '<document label="native"><entry><value>1&secret;</value></entry></document>'
    )
    with pytest.raises(StructureMapError) as caught:
        decompose_xml_to(
            FileSourceSpec(path=str(source)),
            prepare(derive(Document), execution_mapping()),
            OutputTarget(tmp_path, "external-entity"),
            record_path="entries",
            buffer_rows=1,
        )
    assert caught.value.code == "native_parse_failed"
    assert not (tmp_path / "external-entity").exists()


def test_wrapped_native_collection_keeps_interleaved_unwrapped_occurrences(tmp_path):
    from mountainash_structuremap import parse
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from

    xml = (
        '<document label="native"><entry><value>1</value></entry>'
        "<entries><entry><value>2</value></entry></entries>"
        "<entry><value>3</value></entry></document>"
    )
    expected = WrappedDocument("native", [Entry("1"), Entry("2"), Entry("3")], "")
    prepared = prepare(derive(WrappedDocument), execution_mapping())
    assert parse(xml, prepared) == expected
    source = tmp_path / "interleaved-wrapper.xml"
    source.write_text(xml)
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepared,
        OutputTarget(tmp_path, "interleaved-wrapper"),
        record_path="entries",
        buffer_rows=1,
    )
    table = stored_tables(result)["entries"]
    assert table.column("value").to_pylist() == [1, 2, 3]
    assert table.column("_sm_position").to_pylist() == [0, 1, 2]
    assert reconstruct_from(result.path, prepare(derive(WrappedDocument), execution_mapping())) == expected


def test_wrapped_ancestor_keeps_interleaved_scope_identities(tmp_path):
    from mountainash_structuremap import MappingSpec, parse
    from mountainash_structuremap.execution import OutputTarget, decompose_xml_to, reconstruct_from
    from tests.fixtures.execution_xml import WrappedEntryGroup, WrappedGroups

    xml = (
        '<wrapped-groups><group code="a"><entry><value>1</value></entry>'
        "<entries><entry><value>2</value></entry></entries></group>"
        '<groups><group code="b"><entries><entry><value>3</value></entry></entries>'
        "<entry><value>4</value></entry></group></groups>"
        '<group code="c"/></wrapped-groups>'
    )
    expected = WrappedGroups(
        [
            WrappedEntryGroup("a", [Entry("1"), Entry("2")]),
            WrappedEntryGroup("b", [Entry("3"), Entry("4")]),
            WrappedEntryGroup("c"),
        ]
    )
    mapping = MappingSpec(overrides={"groups/entries": {"table": "records"}})
    prepared = prepare(derive(WrappedGroups), mapping)
    assert parse(xml, prepared) == expected
    source = tmp_path / "wrapped-ancestors.xml"
    source.write_text(xml)
    result = decompose_xml_to(
        FileSourceSpec(path=str(source)),
        prepared,
        OutputTarget(tmp_path, "wrapped-ancestors"),
        record_path="groups/entries",
        buffer_rows=1,
    )
    tables = stored_tables(result)
    groups = tables["groups"].to_pylist()
    assert [(row["code"], row["_sm_position"]) for row in groups] == [("a", 0), ("b", 1), ("c", 2)]
    records = tables["records"].to_pylist()
    assert [(row["value"], row["_sm_position"]) for row in records] == [
        ("1", 0),
        ("2", 1),
        ("3", 0),
        ("4", 1),
    ]
    assert [row["_sm_parent"] for row in records] == [
        groups[0]["_sm_key"],
        groups[0]["_sm_key"],
        groups[1]["_sm_key"],
        groups[1]["_sm_key"],
    ]
    assert reconstruct_from(result.path, prepare(derive(WrappedGroups), mapping)) == expected
