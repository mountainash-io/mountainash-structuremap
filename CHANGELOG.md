# Changelog

## Unreleased
- **Breaking:** migrate codecs and table layouts to ordered name-to-FieldSpec mappings, preserving shared declarations through projections and reconstruction. Custom codecs must return `Mapping[str, FieldSpec]` instead of tuples of named `FieldSpec`. Requires Mountainash `dcf43a81a4f2ddebce2b0550f0a26b015f8290f7` (composable field declarations; pin to be moved to the merged Mountainash `develop` commit).

- Adopt Mountainash `c1e8da53822af9b61079a34f00c811ce5d53c733` canonical dtype fields, preserving numeric widths through prepared schemas, descriptor reload and Arrow values.
- Honor explicit native override precedence in schema reconciliation and inverse casts.
- Declare PyArrow directly and add a pinned, installable Files dependency extra plus lxml for XML execution.
- Share one document-scoped decomposition session across complete values and scheduled envelope/record occurrences, retaining finalized relationships and releasing delivered record state.
- Add typed Arrow collection, synchronous bounded Parquet/IPC writers, and schema-checked reconstruction from independent Arrow tables or explicit Files sources.
- Add bidirectional local execution APIs with namespace-aware destination preflight, standalone/managed native output, Atomic/Keep partial staging, and primary-error-preserving cleanup.
- Add incremental XML decomposition through native xsdata parsing and the shared session, with exact member/QName ancestry, wrapped/nested records, nullable choices and scopes, retained headers, and chunk-safe mixed fragments.
- Preflight incremental ancestor key readiness; preserve bounded writer batches and complete records without buffering the selected collection or constructing partial ancestors.
- Qualify installed Parquet/IPC execution with separate writer/readers, independent files, directional codecs, empty/all-null tables, keys/roles/alternatives, incremental XML and native XML/JSON output; retain base-only and minimum/current Pydantic coverage.
- Scope CI read-only dependency access to the five pinned repositories and run the real execution example in hosted source qualification.
- Add canonical JSON/safe-YAML mapping and structure authoring, generated JSON Schemas, namespace assembly and a thin snapshotting fluent builder.
- Retain explicit-null override semantics and Mountainash Frictionless relation descriptors in portable bundles; reject duplicate keys, executable YAML tags, non-JSON values and unknown envelope fields without importing native bindings.
- Exercise portable authoring through independently declared schemas and shared-role rows in the composable mapping example.
- Preserve populated declaration dictionaries and explicit-null selector resets across export; enforce document scalar types and required structural discriminators in the generated schema.
- Extend isolated XML/base/current/minimum-Pydantic wheel qualification with retained mapping JSON, independent schemas and rows, external multi-column codecs, typed tuples and shared collection roles.
- Resolve finite reusable mapping references and qualified definition selectors, including nested suffixes and recursive path-local policies; reject alias and component-delegation cycles.
- Add ancestor-backed source keys, shared collection roles and explicit strict/lenient/unordered policies, with role-aware typed identities and preconstruction relationship validation.
- Add batched Mountainash type casts and inverse projections, post-cast key collision checks, Shared/Exact alternatives and retained finite source-discriminator domains.
- Qualify Mountainash revision `a9725e69831fc209307ed0c82d524e3dbf43479e`, replacing stale cast projection metadata in the previous dependency pin.

- Support explicitly bound typed sequence roots, homogeneous native tuples and retained primitive Literal domains through the existing shared executor.
- Share exact Decimal codecs between native dataclass/Pydantic and XML bindings; add native DATE cells with strict date-versus-datetime checks.
- Add public mapping-component and directional-codec contracts; default and external mappings share preparation and iterative execution.
- Constrain preparation with independent Mountainash schemas and per-table generated/source/composite layouts, with exact typed key identity and early direction/configuration/schema errors.

