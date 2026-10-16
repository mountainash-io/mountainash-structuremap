from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from mountainash_structuremap.definitions import Declaration


class _Canonical(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class AlternativeLayout(_Canonical):
    table: _NonEmpty | None = None
    prefix: str | None = None


class Naming(_Canonical):
    metadata_namespace: str = "_sm_"
    column_naming: Literal["snake_case", "preserve"] = "snake_case"


class DeclaredBinding(_Canonical):
    strategy: Literal["declared"] = "declared"
    name: _NonEmpty


class SourceKey(_Canonical):
    strategy: Literal["source"] = "source"
    columns: tuple[_NonEmpty, ...]


class SharedCollection(_Canonical):
    strategy: Literal["shared"] = "shared"
    table: _NonEmpty
    role_column: _NonEmpty


class TypeCast(_Canonical):
    strategy: Literal["type"] = "type"
    to: _NonEmpty
    on_failure: Literal["throw", "null"] = "throw"


class CustomCast(_Canonical):
    strategy: Literal["custom"] = "custom"
    forward: JsonValue
    reverse: JsonValue = None


class RecordedDiscriminator(_Canonical):
    strategy: Literal["recorded"] = "recorded"
    column: _NonEmpty | None = None
    values: dict[_NonEmpty, _NonEmpty] | None = None


class SourceDiscriminator(_Canonical):
    strategy: Literal["source"] = "source"
    field: _NonEmpty


KeyStrategy = Literal["generated"] | SourceKey
CollectionTableStrategy = Literal["separate"] | SharedCollection
DiscriminatorStrategy = RecordedDiscriminator | SourceDiscriminator
CastStrategy = TypeCast | CustomCast


class UseOverride(_Canonical):
    table: _NonEmpty | None = None
    column: _NonEmpty | None = None
    prefix: str | None = None
    binding: Literal["detected"] | DeclaredBinding | None = None
    construction: Literal["validated", "unvalidated"] | None = None
    key: KeyStrategy | None = None
    positions: Literal["strict", "lenient"] | None = None
    ordered: bool | None = None
    collection_table: CollectionTableStrategy | None = None
    discriminator: DiscriminatorStrategy | None = None
    alternatives: dict[str, AlternativeLayout] = Field(default_factory=dict)
    variant_layout: Literal["split", "prefixed", "shared", "eav"] | None = None
    role_discriminator: Literal["separate", "combined"] | None = None
    shared_types: Literal["exact", "widen"] | None = None
    cast: CastStrategy | None = None
    presence: Literal["normalize", "preserve"] | None = None
    value_space: Literal["typed", "lexical"] | None = None
    json_numbers: Literal["practical", "exact"] | None = None
    duplicate_members: Literal["reject", "last", "first", "preserve"] | None = None
    open_content: Literal["fragment", "drop", "structured", "reject"] | None = None


class MappingDefaults(_Canonical):
    naming: Naming = Field(default_factory=Naming)
    positions: Literal["strict", "lenient"] = "strict"
    presence: Literal["normalize", "preserve"] = "normalize"
    binding: Literal["detected"] | DeclaredBinding | None = None
    construction: Literal["validated", "unvalidated"] | None = None
    variant_layout: Literal["split", "prefixed", "shared", "eav"] | None = None
    role_discriminator: Literal["separate", "combined"] | None = None
    shared_types: Literal["exact", "widen"] | None = None
    value_space: Literal["typed", "lexical"] | None = None
    json_numbers: Literal["practical", "exact"] | None = None
    duplicate_members: Literal["reject", "last", "first", "preserve"] | None = None
    open_content: Literal["fragment", "drop", "structured", "reject"] | None = None


class MappingUse(Declaration):
    component: _NonEmpty
    config: dict[str, JsonValue] = Field(default_factory=dict)
    children: dict[str, MappingNode] = Field(default_factory=dict)


class MappingRef(Declaration):
    ref: _NonEmpty
    config: dict[str, JsonValue] = Field(default_factory=dict)
    children: dict[str, MappingNode] = Field(default_factory=dict)


MappingNode = MappingUse | MappingRef


class RowLayout(Declaration):
    key: tuple[_NonEmpty, ...]
    generated: bool = False
    parent: tuple[str, ...] = ()
    position: str | None = None
    map_key: str | None = None
    parent_table: str | None = None
    role_column: str | None = None
    null_column: str | None = None

    @model_validator(mode="after")
    def _validate_key(self) -> RowLayout:
        if not self.key or len(set(self.key)) != len(self.key):
            raise ValueError("row layout key must be nonempty and contain unique columns")
        return self


MappingUse.model_rebuild()
MappingRef.model_rebuild()


class MappingSpec(_Canonical):
    root: str | None = None
    defaults: MappingDefaults = Field(default_factory=MappingDefaults)
    overrides: dict[str, UseOverride] = Field(default_factory=dict)
    use: MappingNode | None = None
    definitions: dict[str, MappingNode] = Field(default_factory=dict)
    layouts: dict[str, RowLayout] = Field(default_factory=dict)


__all__ = [
    "CastStrategy",
    "CollectionTableStrategy",
    "CustomCast",
    "DeclaredBinding",
    "DiscriminatorStrategy",
    "KeyStrategy",
    "MappingDefaults",
    "AlternativeLayout",
    "MappingNode",
    "MappingRef",
    "MappingSpec",
    "MappingUse",
    "Naming",
    "RecordedDiscriminator",
    "RowLayout",
    "SharedCollection",
    "SourceDiscriminator",
    "SourceKey",
    "TypeCast",
    "UseOverride",
]
