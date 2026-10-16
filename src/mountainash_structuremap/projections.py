from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import cast

from mountainash import BaseExpressionAPI, MountainashError, col, relation
from mountainash.core.dtypes import DecimalDtype, MountainashDtype, parse_cast_target
from mountainash.typespec.converters import resolve_field_canonical
from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType, from_canonical

from mountainash_structuremap.codecs import Cell
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import TypeCast


@dataclass(frozen=True, init=False)
class ValueProjection:
    """Prepared Mountainash casts for one scalar codec's owned fields."""

    _source_fields: tuple[FieldSpec, ...]
    _projected_fields: tuple[FieldSpec, ...]
    forward: tuple[BaseExpressionAPI, ...]
    reverse: tuple[BaseExpressionAPI, ...]
    path: str

    def __init__(
        self,
        source_fields: tuple[FieldSpec, ...],
        projected_fields: tuple[FieldSpec, ...],
        forward: tuple[BaseExpressionAPI, ...],
        reverse: tuple[BaseExpressionAPI, ...],
        *,
        path: str,
    ) -> None:
        object.__setattr__(self, "_source_fields", tuple(deepcopy(source_fields)))
        object.__setattr__(self, "_projected_fields", tuple(deepcopy(projected_fields)))
        object.__setattr__(self, "forward", forward)
        object.__setattr__(self, "reverse", reverse)
        object.__setattr__(self, "path", path)

    @property
    def source_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(deepcopy(self._source_fields))

    @property
    def projected_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(deepcopy(self._projected_fields))

    def apply(
        self,
        rows: Sequence[Mapping[str, Cell]],
        *,
        reverse: bool = False,
    ) -> list[dict[str, Cell]]:
        """Project one batch of rows, returning only this scalar's columns."""
        if not rows:
            return []
        fields = self._projected_fields if reverse else self._source_fields
        expressions = self.reverse if reverse else self.forward
        owned_rows = [
            {field.name: row[field.name] for field in fields}
            for row in rows
        ]
        try:
            typed = relation(owned_rows).conform(
                TypeSpec(fields=list(deepcopy(fields)), missing_values=[])
            )
            return cast(list[dict[str, Cell]], typed.select(*expressions).to_dicts())
        except Exception as exc:
            direction = "reverse" if reverse else "forward"
            raise StructureMapError(
                "cast_failed",
                f"Mountainash {direction} cast failed: {exc}",
                path=self.path,
                remedy="Correct the source values or choose a compatible declared cast target",
            ) from exc


def _field_for_target(field: FieldSpec, target: MountainashDtype | DecimalDtype) -> FieldSpec:
    if isinstance(target, DecimalDtype):
        return FieldSpec(
            name=field.name,
            type=UniversalType.NUMBER,
            dtype=target,
            missing_values=[],
        )
    universal, format_hint = from_canonical(target)
    return FieldSpec(
        name=field.name,
        type=universal,
        format=format_hint or "default",
        backend_type=target.value,
        missing_values=[],
    )


def _reverse_target(field: FieldSpec, *, path: str) -> MountainashDtype | DecimalDtype:
    if field.dtype is not None:
        return field.dtype
    if field.backend_type is not None:
        target = parse_cast_target(field.backend_type)
        if isinstance(target, (MountainashDtype, DecimalDtype)):
            return target
    canonical = resolve_field_canonical(field)
    if isinstance(canonical, (MountainashDtype, DecimalDtype)):
        return canonical
    raise StructureMapError(
        "unavailable_cast",
        f"Cannot compile a reverse cast for untyped field {field.name!r}",
        path=path,
        remedy="Declare a native scalar field with a Mountainash-resolvable type",
    )


def prepare_value_projection(
    fields: tuple[FieldSpec, ...],
    cast: TypeCast,
    *,
    path: str,
) -> ValueProjection:
    """Resolve a cast target and compile forward and native-restoring expressions."""
    try:
        target = parse_cast_target(cast.to)
    except (MountainashError, ValueError) as exc:
        raise StructureMapError(
            "invalid_cast_target",
            f"Mountainash could not resolve cast target {cast.to!r}: {exc}",
            path=path,
            remedy="Use a cast target accepted by Mountainash parse_cast_target",
        ) from exc
    if not isinstance(target, (MountainashDtype, DecimalDtype)):
        raise StructureMapError(
            "invalid_cast_target",
            f"Cast target {cast.to!r} does not resolve to a canonical Mountainash dtype",
            path=path,
            remedy="Use a target accepted by Mountainash parse_cast_target",
        )

    source_fields = tuple(deepcopy(fields))
    if not source_fields:
        raise StructureMapError(
            "invalid_cast",
            "A scalar cast requires at least one codec field",
            path=path,
            remedy="Apply TypeCast to a scalar with declared codec fields",
        )
    try:
        projected_fields = tuple(_field_for_target(field, target) for field in source_fields)
        forward = tuple(
            col(field.name).cast(cast.to, failure_behavior=cast.on_failure).alias(field.name)
            for field in source_fields
        )
        reverse = tuple(
            col(field.name).cast(
                _reverse_target(field, path=path),
                failure_behavior=cast.on_failure,
            ).alias(field.name)
            for field in source_fields
        )
    except StructureMapError:
        raise
    except (MountainashError, ValueError) as exc:
        raise StructureMapError(
            "invalid_cast",
            f"Mountainash could not compile the cast projections: {exc}",
            path=path,
            remedy="Use codec fields and cast targets supported by Mountainash",
        ) from exc
    return ValueProjection(source_fields, projected_fields, forward, reverse, path=path)


__all__ = ["ValueProjection", "prepare_value_projection"]
