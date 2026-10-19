from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest
from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType as U

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct
from mountainash_structuremap.mapping import (
    AlternativeLayout,
    MappingSpec,
    RowLayout,
    SharedCollection,
    SourceKey,
    UseOverride,
)
from tests.fixtures.variants import AllNullCat, AllNullDog, NullablePets


@dataclass(frozen=True)
class Line:
    id: int
    label: str
    batch_id: int
    student_id: int


@dataclass(frozen=True)
class Student:
    id: int
    lines: list[Line]


@dataclass(frozen=True)
class Batch:
    id: int
    students: list[Student]


def _field(kind: U) -> FieldSpec:
    return FieldSpec(type=kind, missing_values=[])


def _ancestor_mapping() -> MappingSpec:
    return MappingSpec(
        overrides={
            "": UseOverride(table="batches"),
            "students": UseOverride(table="students", ordered=False),
            "id": UseOverride(column="batch_id"),
            "students/id": UseOverride(column="student_id"),
            "students/lines/id": UseOverride(column="line_id"),
            "students/lines": UseOverride(
                table="lines",
                ordered=False,
                key=SourceKey(columns=("batch_id", "student_id", "line_id")),
            ),
        },
        layouts={
            "batches": RowLayout(key=("batch_id",), generated=False),
            "students": RowLayout(
                key=("batch_id", "student_id"),
                generated=False,
                parent=("batch_id",),
            ),
            "lines": RowLayout(
                key=("batch_id", "student_id", "line_id"),
                generated=False,
                parent=("batch_id", "student_id"),
            ),
        },
    )


def _ancestor_relations() -> dict[str, TypeSpec]:
    return {
        "batches": TypeSpec(fields={"batch_id": _field(U.INTEGER)}, missing_values=[]),
        "students": TypeSpec(
            fields={"batch_id": _field(U.INTEGER), "student_id": _field(U.INTEGER)},
            missing_values=[],
        ),
        "lines": TypeSpec(
            fields={
                "batch_id": _field(U.INTEGER),
                "student_id": _field(U.INTEGER),
                "line_id": _field(U.INTEGER),
                "label": _field(U.STRING),
            },
            missing_values=[],
        ),
    }


def test_reconstructs_ancestor_source_keys_from_independent_rows():
    prepared = prepare(derive(Batch), _ancestor_mapping(), relations=_ancestor_relations())
    rows = {
        "batches": [{"batch_id": 7}],
        "students": [
            {"batch_id": 7, "student_id": 11},
            {"batch_id": 7, "student_id": 12},
        ],
        "lines": [
            {"batch_id": 7, "student_id": 12, "line_id": 4, "label": "second"},
            {"batch_id": 7, "student_id": 11, "line_id": 3, "label": "first"},
        ],
    }

    actual = reconstruct(rows, prepared)

    assert actual == Batch(
        7,
        [
            Student(11, [Line(3, "first", 7, 11)]),
            Student(12, [Line(4, "second", 7, 12)]),
        ],
    )


def test_rejects_ancestor_key_type_mismatch_in_independent_rows():
    prepared = prepare(derive(Batch), _ancestor_mapping(), relations=_ancestor_relations())
    rows = {
        "batches": [{"batch_id": 7}],
        "students": [{"batch_id": True, "student_id": 11}],
        "lines": [],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)

    assert caught.value.code in {"orphan_row", "cell_type_mismatch"}


