from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Mapping
from collections.abc import Sequence as ComponentSequence
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeVar, cast

from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType
from pydantic import BaseModel

from mountainash_structuremap.bindings.contracts import RecordAdapter
from mountainash_structuremap.bindings.selection import select_binding
from mountainash_structuremap.codecs import Cell, DecodingCodec, EncodingCodec
from mountainash_structuremap.components import Direction, MappingComponent, builtin_components, default_component
from mountainash_structuremap.definitions import (
    Alternatives,
    DerivedModel,
    Map,
    Nullable,
    NumericFacets,
    Positional,
    RecordRef,
    Scalar,
    Sequence,
    Shape,
)
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.mapping import (
    DeclaredBinding,
    MappingNode,
    MappingSpec,
    MappingUse,
    RecordedDiscriminator,
    RowLayout,
    SharedCollection,
    SourceDiscriminator,
    TypeCast,
    UseOverride,
)
from mountainash_structuremap.prepared import (
    LinkProgram,
    MapProgram,
    NullProgram,
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
    TupleProgram,
    ValueProgram,
    VariantBranch,
    VariantProgram,
)
from mountainash_structuremap.projections import prepare_value_projection
from mountainash_structuremap.resolution import effective_override, resolve_mapping_node, resolve_selectors
from mountainash_structuremap.table_layouts import finalize_tables, same_physical_type

T = TypeVar("T")

