"""Native BaseModel data, renamed columns and independent table reconstruction."""
from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from mountainash_structuremap import (
    MappingSpec,
    StructureMapError,
    decompose,
    derive,
    prepare,
    reconstruct,
)


class Item(BaseModel):
    product: str
    quantity: int = Field(alias="qty", ge=1)


factory_calls: list[str] = []


def new_token() -> str:
    factory_calls.append("created")
    return "captured-token"


class Order(BaseModel):
    items: list[Item]
    token: str = Field(default_factory=new_token)


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


if __name__ == "__main__":
    main()