def test_conflicting_payload_values_cannot_overwrite_carried_ancestor_keys():
    prepared = prepare(derive(Batch), _ancestor_mapping(), relations=_ancestor_relations())
    value = Batch(7, [Student(11, [Line(3, "wrong batch", 8, 11)])])

    with pytest.raises(StructureMapError) as caught:
        decompose(value, prepared)

    assert caught.value.code == "contradictory_parent_key"


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        (
            {"batches": [{"batch_id": None}], "students": [], "lines": []},
            "invalid_key",
        ),
        (
            {"batches": [{"batch_id": 7}], "students": [], "lines": [
                {"batch_id": 7, "student_id": 11, "line_id": 3, "label": "one"},
                {"batch_id": 7, "student_id": 11, "line_id": 3, "label": "duplicate"},
            ]},
            "duplicate_key",
        ),
        (
            {"batches": [{"batch_id": 7}], "students": [], "lines": [
                {"batch_id": 7, "student_id": 99, "line_id": 3, "label": "orphan"},
            ]},
            "orphan_row",
        ),
    ],
)
def test_rejects_invalid_ancestor_keys_and_orphan_links(rows, code):
    prepared = prepare(derive(Batch), _ancestor_mapping(), relations=_ancestor_relations())

    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)

    assert caught.value.code == code


@dataclass(frozen=True)
class Contact:
    name: str


@dataclass(frozen=True)
class Household:
    home: list[Contact]
    work: list[Contact]


def _shared_mapping(
    *, positions: str = "strict", ordered: bool = True, has_position: bool = True
) -> MappingSpec:
    return MappingSpec(
        overrides={
            "": UseOverride(table="households"),
            "home": UseOverride(
                collection_table=SharedCollection(
                    table="contacts", role_column="kind", role="home"
                ),
                positions=positions,
                ordered=ordered,
            ),
            "work": UseOverride(
                collection_table=SharedCollection(
                    table="contacts", role_column="kind", role="work"
                ),
                positions=positions,
                ordered=ordered,
            ),
        },
        layouts={
            "households": RowLayout(key=("household_key",), generated=True),
            "contacts": RowLayout(
                key=("contact_key",),
                generated=True,
                parent=("household_key",),
                position="ordinal" if has_position else None,
                role_column="kind",
            ),
        },
    )


def _shared_relations(*, has_position: bool = True) -> dict[str, TypeSpec]:
    contact_fields = {
        "contact_key": _field(U.STRING),
        "household_key": _field(U.STRING),
    }
    if has_position:
        contact_fields["ordinal"] = _field(U.INTEGER)
    contact_fields.update({"kind": _field(U.STRING), "name": _field(U.STRING)})
    return {
        "households": TypeSpec(fields={"household_key": _field(U.STRING)}, missing_values=[]),
        "contacts": TypeSpec(fields=contact_fields, missing_values=[]),
    }


def _shared_prepared(
    *, positions: str = "strict", ordered: bool = True, has_position: bool = True
):
    return prepare(
        derive(Household),
        _shared_mapping(
            positions=positions, ordered=ordered, has_position=has_position
        ),
        relations=_shared_relations(has_position=has_position),
    )

def test_shared_table_routes_each_role_and_position_domain_independently():
    prepared = _shared_prepared()
    rows = {
        "households": [{"household_key": "h1"}],
        "contacts": [
            {"contact_key": "c1", "household_key": "h1", "ordinal": 1, "kind": "home", "name": "Home B"},
            {"contact_key": "c2", "household_key": "h1", "ordinal": 1, "kind": "work", "name": "Work B"},
            {"contact_key": "c3", "household_key": "h1", "ordinal": 0, "kind": "home", "name": "Home A"},
            {"contact_key": "c4", "household_key": "h1", "ordinal": 0, "kind": "work", "name": "Work A"},
        ],
    }

    actual = reconstruct(rows, prepared)

    assert actual == Household(
        [Contact("Home A"), Contact("Home B")],
        [Contact("Work A"), Contact("Work B")],
    )


@pytest.mark.parametrize(
    ("positions", "expected"),
    [
        ([9, 3, 12], ("b", "a", "c")),
        ([None, 0, 0], ("b", "c", "a")),
    ],
)
def test_lenient_positions_sort_unique_and_keep_ties_then_nulls_in_delivery_order(
    positions, expected
):
    prepared = _shared_prepared(positions="lenient")
    rows = {
        "households": [{"household_key": "h1"}],
        "contacts": [
            {"contact_key": f"c{i}", "household_key": "h1", "ordinal": position,
             "kind": "home", "name": name}
            for i, (position, name) in enumerate(zip(positions, ("a", "b", "c")))
        ],
    }

    actual = reconstruct(rows, prepared)

    assert tuple(contact.name for contact in actual.home) == expected


