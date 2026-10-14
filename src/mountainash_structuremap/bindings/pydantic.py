from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import is_dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel

from mountainash_structuremap.bindings.contracts import CapabilityReport, RecordAdapter, VariantAdapter
from mountainash_structuremap.bindings.dataclass import DataclassBinding, describe_annotation
from mountainash_structuremap.bindings.selection import _has_xml_metadata
from mountainash_structuremap.codecs import PrimitiveCodec
from mountainash_structuremap.definitions import Alternative, Member, RecordDefinition, Scalar
from mountainash_structuremap.errors import StructureMapError


class PydanticBinding:
    name = "pydantic"
    capabilities = CapabilityReport(Parse=True, Serialize=True, Validate=False)

    def __init__(self) -> None:
        self._dataclass = DataclassBinding()

    def _check_model(self, cls: type[BaseModel], path: str) -> None:
        if getattr(cls, "__pydantic_root_model__", False):
            raise StructureMapError(
                "unsupported_native_shape", "Pydantic RootModel is unsupported", path=path,
                owner="native limitation", remedy="Use an ordinary BaseModel record",
            )
        if getattr(cls, "__pydantic_custom_init__", False):
            raise StructureMapError(
                "unsupported_native_shape", "Custom Pydantic initialization is unsupported", path=path,
                owner="native limitation",
                remedy="Use standard model initialization and compatible field/model validators",
            )
        if cls.model_config.get("extra") == "allow":
            raise StructureMapError(
                "unsupported_native_shape", "Retained Pydantic extras are unsupported", path=path,
                owner="native limitation",
                remedy="Declare mapped data as explicit model fields; retained extras are not supported",
            )


    def describe_record(
        self, cls: type, register: Callable[[type, str], str], *, path: str
    ) -> RecordDefinition:
        if not issubclass(cls, BaseModel):
            if _has_xml_metadata(cls):
                raise StructureMapError(
                    "unavailable_binding", "XML-shaped records require xsdata", path=path,
                    owner="binding gap", remedy="Use the xsdata binding for XML-shaped records",
                )
            return self._dataclass.describe_record(
                cls, register, path=path,
                record_predicate=lambda typ: issubclass(typ, BaseModel) or is_dataclass(typ),
            )
        self._check_model(cls, path)
        members: list[Member] = []
        for name, field in cls.model_fields.items():
            member_path = "/".join(filter(None, (path, name)))
            annotation = field.annotation
            default: Literal["factory", "value", "required"] = (
                "factory" if field.default_factory is not None
                else "value" if not field.is_required()
                else "required"
            )
            members.append(Member(
                name=name,
                shape=describe_annotation(
                    annotation, member_path, register,
                    record_predicate=lambda typ: issubclass(typ, BaseModel) or is_dataclass(typ),
                    allow_literals=True,
                ),
                default_kind=default,
            ))
        return RecordDefinition(name=f"{cls.__module__}.{cls.__qualname__}", members=tuple(members))

    def prepare_record(self, cls: type, definition: RecordDefinition, *, path: str) -> RecordAdapter:
        if issubclass(cls, BaseModel):
            self._check_model(cls, path)
            return PydanticRecordAdapter(cls)
        return self._dataclass.prepare_record(cls, definition, path=path)

    def prepare_scalar(
        self, declaration: Scalar, *, native_type: type | None, format: str | None,
        column: str, policy: str | None, metadata_namespace: str = "_sm_", path: str,
        field_context: tuple[type, str, str | None] | None = None,
    ) -> PrimitiveCodec:
        return self._dataclass.prepare_scalar(
            declaration, native_type=native_type, format=format, column=column,
            policy=policy, metadata_namespace=metadata_namespace, path=path, field_context=field_context,
        )

    def prepare_variant(
        self, alternatives: tuple[Alternative, ...], *, native_types: Mapping[str, type],
        namespace: str | None = None, path: str,
    ) -> VariantAdapter:
        return self._dataclass.prepare_variant(
            alternatives, native_types=native_types, namespace=namespace, path=path,
        )

    def parse(self, source: str | bytes, root_type: type[Any]) -> Any:
        try:
            return root_type.model_validate_json(source)
        except Exception as exc:
            raise StructureMapError(
                "native_parse_failed", "Pydantic could not parse the JSON document", path="",
                owner="native limitation",
                remedy="Provide a valid document matching the prepared native Pydantic model",
            ) from exc

    def serialize(self, value: object) -> str:
        try:
            return cast(BaseModel, value).model_dump_json()
        except Exception as exc:
            raise StructureMapError(
                "native_serialize_failed", "Pydantic could not serialize the native model", path="",
                owner="native limitation",
                remedy="Provide values supported by the native Pydantic model and its serializers",
            ) from exc


class PydanticRecordAdapter:
    def __init__(self, native_type: type[BaseModel]) -> None:
        self.native_type = native_type

    def read(self, instance: object, member: str, *, path: str) -> object:
        try:
            return getattr(instance, member)
        except Exception as exc:
            raise StructureMapError(
                "native_read_failed", "Could not read Pydantic member", path=path,
                owner="native limitation", remedy="Provide a readable mapped member",
            ) from exc

    def construct(self, values: dict[str, object], *, path: str) -> object:
        try:
            return self.native_type.model_validate(values, by_name=True, by_alias=False)
        except Exception as exc:
            raise StructureMapError(
                "native_construct_failed", "Pydantic construction failed", path=path,
                owner="native limitation",
                remedy="Supply values satisfying the model; before-validators must accept canonical member inputs",
            ) from exc
