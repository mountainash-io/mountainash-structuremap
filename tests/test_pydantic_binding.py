from dataclasses import dataclass
from typing import ClassVar

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)

from mountainash_structuremap import (
    MappingSpec,
    StructureMapError,
    decompose,
    derive,
    prepare,
    reconstruct,
)


class Count(BaseModel):
    quantity: int = Field(alias="qty", ge=1)


class Aliases(BaseModel):
    first: int = Field(validation_alias="second")
    second: int = Field(validation_alias=AliasPath("payload", 0))
    third: int = Field(validation_alias=AliasChoices("choice", AliasPath("other", "value")))


class RawValidator(BaseModel):
    value: int = Field(alias="wire_value")

    @model_validator(mode="before")
    @classmethod
    def needs_wire(cls, data):
        if isinstance(data, dict) and "wire_value" not in data:
            raise ValueError("wire_value required by author validator")
        return data


class CustomCount(BaseModel):
    quantity: int = Field(default=0, alias="qty")

    def __init__(self, **data):
        super().__init__(**data)


class RevalidatedCount(Count):
    model_config = ConfigDict(revalidate_instances="always")


class CustomParent(BaseModel):
    child: RevalidatedCount

    def __init__(self, **data):
        super().__init__(**data)


class ConfiguredParent(CustomParent):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=False)


class NestedCustom(BaseModel):
    child: CustomCount


@dataclass
class Plain:
    value: int


class Mixed(BaseModel):
    child: Plain


class Envelope(BaseModel):
    child: Count


@pytest.mark.parametrize("binding", [None, "pydantic"])
def test_independent_renamed_quantity_runs_native_validation(binding):
    mapping = MappingSpec(overrides={"quantity": {"column": "quantity_count"}})
    prepared = prepare(derive(Count, binding=binding), mapping)
    result = reconstruct({"root": [{"_sm_key": "external", "quantity_count": 7}]}, prepared)
    assert type(result) is Count
    assert result.quantity == 7
    with pytest.raises(StructureMapError) as caught:
        reconstruct({"root": [{"_sm_key": "invalid", "quantity_count": -1}]}, prepared)
    assert isinstance(caught.value.__cause__, ValidationError)
    assert caught.value.__cause__.errors()[0]["loc"] == ("quantity",)


def test_canonical_members_are_not_consumed_by_other_fields_aliases():
    value = reconstruct({"root": [{"_sm_key": "row", "first": 11, "second": 22, "third": 33}]},
                        prepare(derive(Aliases)))
    assert (value.first, value.second, value.third) == (11, 22, 33)


def test_captured_values_survive_without_factory_or_serialization_projection():
    calls = []

    def next_token():
        calls.append("factory")
        return "captured"

    class Data(BaseModel):
        value: int = Field(default=9, exclude=True)
        token: str = Field(default_factory=next_token)
        preparations: ClassVar[int] = 0

        @computed_field
        @property
        def doubled(self) -> int:
            return self.value * 2

    prepared = prepare(derive(Data))
    assert calls == []
    original = Data(value=17)
    rows = decompose(original, prepared)
    assert {key: value for key, value in rows["root"][0].items() if key not in prepared.tables["root"].layout.key} == {
        "value": 17, "token": "captured",
    }
    restored = reconstruct(rows, prepare(derive(Data)))
    assert (restored.value, restored.token, restored.doubled) == (17, "captured", 34)
    assert calls == ["factory"]


def test_before_validator_receives_canonical_input_and_failure_keeps_cause():
    original = RawValidator(wire_value=5)
    rows = decompose(original, prepare(derive(RawValidator)))
    with pytest.raises(StructureMapError) as caught:
        reconstruct(rows, prepare(derive(RawValidator)))
    assert isinstance(caught.value.__cause__, ValidationError)
    assert caught.value.__cause__.errors()[0]["type"] == "value_error"
    assert "canonical" in caught.value.remedy


@pytest.mark.parametrize("model,path", [(CustomCount, ""), (CustomParent, ""),
                                         (ConfiguredParent, ""), (NestedCustom, "child")])
def test_custom_initialization_is_refused_before_it_can_lose_canonical_values(model, path):
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(model))
    assert caught.value.code == "unsupported_native_shape"
    assert caught.value.path == path
    assert "initialization" in caught.value.remedy


def test_standard_nested_revalidation_and_field_validators_remain_native():
    class Normalized(BaseModel):
        child: RevalidatedCount
        label: str

        @field_validator("label")
        @classmethod
        def strip_label(cls, value):
            return value.strip()

    value = reconstruct({"root": [{"_sm_key": "row", "child_quantity": 7, "label": " name "}]},
                        prepare(derive(Normalized)))
    assert value.child.quantity == 7
    assert value.label == "name"


@pytest.mark.parametrize("mapping", [MappingSpec(defaults={"construction": "validated"}),
                                      MappingSpec(overrides={"": {"construction": "validated"}})])
def test_explicit_validated_construction_accepts_native_model(mapping):
    value = reconstruct({"root": [{"_sm_key": "row", "quantity": 3}]}, prepare(derive(Count), mapping))
    assert value.quantity == 3


@pytest.mark.parametrize("policy", [{"construction": "unvalidated"}, {"presence": "preserve"}])
def test_unavailable_native_policies_fail_before_reading_values(policy):
    derived = derive(Count)
    for mapping in (MappingSpec(defaults=policy), MappingSpec(overrides={"": policy})):
        with pytest.raises(StructureMapError) as caught:
            prepare(derived, mapping)
        assert caught.value.code == "unavailable_capability"


def test_independent_validation_operation_is_not_implied_by_construction():
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(Count), validate_native=True)
    assert caught.value.code == "unavailable_capability"


@pytest.mark.parametrize("model,path", [(Plain, ""), (Mixed, "child")])
def test_validated_construction_cannot_be_claimed_for_plain_dataclass(model, path):
    mapping = MappingSpec(overrides={path: {"construction": "validated"}})
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(model), mapping)
    assert caught.value.code == "unavailable_capability"
    assert caught.value.path == path


def test_validated_child_use_retains_native_field_constraints():
    mapping = MappingSpec(overrides={"child": {"construction": "validated"}})
    prepared = prepare(derive(Envelope), mapping)
    value = reconstruct({"root": [{"_sm_key": "row", "child_quantity": 8}]}, prepared)
    assert value.child.quantity == 8


def test_explicit_plain_binding_does_not_accept_a_pydantic_model():
    with pytest.raises(StructureMapError) as caught:
        derive(Count, binding="dataclass")
    assert caught.value.code == "unavailable_binding"


def test_retained_extras_are_refused_instead_of_dropped():
    class OpenData(BaseModel):
        model_config = ConfigDict(extra="allow")
        value: int

    with pytest.raises(StructureMapError) as caught:
        derive(OpenData)
    assert caught.value.code == "unsupported_native_shape"
    assert "extra" in str(caught.value)
