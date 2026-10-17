from dataclasses import dataclass
from datetime import date

import pytest

from mountainash_structuremap import MappingSpec, StructureMapError, UseOverride, derive, prepare, reconstruct
from mountainash_structuremap.mapping import SourceDiscriminator, TypeCast
from tests.fixtures.execution_models import Document, Entry, execution_mapping
from tests.test_mapping_casts import Item, Items, _mapping
from tests.test_mapping_components import order_case, prepared_order
from tests.test_mapping_relationships import (
    Batch,
    Contact,
    Line,
    Student,
    _ancestor_mapping,
    _ancestor_relations,
    _shared_prepared,
)
from tests.test_shared_alternatives import (
    Counted,
    Flagged,
    NullableTaggedDocument,
    NumericTag,
    OtherTag,
    SharedDocument,
    shared_mapping,
)


class RetainingSink:
    """Keep the delivered objects, not copies: later mutation is observable."""

    def __init__(self, prepared):
        self.tables = {name: [] for name in prepared.tables}

    def accept(self, table, rows):
        self.tables[table].extend(rows)


def session_for(prepared):
    from mountainash_structuremap.execution_session import DecompositionSession

    sink = RetainingSink(prepared)
    return DecompositionSession(prepared, sink), sink


def child(prepared, use, name):
    return dict(prepared.programs[use].members)[name]


def test_records_emit_before_late_header_and_keep_finalized_rows():
    prepared = prepare(derive(Document), execution_mapping())
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    session.member(root, "label", "batch")
    entries = child(prepared, prepared.root, "entries")
    session.occurrence(root, entries, Entry("-128"), 0)
    assert sink.tables == {
        "root": [],
        "entries": [
            {"_sm_key": "r1", "_sm_parent": "r0", "_sm_position": 0, "value": -128},
        ],
    }
    first = sink.tables["entries"][0]
    session.occurrence(root, entries, Entry("127"), 1)
    session.member(root, "footer", "done")
    session.close_scope(root)
    session.finish()
    assert first == {"_sm_key": "r1", "_sm_parent": "r0", "_sm_position": 0, "value": -128}
    assert sink.tables["entries"][1] == {
        "_sm_key": "r2",
        "_sm_parent": "r0",
        "_sm_position": 1,
        "value": 127,
    }
    assert reconstruct(sink.tables, prepared) == Document("batch", [Entry("-128"), Entry("127")], "done")


def test_source_key_collision_is_document_scoped_after_projection():
    prepared = prepare(derive(Items), _mapping(TypeCast(to="i8"), source_key=True))
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    items = child(prepared, prepared.root, "items")
    session.occurrence(root, items, Item("01", "first"), 0)
    assert sink.tables["items"] == [{"id": 1, "_sm_parent": "r0", "_sm_position": 0, "value": "first"}]
    with pytest.raises(StructureMapError) as caught:
        session.occurrence(root, items, Item("1", "duplicate"), 1)
    assert caught.value.code == "duplicate_key"
    assert len(sink.tables["items"]) == 1


def test_composite_ancestor_keys_are_ready_for_nested_complete_records():
    prepared = prepare(derive(Batch), _ancestor_mapping(), relations=_ancestor_relations())
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    session.member(root, "id", 7)
    students = child(prepared, prepared.root, "students")
    session.occurrence(root, students, Student(11, [Line(3, "first", 7, 11)]), 0)
    session.occurrence(root, students, Student(12, [Line(3, "second", 7, 12)]), 1)
    assert sink.tables["students"] == [
        {"batch_id": 7, "student_id": 11},
        {"batch_id": 7, "student_id": 12},
    ]
    assert sink.tables["lines"] == [
        {"batch_id": 7, "student_id": 11, "line_id": 3, "label": "first"},
        {"batch_id": 7, "student_id": 12, "line_id": 3, "label": "second"},
    ]
    session.close_scope(root)
    session.finish()
    assert sink.tables["batches"] == [{"batch_id": 7}]


def test_shared_table_roles_have_independent_collection_positions():
    prepared = _shared_prepared()
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    home = child(prepared, prepared.root, "home")
    work = child(prepared, prepared.root, "work")
    session.occurrence(root, home, Contact("Home A"), 0)
    session.occurrence(root, work, Contact("Work A"), 0)
    session.occurrence(root, home, Contact("Home B"), 1)
    session.close_scope(root)
    session.finish()
    assert sink.tables["contacts"] == [
        {"contact_key": "r1", "household_key": "r0", "ordinal": 0, "kind": "home", "name": "Home A"},
        {"contact_key": "r2", "household_key": "r0", "ordinal": 0, "kind": "work", "name": "Work A"},
        {"contact_key": "r3", "household_key": "r0", "ordinal": 1, "kind": "home", "name": "Home B"},
    ]