- Add native Pydantic JSON parsing and serialization with native alias/configuration semantics and retained error causes, separate from canonical table reconstruction.
- Extend existing installed-wheel qualification to Pydantic in XML/base-only environments and the minimum supported Pydantic 2.11.0.
- Preserve native record discovery across nested plain dataclass boundaries, including mixed recursive record graphs, without advertising plain dataclass Literal validation.
- Keep individually Annotated union branches distinct and report nested unresolved annotations with member-path and model-rebuild guidance.
- Extend native Pydantic discovery with homogeneous primitive Literal fields and nested Annotated shapes; leave constraints and union normalization to native validation.
- Qualify shared containers, finite recursion, model composition and recorded Split/Prefixed variants for Pydantic records without changing the shared executor.
- Add the Pydantic BaseModel native-field vertical slice through the shared prepared executor: canonical member naming, direct reads, captured defaults and native validated reconstruction.
- Support explicit validated construction for Pydantic record targets while retaining native error causes and rejecting unsupported plain-dataclass validation claims.
- Reject custom Pydantic initialization and retained extras instead of risking silent canonical-value loss. Require Pydantic 2.11+.
- Add Fragment/Reject policies for native XML wildcards, mixed content, compound wildcard choices and attribute wildcards, with per-use policy precedence.
- Preserve standalone fragment namespace context, typed choice names, generic text/tails and singular wildcard aggregates. Validate fragment syntax and occurrence roles without application constructors before native binding.
- Qualify supplied generated models and combined XML documents through source execution and isolated XML/base-only wheels, including independent recursive/Split/fragment tables and portable fresh-process scalar carriers.
- Add shared Split/Prefixed alternatives for singular, list and map uses, with recorded discriminators, per-alternative naming, finite recursive roles and complete inactive-payload/reachability checks before construction.
- Preserve supported native XML element choices, local unions, `xsi:type` substitutions, selected nil payloads and token-choice QNames. Freeze native associations at preparation and reject unsupported ambiguity.
- Add exact Decimal/facet and unbounded-integer codecs, explicit Typed/Lexical policies, canonical XML temporal/binary/QName conversion and frozen scalar-enum selection.
- Preserve temporal offset presence through microsecond physical carriers and companions; reject unrepresentable Typed values with Lexical guidance and preflight invalid inverse pairs before model construction.
- Qualify actual prepared Decimal and temporal values through Mountainash TypeSpec persistence and Arrow conversion, including empty/all-null data.
- Withdraw issue #5's namespace upstream-blocker diagnosis after generated-model round-trips pass; the handwritten reproduction omitted namespace metadata. Model authorship and XSD-to-model generation belong to package users, not StructureMap.

- Add optional xsdata 26.2 XML model derivation and native `parse`/`serialize` operations, including XML local-name collision guidance, wrappers, token lists and enum reconstruction.
- Move record preparation and scalar conversion into native bindings while retaining the shared finite compiler/executor and complete preconstruction validation.
- Reject unsupported same-type XML compound metadata before reading data; retain native exceptions and explicit capability/install guidance.
- Preserve tuple-backed XML collections, repeated nil occurrences and NaN-valued enum selection; retain nested XML detection and reject non-record enum roots with attributed errors.
- Use the owner-authorized native Unicode stream entrypoint with xsdata's standard-library handler, preserving text with non-UTF-8 declarations while keeping byte parsing unchanged; track the native `from_string` defect in issue #4.

- Add the plain-dataclass in-memory `derive`, `prepare`, `decompose` and `reconstruct` pipeline with prepared Mountainash TypeSpecs.
- Preserve inline records, scalar/record list membership, null and empty scalar values, duplicate members, required empty records and captured default-factory output.
- Attach collection descendants to their actual enclosing row, including optional inline records whose presence is established by descendants.
- Accept independently supplied occurrence keys and gapped strict positions; sort reconstruction metadata through Mountainash without inferring business-cell types.
- Add contextual native/configuration/data errors, explicit unsupported-capability refusals, typed package exports and an executable round-trip example.
- Resolve postponed annotations against the dataclass namespace, retaining nested record classes and class-local scalar aliases.
- Add finite recursive role tables and explicit-stack traversal, distinguishing containment cycles from repeated use of the same object.
- Preserve nested lists, insertion-ordered string maps, fixed tuples, and nullable structured members with explicit null markers.
- Retain declaring-class annotation namespaces under inheritance and support empty fixed tuples.
- Reject detached recursive rows and contradictory null members; retain inactive metadata-like names as business columns in both execution directions.
- Add canonical per-use table, column and prefix overrides, finite recursive override contexts, and configuration-isolated preparation.
- Validate native constructor signatures and reject unsupported mapping policies before reading values or calling constructors.
- Keep anonymous collection levels transparent to named selectors, reserve container roles before recursive expansion, and preserve ordinary sibling role names during compilation.
- Keep future cast declarations portable without introducing a separate dtype or expression vocabulary; require Pydantic 2.5+ for JSON-compatible declaration values.
- Validate logical scalar activity and complete row reachability before native construction, including malformed later siblings and detached recursive components.
- Add independent table acceptance, isolated installed-wheel/fresh-process reconstruction, and Foundation CI jobs pinned to one Mountainash development revision.
- Resolve function-local mutual references through already known native classes in the same lexical scope without replacing declaring-class aliases.
- Source Mountainash directly from GitHub at the approved pinned development commit, shared by source environments, wheel metadata and authenticated CI installs.
