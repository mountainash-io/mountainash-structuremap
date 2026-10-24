from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

import yaml
from mountainash.typespec.spec import TableSchema
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from yaml.constructor import ConstructorError

from mountainash_structuremap.definitions import (
    Alternative,
    Alternatives,
    Declaration,
    Map,
    Nullable,
    Positional,
    RecordDefinition,
    RecordRef,
    Shape,
    StructureSpec,
)
from mountainash_structuremap.definitions import (
    Sequence as SequenceShape,
)
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import (
    MappingDefaults,
    MappingNode,
    MappingSpec,
    RowLayout,
    UseOverride,
)
from mountainash_structuremap.resolution import definition_candidates

Format = Literal["json", "yaml"]
_JSON_VALUE = TypeAdapter(JsonValue, config=ConfigDict(allow_inf_nan=False))


class StructureDocument(Declaration):
    namespace: str = Field(min_length=1)
    structure: StructureSpec

def _duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate mapping key {key!r}")
        result[key] = value
    return result


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueSafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError("while constructing a mapping", node.start_mark, "found an unhashable key", key_node.start_mark) from exc
        if duplicate:
            raise ConstructorError("while constructing a mapping", node.start_mark, f"duplicate key {key!r}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _parse(source: str | bytes, format: Format) -> Any:
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    if format == "json":
        value = json.loads(source, object_pairs_hook=_duplicate_pairs)
    elif format == "yaml":
        value = yaml.load(source, Loader=_UniqueSafeLoader)
    else:
        raise ValueError(f"unsupported document format {format!r}; expected 'json' or 'yaml'")
    return _JSON_VALUE.validate_python(value)


def _load(source: str | bytes, format: Format, model: type[BaseModel], name: str) -> Any:
    try:
        # JSON mode accepts arrays for tuple declarations without coercing scalar policies.
        return model.model_validate_json(json.dumps(_parse(source, format)), strict=True)
    except (UnicodeDecodeError, ValueError, yaml.YAMLError, ValidationError, TypeError) as exc:
        raise StructureMapError(
            "invalid_authoring_document", f"Invalid {name} {format} document: {exc}",
            path=name, remedy="Correct the document syntax and conform it to the generated authoring schema",
        ) from exc


def _dump(value: Any, format: Format) -> str:
    if format not in ("json", "yaml"):
        raise ValueError(f"unsupported document format {format!r}; expected 'json' or 'yaml'")
    value = _JSON_VALUE.validate_python(value)
    if format == "json":
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def load_mapping(source: str | bytes, *, format: Format) -> MappingSpec:
    return cast(MappingSpec, _load(source, format, MappingSpec, "mapping"))


def _mapping_document(mapping: MappingSpec) -> dict[str, Any]:
    document = mapping.model_dump(mode="json")
    # Only override presence affects precedence. Other dictionaries may have
    # been populated after construction without changing model_fields_set.
    document["overrides"] = {
        selector: override.model_dump(mode="json", exclude_unset=True)
        for selector, override in mapping.overrides.items()
    }
    return document


def dump_mapping(mapping: MappingSpec, *, format: Format) -> str:
    return _dump(_mapping_document(mapping), format)


def mapping_schema() -> dict[str, object]:
    return cast(dict[str, object], MappingSpec.model_json_schema())


def load_structure(source: str | bytes, *, format: Format) -> StructureDocument:
    return cast(StructureDocument, _load(source, format, StructureDocument, "structure"))


def dump_structure(document: StructureDocument, *, format: Format) -> str:
    return _dump(document.model_dump(mode="json", exclude_none=True), format)


def structure_schema() -> dict[str, object]:
    return cast(dict[str, object], StructureDocument.model_json_schema())


def _qualified(namespace: str, name: str) -> str:
    return name if name.startswith(namespace + ".") else f"{namespace}.{name}"


def _resolve_name(name: str, definitions: Mapping[str, RecordDefinition], *, context: str) -> str:
    candidates = definition_candidates(name, definitions)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise StructureMapError("ambiguous_definition", f"Definition reference {name!r} is ambiguous: {sorted(candidates)!r}", path=context, remedy="Use a fully qualified definition name")
    raise StructureMapError("unknown_definition", f"Unknown definition reference {name!r}", path=context, remedy="Declare the definition in a supplied structure document")


def _rewrite_shape(shape: Shape, definitions: Mapping[str, RecordDefinition], path: str) -> Shape:
    if isinstance(shape, RecordRef):
        return shape.model_copy(update={"definition": _resolve_name(shape.definition, definitions, context=path)})
    if isinstance(shape, SequenceShape):
        return shape.model_copy(update={"item": _rewrite_shape(shape.item, definitions, f"{path}/item")})
    if isinstance(shape, Map):
        return shape.model_copy(update={"key": _rewrite_shape(shape.key, definitions, f"{path}/key"), "value": _rewrite_shape(shape.value, definitions, f"{path}/value")})
    if isinstance(shape, Positional):
        return shape.model_copy(update={"items": tuple(_rewrite_shape(item, definitions, f"{path}/{index}") for index, item in enumerate(shape.items))})
    if isinstance(shape, Nullable):
        return shape.model_copy(update={"value": _rewrite_shape(shape.value, definitions, f"{path}/value")})
    if isinstance(shape, Alternatives):
        return shape.model_copy(update={"choices": tuple(Alternative.model_validate({**choice.model_dump(), "shape": _rewrite_shape(choice.shape, definitions, f"{path}/{choice.key}")}) for choice in shape.choices)})
    return shape


def assemble_structure(documents: Sequence[StructureDocument], *, root: str | Shape) -> StructureSpec:
    definitions: dict[str, RecordDefinition] = {}
    for document in documents:
        for key, definition in document.structure.definitions.items():
            if key != definition.name:
                raise StructureMapError(
                    "definition_name_mismatch",
                    f"Definition key {key!r} does not match declared name {definition.name!r}",
                    path=f"{document.namespace}.{key}",
                    remedy="Make each definition key and its declared name identical",
                )
            qualified = _qualified(document.namespace, key)
            if qualified in definitions:
                raise StructureMapError("duplicate_definition", f"Duplicate qualified definition {qualified!r}", path=qualified, remedy="Supply exactly one definition for each qualified name")
            definitions[qualified] = definition.model_copy(update={"name": qualified})
    if isinstance(root, str):
        root_value: str | Shape = _resolve_name(root[1:] if root.startswith("@") else root, definitions, context="root")
    else:
        root_value = _rewrite_shape(root, definitions, "root")
    rewritten: dict[str, RecordDefinition] = {}
    for name, definition in definitions.items():
        members = tuple(member.model_copy(update={"shape": _rewrite_shape(member.shape, definitions, f"{name}/{member.name}")}) for member in definition.members)
        rewritten[name] = definition.model_copy(update={"members": members})
    return StructureSpec(root=root_value, definitions=rewritten)

@dataclass(frozen=True)
class AuthoringBundle:
    structure: StructureDocument | None
    mapping: MappingSpec
    relations: Mapping[str, TableSchema]


class _BundleDocument(Declaration):
    structure: StructureDocument | None = None
    mapping: MappingSpec
    relations: dict[str, dict[str, JsonValue]]


def load_bundle(source: str | bytes, *, format: Format) -> AuthoringBundle:
    document = cast(_BundleDocument, _load(source, format, _BundleDocument, "bundle"))
    relations: dict[str, TableSchema] = {}
    for name, descriptor in document.relations.items():
        try:
            relations[name] = TableSchema.from_frictionless(descriptor)
        except Exception as exc:
            raise StructureMapError(
                "invalid_authoring_document", f"Invalid relation descriptor {name!r}: {exc}",
                path=f"relations/{name}", remedy="Supply a valid Mountainash Frictionless table descriptor",
            ) from exc
    return AuthoringBundle(document.structure, document.mapping, relations)


def dump_bundle(bundle: AuthoringBundle, *, format: Format) -> str:
    raw: dict[str, object] = {
        "structure": bundle.structure.model_dump(mode="json", exclude_none=True) if bundle.structure is not None else None,
        "mapping": _mapping_document(bundle.mapping),
        "relations": {name: descriptor.to_frictionless() for name, descriptor in bundle.relations.items()},
    }
    return _dump(raw, format)


class MappingBuilder:
    def __init__(self, mapping: MappingSpec | None = None) -> None:
        self._mapping = (mapping or MappingSpec()).model_copy(deep=True)

    def defaults(self, **settings: JsonValue) -> MappingBuilder:
        defaults = self._mapping.defaults.model_dump()
        defaults.update(settings)
        validated = MappingDefaults.model_validate(defaults)
        self._mapping = self._mapping.model_copy(update={"defaults": validated})
        return self
    def use(self, node: MappingNode) -> MappingBuilder:
        self._mapping = self._mapping.model_copy(update={"use": node.model_copy(deep=True)})
        return self

    def define(self, name: str, node: MappingNode) -> MappingBuilder:
        definitions = dict(self._mapping.definitions)
        definitions[name] = node.model_copy(deep=True)
        self._mapping = self._mapping.model_copy(update={"definitions": definitions})
        return self
    def override(self, selector: str, **settings: JsonValue) -> MappingBuilder:
        overrides = dict(self._mapping.overrides)
        existing = overrides.get(selector)
        values = existing.model_dump(exclude_unset=True) if existing is not None else {}
        values.update(settings)
        overrides[selector] = UseOverride.model_validate(values)
        self._mapping = self._mapping.model_copy(update={"overrides": overrides})
        return self

    def layout(self, table: str, layout: RowLayout) -> MappingBuilder:
        layouts = dict(self._mapping.layouts)
        layouts[table] = layout.model_copy(deep=True)
        self._mapping = self._mapping.model_copy(update={"layouts": layouts})
        return self

    def build(self) -> MappingSpec:
        return self._mapping.model_copy(deep=True)
