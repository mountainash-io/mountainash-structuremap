from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import ClassVar

import pytest

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct
from tests.fixtures.models import Address, Document, Person


def test_position_outside_prepared_i64_schema_has_input_location():
    prepared = prepare(derive(Document))
    tables = decompose(Document([Person("Ada", Address("Oslo"), ["x"])]), prepared)
    tables["people__tags"][0]["_sm_position"] = 2**63

    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)

    assert caught.value.code == "invalid_position"
    assert caught.value.table == "people__tags"
    assert caught.value.row == 0
    assert caught.value.column == "_sm_position"


@dataclass
class _Entry:
    label: str | None


@dataclass
class _Ledger:
    entries: list[_Entry]


def _ledger_tables(entries, *, roots=None):
    return {
        "root": roots if roots is not None else [dict(_sm_key="header")],
        "entries": [
            dict(
                _sm_key=key,
                _sm_parent="header",
                _sm_position=position,
                label=label,
            )
            for key, position, label in entries
        ],
    }


def test_reconstructs_independently_authored_opaque_keys_and_gapped_positions():
    prepared = prepare(derive(_Ledger))
    supplied = _ledger_tables(
        [("arbitrary-z", 20, "end"), ("header", 3, None), ("opaque", 8, "")],
    )
    original = deepcopy(supplied)

    rebuilt = reconstruct(supplied, prepared)

    assert rebuilt == _Ledger([_Entry(None), _Entry(""), _Entry("end")])
    assert supplied == original


@dataclass
class _NullableContainers:
    members: list[_Entry | None]
    groups: list[list[int | None]]


def test_reconstructs_all_null_member_and_repeated_containers():
    prepared = prepare(derive(_NullableContainers))
    supplied = {
        "root": [dict(_sm_key="outside")],
        "members": [
            dict(_sm_key="all-null", _sm_parent="outside", _sm_position=14, label=None, _sm_null=False),
            dict(_sm_key="null-member", _sm_parent="outside", _sm_position=2, label=None, _sm_null=True),
        ],
        "groups": [
            dict(_sm_key="empty-container", _sm_parent="outside", _sm_position=9),
            dict(_sm_key="container", _sm_parent="outside", _sm_position=4),
        ],
        "groups__item": [
            dict(_sm_key="null-item", _sm_parent="container", _sm_position=7, value=None),
        ],
    }
    original = deepcopy(supplied)

    rebuilt = reconstruct(supplied, prepared)

    assert rebuilt == _NullableContainers([None, _Entry(None)], [[None], []])
    assert supplied == original


@dataclass
class _MapRoot:
    values: dict[str, int]


def test_reconstructs_independently_authored_map_keys_in_position_order():
    prepared = prepare(derive(_MapRoot))
    supplied = {
        "root": [dict(_sm_key="catalog")],
        "values": [
            dict(_sm_key="last", _sm_parent="catalog", _sm_position=18, _sm_map_key="slash/key", value=9),
            dict(_sm_key="first", _sm_parent="catalog", _sm_position=1, _sm_map_key="", value=2),
        ],
    }

    original = deepcopy(supplied)
    rebuilt = reconstruct(supplied, prepared)
    assert list(rebuilt.values.items()) == [("", 2), ("slash/key", 9)]
    assert supplied == original


@pytest.mark.parametrize(
    ("bad_key", "expected"),
    [(None, "invalid_map_key"), ("same", "duplicate_map_key")],
)
def test_rejects_invalid_or_duplicate_independent_map_keys(bad_key, expected):
    prepared = prepare(derive(_MapRoot))
    first = dict(
        _sm_key="first",
        _sm_parent="catalog",
        _sm_position=0,
        _sm_map_key="same",
        value=1,
    )
    second = dict(
        _sm_key="second",
        _sm_parent="catalog",
        _sm_position=1,
        _sm_map_key=bad_key,
        value=2,
    )
    supplied = {"root": [dict(_sm_key="catalog")], "values": [first, second]}
    original = deepcopy(supplied)

    with pytest.raises(StructureMapError) as caught:
        reconstruct(supplied, prepared)

    assert caught.value.code == expected
    assert supplied == original


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda supplied: supplied["entries"][0].pop("label"), "row_shape_mismatch"),
        (lambda supplied: supplied["entries"][0].pop("_sm_position"), "row_shape_mismatch"),
        (lambda supplied: supplied["entries"][0].update(extra="unexpected"), "row_shape_mismatch"),
        (lambda supplied: supplied["entries"][0].update(_sm_position=-1), "invalid_position"),
        (lambda supplied: supplied.pop("entries"), "table_set_mismatch"),
        (lambda supplied: supplied.update(extra=[]), "table_set_mismatch"),
        (lambda supplied: supplied.update({None: [], 7: []}), "table_set_mismatch"),
        (lambda supplied: supplied["entries"][0].update(label=[]), "cell_type_mismatch"),
        (lambda supplied: supplied["entries"][0].update(_sm_position=None), "invalid_position"),
        (lambda supplied: supplied["entries"][0].update(_sm_position=True), "invalid_position"),
        (
            lambda supplied: supplied["entries"].append(
                dict(_sm_key="other", _sm_parent="header", _sm_position=0, label=None)
            ),
            "duplicate_position",
        ),
        (lambda supplied: supplied["entries"][0].update(_sm_parent="unknown"), "orphan_row"),
        (
            lambda supplied: supplied["entries"].append(deepcopy(supplied["entries"][0])),
            "duplicate_key",
        ),
    ],
)
def test_rejects_malformed_rows_without_mutating_input(mutate, code):
    prepared = prepare(derive(_Ledger))
    supplied = _ledger_tables([("member", 0, None)])
    mutate(supplied)
    original = deepcopy(supplied)

    with pytest.raises(StructureMapError) as caught:
        reconstruct(supplied, prepared)

    assert caught.value.code == code
    assert supplied == original


