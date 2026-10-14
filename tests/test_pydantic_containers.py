from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mountainash_structuremap import decompose, derive, prepare, reconstruct


class Leaf(BaseModel):
    label: str | None = None
    quantity: int | None = None


class Containers(BaseModel):
    records: list[Leaf]
    by_name: dict[str, Leaf]
    pair: tuple[Leaf, Leaf]
    grid: list[list[int | None] | None]


def test_nested_native_containers_preserve_values_types_and_order():
    original = Containers(records=[Leaf(label="", quantity=0), Leaf(label="x", quantity=2), Leaf(label="x", quantity=2)],
                          by_name={"z": Leaf(quantity=1), "": Leaf(), "a/b": Leaf(label="last")},
                          pair=(Leaf(label="left"), Leaf(label="right")),
                          grid=[[], None, [None], [1, None, 1]])
    tables = decompose(original, prepare(derive(Containers)))
    delivered = {name: list(reversed(rows)) for name, rows in reversed(list(tables.items()))}
    rebuilt = reconstruct(delivered, prepare(derive(Containers)))
    assert rebuilt == original
    assert list(rebuilt.by_name) == ["z", "", "a/b"]
    assert type(rebuilt.pair) is tuple
    assert [type(item) for item in rebuilt.records] == [Leaf, Leaf, Leaf]
    assert type(rebuilt.by_name[""]) is Leaf
    assert [type(item) for item in rebuilt.pair] == [Leaf, Leaf]


class Empty(BaseModel):
    pass


class Presence(BaseModel):
    optional_record: Leaf | None
    optional_list: list[int] | None
    records: list[Leaf | None]
    required: Empty
    empty_tuple: tuple[()]


def test_normalize_collapses_optional_inline_values_but_preserves_members():
    original = Presence(optional_record=Leaf(), optional_list=[], records=[Leaf(), None, Leaf(label="")],
                        required=Empty(), empty_tuple=())
    tables = decompose(original, prepare(derive(Presence)))
    restored = reconstruct(tables, prepare(derive(Presence)))
    assert restored.optional_record is None
    assert restored.optional_list is None
    assert restored.records == [Leaf(), None, Leaf(label="")]
    assert type(restored.records[0]) is Leaf
    assert type(restored.required) is Empty
    assert restored.empty_tuple == ()


class Entries(BaseModel):
    entries: list[Leaf]


def test_independent_nested_rows_select_parent_membership_and_position():
    supplied = {
        "root": [{"_sm_key": "external"}],
        "entries": [
            {"_sm_key": "late", "_sm_parent": "external", "_sm_position": 9, "label": "last", "quantity": 0},
            {"_sm_key": "early", "_sm_parent": "external", "_sm_position": 2, "label": "first", "quantity": None},
        ],
    }
    restored = reconstruct(supplied, prepare(derive(Entries)))
    assert restored.entries == [Leaf(label="first"), Leaf(label="last", quantity=0)]
    assert [type(item) for item in restored.entries] == [Leaf, Leaf]


class Node(BaseModel):
    name: str
    children: list[Node] = Field(default_factory=list)


def test_finite_recursion_preserves_membership_without_sharing_occurrences():
    leaf = Node(name="shared")
    original = Node(name="root", children=[Node(name="branch", children=[leaf]), leaf])
    tables = decompose(original, prepare(derive(Node)))
    restored = reconstruct({name: list(reversed(rows)) for name, rows in tables.items()}, prepare(derive(Node)))
    assert restored == original
    assert [node.name for node in restored.children] == ["branch", "shared"]
    assert restored.children[0].children[0].name == "shared"
    assert restored.children[0].children[0] is not restored.children[1]
    assert type(restored.children[0].children[0]) is Node


class Base(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class Inherited(Base):
    detail: str


def test_inherited_field_validator_runs_on_independent_canonical_values():
    restored = reconstruct({"root": [{"_sm_key": "external", "code": "  a-1  ", "detail": "kept"}]},
                           prepare(derive(Inherited)))
    assert restored.code == "A-1"
    assert restored.detail == "kept"


T = TypeVar("T")


class Box(BaseModel, Generic[T]):
    value: T


class Specialized(BaseModel):
    boxes: list[Box[int]]


def test_resolved_generic_specialization_reconstructs_concrete_native_type():
    original = Specialized(boxes=[Box[int](value=7), Box[int](value=0)])
    restored = reconstruct(decompose(original, prepare(derive(Specialized))), prepare(derive(Specialized)))
    assert [box.value for box in restored.boxes] == [7, 0]
    assert [type(box) for box in restored.boxes] == [Box[int], Box[int]]


@dataclass
class PlainChild:
    code: str
    count: int


class Mixed(BaseModel):
    children: list[PlainChild]


def test_nested_ordinary_dataclasses_keep_concrete_type_and_values():
    original = Mixed(children=[PlainChild("x", 1), PlainChild("", 0)])
    restored = reconstruct(decompose(original, prepare(derive(Mixed))), prepare(derive(Mixed)))
    assert restored.children == [PlainChild("x", 1), PlainChild("", 0)]
    assert [type(item) for item in restored.children] == [PlainChild, PlainChild]


class Revalidated(Base):
    model_config = ConfigDict(revalidate_instances="always")


class RevalidatingParent(BaseModel):
    model_config = ConfigDict(revalidate_instances="always")
    children: list[Revalidated]


def test_nested_revalidation_keeps_idempotent_native_validator_result():
    original = RevalidatingParent(children=[Revalidated(code="first")])
    original.children[0].code = "  second  "
    prepared = prepare(derive(RevalidatingParent))
    restored = reconstruct(decompose(original, prepared), prepared)
    assert restored.children[0].code == "SECOND"
    twice = reconstruct(decompose(restored, prepared), prepare(derive(RevalidatingParent)))
    assert twice.children[0].code == "SECOND"
    assert type(twice.children[0]) is Revalidated
