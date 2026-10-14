from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeVar, cast

from mountainash.typespec.spec import FieldSpec, TypeSpec
from mountainash.typespec.universal_types import UniversalType

from mountainash_structuremap.bindings.contracts import RecordAdapter
from mountainash_structuremap.bindings.selection import select_binding
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
from mountainash_structuremap.mapping import DeclaredBinding, MappingSpec, RecordedDiscriminator, SourceDiscriminator
from mountainash_structuremap.prepared import (
    LinkProgram,
    MapProgram,
    NullProgram,
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
    TablePlan,
    TupleProgram,
    ValueProgram,
    VariantBranch,
    VariantProgram,
)

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
        return list(program.codec.fields)
    if isinstance(program, RecordProgram):
        return [field for _, child in program.members for field in _program_fields(child, programs)]
    if isinstance(program, TupleProgram):
        return [field for child in program.items for field in _program_fields(child, programs)]
    if isinstance(program, VariantProgram) and not (program.repeated and program.layout == "split"):
        fields = [FieldSpec(name=program.discriminator_column, type=UniversalType.STRING, missing_values=[])]
        if program.payload_null_column is not None:
            fields.append(FieldSpec(name=program.payload_null_column, type=UniversalType.BOOLEAN, missing_values=[]))
        fields.extend(
            field
            for branch in program.alternatives
            for field in _program_fields(branch.program, programs)
        )
        return fields
    return []



def _program_scalar_paths(program_id: int, programs: dict[int, ValueProgram]) -> list[tuple[str, str]]:
    program = programs[program_id]
    if isinstance(program, ScalarProgram):
        return [(field.name, program.path) for field in program.codec.fields]
    if isinstance(program, RecordProgram):
        return [item for _, child in program.members for item in _program_scalar_paths(child, programs)]
    if isinstance(program, TupleProgram):
        return [item for child in program.items for item in _program_scalar_paths(child, programs)]
    if isinstance(program, VariantProgram) and not (program.repeated and program.layout == "split"):
        paths = [(program.discriminator_column, f"{program.path}#variant")]
        if program.payload_null_column is not None:
            paths.append((program.payload_null_column, f"{program.path}#variant-null"))
        paths.extend(
            item
            for branch in program.alternatives
            for item in _program_scalar_paths(branch.program, programs)
        )
        return paths
    return []