@pytest.mark.parametrize("root_count", [0, 2])
def test_rejects_zero_or_multiple_root_rows(root_count):
    prepared = prepare(derive(_Ledger))
    roots = [dict(_sm_key=f"root-{index}") for index in range(root_count)]
    supplied = _ledger_tables([], roots=roots)

    with pytest.raises(StructureMapError) as caught:
        reconstruct(supplied, prepared)

    assert caught.value.code == "root_cardinality"


@dataclass
class _Branch:
    label: str
    left: _Branch | None = None
    right: _Branch | None = None


def test_rejects_parent_key_resolved_in_wrong_recursive_role():
    prepared = prepare(derive(_Branch))
    supplied = {
        "root": [dict(_sm_key="root", label="root")],
        "left": [dict(_sm_key="wrong-parent", _sm_parent="right-only", _sm_parent_table="root", label="child")],
        "right": [dict(_sm_key="right-only", _sm_parent="root", _sm_parent_table="root", label="sibling")],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(supplied, prepared)

    assert caught.value.code == "orphan_row"
    assert caught.value.table == "left"


def test_rejects_multiple_rows_for_singular_recursive_link():
    prepared = prepare(derive(_Branch))
    supplied = {
        "root": [dict(_sm_key="root", label="root")],
        "left": [
            dict(_sm_key="one", _sm_parent="root", _sm_parent_table="root", label="one"),
            dict(_sm_key="two", _sm_parent="root", _sm_parent_table="root", label="two"),
        ],
        "right": [],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(supplied, prepared)

    assert caught.value.code == "singular_cardinality"


@dataclass
class _TrackedBranch:
    label: str
    left: _TrackedBranch | None = None

    constructions: ClassVar[list[str]] = []

    def __post_init__(self):
        type(self).constructions.append(self.label)


@dataclass
class _TrackedLedger:
    entries: list[_TrackedEntry]


@dataclass
class _TrackedEntry:
    value: int

    constructions = []

    def __post_init__(self):
        type(self).constructions.append(self.value)


def test_rejects_null_cell_for_required_scalar():
    prepared = prepare(derive(_TrackedLedger))
    supplied = {
        "root": [dict(_sm_key="root")],
        "entries": [dict(_sm_key="entry", _sm_parent="root", _sm_position=0, value=None)],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(supplied, prepared)

    assert caught.value.code == "null_not_allowed"


def test_validates_later_sibling_before_constructing_any_native_model():
    prepared = prepare(derive(_TrackedLedger))
    supplied = {
        "root": [dict(_sm_key="root")],
        "entries": [
            dict(_sm_key="valid", _sm_parent="root", _sm_position=0, value=1),
            dict(_sm_key="invalid", _sm_parent="root", _sm_position=1, value="wrong"),
        ],
    }
    original = deepcopy(supplied)
    _TrackedEntry.constructions.clear()
    try:
        with pytest.raises(StructureMapError) as caught:
            reconstruct(supplied, prepared)
        assert caught.value.code == "cell_type_mismatch"
        assert _TrackedEntry.constructions == []
        assert supplied == original
    finally:
        _TrackedEntry.constructions.clear()


def test_validates_detached_recursive_cycle_before_constructing_any_native_model():
    prepared = prepare(derive(_TrackedBranch))
    supplied = {
        "root": [dict(_sm_key="root", label="root")],
        "left": [
            dict(_sm_key="detached-a", _sm_parent="detached-b", _sm_parent_table="left", label="a"),
            dict(_sm_key="detached-b", _sm_parent="detached-a", _sm_parent_table="left", label="b"),
        ],
    }
    original = deepcopy(supplied)
    _TrackedBranch.constructions.clear()
    try:
        with pytest.raises(StructureMapError) as caught:
            reconstruct(supplied, prepared)
        assert caught.value.code == "unattached_row"
        assert _TrackedBranch.constructions == []
        assert supplied == original
    finally:
        _TrackedBranch.constructions.clear()


@dataclass
class _Details:
    required: str
    children: list[str]


@dataclass
class _Envelope:
    detail: _Details | None


@pytest.mark.parametrize("present", [False, True])
def test_required_scalar_nullability_follows_optional_record_activity(present):
    supplied = {
        "root": [{"_sm_key": "header", "detail_required": "set" if present else None}],
        "detail__children": [{"_sm_key": "child", "_sm_parent": "header", "_sm_position": 4, "value": "x"}]
        if present
        else [],
    }
    original = deepcopy(supplied)
    rebuilt = reconstruct(supplied, prepare(derive(_Envelope)))
    assert rebuilt == _Envelope(_Details("set", ["x"]) if present else None)
    assert supplied == original


def test_descendants_activate_required_scalar_validation_in_optional_record():
    supplied = {
        "root": [{"_sm_key": "header", "detail_required": None}],
        "detail__children": [{"_sm_key": "child", "_sm_parent": "header", "_sm_position": 4, "value": "x"}],
    }
    original = deepcopy(supplied)
    with pytest.raises(StructureMapError) as caught:
        reconstruct(supplied, prepare(derive(_Envelope)))
    assert caught.value.code == "null_not_allowed"
    assert caught.value.path == "detail/required"
    assert supplied == original