def test_shared_alternatives_finalize_branch_metadata_per_occurrence():
    prepared = prepare(derive(SharedDocument), shared_mapping())
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    items = child(prepared, prepared.root, "items")
    session.occurrence(root, items, Counted("one", 7), 0)
    session.occurrence(root, items, Flagged("two", False), 1)
    session.close_scope(root)
    session.finish()
    assert sink.tables["items"] == [
        {
            "_sm_key": "r1",
            "_sm_parent": "r0",
            "_sm_position": 0,
            "_sm_variant": "Counted",
            "common": "one",
            "count": 7,
            "flag": None,
        },
        {
            "_sm_key": "r2",
            "_sm_parent": "r0",
            "_sm_position": 1,
            "_sm_variant": "Flagged",
            "common": "two",
            "count": None,
            "flag": False,
        },
    ]


def test_split_alternatives_keep_positions_across_branch_and_null_tables():
    mapping = MappingSpec(
        overrides={
            "items": UseOverride(
                variant_layout="split",
                discriminator=SourceDiscriminator(field="kind"),
                alternatives={
                    "NumericTag": {"table": "numbers", "prefix": ""},
                    "OtherTag": {"table": "labels", "prefix": ""},
                    "$null": {"table": "nulls"},
                },
            )
        }
    )
    prepared = prepare(derive(NullableTaggedDocument), mapping)
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    items = child(prepared, prepared.root, "items")
    for position, value in enumerate((NumericTag(3, 7), None, OtherTag(2, "last"))):
        session.occurrence(root, items, value, position)
    session.close_scope(root)
    session.finish()
    assert sink.tables == {
        "root": [{"_sm_key": "r0"}],
        "numbers": [{"_sm_key": "r1", "_sm_parent": "r0", "_sm_position": 0, "kind": 3, "count": 7}],
        "nulls": [{"_sm_key": "r2", "_sm_parent": "r0", "_sm_position": 1, "_sm_null": True}],
        "labels": [{"_sm_key": "r3", "_sm_parent": "r0", "_sm_position": 2, "kind": 2, "label": "last"}],
    }


def test_multicolumn_components_share_finalization_with_source_key_carry():
    schemas, mapping, _, expected = order_case()
    prepared = prepared_order(schemas, mapping)
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    session.member(root, "order_id", 42)
    session.member(root, "home", expected.home)
    session.member(root, "work", expected.work)
    lines = child(prepared, prepared.root, "lines")
    session.occurrence(root, lines, expected.lines[0], 0)
    assert sink.tables["lines"] == [
        {
            "order_id": 42,
            "line_no": 0,
            "country": "AU",
            "amount": "0.01",
            "effective": date(2023, 9, 30),
            "source_date": "2023-09",
        }
    ]
    session.member(root, "issued", date(2023, 9, 30))
    session.close_scope(root)
    session.finish()
    assert sink.tables["orders"] == [
        {
            "order_id": 42,
            "home_city": "Sydney",
            "home_country": "AU",
            "work_city": "Wellington",
            "work_country": "NZ",
            "year": 2023,
            "month": 9,
            "day": 30,
        }
    ]


def test_omitted_envelope_defaults_and_empty_tables_are_preserved():
    from mountainash_structuremap.execution_session import DecompositionSession, RowCollector

    prepared = prepare(derive(Document), execution_mapping())
    collector = RowCollector(prepared.tables)
    session = DecompositionSession(prepared, collector)
    root = session.open_scope(prepared.root, None, None, None)
    session.member(root, "label", "empty")
    session.close_scope(root)
    session.finish()
    assert collector.tables == {"root": [{"_sm_key": "r0", "label": "empty", "footer": ""}], "entries": []}


@dataclass
class Envelope:
    document: Document
    note: str = "later"


def test_inline_ancestor_scope_keeps_shared_row_open_until_outer_close():
    prepared = prepare(derive(Envelope, binding="dataclass"))
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    document_use = child(prepared, prepared.root, "document")
    inner = session.open_scope(document_use, root, None, None)
    session.member(inner, "label", "nested")
    session.occurrence(inner, child(prepared, document_use, "entries"), Entry("3"), 0)
    session.close_scope(inner)
    assert sink.tables["root"] == []
    session.close_scope(root)
    session.finish()
    assert reconstruct(sink.tables, prepared) == Envelope(Document("nested", [Entry("3")]))


def test_missing_source_key_fails_before_descendant_handoff():
    prepared = prepare(derive(Batch), _ancestor_mapping(), relations=_ancestor_relations())
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    with pytest.raises(StructureMapError) as caught:
        session.occurrence(root, child(prepared, prepared.root, "students"), Student(11, []), 0)
    assert caught.value.code == "invalid_key"
    assert sink.tables == {"batches": [], "students": [], "lines": []}


