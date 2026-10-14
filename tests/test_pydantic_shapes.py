from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, ForwardRef, Literal

import pytest
from pydantic import BaseModel, Field, RootModel, ValidationError

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct


class LiteralChild(BaseModel):
    kind: Literal["allowed", "also-allowed"]


class LiteralEnvelope(BaseModel):
    child: LiteralChild


@dataclass
class PlainLiteral:
    kind: Literal["allowed"]


class MixedLiteral(BaseModel):
    child: PlainLiteral


class NestedFiniteValues(BaseModel):
    groups: dict[str, list[tuple[Literal["a", "b"], Annotated[int, Field(ge=0)]]]]
    flags: list[Literal[True, False]]
    optional: Literal["yes"] | None = None


class DateValue(BaseModel):
    value: date


class MixedDomain(BaseModel):
    value: Literal["one", 1]


class MissingForward(BaseModel):
    child: ForwardRef("UndefinedShapeClass")


@dataclass
class XmlChild:
    value: int = field(metadata={"type": "Element"})


class XmlEnvelope(BaseModel):
    child: XmlChild


class IntegerValue(BaseModel):
    value: int


def test_native_literal_allowed_values_are_checked_on_independent_input():
    prepared = prepare(derive(LiteralEnvelope))
    result = reconstruct({"root": [{"_sm_key": "row", "child_kind": "allowed"}]}, prepared)
    assert type(result.child) is LiteralChild
    assert result.child.kind == "allowed"
    with pytest.raises(StructureMapError) as caught:
        reconstruct({"root": [{"_sm_key": "row", "child_kind": "other"}]}, prepared)
    assert isinstance(caught.value.__cause__, ValidationError)
    assert caught.value.__cause__.errors()[0]["type"] == "literal_error"
    assert caught.value.path == "child"


def test_plain_dataclass_literal_is_not_advertised_as_native_validated():
    with pytest.raises(StructureMapError) as caught:
        derive(MixedLiteral)
    assert caught.value.code == "unsupported_annotation"
    assert caught.value.path == "child/kind"


def test_literal_and_annotated_metadata_survive_container_discovery():
    original = NestedFiniteValues(groups={"": [], "items": [("b", 2), ("a", 0), ("b", 2)]},
                                  flags=[False, True, False], optional="yes")
    rows = decompose(original, prepare(derive(NestedFiniteValues)))
    delivered = {name: list(reversed(values)) for name, values in rows.items()}
    restored = reconstruct(delivered, prepare(derive(NestedFiniteValues)))
    assert list(restored.groups) == ["", "items"]
    assert restored.groups == {"": [], "items": [("b", 2), ("a", 0), ("b", 2)]}
    assert restored.flags == [False, True, False]
    assert restored.optional == "yes"
    assert type(restored.groups["items"][0]) is tuple


@pytest.mark.parametrize("model,path,code", [
    (RootModel[int], "", "unsupported_native_shape"),
    (DateValue, "value", "unsupported_annotation"),
    (MixedDomain, "value", "unsupported_annotation"),
    (MissingForward, "child", "annotation_resolution_failed"),
    (XmlEnvelope, "child", "unavailable_binding"),
])
def test_unsupported_shapes_report_the_actual_member_and_limitation(model, path, code):
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(model))
    assert caught.value.code == code
    assert caught.value.path == path
    assert caught.value.remedy


@pytest.mark.parametrize("value", [-(2**63), 2**63 - 1])
def test_native_integer_uses_existing_signed_i64_boundary(value):
    prepared = prepare(derive(IntegerValue))
    result = reconstruct({"root": [{"_sm_key": "external", "value": value}]}, prepared)
    assert result.value == value


@pytest.mark.parametrize("value", [-(2**63) - 1, 2**63])
def test_native_integer_does_not_silently_widen_or_round(value):
    prepared = prepare(derive(IntegerValue))
    with pytest.raises(StructureMapError) as caught:
        decompose(IntegerValue(value=value), prepared)
    assert caught.value.code == "value_out_of_range"
    assert caught.value.path == "value"


def test_annotated_container_constraints_are_enforced_by_native_construction():
    class Constrained(BaseModel):
        items: list[Annotated[int, Field(ge=0)]]

    supplied = {
        "root": [{"_sm_key": "external"}],
        "items": [{"_sm_key": "item", "_sm_parent": "external", "_sm_position": 0, "value": -1}],
    }
    with pytest.raises(StructureMapError) as caught:
        reconstruct(supplied, prepare(derive(Constrained)))
    assert isinstance(caught.value.__cause__, ValidationError)
    error = caught.value.__cause__.errors()[0]
    assert error["loc"] == ("items", 0)
    assert error["type"] == "greater_than_equal"