@pytest.mark.parametrize("positions", ([0, 0], [None, 1]))
def test_strict_order_rejects_duplicate_and_null_positions(positions):
    prepared = _shared_prepared()
    rows = {
        "households": [{"household_key": "h1"}],
        "contacts": [
            {
                "contact_key": f"c{i}",
                "household_key": "h1",
                "ordinal": position,
                "kind": "home",
                "name": f"contact {i}",
            }
            for i, position in enumerate(positions)
        ],
    }

    with pytest.raises(StructureMapError):
        reconstruct(rows, prepared)


def test_strict_order_rejects_a_missing_position_column():
    with pytest.raises(StructureMapError):
        prepared = _shared_prepared(has_position=False)
        reconstruct(
            {
                "households": [{"household_key": "h1"}],
                "contacts": [
                    {
                        "contact_key": "c1",
                        "household_key": "h1",
                        "kind": "home",
                        "name": "contact",
                    }
                ],
            },
            prepared,
        )


def test_lenient_missing_position_column_uses_delivery_order():
    prepared = _shared_prepared(positions="lenient", has_position=False)
    rows = {
        "households": [{"household_key": "h1"}],
        "contacts": [
            {"contact_key": "c2", "household_key": "h1", "kind": "home", "name": "second"},
            {"contact_key": "c1", "household_key": "h1", "kind": "home", "name": "first"},
        ],
    }

    actual = reconstruct(rows, prepared)

    assert tuple(contact.name for contact in actual.home) == ("second", "first")



def test_unordered_collection_needs_no_position_column():
    prepared = _shared_prepared(ordered=False, has_position=False)
    rows = {
        "households": [{"household_key": "h1"}],
        "contacts": [
            {"contact_key": "c2", "household_key": "h1", "kind": "home", "name": "second"},
            {"contact_key": "c1", "household_key": "h1", "kind": "home", "name": "first"},
        ],
    }

    actual = reconstruct(rows, prepared)

    assert {contact.name for contact in actual.home} == {"first", "second"}


def test_lenient_order_does_not_relax_required_payload_columns():
    prepared = _shared_prepared(positions="lenient")
    rows = {
        "households": [{"household_key": "h1"}],
        "contacts": [
            {"contact_key": "c1", "household_key": "h1", "ordinal": None, "kind": "home"},
        ],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)

    assert caught.value.code == "row_shape_mismatch"


def test_lenient_split_nulls_follow_alternative_declaration_then_delivery_order():
    mapping = MappingSpec(
        overrides={
            "pets": UseOverride(
                variant_layout="split",
                positions="lenient",
                discriminator={
                    "strategy": "recorded",
                    "column": "kind",
                    "values": {"AllNullDog": "DOG", "AllNullCat": "CAT"},
                },
                alternatives={
                    "AllNullDog": AlternativeLayout(table="dogs", prefix=""),
                    "AllNullCat": AlternativeLayout(table="cats", prefix=""),
                    "$null": AlternativeLayout(table="null_values"),
                },
            )
        }
    )
    prepared = prepare(derive(NullablePets), mapping)
    rows = {
        "root": [{"_sm_key": "root"}],
        "dogs": [
            {
                "_sm_key": "d2",
                "_sm_parent": "root",
                "_sm_position": None,
                "kind": "DOG",
                "label": "second dog",
            },
            {
                "_sm_key": "d1",
                "_sm_parent": "root",
                "_sm_position": None,
                "kind": "DOG",
                "label": "first dog",
            },
        ],
        "cats": [
            {
                "_sm_key": "c1",
                "_sm_parent": "root",
                "_sm_position": None,
                "kind": "CAT",
                "label": "cat",
            }
        ],
        "null_values": [
            {
                "_sm_key": "n1",
                "_sm_parent": "root",
                "_sm_position": None,
                "kind": None,
                "_sm_null": True,
            }
        ],
    }

    actual = reconstruct(rows, prepared)

    assert actual.pets == [
        AllNullDog("second dog"),
        AllNullDog("first dog"),
        AllNullCat("cat"),
        None,
    ]


