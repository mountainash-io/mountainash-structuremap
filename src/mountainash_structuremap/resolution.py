from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Collection, Mapping

from mountainash_structuremap.definitions import Nullable, RecordRef, StructureSpec
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import MappingNode, MappingRef, MappingSpec, MappingUse, UseOverride


@dataclass(frozen=True)
class DefinitionSelector:
    definition: str
    suffix: str
    name: str
    override: UseOverride


def definition_candidates(name: str, names: Collection[str]) -> tuple[str, ...]:
    """Share exact/unique-short-name semantics across assembly and selectors."""
    if name in names:
        return (name,)
    return tuple(candidate for candidate in names if candidate.rsplit(".", 1)[-1] == name)


def resolve_selectors(structure: StructureSpec, mapping: MappingSpec) -> tuple[DefinitionSelector, ...]:
    names = tuple(structure.definitions)
    selectors: list[DefinitionSelector] = []
    for raw, override in mapping.overrides.items():
        if not raw.startswith("@"):
            continue
        selector, _, suffix = raw[1:].partition("/")
        candidates = definition_candidates(selector, names)
        if not candidates:
            raise StructureMapError(
                "unknown_selector", f"Unknown definition selector {raw!r}", path=raw,
                remedy=f"Use one of the qualified definitions {sorted(names)!r}",
            )
        if len(candidates) > 1:
            raise StructureMapError(
                "ambiguous_selector", f"Definition selector {raw!r} matches {sorted(candidates)!r}", path=raw,
                remedy="Use a fully qualified definition selector",
            )
        selectors.append(DefinitionSelector(candidates[0], suffix, raw, override))
    return tuple(selectors)


def resolve_mapping_node(
    node: MappingNode | None,
    definitions: Mapping[str, MappingNode],
    *,
    path: str,
) -> MappingUse | None:
    """Resolve a finite chain of named aliases, merging outer overlays last."""
    if node is None:
        return None
    chain: list[MappingRef] = []
    positions: dict[str, int] = {}
    current = node
    while isinstance(current, MappingRef):
        if current.ref in positions:
            cycle = [ref.ref for ref in chain[positions[current.ref]:]] + [current.ref]
            raise StructureMapError(
                "mapping_cycle", f"Mapping aliases form a cycle: {' -> '.join(cycle)}", path=path,
                remedy="Make the reference chain reach a structural MappingUse",
            )
        positions[current.ref] = len(chain)
        chain.append(current)
        target = definitions.get(current.ref)
        if target is None:
            raise StructureMapError(
                "missing_mapping", f"Unknown mapping {current.ref!r}", path=path,
                remedy="Declare the referenced mapping in definitions",
            )
        current = target
    result = current
    for ref in reversed(chain):
        result = result.model_copy(update={
            "config": {**result.config, **ref.config},
            "children": {**result.children, **ref.children},
        })
    return result


def effective_override(
    mapping: MappingSpec,
    selectors: tuple[DefinitionSelector, ...],
    *,
    path: str,
    definitions_at: Callable[[str], frozenset[str]],
    shape: object,
) -> UseOverride | None:

    use_shape = shape
    while isinstance(use_shape, Nullable):
        use_shape = use_shape.value
    applicable = [
        selector
        for selector in selectors
        if (
            isinstance(use_shape, RecordRef)
            and use_shape.definition == selector.definition
            and selector.suffix == ""
        )
        or (
            selector.suffix != ""
            and (path == selector.suffix or path.endswith("/" + selector.suffix))
            and selector.definition in definitions_at(path[:-len(selector.suffix)].rstrip("/"))
        )
    ]
    values: dict[str, object] = {}
    origins: dict[str, str] = {}
    for selector in applicable:
        for key, value in selector.override.model_dump(exclude_unset=True).items():
            if key in values and values[key] != value:
                raise StructureMapError(
                    "conflicting_override",
                    f"Definition selectors {origins[key]!r} and {selector.name!r} conflict on {key!r}",
                    path=path,
                    remedy="Remove the conflicting selector or make equally specific declarations agree",
                )
            values[key] = value
            origins[key] = selector.name
    selected = mapping.overrides.get(path)
    if selected is not None:
        values.update(selected.model_dump(exclude_unset=True))
    return UseOverride.model_validate(values) if values else None