def test_nested_collection_scopes_keep_positions_local_to_each_parent():
    prepared = prepare(derive(Batch), _ancestor_mapping(), relations=_ancestor_relations())
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    session.member(root, "id", 7)
    students = child(prepared, prepared.root, "students")
    student_use = prepared.tables["students"].roles[None].program
    lines = child(prepared, student_use, "lines")
    for position, student_id in enumerate((11, 12)):
        student = session.open_scope(students, root, position, None)
        session.member(student, "id", student_id)
        session.occurrence(student, lines, Line(3, "same", 7, student_id), 0)
        session.close_scope(student)
    assert sink.tables["students"] == [
        {"batch_id": 7, "student_id": 11},
        {"batch_id": 7, "student_id": 12},
    ]
    assert sink.tables["lines"] == [
        {"batch_id": 7, "student_id": 11, "line_id": 3, "label": "same"},
        {"batch_id": 7, "student_id": 12, "line_id": 3, "label": "same"},
    ]
    session.close_scope(root)
    session.finish()


@dataclass
class MappedDocuments:
    documents: dict[str, Document | None]


def test_map_records_and_nullable_occurrences_use_native_map_key_metadata():
    prepared = prepare(derive(MappedDocuments, binding="dataclass"))
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    documents = child(prepared, prepared.root, "documents")
    session.occurrence(root, documents, Document("first", [Entry("x")]), 0, "primary")
    session.occurrence(root, documents, None, 1, "absent")
    session.close_scope(root)
    session.finish()
    assert [row["_sm_map_key"] for row in sink.tables["documents"]] == ["primary", "absent"]
    assert [row["_sm_null"] for row in sink.tables["documents"]] == [False, True]
    assert reconstruct(sink.tables, prepared) == MappedDocuments(
        {
            "primary": Document("first", [Entry("x")]),
            "absent": None,
        }
    )


def test_pydantic_envelope_omissions_use_native_defaults():
    from pydantic import BaseModel, Field

    class NativeEnvelope(BaseModel):
        label: str
        values: list[int] = Field(default_factory=list)
        footer: str = "native default"

    prepared = prepare(derive(NativeEnvelope))
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    session.member(root, "label", "pydantic")
    session.close_scope(root)
    session.finish()
    assert sink.tables == {
        "root": [{"_sm_key": "r0", "label": "pydantic", "footer": "native default"}],
        "values": [],
    }


@dataclass
class DatedRecord:
    issued: date | None
    labels: list[str]


@dataclass
class DatedRecords:
    records: list[DatedRecord]


def test_complete_records_finalize_nested_multicolumn_external_codecs():
    from mountainash.typespec.spec import FieldSpec, TypeSpec
    from mountainash.typespec.universal_types import UniversalType as U

    from mountainash_structuremap.mapping import MappingUse
    from tests.fixtures.external_mapping import DateParts

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
                                    component="example.date-parts.v1",
                                    config={
                                        "columns": ["year", "month", "day"],
                                    },
                                ),
                            },
                        ),
                    },
                ),
            },
        )
    )
    relations = {
        "root": TypeSpec(fields=[FieldSpec(name="_sm_key", type=U.STRING, missing_values=[])], missing_values=[]),
        "records__labels": TypeSpec(
            fields=[
                FieldSpec(name=name, type=kind, missing_values=[])
                for name, kind in (
                    ("_sm_key", U.STRING),
                    ("_sm_parent", U.STRING),
                    ("_sm_position", U.INTEGER),
                    ("value", U.STRING),
                )
            ],
            missing_values=[],
        ),
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
        ),
    }
    prepared = prepare(derive(DatedRecords), mapping, relations=relations, components=(DateParts(),))
    session, sink = session_for(prepared)
    root = session.open_scope(prepared.root, None, None, None)
    records = child(prepared, prepared.root, "records")
    session.occurrence(root, records, DatedRecord(date(2023, 9, 30), ["one"]), 0)
    session.occurrence(root, records, DatedRecord(None, ["two"]), 1)
    assert sink.tables["records"] == [
        {"_sm_key": "r1", "_sm_parent": "r0", "_sm_position": 0, "year": 2023, "month": 9, "day": 30},
        {"_sm_key": "r3", "_sm_parent": "r0", "_sm_position": 1, "year": None, "month": None, "day": None},
    ]
    assert sink.tables["records__labels"] == [
        {"_sm_key": "r2", "_sm_parent": "r1", "_sm_position": 0, "value": "one"},
        {"_sm_key": "r4", "_sm_parent": "r3", "_sm_position": 0, "value": "two"},
    ]
    session.close_scope(root)
    session.finish()
