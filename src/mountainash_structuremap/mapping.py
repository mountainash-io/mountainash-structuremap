from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints


class _Canonical(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_NonEmpty = Annotated[str, StringConstraints(min_length=1)]


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


class MappingSpec(_Canonical):
    root: str | None = None
    defaults: MappingDefaults = Field(default_factory=MappingDefaults)
    overrides: dict[str, UseOverride] = Field(default_factory=dict)


__all__ = [
    "CastStrategy",
    "CollectionTableStrategy",
    "CustomCast",
    "DeclaredBinding",
    "DiscriminatorStrategy",
    "KeyStrategy",
    "MappingDefaults",
    "MappingSpec",
    "Naming",
    "RecordedDiscriminator",
    "SharedCollection",
    "SourceDiscriminator",
    "SourceKey",
    "TypeCast",
    "UseOverride",
]
