from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Canonical(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Naming(_Canonical):
    metadata_namespace: str = "_sm_"
    column_naming: Literal["snake_case", "preserve"] = "snake_case"


class UseOverride(_Canonical):
    table: str | None = None
    column: str | None = None
    prefix: str | None = None


class MappingDefaults(_Canonical):
    naming: Naming = Field(default_factory=Naming)
    positions: str = "strict"
    presence: str = "normalize"
    binding: str | None = None


class MappingSpec(_Canonical):
    root: str | None = None
    defaults: MappingDefaults = Field(default_factory=MappingDefaults)
    overrides: dict[str, UseOverride] = Field(default_factory=dict)


__all__ = ["MappingDefaults", "MappingSpec", "Naming", "UseOverride"]
