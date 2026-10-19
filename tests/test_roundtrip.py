from dataclasses import dataclass, field

from mountainash.typespec.universal_types import UniversalType

from tests.fixtures.models import Address, Document, Empty, Person, RequiredEmpty


def test_inline_values_and_record_lists_survive_independent_preparation():
    from mountainash_structuremap import decompose, derive, prepare, reconstruct

    value = Document(
        [
            Person("Ada", Address("Oslo"), age=42, active=True, score=1.5),
            Person("Lin", Address("Kyoto"), age=17, active=False, score=-2.25),
        ]
    )
    prepared = prepare(derive(Document))
    # Schema preparation must also describe tables which will have no rows.
    fields = {name: definition.type for name, definition in prepared.tables["people"].typespec.fields.items()}
    assert fields["name"] == UniversalType.STRING
    assert fields["home_city"] == UniversalType.STRING
    assert fields["age"] == UniversalType.INTEGER
    assert fields["active"] == UniversalType.BOOLEAN
    assert fields["score"] == UniversalType.NUMBER
    tables = decompose(value, prepared)
    assert tables["people__tags"] == []
    assert [row["home_city"] for row in tables["people"]] == ["Oslo", "Kyoto"]
    delivered = {name: list(reversed(rows)) for name, rows in reversed(list(tables.items()))}
    rebuilt = reconstruct(delivered, prepare(derive(Document)))
    assert rebuilt == Document(
        [
            Person("Ada", Address("Oslo"), age=42, active=True, score=1.5),
            Person("Lin", Address("Kyoto"), age=17, active=False, score=-2.25),
        ]
    )


def test_scalar_members_retain_duplicates_null_and_empty_text():
    from mountainash_structuremap import decompose, derive, prepare, reconstruct

    value = Document([Person("same", Address("Oslo"), [None, "", "same", "same"])])
    tables = decompose(value, prepare(derive(Document)))
    assert [row["value"] for row in tables["people__tags"]] == [None, "", "same", "same"]
    tables["people__tags"].reverse()
    rebuilt = reconstruct(tables, prepare(derive(Document)))
    assert rebuilt.people[0].tags == [None, "", "same", "same"]


def test_required_empty_inline_record_is_constructed():
    from mountainash_structuremap import decompose, derive, prepare, reconstruct

    value = RequiredEmpty(Empty())
    tables = decompose(value, prepare(derive(RequiredEmpty)))
    rebuilt = reconstruct(tables, prepare(derive(RequiredEmpty)))
    assert rebuilt == RequiredEmpty(Empty())


def test_captured_factory_value_is_not_generated_again():
    from mountainash_structuremap import decompose, derive, prepare, reconstruct

    calls = 0

    def next_token():
        nonlocal calls
        calls += 1
        return f"token-{calls}"

    @dataclass
    class Snapshot:
        token: str = field(default_factory=next_token)

    prepared = prepare(derive(Snapshot))
    assert calls == 0
    value = Snapshot()
    rebuilt = reconstruct(decompose(value, prepared), prepare(derive(Snapshot)))
    assert rebuilt.token == "token-1"
    assert calls == 1


def test_imported_record_annotations_keep_the_actual_native_class():
    from mountainash_structuremap import decompose, derive, prepare, reconstruct
    from tests.fixtures.foreign_model import Shipment

    source = Shipment([Address("Oslo"), Address("Kyoto")])
    tables = decompose(source, prepare(derive(Shipment)))
    rebuilt = reconstruct(tables, prepare(derive(Shipment)))
    assert rebuilt == Shipment([Address("Oslo"), Address("Kyoto")])


def test_optional_inline_record_with_descendants_is_not_discarded():
    from mountainash_structuremap import decompose, derive, prepare, reconstruct

    @dataclass
    class History:
        events: list[int]

    @dataclass
    class Entry:
        history: History | None

    source = Entry(History([2, 2, 7]))
    tables = decompose(source, prepare(derive(Entry)))
    rebuilt = reconstruct(tables, prepare(derive(Entry)))
    assert rebuilt == Entry(History([2, 2, 7]))


def test_postponed_annotations_resolve_nested_records_and_class_local_aliases():
    from mountainash_structuremap import decompose, derive, prepare, reconstruct
    from tests.fixtures.postponed_models import NestedDocument

    prepared = prepare(derive(NestedDocument))
    tables = {"root": [{"_sm_key": "external", "home_city": "Oslo", "count": 3}]}
    rebuilt = reconstruct(tables, prepared)

    assert rebuilt == NestedDocument(NestedDocument.Address("Oslo"), 3)
    row = decompose(rebuilt, prepared)["root"][0]
    assert (row["home_city"], row["count"]) == ("Oslo", 3)