def _snake(value: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()


def _validate_facets(facets: NumericFacets | None, path: str) -> None:
    if facets is None:
        return
    values: dict[str, Decimal] = {}
    for name in ("min_inclusive", "max_inclusive", "min_exclusive", "max_exclusive"):
        text = getattr(facets, name)
        if text is None:
            continue
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise StructureMapError(
                "invalid_numeric_facets",
                f"Numeric facet {name} is not a valid decimal",
                path=path,
                owner="binding gap",
                remedy="Supply exact finite decimal facet values",
            ) from exc
        if not number.is_finite():
            raise StructureMapError(
                "invalid_numeric_facets",
                f"Numeric facet {name} must be finite",
                path=path,
                owner="binding gap",
                remedy="Supply exact finite decimal facet values",
            )
        values[name] = number
    if "min_inclusive" in values and "min_exclusive" in values:
        conflict = "minInclusive and minExclusive cannot both be declared"
    elif "max_inclusive" in values and "max_exclusive" in values:
        conflict = "maxInclusive and maxExclusive cannot both be declared"
    else:
        lower = values.get("min_exclusive", values.get("min_inclusive"))
        upper = values.get("max_exclusive", values.get("max_inclusive"))
        lower_exclusive = "min_exclusive" in values
        upper_exclusive = "max_exclusive" in values
        conflict = (
            "Minimum bound exceeds maximum bound"
            if lower is not None
            and upper is not None
            and (lower > upper or (lower == upper and (lower_exclusive or upper_exclusive)))
            else ""
        )
    if conflict:
        raise StructureMapError(
            "invalid_numeric_facets",
            conflict,
            path=path,
            owner="binding gap",
            remedy="Supply mutually consistent source numeric facets",
        )


def _record_refs(shape: Shape) -> set[str]:
    if isinstance(shape, RecordRef):
        return {shape.definition}
    if isinstance(shape, Nullable):
        return _record_refs(shape.value)
    if isinstance(shape, Sequence):
        return _record_refs(shape.item)
    if isinstance(shape, Map):
        return _record_refs(shape.key) | _record_refs(shape.value)
    if isinstance(shape, Positional):
        return set().union(*(_record_refs(item) for item in shape.items)) if shape.items else set()
    if isinstance(shape, Alternatives):
        return set().union(*(_record_refs(choice.shape) for choice in shape.choices))
    return set()


def _components(model: DerivedModel[object]) -> dict[str, int]:
    graph = {
        name: set().union(*(_record_refs(member.shape) for member in definition.members))
        for name, definition in model.structure.definitions.items()
    }
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    component: dict[str, int] = {}
    component_id = 0

    def visit(node: str) -> None:
        nonlocal index, component_id
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for child in graph[node]:
            if child not in graph:
                continue
            if child not in indices:
                visit(child)
                low[node] = min(low[node], low[child])
            elif child in on_stack:
                low[node] = min(low[node], indices[child])
        if low[node] == indices[node]:
            while True:
                child = stack.pop()
                on_stack.remove(child)
                component[child] = component_id
                if child == node:
                    break
            component_id += 1

    for name in graph:
        if name not in indices:
            visit(name)
    return component



def _program_fields(program_id: int, programs: dict[int, ValueProgram]) -> list[FieldSpec]:
    program = programs[program_id]
    if isinstance(program, ScalarProgram):
        return list(program.projection.projected_fields if program.projection is not None else program.codec.fields)
    fields: dict[str, FieldSpec] = {}
    shared = isinstance(program, VariantProgram) and program.layout == "shared"
    children: list[int] = []
    if isinstance(program, RecordProgram):
        children = [child for _, child in program.members]
    elif isinstance(program, TupleProgram):
        children = list(program.items)
    elif isinstance(program, VariantProgram) and not (program.repeated and program.layout == "split"):
        if not program.source_discriminator:
            fields[program.discriminator_column] = FieldSpec(
                name=program.discriminator_column, type=UniversalType.STRING, missing_values=[])
        if program.payload_null_column is not None:
            fields[program.payload_null_column] = FieldSpec(
                name=program.payload_null_column, type=UniversalType.BOOLEAN, missing_values=[])
        children = [branch.program for branch in program.alternatives]
    for child in children:
        for field in _program_fields(child, programs):
            if field.name in fields:
                if not shared:
                    path = programs[child].path
                    pending = [child]
                    while pending:
                        source = programs[pending.pop()]
                        if isinstance(source, ScalarProgram):
                            if any(item.name == field.name for item in source.codec.fields):
                                path = source.path
                                break
                        elif isinstance(source, RecordProgram):
                            pending.extend(item for _, item in reversed(source.members))
                        elif isinstance(source, TupleProgram):
                            pending.extend(reversed(source.items))
                        elif isinstance(source, VariantProgram):
                            pending.extend(item.program for item in reversed(source.alternatives))
                    raise StructureMapError(
                        "column_collision", f"Column {field.name!r} has conflicting mapped sources",
                        path=path, column=field.name,
                        remedy="Declare distinct columns or prefixes, or select Shared/Exact for alternatives",
                    )
                if not same_physical_type(fields[field.name], field):
                    raise StructureMapError(
                        "incompatible_schema", f"Shared column {field.name!r} has incompatible projected types",
                        path=programs[child].path, column=field.name,
                        remedy="Declare compatible physical types with explicit casts or use Prefixed alternatives",
                    )
            fields[field.name] = field
    return list(fields.values())


def prepare(
    model: DerivedModel[T],
    mapping: MappingSpec | None = None,
    *,
    relations: Mapping[str, TypeSpec] | None = None,
    components: ComponentSequence[MappingComponent] = (),
    directions: frozenset[Direction] = frozenset({"decompose", "reconstruct"}),
    validate_native: bool = False,
) -> PreparedMapping[T]:
    mapping = (mapping or MappingSpec()).model_copy(deep=True)
    model = model.model_copy(deep=True)
    relational_schemas = deepcopy(dict(relations)) if relations is not None else None
    directions = frozenset(directions)
    if not directions or not directions <= {"decompose", "reconstruct"}:
        raise StructureMapError("invalid_direction", "Select one or both execution directions",
                                remedy="Use decompose and/or reconstruct")
    implementations = {component.identity: component for component in builtin_components()}
    for component in components:
        if component.identity in implementations:
            raise StructureMapError("duplicate_component", f"Duplicate component {component.identity!r}",
                                    remedy="Supply exactly one implementation per identity")
        implementations[component.identity] = component
    if mapping.root not in (None, model.structure.root):
        raise StructureMapError(
            "unknown_root",
            "Mapping root does not select the derived root",
            path=mapping.root or "",
            remedy="Select the derived model root",
        )
    requested_binding = mapping.defaults.binding
    declared_binding = requested_binding.name if isinstance(requested_binding, DeclaredBinding) else requested_binding
    if declared_binding not in (None, "detected", model.binding):
        raise StructureMapError(
            "binding_mismatch",
            "Requested mapping binding differs from derivation",
            remedy="Derive and prepare with the same binding",
        )
    root_shape = (
        RecordRef(definition=model.structure.root)
        if isinstance(model.structure.root, str)
        else model.structure.root
    )
    root_native = model.root_native
    if root_native is None and isinstance(root_shape, RecordRef):
        root_native = model.native_types.get(root_shape.definition)
    if root_native is None:
        raise StructureMapError(
            "missing_native_root", "The structural root has no native association",
            remedy="Supply root_native when constructing a DerivedModel for a non-record root",
        )
    native_binding = select_binding(root_native, model.binding)
    for direction, capability in (("decompose", "Read"), ("reconstruct", "Construct")):
        if direction in directions and not getattr(native_binding.capabilities, capability):
            raise StructureMapError("unavailable_direction", f"Binding lacks {capability}",
                                    remedy=f"Prepare only directions supported by {model.binding}")

    def _is_pydantic_record(definition: str) -> bool:
        if native_binding.name != "pydantic":
            return False
        native_type = model.native_types.get(definition)
        return native_type is not None and issubclass(native_type, BaseModel)


    record_adapters: dict[str, RecordAdapter] = {}
    if validate_native:
        raise StructureMapError(
            "unavailable_capability",
            f"The {model.binding} binding does not provide native validation",
            owner="binding gap",
            remedy=f"Set validate_native=False; the {model.binding} binding does not provide native validation",
        )
    if mapping.defaults.presence != "normalize":
        path = "presence"
        raise StructureMapError(
            "unavailable_capability",
            f"The requested {path} policy is not available in this delivery",
            owner="binding gap",
            path=path,
            remedy="Use presence='normalize'",
        )
    limited_policies = {"role_discriminator": "separate", "shared_types": "exact"}
    construction = mapping.defaults.construction
    if construction is not None:
        if construction != "validated":
            raise StructureMapError(
                "unavailable_capability",
                "Unvalidated construction is not available in this delivery",
                owner="binding gap",
                path="construction",
                remedy="Use construction='validated' for Pydantic model records or omit the request",
            )
        if any(
            not _is_pydantic_record(definition)
            for definition in model.structure.definitions
        ):
            raise StructureMapError(
                "unavailable_capability",
                "Validated construction is available only for Pydantic model records",
                owner="binding gap",
                path="construction",
                remedy="Use standard-initialization Pydantic records throughout the mapping or omit construction",
            )
    for policy, supported in limited_policies.items():
        if getattr(mapping.defaults, policy) not in (None, supported):
            raise StructureMapError(
                "unavailable_capability",
                f"The declared {policy} policy is not available in this delivery",
                owner="binding gap",
                path=policy,
                remedy=f"Use {policy}={supported!r}",
            )
    if mapping.defaults.variant_layout not in (None, "split", "prefixed", "shared"):
        raise StructureMapError(
            "unavailable_capability",
            f"The declared {mapping.defaults.variant_layout!r} variant layout is unavailable",
            owner="binding gap",
            path="variant_layout",
            remedy="Use variant_layout='split', 'prefixed' or 'shared'",
        )
    if mapping.defaults.open_content == "fragment" and model.binding != "xsdata":
        raise StructureMapError(
            "unavailable_capability", "Fragment open content is available only through xsdata",
            owner="binding gap", path="open_content",
            remedy="Use an xsdata binding or omit the fragment strategy",
        )
    if mapping.defaults.open_content in ("drop", "structured"):
        raise StructureMapError(
            "unavailable_capability",
            f"XML open_content={mapping.defaults.open_content!r} is not supported by xsdata",
            owner="binding gap", path="open_content",
            remedy="Use open_content='fragment' or omit the strategy",
        )
    if mapping.defaults.value_space is not None and model.binding != "xsdata":
        raise StructureMapError(
            "unavailable_capability",
            f"The value_space policy is unavailable for the {model.binding} binding",
            owner="binding gap",
            path="value_space",
            remedy="Omit value_space or use the xsdata binding",
        )

    recursive_component = _components(cast(DerivedModel[object], model))

    def collection_payload(shape: Shape) -> Shape:
        while isinstance(shape, (Nullable, Sequence, Map)):
            shape = shape.item if isinstance(shape, Sequence) else shape.value
        return shape

    def record_targets(shape: Shape) -> set[str] | None:
        shape = collection_payload(shape)
        if isinstance(shape, RecordRef):
            return {shape.definition}
        if isinstance(shape, Alternatives):
            definitions: set[str] = set()
            for choice in shape.choices:
                choice_targets = record_targets(choice.shape)
                if choice_targets is None:
                    return None
                definitions.update(choice_targets)
            return definitions or None
        return None

    def target(path: str, start_shape: Shape | None = None) -> list[tuple[str, Shape]]:
        start = root_shape if start_shape is None else start_shape
        if not path:
            return [("root", start)]
        root_definition = start.definition if isinstance(start, RecordRef) else ""
        candidates: list[tuple[Shape, str]] = [(start, root_definition)]
        for segment in path.split("/"):
            next_candidates: list[tuple[Shape, str]] = []
            pending_candidates = list(reversed(candidates))
            while pending_candidates:
                candidate, source_definition = pending_candidates.pop()
                current = collection_payload(candidate)
                if isinstance(current, RecordRef):
                    members = {
                        member.name: member.shape
                        for member in model.structure.definitions[current.definition].members
                    }
                    if segment in members:
                        next_candidates.append((members[segment], current.definition))
                elif isinstance(current, Alternatives):
                    pending_candidates.extend(
                        (alternative.shape, source_definition) for alternative in reversed(current.choices)
                    )
                elif isinstance(current, Positional) and segment.isdecimal():
                    index = int(segment)
                    if str(index) == segment and index < len(current.items):
                        next_candidates.append((current.items[index], source_definition))
            if not next_candidates:
                raise StructureMapError(
                    "unknown_path",
                    f"Mapping path does not exist at {segment!r}",
                    path=path,
                    remedy="Use declared native member names, transparent collection membership, alternatives and fixed tuple ordinals",
                )
            candidates = next_candidates
        targets: list[tuple[str, Shape]] = []
        for current, source_definition in candidates:
            unwrapped = current.value if isinstance(current, Nullable) else current
            if isinstance(unwrapped, (Sequence, Map)):
                kind = "collection"
            elif isinstance(unwrapped, RecordRef):
                kind = (
                    "link"
                    if recursive_component[source_definition] == recursive_component[unwrapped.definition]
                    else "record"
                )
            else:
                kind = (
                    "scalar"
                    if isinstance(unwrapped, Scalar)
                    else "tuple"
                    if isinstance(unwrapped, Positional)
                    else "variant"
                    if isinstance(unwrapped, Alternatives)
                    else "unsupported"
                )
            target = (kind, current)
            if target not in targets:
                targets.append(target)
        return targets

    def unavailable(path: str, policy: str) -> None:
        raise StructureMapError(
            "unavailable_capability",
            f"The declared {policy} policy is not available in this delivery",
            owner="binding gap",
            path=path,
            remedy={
                "construction": "Use construction='validated' for Pydantic model records or omit the request",
                "presence": "Use presence='normalize'; this delivery does not retain supplied-field presence",
                "positions": "Use positions='strict'; lenient reconstruction is unavailable in Foundation",
                "key": "Use key='generated'; source-backed keys are unavailable in Foundation",
                "ordered": "Use ordered=True; unordered collections are unavailable in Foundation",
                "collection_table": "Use collection_table='separate' and distinct table names",
            }.get(policy, f"No {policy} strategy is available in Foundation; omit the request to use this binding"),
        )

    def validate_override(path: str, override: UseOverride, start_shape: Shape | None = None) -> None:
        targets = target(path, start_shape)
        kind, shape = targets[0]
        payload = collection_payload(shape) if kind == "collection" else shape
        for candidate_kind, candidate_shape in targets[1:]:
            candidate_payload = collection_payload(candidate_shape) if candidate_kind == "collection" else candidate_shape
            applicable = (
                (override.table is None or candidate_kind in ("root", "collection", "link"))
                and (
                    override.column is None
                    or candidate_kind == "scalar"
                    or (candidate_kind == "collection" and isinstance(candidate_payload, Scalar))
                )
                and (
                    override.prefix is None
                    or candidate_kind in ("root", "record", "link", "variant")
                    or (
                        candidate_kind == "collection"
                        and isinstance(candidate_payload, (RecordRef, Alternatives))
                    )
                )
                and not any(
                    getattr(override, policy) is not None and candidate_kind != "collection"
                    for policy in ("key", "positions", "ordered", "collection_table")
                )
                and (
                    override.variant_layout is None
                    or candidate_kind == "variant"
                    or (candidate_kind == "collection" and isinstance(candidate_payload, Alternatives))
                )
                and (
                    override.discriminator is None
                    or candidate_kind == "variant"
                    or (candidate_kind == "collection" and isinstance(candidate_payload, Alternatives))
                )
                and (
                    not override.alternatives
                    or candidate_kind == "variant"
                    or (candidate_kind == "collection" and isinstance(candidate_payload, Alternatives))
                )
            )
            if not applicable:
                raise StructureMapError(
                    "inapplicable_override",
                    "The override is not applicable to every alternative reached by this path",
                    path=path,
                    remedy="Use an override compatible with every matching alternative",
                )
        if override.table is not None and kind not in ("root", "collection", "link"):
            raise StructureMapError(
                "inapplicable_override",
                "Table names apply only to root, collection or recursive link uses",
                path=path,
                remedy="Set table on a use that owns a row; nonrecursive records are inline",
            )
        if override.column is not None and (
            kind not in ("scalar", "collection") or (kind == "collection" and not isinstance(payload, Scalar))
        ):
            raise StructureMapError(
                "inapplicable_override",
                "Column names apply only to scalar payload uses",
                path=path,
                remedy="Set column on a scalar or scalar collection use",
            )
        if override.prefix is not None and not (
            kind in ("root", "record", "link", "variant")
            or (kind == "collection" and isinstance(payload, (RecordRef, Alternatives)))
        ):
            raise StructureMapError(
                "inapplicable_override",
                "Prefixes apply only to record or variant projections",
                path=path,
                remedy="Set prefix on a record or variant use",
            )
        binding = override.binding
        if binding is not None:
            selected = binding.name if isinstance(binding, DeclaredBinding) else binding
            if selected not in ("detected", model.binding):
                raise StructureMapError(
                    "binding_mismatch",
                    "Requested mapping binding differs from derivation",
                    path=path,
                    remedy="Derive and prepare with the same binding",
                )
        for policy, supported in limited_policies.items():
            if getattr(override, policy) not in (None, supported):
                unavailable(path, policy)
        if override.construction is not None:
            if override.construction != "validated":
                unavailable(path, "construction")

            target_definitions: set[str] = set()
            for candidate_kind, candidate_shape in targets:
                candidate_definitions = record_targets(candidate_shape)
                if (
                    candidate_definitions is None
                    or candidate_kind not in ("root", "record", "link", "variant", "collection")
                ):
                    raise StructureMapError(
                        "inapplicable_override",
                        "Construction policies apply only to native record uses",
                        path=path,
                        remedy="Set construction on a Pydantic model record use",
                    )
                target_definitions.update(candidate_definitions)
            if any(not _is_pydantic_record(definition) for definition in target_definitions):
                raise StructureMapError(
                    "unavailable_capability",
                    "Validated construction is available only for Pydantic model records",
                    owner="binding gap",
                    path=path,
                    remedy="Use a standard-initialization Pydantic model record or omit construction",
                )
        if override.open_content is not None:
            if model.binding != "xsdata" or override.open_content not in ("fragment", "reject"):
                unavailable(path, "open_content")
            for _, candidate_shape in targets:
                open_shape = collection_payload(candidate_shape)
                if not isinstance(open_shape, Scalar) or open_shape.type != "fragment":
                    raise StructureMapError(
                        "inapplicable_override", "Open-content policies apply only to native wildcard or mixed fields",
                        path=path, remedy="Set open_content on a native open-content field",
                    )
        for policy in ("key", "positions", "ordered", "collection_table"):
            permitted = ("root", "collection", "link") if policy == "key" else ("collection",)
            if getattr(override, policy) is not None and kind not in permitted:
                raise StructureMapError(
                    "inapplicable_override", f"The {policy} policy does not apply to {kind}",
                    path=path, remedy=f"Set {policy} on one of {permitted!r}",
                )
        if override.cast is not None:
            if not isinstance(override.cast, TypeCast):
                unavailable(path, "cast")
            if any(not isinstance(collection_payload(candidate_shape), Scalar) for _, candidate_shape in targets):
                raise StructureMapError(
                    "inapplicable_override", "Type casts apply to scalar projections",
                    path=path, remedy="Set cast on a scalar field or scalar collection payload",
                )
        if override.presence not in (None, "normalize"):
            unavailable(path, "presence")
        if override.variant_layout is not None and override.variant_layout not in ("split", "prefixed", "shared"):
            unavailable(path, "variant_layout")
        if override.variant_layout is not None and not (
            kind == "variant" or (kind == "collection" and isinstance(payload, Alternatives))
        ):
            raise StructureMapError(
                "inapplicable_override",
                "Variant layouts apply only to alternative uses",
                path=path,
                remedy="Set variant_layout on a union or compound-choice use",
            )
        if override.value_space is not None and model.binding != "xsdata":
            unavailable(path, "value_space")
        if override.discriminator is not None and not (
            kind == "variant" or (kind == "collection" and isinstance(payload, Alternatives))
        ):
            raise StructureMapError(
                "inapplicable_override",
                "Discriminators apply only to alternative uses",
                path=path,
                remedy="Set a discriminator on an alternative use",
            )
        if override.alternatives and not (
            kind == "variant" or (kind == "collection" and isinstance(payload, Alternatives))
        ):
            raise StructureMapError(
                "inapplicable_override",
                "Alternative layouts apply only to alternative uses",
                path=path,
                remedy="Set alternatives on a union or compound-choice use",
            )

    selectors = resolve_selectors(model.structure, mapping)
    for path, override in mapping.overrides.items():
        if not path.startswith("@"):
            validate_override(path, override)
    for selector in selectors:
        validate_override(
            selector.suffix,
            selector.override,
            RecordRef(definition=selector.definition),
        )

    def definitions_at(path: str) -> frozenset[str]:
        return frozenset().union(*(_record_refs(shape) for _, shape in target(path)))

    def definition_context(path: str) -> tuple[tuple[str, str], ...]:
        residuals: list[tuple[str, str]] = []
        for selector in selectors:
            if not selector.suffix:
                continue
            parts = selector.suffix.split("/")
            for consumed in range(len(parts) + 1):
                prefix = "/".join(parts[:consumed])
                if prefix and path != prefix and not path.endswith("/" + prefix):
                    continue
                anchor = path[:-len(prefix)].rstrip("/") if prefix else path
                if selector.definition in definitions_at(anchor):
                    residuals.append((selector.name, "/".join(parts[consumed:])))
        return tuple(residuals)

    class Context:
        def __init__(self, shape: Shape, path: str, table: str, source_definition: str, relative_path: str):
            self.shape, self.path, self.table = shape, path, table
            self.directions = directions
            self.options = effective_override(
                cast(MappingSpec, mapping), selectors, path=path,
                definitions_at=definitions_at, shape=shape,
            ) or UseOverride()
            self.children: dict[str, MappingNode] = {}
            self.codec: EncodingCodec | DecodingCodec | None = None
            self.operation: str | None = None
            self.pending: list[MappingNode] = []

        def field(self, column: str, *, table: str | None = None) -> FieldSpec:
            target_table = table or self.table
            target_table = cast(str, tables.get(target_table, {}).get("physical", target_table))
            schema = relational_schemas.get(target_table) if relational_schemas is not None else None
            if schema is not None:
                for field in schema.fields:
                    if field.name == column:
                        return deepcopy(field)
            raise StructureMapError(
                "missing_field", f"No declared field {target_table}.{column}", path=self.path, table=target_table,
                column=column, remedy="Supply the required table and field in relations",
            )

        def apply(self, node: MappingNode) -> None:
            self.pending.append(node)

        def select(self, operation: str, expected: type, children: Mapping[str, MappingNode],
                   options: UseOverride | None) -> None:
            shape = self.shape.value if isinstance(self.shape, Nullable) else self.shape
            if self.operation is not None or not isinstance(shape, expected):
                raise StructureMapError(
                    "invalid_component", "Each use must contribute exactly one compatible structural operation",
                    path=self.path, remedy="Compose one operation matching the declared shape",
                )
            allowed: set[str]
            if isinstance(shape, RecordRef):
                allowed = {member.name for member in model.structure.definitions[shape.definition].members}
            elif isinstance(shape, Sequence):
                allowed = {"item"}
            elif isinstance(shape, Map):
                allowed = {"key", "value"}
            elif isinstance(shape, Positional):
                allowed = {str(index) for index in range(len(shape.items))}
            elif isinstance(shape, Alternatives):
                allowed = {branch.key for branch in shape.choices}
            else:
                allowed = set()
            unexpected = set(children) - allowed
            if unexpected:
                name = sorted(unexpected)[0]
                raise StructureMapError("unknown_child", f"Unknown mapped child {name!r}",
                                        path="/".join(filter(None, (self.path, name))),
                                        remedy=f"Use declared children {sorted(allowed)!r}")
            if options is not None:
                supplied = options.model_dump(exclude_unset=True)
                selected = self.options.model_dump(exclude_unset=True)
                for key, value in supplied.items():
                    if key in selected and selected[key] != value:
                        raise StructureMapError(
                            "conflicting_mapping", f"Component and selector disagree on {key!r}",
                            path=self.path, remedy="Remove the conflicting declaration or make both choices agree",
                        )
                self.options = UseOverride.model_validate({**selected, **supplied})
                validate_override(self.path, self.options)
            self.operation, self.children = operation, dict(children)

        def value(self, *, codec: EncodingCodec | DecodingCodec | None = None,
                  options: UseOverride | None = None) -> None:
            self.select("value", Scalar, {}, options)
            self.codec = codec

        def record(self, children: Mapping[str, MappingNode], *, options: UseOverride | None = None) -> None:
            self.select("record", RecordRef, children, options)

        def sequence(self, item: MappingNode | None = None, *, options: UseOverride | None = None) -> None:
            self.select("sequence", Sequence, {"item": item} if item is not None else {}, options)

        def map(self, *, key: MappingNode | None = None, value: MappingNode | None = None,
                options: UseOverride | None = None) -> None:
            children = {}
            if key is not None:
                children["key"] = key
            if value is not None:
                children["value"] = value
            self.select("map", Map, children, options)

        def positional(self, items: Mapping[int, MappingNode], *, options: UseOverride | None = None) -> None:
            self.select("positional", Positional, {str(index): node for index, node in items.items()}, options)

        def alternatives(self, branches: Mapping[str, MappingNode], *, options: UseOverride | None = None) -> None:
            self.select("alternatives", Alternatives, branches, options)

    def configure(
        shape: Shape,
        path: str,
        table: str,
        node: MappingNode | None,
        source_definition: str = "",
        relative_path: str = "",
    ) -> Context:
        context = Context(shape, path, table, source_definition, relative_path)
        context.apply(node or MappingUse(component=default_component(shape).identity))
        delegated: set[str] = set()
        while context.pending:
            current = resolve_mapping_node(context.pending.pop(), mapping.definitions, path=path)
            if current is None:
                continue
            signature = current.model_dump_json()
            if signature in delegated:
                raise StructureMapError(
                    "mapping_reference_cycle", f"Component delegation repeats {current.component!r} without structural progress",
                    path=path, remedy="Delegate to a structural operation rather than the same mapping use",
                )
            delegated.add(signature)
            implementation = implementations.get(current.component)
            if implementation is None:
                raise StructureMapError("missing_component", f"Missing component {current.component!r}", path=path,
                                        remedy="Supply its implementation explicitly through components")
            if not directions <= implementation.directions:
                raise StructureMapError("unavailable_direction", f"Component {current.component!r} lacks requested directions",
                                        path=path, remedy="Prepare only supported directions or supply a capable component")
            implementation.prepare(current, context)
        if context.operation is None:
            raise StructureMapError("invalid_component", "Component contributed no structural operation",
                                    path=path, remedy="Use a context composition operation")
        return context

    naming = mapping.defaults.naming
    programs: dict[int, ValueProgram] = {}
    next_program = 0
    tables: dict[str, dict[str, object]] = {}
    roles: dict[tuple[object, ...], str] = {}
    pending: deque[Callable[[], None]] = deque()
    layout_overrides = tuple(path for path in sorted(mapping.overrides) if not path.startswith("@"))

    def norm(value: str) -> str:
        return _snake(value) if naming.column_naming == "snake_case" else value

    def role_fragment(value: str) -> str:
        return norm(re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_"))

    def join(path: str, segment: str) -> str:
        return f"{path}/{segment}" if path else segment

    def reserve() -> int:
        nonlocal next_program
        program_id = next_program
        next_program += 1
        return program_id

    def emit(program: ValueProgram, destination: int | None = None) -> int:
        program_id = reserve() if destination is None else destination
        programs[program_id] = program
        return program_id

    def record_program(
        definition: str,
        path: str,
        authored_path: str,
        prefix: str,
        *,
        family: str,
        owner: str,
        namespace: str | None = None,
        nullable: bool = False,
        role_suffix: str = "",
        destination: int | None = None,
        node: MappingNode | None = None,
        contribution: Context | None = None,
    ) -> int:
        contribution = contribution or configure(
            Nullable(value=RecordRef(definition=definition)) if nullable else RecordRef(definition=definition),
            authored_path, owner, node, definition, "",
        )
        if contribution.options.prefix is not None:
            prefix = contribution.options.prefix
        if definition not in record_adapters:
            record_adapters[definition] = native_binding.prepare_record(
                model.native_types[definition], model.structure.definitions[definition], path=authored_path
            )
        definition_model = model.structure.definitions[definition]
        record_namespace = (
            definition_model.xml.namespace
            if definition_model.xml is not None and definition_model.xml.namespace is not None
            else namespace
        )
        members = definition_model.members
        children = tuple(
            (
                member.name,
                compile_shape(
                    member.shape,
                    join(path, member.name),
                    join(authored_path, member.name),
                    "_".join(filter(None, (prefix, norm(member.xml.name if member.xml and member.xml.name else member.name)))),
                    family=family,
                    owner=owner,
                    source_definition=definition,
                    relative_path=member.name,
                    format=member.xml.format if member.xml else None,
                    namespace=record_namespace,
                    role_suffix=role_suffix,
                    field_context=(model.native_types[definition], member.name, record_namespace),
                    node=contribution.children.get(member.name),
                ),
            )
            for member in members
        )
        return emit(RecordProgram(authored_path, definition, children, nullable), destination)

    def row_table(
        payload: Shape | None,
        path: str,
        authored_path: str,
        *,
        family: str,
        owner: str,
        source_definition: str,
        relative_path: str,
        kind: Literal["single", "sequence", "map"],
        nullable_member: bool,
        anonymous_parent: str | None = None,
        role_suffix: str = "",
        ignore_table_override: bool = False,
        table_override: str | None = None,
        prefix_override: str | None = None,
        discriminator_column: str | None = None,
        discriminator_value: str | None = None,
        payload_null_column: str | None = None,
        position_group: str | None = None,
        namespace: str | None = None,
        format: str | None = None,
        field_context: tuple[type, str, str | None] | None = None,
        node: MappingNode | None = None,
        options: UseOverride | None = None,
        key_node: MappingNode | None = None,
        payload_contribution: Context | None = None,
    ) -> str:
        override = options or effective_override(
            mapping, selectors, path=authored_path, definitions_at=definitions_at, shape=payload,
        )
        # Context contains only suffixes of finite authored selectors and their
        # declaration identities. Runtime depth is never an interning key.
        context = tuple(
            (selector[len(authored_path) :].lstrip("/"), selector)
            for selector in layout_overrides
            if selector == authored_path or selector.startswith(authored_path + "/")
        )
        role = (family, source_definition, relative_path, kind, role_suffix, anonymous_parent, context,
                definition_context(authored_path),
                override.model_dump_json() if override is not None else None,
                node.model_dump_json() if node is not None else None)
        if role in roles:
            table = roles[role]
            cast(set[str], tables[table]["parents"]).add(owner)
            return table
        shared = override.collection_table if override is not None else None
        if override is not None and isinstance(shared, SharedCollection) and override.table not in (None, shared.table):
            raise StructureMapError(
                "conflicting_override", "table and SharedCollection.table select different targets",
                path=authored_path, remedy="Declare the same table target or remove one explicit selection",
            )
        if anonymous_parent is not None:
            table = anonymous_parent + "__item"
        elif table_override is not None and not isinstance(shared, SharedCollection):
            table = table_override
        elif (not ignore_table_override and override is not None and override.table is not None
              and not isinstance(shared, SharedCollection)):
            table = override.table
        else:
            table = "__".join(norm(part) for part in path.split("/"))
            role_name = "__".join(
                role_fragment(part.split(":", 1)[1])
                for part in role_suffix.split("|")
                if part.startswith("variant:")
            )
            if role_name:
                table += "__" + role_name
        if table in tables:
            prior = tables[table]["path"]
            null_role = role_suffix.endswith("|variant:$null")
            raise StructureMapError(
                "table_collision",
                f"Multiple uses at {prior!r} and {authored_path!r} resolve to table {table!r}",
                path=authored_path,
                table=table,
                remedy=(
                    "Choose an explicit table for alternatives['$null']; the real alternative's "
                    "default table name collides with the null role"
                    if null_role
                    else "Choose distinct explicit table names; shared tables are unavailable in Foundation"
                ),
            )
        payload_id = reserve()
        roles[role] = table
        tables[table] = {
            "payload_null_column": payload_null_column,
            "path": authored_path,
            "parents": {owner},
            "program": payload_id,
            "kind": kind,
            "nullable_member": nullable_member,
            "position_group": position_group,
            "discriminator_column": discriminator_column,
            "discriminator_value": discriminator_value,
            "key_node": key_node,
        }
        tables[table]["physical"] = (
            (table_override or shared.table) if isinstance(shared, SharedCollection) else table
        )
        tables[table]["role"] = (shared.role or authored_path) if isinstance(shared, SharedCollection) else None
        tables[table]["options"] = override or UseOverride()

        def compile_payload() -> None:
            if payload is None:
                emit(NullProgram(authored_path), payload_id)
                return
            branch_shape = payload.value if isinstance(payload, Nullable) else payload
            if isinstance(branch_shape, RecordRef):
                next_family = (
                    family
                    if recursive_component.get(source_definition) == recursive_component[branch_shape.definition]
                    else path
                )
                prefix = (
                    prefix_override
                    if prefix_override is not None
                    else override.prefix
                    if override is not None and override.prefix is not None
                    else ""
                )
                record_program(
                    branch_shape.definition,
                    path,
                    authored_path,
                    prefix,
                    family=next_family,
                    owner=table,
                    namespace=namespace,
                    destination=payload_id,
                    node=node,
                )
            else:
                nested = isinstance(branch_shape, (Sequence, Map))
                scalar_column = "value"
                if isinstance(branch_shape, Scalar) and prefix_override:
                    scalar_column = f"{prefix_override}_{scalar_column}"
                compile_shape(
                    payload,
                    join(path, "item") if nested else path,
                    authored_path,
                    scalar_column if isinstance(branch_shape, Scalar) else "",
                    family=family,
                    owner=table,
                    source_definition=source_definition,
                    relative_path=join(relative_path, "item") if nested else relative_path,
                    format=format,
                    anonymous_parent=table if nested else None,
                    namespace=namespace,
                    role_suffix=role_suffix,
                    branch_prefix=prefix_override,
                    override_applied=discriminator_column is not None,
                    destination=payload_id,
                    field_context=field_context,
                    node=node,
                    contribution=payload_contribution,
                )

        pending.append(compile_payload)
        return table

    def compile_shape(
        shape: Shape,
        path: str,
        authored_path: str,
        column: str,
        *,
        family: str,
        owner: str,
        source_definition: str,
        relative_path: str,
        format: str | None = None,
        anonymous_parent: str | None = None,
        namespace: str | None = None,
        branch_prefix: str | None = None,
        role_suffix: str = "",
        ignore_table_override: bool = False,
        override_applied: bool = False,
        destination: int | None = None,
        field_context: tuple[type, str, str | None] | None = None,
        node: MappingNode | None = None,
        contribution: Context | None = None,
    ) -> int:
        nullable = isinstance(shape, Nullable)
        value_shape = shape.value if isinstance(shape, Nullable) else shape
        contribution = contribution or configure(shape, authored_path, owner, node, source_definition, relative_path)
        override = None if override_applied else contribution.options
        if isinstance(value_shape, Scalar):
            _validate_facets(value_shape.facets, authored_path)
            scalar_column = override.column if override is not None and override.column is not None else column
            native_type = model.native_types.get(value_shape.native_type) if value_shape.native_type else None
            scalar_override = contribution.options
            policy = (
                scalar_override.value_space
                if scalar_override is not None and scalar_override.value_space is not None
                else mapping.defaults.value_space
            )
            if value_shape.type == "fragment":
                open_policy = (
                    scalar_override.open_content
                    if scalar_override is not None and scalar_override.open_content is not None
                    else mapping.defaults.open_content
                )
                if open_policy == "reject":
                    raise StructureMapError(
                        "unsupported_open_content", "Native open content is rejected by policy",
                        owner="mapping restriction", path=authored_path,
                        remedy="Use open_content='fragment' at this path or remove the open-content field",
                    )
            codec = contribution.codec or native_binding.prepare_scalar(
                value_shape,
                native_type=native_type,
                format=value_shape.format if value_shape.format is not None else format,
                column=scalar_column,
                policy=policy,
                metadata_namespace=naming.metadata_namespace,
                path=authored_path,
                field_context=field_context,
            )
            for direction, methods in (("decompose", ("encode",)), ("reconstruct", ("check", "decode"))):
                if direction in directions and any(not callable(getattr(codec, method, None)) for method in methods):
                    raise StructureMapError("unavailable_direction", f"Codec lacks {direction} operations",
                                            path=authored_path, remedy="Supply the requested codec operations or prepare one direction")
            projection = (
                prepare_value_projection(codec.fields, scalar_override.cast, path=authored_path)
                if isinstance(scalar_override.cast, TypeCast) else None
            )
            return emit(ScalarProgram(authored_path, codec, nullable, projection), destination)
        if isinstance(value_shape, RecordRef):
            if recursive_component.get(source_definition) == recursive_component[value_shape.definition]:
                table = row_table(
                    value_shape,
                    path,
                    authored_path,
                    family=family,
                    owner=owner,
                    source_definition=source_definition,
                    relative_path=relative_path,
                    kind="single",
                    nullable_member=False,
                    role_suffix=role_suffix,
                    ignore_table_override=ignore_table_override,
                    prefix_override=branch_prefix,
                    namespace=namespace,
                    node=node, options=override,
                )
                return emit(LinkProgram(authored_path, table, nullable), destination)
            prefix = (
                branch_prefix
                if branch_prefix is not None
                else override.prefix
                if override is not None and override.prefix is not None
                else column
            )
            return record_program(
                value_shape.definition,
                path,
                authored_path,
                prefix,
                family=path,
                owner=owner,
                namespace=namespace,
                nullable=nullable,
                role_suffix=role_suffix,
                destination=destination,
                contribution=contribution,
            )
        if isinstance(value_shape, Alternatives):
            return compile_variant(
                value_shape,
                path,
                authored_path,
                column,
                family=family,
                owner=owner,
                source_definition=source_definition,
                relative_path=relative_path,
                namespace=namespace,
                nullable=nullable,
                nullable_member=False,
                repeated=False,
                container="list",
                role_suffix=role_suffix,
                override_applied=override_applied,
                inherited_prefix=branch_prefix,
                destination=destination,
                field_context=field_context,
                contribution=contribution,
            )
        if isinstance(value_shape, (Sequence, Map)):
            if isinstance(value_shape, Map):
                if not isinstance(value_shape.key, Scalar) or value_shape.key.type != "str":
                    raise StructureMapError(
                        "unavailable_construct",
                        "Maps require string keys",
                        path=authored_path,
                        owner="binding gap",
                        remedy="Declare string map keys; other key types are unavailable in Foundation",
                    )
                member = value_shape.value
                kind: Literal["single", "sequence", "map"] = "map"
            else:
                member = value_shape.item
                kind = "sequence"
            inner = member.value if isinstance(member, Nullable) else member
            marked = isinstance(member, Nullable) and not isinstance(inner, Scalar)
            item_contribution: Context | None = None
            if isinstance(inner, Alternatives):
                item_contribution = configure(
                    inner, authored_path, owner,
                    contribution.children.get("value" if isinstance(value_shape, Map) else "item"),
                    source_definition, relative_path,
                )
                inherited = override.model_dump(exclude_unset=True) if override is not None else {}
                selected = item_contribution.options.model_dump(exclude_unset=True)
                for option in inherited.keys() & selected.keys():
                    if inherited[option] != selected[option]:
                        raise StructureMapError(
                            "conflicting_mapping", f"Collection and item disagree on {option!r}",
                            path=authored_path, remedy="Use consistent collection and item configuration",
                        )
                item_contribution.options = UseOverride.model_validate({**inherited, **selected})
                selected_layout = (
                    item_contribution.options.variant_layout
                    or (override.variant_layout if override is not None else None)
                    or mapping.defaults.variant_layout or "split"
                )
                if selected_layout == "split":
                    return compile_variant(
                        inner,
                        path,
                        authored_path,
                        column,
                        family=family,
                        owner=owner,
                        source_definition=source_definition,
                        relative_path=relative_path,
                        namespace=namespace,
                        nullable=nullable,
                        nullable_member=isinstance(member, Nullable),
                        repeated=True,
                        container=value_shape.container if isinstance(value_shape, Sequence) else "list",
                        map_values=isinstance(value_shape, Map),
                        role_suffix=role_suffix,
                        override_applied=override_applied,
                        field_context=field_context,
                        destination=destination,
                        key_node=contribution.children.get("key"),
                        contribution=item_contribution,
                    )
            table = row_table(
                inner if marked else member,
                path,
                authored_path,
                family=family,
                owner=owner,
                source_definition=source_definition,
                relative_path=relative_path,
                kind=kind,
                nullable_member=marked,
                anonymous_parent=anonymous_parent,
                role_suffix=role_suffix,
                ignore_table_override=ignore_table_override,
                prefix_override=override.prefix if override is not None else None,
                namespace=namespace,
                format=format,
                field_context=field_context,
                node=contribution.children.get("value" if isinstance(value_shape, Map) else "item"),
                options=override,
                key_node=contribution.children.get("key"),
                payload_contribution=item_contribution,
            )
            edge: ValueProgram = (
                MapProgram(authored_path, table, nullable)
                if isinstance(value_shape, Map)
                else SequenceProgram(authored_path, table, nullable, value_shape.container)
            )
            return emit(edge, destination)

        if isinstance(value_shape, Positional):
            items = tuple(
                compile_shape(
                    item,
                    join(path, str(index)),
                    join(authored_path, str(index)),
                    f"{column}_{index}",
                    family=family,
                    owner=owner,
                    source_definition=source_definition,
                    relative_path=join(relative_path, str(index)),
                    namespace=namespace,
                    role_suffix=role_suffix,
                    field_context=field_context,
                    node=contribution.children.get(str(index)),
                )
                for index, item in enumerate(value_shape.items)
            )
            return emit(TupleProgram(authored_path, items, nullable), destination)
        raise StructureMapError(
            "unavailable_construct",
            "This structural shape has no executor in this delivery",
            path=authored_path,
            owner="binding gap",
            remedy="Use scalar, record, sequence, map, fixed tuple, or declared variant shapes",
        )

    source_variant_fields: dict[int, str] = {}

    def compile_variant(
        alternatives: Alternatives,
        path: str,
        authored_path: str,
        column: str,
        *,
        family: str,
        owner: str,
        source_definition: str,
        relative_path: str,
        namespace: str | None,
        nullable: bool,
        nullable_member: bool,
        repeated: bool,
        container: Literal["list", "tuple"],
        map_values: bool = False,
        role_suffix: str,
        override_applied: bool = False,
        inherited_prefix: str | None = None,
        destination: int | None,
        field_context: tuple[type, str, str | None] | None = None,
        contribution: Context | None = None,
        key_node: MappingNode | None = None,
    ) -> int:
        contribution = contribution or configure(
            alternatives, authored_path, owner, None, source_definition, relative_path,
        )
        override = None if override_applied else contribution.options
        layout = (
            override.variant_layout
            if override is not None and override.variant_layout is not None
            else mapping.defaults.variant_layout or "split"
        )
        if layout not in ("split", "prefixed", "shared"):
            raise StructureMapError(
                "unavailable_capability",
                f"Variant layout {layout!r} is unavailable",
                owner="binding gap",
                path=authored_path,
                remedy="Use variant_layout='split', 'prefixed' or 'shared'",
            )
        keys = tuple(alternative.key for alternative in alternatives.choices)
        if not keys or len(set(keys)) != len(keys):
            raise StructureMapError(
                "ambiguous_variant",
                "Variant alternatives require distinct nonempty keys",
                path=authored_path,
                owner="mapping restriction",
                remedy="Give every native alternative a unique declared key",
            )
        declared_discriminator = override.discriminator if override is not None else None
        source_discriminator = isinstance(declared_discriminator, SourceDiscriminator)
        source_domains: dict[str, tuple[Cell, ...]] = {}
        if isinstance(declared_discriminator, SourceDiscriminator):
            seen_tags: dict[tuple[type, Cell], str] = {}
            for alternative in alternatives.choices:
                branch_shape = alternative.shape.value if isinstance(alternative.shape, Nullable) else alternative.shape
                member = next(
                    (member for member in model.structure.definitions[branch_shape.definition].members
                     if member.name == declared_discriminator.field),
                    None,
                ) if isinstance(branch_shape, RecordRef) else None
                if member is None or not isinstance(member.shape, Scalar) or not member.shape.allowed_values:
                    raise StructureMapError(
                        "invalid_discriminator", f"Alternative {alternative.key!r} lacks a finite scalar tag domain",
                        path=authored_path,
                        remedy=f"Declare {declared_discriminator.field!r} as a non-null homogeneous Literal field in every branch",
                    )
                domain = cast(tuple[Cell, ...], member.shape.allowed_values)
                for tag in domain:
                    identity = (type(tag), tag)
                    if identity in seen_tags:
                        raise StructureMapError(
                            "ambiguous_discriminator",
                            f"Tag {tag!r} occurs in {seen_tags[identity]!r} and {alternative.key!r}",
                            path=authored_path, remedy="Declare disjoint finite source tag domains",
                        )
                    seen_tags[identity] = alternative.key
                source_domains[alternative.key] = domain
        discriminator = (
            declared_discriminator if isinstance(declared_discriminator, RecordedDiscriminator) else None
        )
        nested_variant_name = "__".join(
            role_fragment(part.split(":", 1)[1])
            for part in role_suffix.split("|")
            if part.startswith("variant:")
        )
        default_discriminator_column = naming.metadata_namespace + "variant"
        if nested_variant_name and not (repeated and layout == "split"):
            default_discriminator_column += "_" + nested_variant_name
        discriminator_column = (
            discriminator.column
            if discriminator is not None and discriminator.column is not None
            else default_discriminator_column
        )
        tags = (
            dict(discriminator.values)
            if discriminator is not None and discriminator.values is not None
            else {key: key for key in keys}
        )
        domains: dict[str, tuple[Cell, ...]] = source_domains or {key: (tag,) for key, tag in tags.items()}
        if set(tags) != set(keys) or len(set(tags.values())) != len(keys):
            raise StructureMapError(
                "invalid_discriminator",
                "Discriminator values must map every declared alternative to a unique tag",
                path=authored_path,
                owner="mapping restriction",
                remedy=f"Map exactly these alternative keys: {list(keys)!r}",
            )
        adapter = native_binding.prepare_variant(
            alternatives.choices,
            native_types=model.native_types,
            namespace=namespace,
            path=authored_path,
        )
        layouts = override.alternatives if override is not None else {}
        allowed_layout_keys = set(keys)
        has_null_role = repeated and layout == "split" and nullable_member
        if has_null_role:
            allowed_layout_keys.add("$null")
        unknown = set(layouts) - allowed_layout_keys
        if unknown:
            raise StructureMapError(
                "unknown_alternative",
                f"Unknown alternative layout keys {sorted(unknown)!r}",
                path=authored_path,
                owner="mapping restriction",
                remedy=f"Use one of {sorted(allowed_layout_keys)!r}",
            )
        null_layout = layouts.get("$null")
        if null_layout is not None and not has_null_role:
            raise StructureMapError(
                "inapplicable_override",
                "The $null layout requires a nullable repeated Split alternative",
                path=authored_path,
                owner="mapping restriction",
                remedy="Declare a nullable collection member and use Split, or remove $null",
            )
        if null_layout is not None and null_layout.prefix is not None:
            raise StructureMapError(
                "inapplicable_override",
                "The $null role has no payload columns to prefix",
                path=authored_path,
                owner="mapping restriction",
                remedy="Configure only the $null table name",
            )
        base_table = override.table if override is not None and override.table is not None else (
            "__".join(norm(part) for part in path.split("/"))
        )
        if override is not None and isinstance(override.collection_table, SharedCollection):
            base_table = override.collection_table.table
        if nested_variant_name and (override is None or override.table is None):
            base_table += "__" + nested_variant_name
        branches: list[VariantBranch] = []
        payload_null_column = (
            f"{naming.metadata_namespace}variant_null_"
            + ("_".join(norm(part) for part in authored_path.split("/") if part) or "root")
            if any(isinstance(item.shape, Nullable) for item in alternatives.choices)
            else None
        )
        for alternative in alternatives.choices:
            key = alternative.key
            alternative_layout = layouts.get(key)
            if alternative_layout is not None and alternative_layout.table is not None and not (
                repeated and layout == "split"
            ):
                raise StructureMapError(
                    "inapplicable_override",
                    f"Alternative table override for {key!r} applies only to repeated Split alternatives",
                    path=authored_path,
                    owner="mapping restriction",
                    remedy="Use table in a repeated Split mapping or use prefix for inline layouts",
                )
            local_key = key.rsplit("}", 1)[-1]
            default_prefix = norm(local_key)
            if layout == "shared":
                default_prefix = ""
            branch_prefix = (
                alternative_layout.prefix
                if alternative_layout is not None and alternative_layout.prefix is not None
                else "_".join(
                    filter(
                        None,
                        (
                            override.prefix if override is not None and override.prefix is not None else inherited_prefix,
                            default_prefix,
                        ),
                    )
                )
            )
            branch_format = alternative.xml.format if alternative.xml is not None else None
            branch_shape = alternative.shape.value if isinstance(alternative.shape, Nullable) else alternative.shape
            branch_nullable = isinstance(alternative.shape, Nullable)
            if repeated and layout == "split":
                table_name = (
                    alternative_layout.table
                    if alternative_layout is not None and alternative_layout.table is not None
                    else f"{base_table}__{default_prefix}"
                )
                table = row_table(
                    branch_shape,
                    path,
                    authored_path,
                    family=family,
                    owner=owner,
                    source_definition=source_definition,
                    relative_path=relative_path,
                    kind="map" if map_values else "sequence",
                    nullable_member=False,
                    role_suffix=f"{role_suffix}|variant:{key}",
                    table_override=table_name,
                    prefix_override=branch_prefix,
                    discriminator_column=None if source_discriminator else discriminator_column,
                    discriminator_value=None if source_discriminator else tags[key],
                    payload_null_column=payload_null_column,
                    position_group=f"{family}:{source_definition}:{relative_path}:{authored_path}:{role_suffix}",
                    namespace=namespace,
                    format=branch_format,
                    field_context=field_context,
                    node=contribution.children.get(key), options=override,
                    key_node=key_node,
                )
                branch_program = cast(int, tables[table]["program"])
                branches.append(VariantBranch(key, tags[key], branch_program, table, branch_nullable, tags=domains[key]))
            else:
                branch_column = (
                    "_".join(filter(None, (branch_prefix, column or "value")))
                    if isinstance(branch_shape, Scalar)
                    else ""
                )
                branch_program = compile_shape(
                    branch_shape,
                    path,
                    authored_path,
                    branch_column,
                    family=family,
                    owner=owner,
                    source_definition=source_definition,
                    relative_path=relative_path,
                    format=branch_format,
                    namespace=namespace,
                    branch_prefix=branch_prefix,
                    role_suffix=f"{role_suffix}|variant:{key}",
                    ignore_table_override=True,
                    override_applied=True,
                    field_context=field_context,
                    node=contribution.children.get(key),
                )
                branches.append(VariantBranch(
                    key, tags[key], branch_program, nullable_payload=branch_nullable, tags=domains[key]))
        null_table: str | None = None
        if has_null_role:
            null_table = (
                null_layout.table
                if null_layout is not None and null_layout.table is not None
                else f"{base_table}__null"
            )
            null_table = row_table(
                None,
                path,
                authored_path,
                family=family,
                owner=owner,
                source_definition=source_definition,
                relative_path=relative_path,
                kind="map" if map_values else "sequence",
                nullable_member=True,
                role_suffix=f"{role_suffix}|variant:$null",
                table_override=null_table,
                discriminator_column=None if source_discriminator else discriminator_column,
                discriminator_value=None,
                position_group=f"{family}:{source_definition}:{relative_path}:{authored_path}:{role_suffix}",
                namespace=namespace,
                key_node=key_node,
                options=override,
            )
        variant_id = emit(
            VariantProgram(
                path=authored_path,
                adapter=adapter,
                discriminator_column=discriminator_column,
                alternatives=tuple(branches),
                layout=layout,
                nullable=nullable,
                nullable_member=nullable_member,
                repeated=repeated,
                container=container,
                null_table=null_table,
                map_values=map_values,
                payload_null_column=payload_null_column,
                source_discriminator=source_discriminator,
            ),
            destination,
        )
        if isinstance(declared_discriminator, SourceDiscriminator):
            source_variant_fields[variant_id] = declared_discriminator.field
        return variant_id

    root_definition = root_shape.definition if isinstance(root_shape, RecordRef) else ""
    root_contribution = configure(root_shape, "", "root", mapping.use, root_definition, "")
    root_override = root_contribution.options
    root_name = root_override.table if root_override and root_override.table is not None else "root"
    root_prefix = root_override.prefix if root_override and root_override.prefix is not None else ""
    root_id = reserve()
    tables[root_name] = {
        "path": "",
        "parents": {""},
        "program": root_id,
        "kind": "root",
        "nullable_member": False,
        "position_group": None,
        "discriminator_column": None,
        "discriminator_value": None,
        "payload_null_column": None,
        "physical": root_name,
        "role": None,
        "options": root_override or UseOverride(),
    }
    if isinstance(root_shape, RecordRef):
        record_program(root_shape.definition, "", "", root_prefix, family="", owner=root_name,
                       destination=root_id, contribution=root_contribution)
    else:
        compile_shape(
            root_shape, "", "", root_prefix or "value",
            family="", owner=root_name, source_definition="", relative_path="",
            anonymous_parent=root_name, ignore_table_override=True, destination=root_id,
            contribution=root_contribution,
        )
    # Discover sibling row roles before descending: recursive children must not
    # lend their traversal depth to an ordinary sibling's default table name.
    while pending:
        pending.popleft()()
    for variant_id, source_field in source_variant_fields.items():
        variant = cast(VariantProgram, programs[variant_id])
        source_branches: list[VariantBranch] = []
        for branch in variant.alternatives:
            record = programs[branch.program]
            scalar = next(
                (programs[child] for name, child in record.members if name == source_field), None
            ) if isinstance(record, RecordProgram) else None
            if not isinstance(scalar, ScalarProgram) or len(scalar.codec.fields) != 1:
                raise StructureMapError(
                    "invalid_discriminator", "Source discriminator requires one retained scalar column",
                    path=variant.path, remedy="Use a single-column Literal field or a recorded discriminator",
                )
            if scalar.projection is not None:
                raise StructureMapError(
                    "invalid_discriminator", "Source discriminator values cannot be cast",
                    path=scalar.path, remedy="Remove this cast or select a recorded discriminator",
                )
            source_branches.append(replace(branch, source_column=scalar.codec.fields[0].name))
        programs[variant_id] = replace(
            variant, alternatives=tuple(source_branches),
            discriminator_column=cast(str, source_branches[0].source_column),
        )
    def map_codec_for(
        descriptor: dict[str, object], layout: RowLayout, name: str,
    ) -> EncodingCodec | DecodingCodec | None:
        if descriptor["kind"] != "map":
            return None
        key_shape = Scalar(type="str")
        key_context = configure(key_shape, cast(str, descriptor["path"]), name,
                                cast(MappingNode | None, descriptor.get("key_node")))
        codec = key_context.codec or native_binding.prepare_scalar(
            key_shape, native_type=str, format=None, column=cast(str, layout.map_key),
            policy=None, path=cast(str, descriptor["path"]),
        )
        if (len(codec.fields) != 1 or codec.fields[0].name != layout.map_key
                or codec.fields[0].type != UniversalType.STRING):
            raise StructureMapError(
                "incompatible_schema", "Map key codecs require one declared STRING field",
                table=name, remedy="Use the layout map_key column with a STRING codec",
            )
        for direction, methods in (("decompose", ("encode",)), ("reconstruct", ("check", "decode"))):
            if direction in directions and any(not callable(getattr(codec, method, None)) for method in methods):
                raise StructureMapError(
                    "unavailable_direction", f"Map key codec lacks {direction}",
                    table=name, remedy="Supply requested map key operations or prepare one direction",
                )
        return codec

    plans, programs = finalize_tables(
        tables, programs, mapping, relational_schemas,
        fields_for=lambda program_id: _program_fields(program_id, programs), map_codec_for=map_codec_for,
    )
    return PreparedMapping(
        root=root_id,
        programs=programs,
        root_table=root_name,
        root_type=cast(type[T], root_native),
        tables=plans,
        native_types=model.native_types,
        binding=native_binding,
        record_adapters=record_adapters,
        capabilities=native_binding.capabilities,
        directions=directions,
    )
