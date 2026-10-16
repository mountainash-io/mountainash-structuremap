from dataclasses import dataclass

import pytest
from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType as U

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct
from mountainash_structuremap.mapping import MappingSpec, MappingUse, RowLayout


@dataclass(frozen=True)
class Line:
    parent_id: int
    line_no: int
    label: str


@dataclass(frozen=True)
class Batch:
    id: int
    lines: list[Line]


def source_layouts():
    relations = {
        "batches": TypeSpec(fields=[FieldSpec(name="id", type=U.INTEGER, missing_values=[])], missing_values=[]),
        "lines": TypeSpec(fields=[
            FieldSpec(name="parent_id", type=U.INTEGER, missing_values=[]),
            FieldSpec(name="line_no", type=U.INTEGER, missing_values=[]),
            FieldSpec(name="label", type=U.STRING, missing_values=[]),
        ], missing_values=[]),
    }
    mapping = MappingSpec(
        use=MappingUse(component="core.record", config={"table": "batches"}),
        layouts={
            "batches": RowLayout(key=("id",), generated=False),
            "lines": RowLayout(key=("parent_id", "line_no"), generated=False,
                               parent=("parent_id",), position="line_no"),
        },
    )
    return prepare(derive(Batch), mapping, relations=relations)


def test_source_composite_layout_decomposes_and_reconstructs_with_declared_columns():
    prepared = source_layouts()
    value = Batch(23, [Line(23, 0, "first"), Line(23, 1, "second")])

    rows = decompose(value, prepared)

    assert rows["batches"] == [{"id": 23}]
    assert rows["lines"] == [
        {"parent_id": 23, "line_no": 0, "label": "first"},
        {"parent_id": 23, "line_no": 1, "label": "second"},
    ]
    assert reconstruct(rows, prepared) == value



def test_position_metadata_does_not_overwrite_a_conflicting_payload_cell():
    prepared = source_layouts()
    value = Batch(23, [Line(23, 7, "wrong ordinal")])

    with pytest.raises(StructureMapError):
        decompose(value, prepared)


def test_source_parent_payload_overlap_accepts_same_value_and_rejects_contradiction():
    prepared = source_layouts()
    value = Batch(23, [Line(23, 0, "same")])
    assert decompose(value, prepared)["lines"][0]["parent_id"] == 23

    contradictory = Batch(23, [Line(24, 0, "different")])
    with pytest.raises(StructureMapError) as caught:
        decompose(contradictory, prepared)
    assert caught.value.code == "contradictory_parent_key"


def test_source_parent_keys_keep_exact_cell_types():
    prepared = source_layouts()
    rows = {
        "batches": [{"id": 1}],
        "lines": [{"parent_id": True, "line_no": 0, "label": "not linked"}],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code in {"orphan_row", "cell_type_mismatch"}


@pytest.mark.parametrize("bad_key, code", [(None, "invalid_key"), ("duplicate", "duplicate_key")])
def test_source_keys_reject_null_and_duplicate_cells(bad_key, code):
    prepared = source_layouts()
    rows = {"batches": [{"id": 23}], "lines": []}
    if bad_key is None:
        rows["batches"][0]["id"] = None
    else:
        rows["lines"] = [
            {"parent_id": 23, "line_no": 0, "label": "first"},
            {"parent_id": 23, "line_no": 0, "label": "second"},
        ]

    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepared)
    assert caught.value.code == code
