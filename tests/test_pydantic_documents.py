import json

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_serializer, model_validator
from pydantic_core import PydanticSerializationError

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


class WireCount(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    quantity: int = Field(alias="qty", ge=1)


class MemberOutput(WireCount):
    model_config = ConfigDict(serialize_by_alias=False)


@pytest.mark.parametrize("model,expected", [(WireCount, {"qty": 3}), (MemberOutput, {"quantity": 3})])
@pytest.mark.parametrize("source", ['{"qty": 3}', b'{"qty": 3}'])
def test_native_document_names_remain_independent_of_table_column_names(model, expected, source):
    prepared = prepare(derive(model), MappingSpec(overrides={"quantity": {"column": "count"}}))
    value = parse(source, prepared)
    assert type(value) is model
    assert value.quantity == 3
    assert decompose(value, prepared)["root"][0]["count"] == 3
    assert json.loads(serialize(value, prepared)) == expected


@pytest.mark.parametrize("source,error_type", [
    ('{"qty":', "json_invalid"),
    ('{"qty": -1}', "greater_than_equal"),
    ('{"quantity": 3}', "missing"),
])
def test_document_failures_keep_native_causes_without_alias_retry(source, error_type):
    with pytest.raises(StructureMapError) as caught:
        parse(source, prepare(derive(WireCount)))
    assert caught.value.code == "native_parse_failed"
    assert caught.value.path == ""
    assert isinstance(caught.value.__cause__, ValidationError)
    assert caught.value.__cause__.errors()[0]["type"] == error_type


def test_document_serialization_failure_retains_native_error():
    class Unserializable(BaseModel):
        value: int

        @field_serializer("value")
        def serialize_value(self, value):
            raise ValueError("value cannot be serialized")

    with pytest.raises(StructureMapError) as caught:
        serialize(Unserializable(value=1), prepare(derive(Unserializable)))
    assert caught.value.code == "native_serialize_failed"
    assert caught.value.path == ""
    assert isinstance(caught.value.__cause__, PydanticSerializationError)
    assert "value cannot be serialized" in str(caught.value.__cause__)


def test_alias_only_before_validator_accepts_documents_but_not_canonical_tables():
    class AliasOnly(WireCount):
        @model_validator(mode="before")
        @classmethod
        def require_wire_input(cls, values):
            if "qty" not in values:
                raise ValueError("requires wire qty")
            return values

    prepared = prepare(derive(AliasOnly))
    assert parse('{"qty": 7}', prepared).quantity == 7
    with pytest.raises(StructureMapError) as caught:
        reconstruct({"root": [{"_sm_key": "external", "quantity": 7}]}, prepared)
    assert caught.value.code == "native_construct_failed"
    assert isinstance(caught.value.__cause__, ValidationError)
    assert "requires wire qty" in str(caught.value.__cause__)