def prepare(
    model: DerivedModel[T],
    mapping: MappingSpec | None = None,
    *,
    validate_native: bool = False,
) -> PreparedMapping[T]:
    mapping = mapping or MappingSpec()
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
    native_binding = select_binding(model.native_types[model.structure.root], model.binding)
    record_adapters: dict[str, RecordAdapter] = {}
    if validate_native:
        raise StructureMapError(
            "unavailable_capability",
            f"The {model.binding} binding does not provide native validation",
            owner="binding gap",
            remedy=f"Set validate_native=False; the {model.binding} binding does not provide native validation",
        )
    if mapping.defaults.presence != "normalize" or mapping.defaults.positions != "strict":
        path = "presence" if mapping.defaults.presence != "normalize" else "positions"
        raise StructureMapError(
            "unavailable_capability",
            f"The requested {path} policy is not available in this delivery",
            owner="binding gap",
            path=path,
            remedy="Use presence='normalize' and positions='strict'",
        )
    global_unavailable = (
        "construction",
        "role_discriminator",
        "shared_types",
        "json_numbers",
        "duplicate_members",
        "open_content",
    )
    for policy in global_unavailable:
        if getattr(mapping.defaults, policy) is not None:
            raise StructureMapError(
                "unavailable_capability",
                f"The declared {policy} policy is not available in this delivery",
                owner="binding gap",
                path=policy,
                remedy=f"No {policy} strategy is available in this delivery; omit this request",
            )
    if mapping.defaults.variant_layout not in (None, "split", "prefixed"):
        raise StructureMapError(
            "unavailable_capability",
            f"The declared {mapping.defaults.variant_layout!r} variant layout is unavailable",
            owner="binding gap",
            path="variant_layout",
            remedy="Use variant_layout='split' or 'prefixed'",
        )
    if mapping.defaults.value_space is not None and model.binding != "xsdata":
        raise StructureMapError(
            "unavailable_capability",
            "The value_space policy is unavailable for the dataclass binding",
            owner="binding gap",
            path="value_space",
            remedy="Omit value_space or use the xsdata binding",
        )

    recursive_component = _components(cast(DerivedModel[object], model))

    def collection_payload(shape: Shape) -> Shape:
        while isinstance(shape, (Nullable, Sequence, Map)):
            shape = shape.item if isinstance(shape, Sequence) else shape.value
        return shape

    def target(path: str) -> list[tuple[str, Shape]]:
        if not path:
            return [("root", RecordRef(definition=model.structure.root))]
        candidates: list[tuple[Shape, str]] = [(RecordRef(definition=model.structure.root), model.structure.root)]
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
                "presence": "Use presence='normalize'; plain dataclasses cannot retain supplied-field presence",
                "positions": "Use positions='strict'; lenient reconstruction is unavailable in Foundation",
                "key": "Use key='generated'; source-backed keys are unavailable in Foundation",
                "ordered": "Use ordered=True; unordered collections are unavailable in Foundation",
                "collection_table": "Use collection_table='separate' and distinct table names",
            }.get(policy, f"No {policy} strategy is available in Foundation; omit the request to use this binding"),
        )

    for path, override in mapping.overrides.items():
        targets = target(path)
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
        for policy in global_unavailable:
            if getattr(override, policy) is not None:
                unavailable(path, policy)
        collection_policies = ("key", "positions", "ordered", "collection_table")
        for policy in collection_policies:
            if getattr(override, policy) is not None and kind != "collection":
                raise StructureMapError(
                    "inapplicable_override",
                    f"The {policy} policy applies only to a collection use",
                    path=path,
                    remedy="Set this policy on a collection use",
                )
        unsupported = (
            "key",
            "positions",
            "ordered",
            "collection_table",
            "role_discriminator",
            "shared_types",
            "cast",
            "presence",
            "json_numbers",
            "duplicate_members",
            "open_content",
        )
        for policy in unsupported:
            value = getattr(override, policy)
            if value is not None and not (
                (policy == "key" and value == "generated")
                or (policy == "positions" and value == "strict")
                or (policy == "ordered" and value is True)
                or (policy == "collection_table" and value == "separate")
                or (policy == "presence" and value == "normalize")
            ):
                unavailable(path, policy)
        if override.variant_layout is not None and override.variant_layout not in ("split", "prefixed"):
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
        if override.discriminator is not None and override.discriminator.strategy != "recorded":
            raise StructureMapError(
                "unavailable_capability",
                "Source-discriminator inference is unavailable for the declared binding",
                owner="binding gap",
                path=path,
                remedy=(
                    "Use strategy='recorded' with explicit alternative tag values; source tag domains "
                    "are unavailable for these declarations"
                ),
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

    naming = mapping.defaults.naming
    key_col = naming.metadata_namespace + "key"
    parent_col = naming.metadata_namespace + "parent"
    position_col = naming.metadata_namespace + "position"
    map_key_col = naming.metadata_namespace + "map_key"
    null_col = naming.metadata_namespace + "null"
    parent_table_col = naming.metadata_namespace + "parent_table"
    programs: dict[int, ValueProgram] = {}
    next_program = 0
    tables: dict[str, dict[str, object]] = {}
    roles: dict[tuple[object, ...], str] = {}
    pending: deque[Callable[[], None]] = deque()
    layout_overrides = tuple(
        path
        for path, override in sorted(mapping.overrides.items())
        if any(
            value is not None
            for value in (override.table, override.column, override.prefix, override.variant_layout,
                          override.discriminator, override.value_space)
        )
        or bool(override.alternatives)
    )

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
    ) -> int:
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
    ) -> str:
        override = mapping.overrides.get(authored_path)
        # Context contains only suffixes of finite authored selectors and their
        # declaration identities. Runtime depth is never an interning key.
        context = tuple(
            (selector[len(authored_path) :].lstrip("/"), selector)
            for selector in layout_overrides
            if selector == authored_path or selector.startswith(authored_path + "/")
        )
        role = (family, source_definition, relative_path, kind, role_suffix, anonymous_parent, context)
        if role in roles:
            table = roles[role]
            cast(set[str], tables[table]["parents"]).add(owner)
            return table
        if anonymous_parent is not None:
            table = anonymous_parent + "__item"
        elif table_override is not None:
            table = table_override
        elif not ignore_table_override and override is not None and override.table is not None:
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
        }

        def compile_payload() -> None:
            if payload is None:
                emit(NullProgram(authored_path), payload_id)
                return
            branch_shape = payload.value if isinstance(payload, Nullable) else payload
            if isinstance(branch_shape, RecordRef):
                next_family = (
                    family
                    if recursive_component[source_definition] == recursive_component[branch_shape.definition]
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
    ) -> int:
        nullable = isinstance(shape, Nullable)
        value_shape = shape.value if isinstance(shape, Nullable) else shape
        override = None if override_applied else mapping.overrides.get(authored_path)
        if isinstance(value_shape, Scalar):
            _validate_facets(value_shape.facets, authored_path)
            scalar_column = override.column if override is not None and override.column is not None else column
            native_type = model.native_types.get(value_shape.native_type) if value_shape.native_type else None
            scalar_override = mapping.overrides.get(authored_path)
            policy = (
                scalar_override.value_space
                if scalar_override is not None and scalar_override.value_space is not None
                else mapping.defaults.value_space
            )
            codec = native_binding.prepare_scalar(
                value_shape,
                native_type=native_type,
                format=value_shape.format if value_shape.format is not None else format,
                column=scalar_column,
                policy=policy,
                metadata_namespace=naming.metadata_namespace,
                path=authored_path,
            )
            return emit(ScalarProgram(authored_path, codec, nullable), destination)
        if isinstance(value_shape, RecordRef):
            if recursive_component[source_definition] == recursive_component[value_shape.definition]:
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
            if isinstance(inner, Alternatives):
                selected_layout = (
                    override.variant_layout
                    if override is not None and override.variant_layout is not None
                    else mapping.defaults.variant_layout or "split"
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
                        destination=destination,
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
    ) -> int:
        override = None if override_applied else mapping.overrides.get(authored_path)
        layout = (
            override.variant_layout
            if override is not None and override.variant_layout is not None
            else mapping.defaults.variant_layout or "split"
        )
        if layout not in ("split", "prefixed"):
            raise StructureMapError(
                "unavailable_capability",
                f"Variant layout {layout!r} is unavailable",
                owner="binding gap",
                path=authored_path,
                remedy="Use variant_layout='split' or 'prefixed'",
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
        if isinstance(declared_discriminator, SourceDiscriminator):
            raise StructureMapError(
                "unavailable_capability",
                "Source-discriminator inference is unavailable for the declared binding",
                owner="binding gap",
                path=authored_path,
                remedy="Use strategy='recorded' with explicit alternative tag values",
            )
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
                    discriminator_column=discriminator_column,
                    discriminator_value=tags[key],
                    payload_null_column=payload_null_column,
                    position_group=f"{family}:{source_definition}:{relative_path}:{authored_path}:{role_suffix}",
                    namespace=namespace,
                    format=branch_format,
                )
                branch_program = cast(int, tables[table]["program"])
                branches.append(VariantBranch(key, tags[key], branch_program, table, branch_nullable))
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
                )
                branches.append(VariantBranch(key, tags[key], branch_program, nullable_payload=branch_nullable))
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
                discriminator_column=discriminator_column,
                discriminator_value=None,
                position_group=f"{family}:{source_definition}:{relative_path}:{authored_path}:{role_suffix}",
                namespace=namespace,
            )
        return emit(
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
            ),
            destination,
        )

    root_override = mapping.overrides.get("")
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
    }
    record_program(model.structure.root, "", "", root_prefix, family="", owner=root_name, destination=root_id)
    # Discover sibling row roles before descending: recursive children must not
    # lend their traversal depth to an ordinary sibling's default table name.
    while pending:
        pending.popleft()()
    plans: dict[str, TablePlan] = {}
    for name, descriptor in tables.items():
        program_id = cast(int, descriptor["program"])
        kind = cast(str, descriptor["kind"])
        parents = tuple(sorted(cast(set[str], descriptor["parents"])))
        fields = [FieldSpec(name=key_col, type=UniversalType.STRING, missing_values=[])]
        if parents != ("",):
            fields.append(FieldSpec(name=parent_col, type=UniversalType.STRING, missing_values=[]))
            if kind in ("sequence", "map"):
                fields.append(FieldSpec(name=position_col, type=UniversalType.INTEGER, missing_values=[]))
            if kind == "map":
                fields.append(FieldSpec(name=map_key_col, type=UniversalType.STRING, missing_values=[]))
            if len(parents) > 1:
                fields.append(FieldSpec(name=parent_table_col, type=UniversalType.STRING, missing_values=[]))
        if cast(bool, descriptor["nullable_member"]):
            fields.append(FieldSpec(name=null_col, type=UniversalType.BOOLEAN, missing_values=[]))
        discriminator_column = cast(str | None, descriptor["discriminator_column"])
        if discriminator_column is not None:
            fields.append(FieldSpec(name=discriminator_column, type=UniversalType.STRING, missing_values=[]))
        payload_null_column = cast(str | None, descriptor["payload_null_column"])
        if payload_null_column is not None:
            fields.append(FieldSpec(name=payload_null_column, type=UniversalType.BOOLEAN, missing_values=[]))
        fields.extend(_program_fields(program_id, programs))
        columns = tuple(field.name for field in fields)
        if len(set(columns)) != len(columns):
            scalar_sources = _program_scalar_paths(program_id, programs)
            seen: dict[str, str] = {}
            conflicts: list[tuple[str, str, str]] = []
            for column, source_path in scalar_sources:
                if column in seen:
                    conflicts.append((column, seen[column], source_path))
                else:
                    seen[column] = source_path
            if not conflicts:
                metadata_columns = (
                    {field.name for field in fields[: -len(scalar_sources)]}
                    if scalar_sources
                    else {field.name for field in fields}
                )
                conflicts = [
                    (column, f"metadata:{column}", source_path)
                    for column, source_path in scalar_sources
                    if column in metadata_columns
                ]
            column, first_path, second_path = conflicts[0]
            raise StructureMapError(
                "column_collision",
                f"Column {column!r} in table {name!r} maps both {first_path!r} and {second_path!r}",
                path=second_path,
                table=name,
                remedy="Change column_naming or metadata_namespace, or use distinct explicit names",
            )
        plans[name] = TablePlan(
            name=name,
            source_path=cast(str, descriptor["path"]),
            parent_tables=tuple(parent for parent in parents if parent),
            columns=columns,
            _typespec=TypeSpec(fields=fields, missing_values=[]),
            _program=program_id,
            _kind=cast(Literal["root", "single", "sequence", "map"], kind),
            _nullable_member=cast(bool, descriptor["nullable_member"]),
            _position_group=cast(str | None, descriptor["position_group"]),
        )
    return PreparedMapping(
        root=root_id,
        programs=programs,
        root_table=root_name,
        root_type=cast(type[T], model.native_types[model.structure.root]),
        tables=plans,
        native_types=model.native_types,
        binding=native_binding,
        record_adapters=record_adapters,
        capabilities=native_binding.capabilities,
        key_column=key_col,
        parent_column=parent_col,
        position_column=position_col,
        map_key_column=map_key_col,
        null_column=null_col,
        parent_table_column=parent_table_col,
    )
