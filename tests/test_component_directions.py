from dataclasses import dataclass

import pytest
from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType as U

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct
from mountainash_structuremap.codecs import Cell, CellLocation
from mountainash_structuremap.components import PreparationContext
from mountainash_structuremap.mapping import MappingSpec, MappingUse, RowLayout, UseOverride


@dataclass(frozen=True)
class Value:
    amount: int


class EncodeOnlyCodec:
    fields = {"amount": FieldSpec(type=U.INTEGER, missing_values=[])}

    def encode(self, value: object) -> tuple[Cell, ...]:
        return (value,)


class DecodeOnlyCodec:
    fields = {"amount": FieldSpec(type=U.INTEGER, missing_values=[])}

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        assert len(cells) == 1 and type(cells[0]) is int
        return cells[0]

    def decode(self, checked: object) -> object:
        return checked


class EncodeOnly:
    identity = "test.encode-only"
    directions = frozenset({"decompose"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        context.value(codec=EncodeOnlyCodec())


class DecodeOnly:
    identity = "test.decode-only"
    directions = frozenset({"reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        context.value(codec=DecodeOnlyCodec())


def mapping(component: str) -> MappingSpec:
    return MappingSpec(
        use=MappingUse(component="core.record", config={"table": "values"}, children={
            "amount": MappingUse(component=component),
        }),
    )


def test_encode_only_component_needs_no_decode_methods_and_rejects_reconstruction_first():
    prepared = prepare(derive(Value), mapping("test.encode-only"), components=(EncodeOnly(),),
                       directions=frozenset({"decompose"}))

    assert decompose(Value(7), prepared)["values"][0]["amount"] == 7
    with pytest.raises(StructureMapError, match="direction"):
        reconstruct(object(), prepared)


def test_decode_only_component_needs_no_encode_method_and_rejects_decomposition_first():
    prepared = prepare(derive(Value), mapping("test.decode-only"), components=(DecodeOnly(),),
                       directions=frozenset({"reconstruct"}))

    assert reconstruct({"values": [{"_sm_key": "0", "amount": 7}]}, prepared) == Value(7)
    with pytest.raises(StructureMapError, match="direction"):
        decompose(object(), prepared)


@dataclass(frozen=True)
class SharedPair:
    left: int
    right: int


_QUANTITY = FieldSpec(type=U.INTEGER, minimum=0, missing_values=[])


class SharedQuantityCodec:
    def __init__(self, column: str):
        self.fields = {column: _QUANTITY}

    def encode(self, value: object) -> tuple[Cell, ...]:
        if type(value) is not int:
            raise StructureMapError("native_type_mismatch", "Quantity requires an integer")
        return (value,)

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        assert len(cells) == 1 and type(cells[0]) is int
        return cells[0]

    def decode(self, checked: object) -> object:
        return checked


class SharedQuantity:
    identity = "test.shared-quantity"
    directions = frozenset({"decompose", "reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        context.value(codec=SharedQuantityCodec(context.options.column or "quantity"))


def test_shared_codec_definition_is_projected_under_two_names_and_reconstructed():
    mapping_spec = MappingSpec(
        use=MappingUse(component="core.record", config={"table": "values"}, children={
            "left": MappingUse(component="test.shared-quantity"),
            "right": MappingUse(component="test.shared-quantity"),
        }),
        overrides={
            "left": UseOverride(column="left_value"),
            "right": UseOverride(column="right_value"),
        },
    )
    prepared = prepare(derive(SharedPair), mapping_spec, components=(SharedQuantity(),))

    emitted = decompose(SharedPair(3, 8), prepared)
    assert emitted["values"][0]["left_value"] == 3
    assert emitted["values"][0]["right_value"] == 8
    assert tuple(prepared.tables["values"].typespec.fields) == (
        "_sm_key", "left_value", "right_value",
    )
    assert prepared.tables["values"].typespec.fields["left_value"].constraints.minimum == 0
    assert prepared.tables["values"].typespec.fields["left_value"] is _QUANTITY
    assert prepared.tables["values"].typespec.fields["right_value"] is _QUANTITY
    assert prepared.tables["values"].typespec.fields["right_value"].constraints.minimum == 0
    assert reconstruct(emitted, prepared) == SharedPair(3, 8)


_BULK_QUANTITY = _QUANTITY.variant(minimum=10)


class BulkQuantityCodec(SharedQuantityCodec):
    def __init__(self, column: str):
        self.fields = {column: _BULK_QUANTITY}


class BulkQuantity:
    identity = "test.bulk-quantity"
    directions = frozenset({"decompose", "reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        context.value(codec=BulkQuantityCodec(context.options.column or "quantity"))


@dataclass(frozen=True)
class OrderLine:
    qty: int
    bulk: int


@dataclass(frozen=True)
class Order:
    lines: list[OrderLine]


def test_shared_definition_and_variant_in_nested_collection_reconstruct():
    mapping_spec = MappingSpec(
        use=MappingUse(component="core.record", children={"lines": MappingUse(
            component="core.sequence", children={"item": MappingUse(component="core.record", children={
                "qty": MappingUse(component="test.shared-quantity"),
                "bulk": MappingUse(component="test.bulk-quantity"),
            })},
        )}),
        overrides={"lines/bulk": UseOverride(column="bulk_qty")},
    )
    prepared = prepare(derive(Order), mapping_spec, components=(SharedQuantity(), BulkQuantity()))
    value = Order([OrderLine(3, 12), OrderLine(0, 10)])

    emitted = decompose(value, prepared)
    fields = prepared.tables["lines"].typespec.fields
    assert tuple(fields) == ("_sm_key", "_sm_parent", "_sm_position", "quantity", "bulk_qty")
    assert [(row["quantity"], row["bulk_qty"]) for row in emitted["lines"]] == [(3, 12), (0, 10)]
    assert fields["quantity"] is _QUANTITY
    assert fields["bulk_qty"] is _BULK_QUANTITY
    assert (_QUANTITY.constraints.minimum, _BULK_QUANTITY.constraints.minimum) == (0, 10)
    assert reconstruct(emitted, prepared) == value



@dataclass(frozen=True)
class MapRoot:
    values: dict[str, int]


class PrefixedStringKeyCodec:
    fields = {"wire_key": FieldSpec(type=U.STRING, missing_values=[])}

    def encode(self, value: object) -> tuple[Cell, ...]:
        assert type(value) is str
        return (f"wire:{value}",)

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        if len(cells) != 1 or type(cells[0]) is not str or not cells[0].startswith("wire:"):
            raise StructureMapError("invalid_map_key", "Expected a wire-prefixed key", path=location.path,
                                    remedy="Supply a wire-prefixed key")
        return cells[0][5:]

    def decode(self, checked: object) -> object:
        return str(checked).lower()


class PrefixedStringKeys:
    identity = "test.prefixed-string-keys"
    directions = frozenset({"decompose", "reconstruct"})

    def prepare(self, use: MappingUse, context: PreparationContext) -> None:
        context.value(codec=PrefixedStringKeyCodec())


def prepared_custom_map():
    relations = {
        "root": TypeSpec(fields={"_sm_key": FieldSpec(type=U.STRING, missing_values=[])}, missing_values=[]),
        "values": TypeSpec(fields={
            "_sm_key": FieldSpec(type=U.STRING, missing_values=[]),
            "_sm_parent": FieldSpec(type=U.STRING, missing_values=[]),
            "_sm_position": FieldSpec(type=U.INTEGER, missing_values=[]),
            "wire_key": FieldSpec(type=U.STRING, missing_values=[]),
            "value": FieldSpec(type=U.INTEGER, missing_values=[]),
        }, missing_values=[]),
    }
    mapping_spec = MappingSpec(
        use=MappingUse(component="core.record", config={"table": "root"}, children={
            "values": MappingUse(component="core.map", config={"table": "values"}, children={
                "key": MappingUse(component="test.prefixed-string-keys"),
            }),
        }),
        layouts={
            "root": RowLayout(key=("_sm_key",), generated=True),
            "values": RowLayout(key=("_sm_key",), generated=True, parent=("_sm_parent",),
                                position="_sm_position", map_key="wire_key"),
        },
    )
    return prepare(derive(MapRoot), mapping_spec, relations=relations, components=(PrefixedStringKeys(),))


def test_custom_map_key_codec_projects_and_reconstructs_independent_wire_keys():
    prepared = prepared_custom_map()

    emitted = decompose(MapRoot({"a": 2}), prepared)
    assert emitted == {
        "root": [{"_sm_key": "r0"}],
        "values": [{
            "_sm_key": "r1", "_sm_parent": "r0", "_sm_position": 0,
            "wire_key": "wire:a", "value": 2,
        }],
    }
    supplied = {
        "root": [{"_sm_key": "parent"}],
        "values": [{
            "_sm_key": "member", "_sm_parent": "parent", "_sm_position": 0,
            "wire_key": "wire:a", "value": 2,
        }],
    }
    assert reconstruct(supplied, prepared) == MapRoot({"a": 2})


def test_custom_map_key_codec_rejects_native_key_collisions_after_decode():
    prepared = prepared_custom_map()
    supplied = {
        "root": [{"_sm_key": "parent"}],
        "values": [
            {"_sm_key": "first", "_sm_parent": "parent", "_sm_position": 0, "wire_key": "wire:A", "value": 2},
            {"_sm_key": "second", "_sm_parent": "parent", "_sm_position": 1, "wire_key": "wire:a", "value": 3},
        ],
    }

    with pytest.raises(StructureMapError) as caught:
        reconstruct(supplied, prepared)
    assert caught.value.code == "duplicate_map_key"
