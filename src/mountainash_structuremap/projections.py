from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from mountainash import BaseExpressionAPI, MountainashError, col, relation
from mountainash.core.dtypes import DecimalDtype, MountainashDtype, NativeDtype, parse_cast_target
from mountainash.typespec.converters import resolve_field_canonical, to_polars_schema
from mountainash.typespec.spec import FieldSpec, TypeSpec

from mountainash_structuremap.codecs import Cell
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import TypeCast


@dataclass(frozen=True, init=False)
class ValueProjection:
    """Prepared Mountainash casts for one scalar codec's owned fields."""

    _source_fields: Mapping[str, FieldSpec]
    _projected_fields: Mapping[str, FieldSpec]
    forward: tuple[BaseExpressionAPI, ...]
    reverse: tuple[BaseExpressionAPI, ...]
    path: str

    def __init__(
        self,
        source_fields: Mapping[str, FieldSpec],
        projected_fields: Mapping[str, FieldSpec],
        forward: tuple[BaseExpressionAPI, ...],
        reverse: tuple[BaseExpressionAPI, ...],
        *,
        path: str,
    ) -> None:
        object.__setattr__(self, "_source_fields", dict(source_fields))
        object.__setattr__(self, "_projected_fields", dict(projected_fields))
        object.__setattr__(self, "forward", forward)
        object.__setattr__(self, "reverse", reverse)
        object.__setattr__(self, "path", path)

    @property
    def source_fields(self) -> Mapping[str, FieldSpec]:
        return dict(self._source_fields)

    @property
    def projected_fields(self) -> Mapping[str, FieldSpec]:
        return dict(self._projected_fields)

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
        owned_rows = [{name: row[name] for name in fields} for row in rows]
        try:
            typed = relation(owned_rows).conform(TypeSpec(fields=dict(fields), missing_values=[]))
            return cast(list[dict[str, Cell]], typed.select(*expressions).to_dicts())
        except Exception as exc:
            direction = "reverse" if reverse else "forward"
            raise StructureMapError(
                "cast_failed",
                f"Mountainash {direction} cast failed: {exc}",
                path=self.path,
                remedy="Correct the source values or choose a compatible declared cast target",
            ) from exc


def _reverse_target(
    field: FieldSpec, *, path: str, name: str,
) -> MountainashDtype | DecimalDtype | NativeDtype:
    if field.backend_type is not None:
        return parse_cast_target(to_polars_schema(TypeSpec(fields={name: field}))[name])
    canonical = resolve_field_canonical(field)
    if isinstance(canonical, (MountainashDtype, DecimalDtype)):
        return canonical
    raise StructureMapError(
        "unavailable_cast",
        f"Cannot compile a reverse cast for untyped field {name!r}",
        path=path,
        remedy="Declare a native scalar field with a Mountainash-resolvable type",
    )


def prepare_value_projection(
    fields: Mapping[str, FieldSpec],
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

    source_fields = dict(fields)
    if not source_fields:
        raise StructureMapError(
            "invalid_cast",
            "A scalar cast requires at least one codec field",
            path=path,
            remedy="Apply TypeCast to a scalar with declared codec fields",
        )
    try:
        projected_fields = dict.fromkeys(source_fields, FieldSpec(dtype=target, missing_values=[]))
        forward = tuple(
            col(name).cast(cast.to, failure_behavior=cast.on_failure).alias(name)
            for name in source_fields
        )
        reverse = tuple(
            col(name).cast(
                _reverse_target(field, path=path, name=name),
                failure_behavior=cast.on_failure,
            ).alias(name)
            for name, field in source_fields.items()
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
