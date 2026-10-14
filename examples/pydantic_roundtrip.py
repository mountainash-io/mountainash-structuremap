"""Native BaseModel data, renamed columns and independent table reconstruction."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mountainash_structuremap import (
    MappingSpec,
    StructureMapError,
    decompose,
    derive,
    parse,
    prepare,
    reconstruct,
    serialize,
)


class Item(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    product: str
    quantity: int = Field(alias="qty", ge=1)


factory_calls: list[str] = []


def new_token() -> str:
    factory_calls.append("created")
    return "captured-token"


class Order(BaseModel):
    items: list[Item]
    token: str = Field(default_factory=new_token)


class StockAdded(BaseModel):
    kind: Literal["added"]
    quantity: int = Field(ge=1)


class StockRemoved(BaseModel):
    kind: Literal["removed"]
    reason: str


class Inventory(BaseModel):
    events: list[Annotated[StockAdded | StockRemoved, Field(discriminator="kind")]]


def main() -> None:
    mapping = MappingSpec(overrides={"items/quantity": {"column": "quantity_count"}})
    prepared = prepare(derive(Order), mapping)
    assert factory_calls == []
    original = Order(items=[Item(product="A", qty=2), Item(product="B", qty=4)])
    tables = decompose(original, prepared)
    print("Emitted tables:", tables)
    tables["items"].reverse()
    restored = reconstruct(tables, prepare(derive(Order), mapping))
    assert [(item.product, item.quantity) for item in restored.items] == [("A", 2), ("B", 4)]
    assert restored.token == "captured-token"
    assert factory_calls == ["created"]
    print("Reordered delivery reconstructed:", restored)

    independent = {
        "root": [{"_sm_key": "external", "token": "authored-token"}],
        "items": [
            {"_sm_key": "last", "_sm_parent": "external", "_sm_position": 9,
             "product": "Y", "quantity_count": 7},
            {"_sm_key": "first", "_sm_parent": "external", "_sm_position": 2,
             "product": "X", "quantity_count": 3},
        ],
    }
    external = reconstruct(independent, prepare(derive(Order), mapping))
    assert [(item.product, item.quantity) for item in external.items] == [("X", 3), ("Y", 7)]
    assert external.token == "authored-token"
    assert factory_calls == ["created"]
    print("Independent table reconstruction:", external)

    independent["items"][0]["quantity_count"] = -1
    try:
        reconstruct(independent, prepare(derive(Order), mapping))
    except StructureMapError as exc:
        assert isinstance(exc.__cause__, ValidationError)
        print("Native validation rejection:", exc)
    else:
        raise AssertionError("Negative quantity unexpectedly passed native validation")

    variants = MappingSpec(overrides={"events": {
        "variant_layout": "split",
        "discriminator": {
            "strategy": "recorded", "column": "event_type",
            "values": {"StockAdded": "ADD", "StockRemoved": "REMOVE"},
        },
        "alternatives": {
            "StockAdded": {"table": "additions", "prefix": "added"},
            "StockRemoved": {"table": "removals", "prefix": "removed"},
        },
    }})
    inventory = reconstruct({
        "root": [{"_sm_key": "inventory"}],
        "additions": [{"_sm_key": "a", "_sm_parent": "inventory", "_sm_position": 9,
                       "event_type": "ADD", "added_kind": "added", "added_quantity": 3}],
        "removals": [{"_sm_key": "r", "_sm_parent": "inventory", "_sm_position": 2,
                      "event_type": "REMOVE", "removed_kind": "removed", "removed_reason": "damaged"}],
    }, prepare(derive(Inventory), variants))
    assert inventory.events == [
        StockRemoved(kind="removed", reason="damaged"), StockAdded(kind="added", quantity=3),
    ]
    emitted = decompose(inventory, prepare(derive(Inventory), variants))
    delivered = {name: list(reversed(rows)) for name, rows in emitted.items()}
    assert reconstruct(delivered, prepare(derive(Inventory), variants)) == inventory
    print("Recorded Split variants reconstructed:", inventory)

    wire = serialize(external, prepared)
    assert json.loads(wire) == {
        "items": [{"product": "X", "qty": 3}, {"product": "Y", "qty": 7}],
        "token": "authored-token",
    }
    assert parse(wire, prepared) == external
    assert factory_calls == ["created"]
    print("Native alias-oriented JSON:", wire)


if __name__ == "__main__":
    main()