def test_shared_table_rejects_an_unknown_relationship_role():
    prepared = _shared_prepared()
    rows = {
        "households": [{"household_key": "h1"}],
        "contacts": [
            {"contact_key": "c1", "household_key": "h1", "ordinal": 0, "kind": "other", "name": "Unknown"},
        ],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)

    assert caught.value.code in {"invalid_role", "orphan_row"}


def test_ancestor_carry_is_validated_even_when_not_in_immediate_parent_key():
    mapping = _ancestor_mapping()
    mapping.layouts["students"] = RowLayout(key=("student_id",), parent=("batch_id",))
    mapping.layouts["lines"] = RowLayout(
        key=("batch_id", "student_id", "line_id"), parent=("student_id",),
    )
    prepared = prepare(derive(Batch), mapping, relations=_ancestor_relations())
    rows = {
        "batches": [{"batch_id": 10}],
        "students": [{"batch_id": 10, "student_id": 7}],
        "lines": [{"batch_id": 99, "student_id": 7, "line_id": 3, "label": "bad carry"}],
    }
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code == "contradictory_parent_key"


def test_lenient_delivery_without_position_cells_does_not_relax_payload_shape():
    prepared = _shared_prepared(positions="lenient")
    rows = {
        "households": [{"household_key": "root"}],
        "contacts": [
            {"contact_key": "b", "household_key": "root", "kind": "home", "name": "second"},
            {"contact_key": "a", "household_key": "root", "kind": "home", "name": "first"},
        ],
    }
    assert reconstruct(rows, prepared) == Household([Contact("second"), Contact("first")], [])
    del rows["contacts"][0]["name"]
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code == "row_shape_mismatch"


@pytest.mark.parametrize("selector", ["wrong", None, 1])
def test_declared_parent_table_selector_must_agree_with_single_role_parent(selector):
    mapping = _shared_mapping()
    mapping.layouts["contacts"] = mapping.layouts["contacts"].model_copy(
        update={"parent_table": "owner_table"},
    )
    schemas = _shared_relations()
    schemas["contacts"].fields["owner_table"] = _field(U.STRING)
    prepared = prepare(derive(Household), mapping, relations=schemas)
    rows = {
        "households": [{"household_key": "h"}],
        "contacts": [{
            "contact_key": "c", "household_key": "h", "ordinal": 0,
            "kind": "home", "name": "Ada", "owner_table": selector,
        }],
    }
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code == "invalid_parent_table"


def test_source_key_ambiguity_requires_explicit_ancestor_column_names():
    mapping = MappingSpec(overrides={
        "students/lines/id": UseOverride(column="line_id"),
        "students/lines": UseOverride(key=SourceKey(columns=("id", "line_id"))),
    })
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Batch), mapping)
    assert caught.value.code == "ambiguous_source_key"
    assert caught.value.path == "students/lines"


@dataclass
class NullableContact:
    name: str | None
    constructed: ClassVar[list[str | None]] = []

    def __post_init__(self):
        self.constructed.append(self.name)


@dataclass
class NullableHousehold:
    home: list[NullableContact]
    work: list[NullableContact]


def test_lenient_position_omission_does_not_allow_missing_nullable_payload(monkeypatch):
    monkeypatch.setattr(NullableContact, "constructed", [])
    prepared = prepare(
        derive(NullableHousehold), _shared_mapping(positions="lenient"),
        relations=_shared_relations(),
    )
    rows = {
        "households": [{"household_key": "root"}],
        "contacts": [
            {"contact_key": "a", "household_key": "root", "kind": "home", "name": None},
            {"contact_key": "b", "household_key": "root", "kind": "home"},
        ],
    }
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code == "row_shape_mismatch"
    assert NullableContact.constructed == []
