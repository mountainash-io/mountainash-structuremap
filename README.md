# Mountainash StructureMap

Map hierarchical documents into typed relational data for querying, and map
relational data back into structured documents.

StructureMap connects native Python dataclass, Pydantic BaseModel and xsdata XML
models with typed table representations. The mapping defines how relationships,
ordering and alternatives are represented. DataFrame and database libraries own
querying and persistence; native codecs own document parsing and serialization.

## Two directions

- **Document to tables:** You have an XML document and want its contents in tables
  or DataFrames for relational queries. Native bindings interpret the document;
  StructureMap decomposes its hierarchy into typed tables. Use a DataFrame or
  database library to query the results.
- **Tables to document:** You have relational data and need a structured document.
  Supply tables that conform to the mapping; StructureMap reconstructs the native
  hierarchy, and the native codec serializes it. The tables need not have
  originated from StructureMap.

For multiple documents, apply the mapping to each document. File discovery,
orchestration, cross-document identity and aggregation are separate concerns;
mapping each document does not define how their datasets should be combined.

## StructureMap and ORMs

An ORM provides a database-facing workflow for querying and persisting entities.
StructureMap is not a database access system: it does not establish database
connections, issue database queries or manage persistent entity sessions.
Use it because data needs a different structure, not a database connection.

Its file-oriented execution APIs read and publish supported table/document
artifacts through storage and native codec integrations. They do not provide a
database connection or query layer.

The hierarchy supplies structural meaning, while a database is one possible
physical realization of the relational representation—not the foundation of the
mapping. In-memory tables, Arrow, Parquet and IPC serve that representation without
requiring a database. Independently supplied conforming tables can reconstruct
native values without the original object graph.

Projected rows represent structural occurrences, not necessarily independently
managed entities. Shared entity resolution, change tracking and database
transactions are separate responsibilities. StructureMap and an ORM can complement
each other through an explicitly defined integration; neither is a reduced version
of the other. Preservation follows the selected mapping and native capabilities,
including their normalization rules, rather than a universal lossless guarantee.

## Example

```python
from dataclasses import dataclass
from mountainash_structuremap import derive, prepare, decompose, reconstruct

@dataclass
class Person:
    name: str
    tags: list[str | None]

@dataclass
class Document:
    people: list[Person]

source = Document([Person("Ada", [None, "", "same", "same"])])
prepared = prepare(derive(Document))
tables = decompose(source, prepared)

# Delivery order is not collection order.
tables["people__tags"].reverse()
rebuilt = reconstruct(tables, prepare(derive(Document)))
assert rebuilt == source
```

`prepared.tables` exposes table names, source paths, parent tables, columns and
Mountainash `TypeSpec` schemas. Schemas exist before reading instances, including
for empty tables. TypeSpec inspection returns a copy so callers cannot change
the executor's retained schema.

## Supported behavior

- Dataclass record roots and explicitly selected `binding="dataclass"` typed
  `list[T]` / `tuple[T, ...]` roots; `str`, `bool`, signed-i64 `int`, `float`,
  homogeneous primitive `Literal`, exact `Decimal` and native `date` fields.
- Nullable scalar fields, inline records, recursive dataclass links, lists,
  homogeneous tuples, string-key maps and fixed tuples (including empty tuples).
- Separate tables for collection uses and recursive role families, including
  nested collections and repeated uses of the same native class.
- Required empty records, all-null record occurrences, empty nested containers,
  null structured members, duplicate scalar members,
  empty strings and null scalar members.
- Captured default values and factory output: preparation never calls factories,
  and reconstruction passes captured values to the ordinary native constructor.
- Document-local generated occurrence keys; independently supplied nonempty
  string keys are accepted and need not resemble generated keys.
- Strict positions: unique within each parent collection, in the nonnegative
  signed-i64 range. Gaps are accepted; decomposition renumbers from zero.
- Explicit lenient ordering: positions sort ascending, ties preserve delivery
  order, and null positions come last. Omitted position columns use delivery
  order. Unordered collections need no position column.
- Global `Naming` configuration for metadata namespace and `snake_case`/`preserve`
  columns. Name collisions fail rather than overwrite.

Tables use `_sm_key`, `_sm_parent` and `_sm_position` by default. Inline records
share their enclosing row's key; descendants attach to that row, not a fictitious
inline-record row. Strict collection positions, not delivery order, control reconstruction.
Source instances and supplied tables are not mutated.

Reconstruction requires the prepared table and column sets, including empty
tables, and exactly one root row. Lenient ordering permits omitted position
metadata, not missing payload or key columns. It validates links, positions, null
markers, active scalar cells and row reachability before calling any native
constructor. A malformed later sibling or detached row cannot cause partial
native construction. Exceptions raised by native constructors themselves retain
their cause; the package cannot roll back those constructors' external effects.

Typed sequence roots retain one document-root row and a positioned `root__item`
table, including for an empty sequence. No synthetic native record is required.
Unfaceted Decimal values use exact lexical STRING cells, not floats; date fields
use DATE cells and reject datetime values. Partial dates declared as strings stay
strings. Literal domains are checked in both directions before native construction.

Recursive roles use finite table families, not a table per runtime depth.
`_sm_parent_table` disambiguates links when a role can attach to multiple parent
tables; singular recursive links have no position column. Traversal and native
construction use explicit stacks. Repeated use of one source object produces
independent occurrences; an actual containment cycle fails.

Nested collection tables append `__item`. Maps carry `_sm_map_key` and positions
to preserve insertion order, including empty keys and keys containing `/`.
Nullable structured collection members carry `_sm_null`: a true marker means
null, while false preserves present empty/all-null members. Contradictory null
rows with populated payload or descendants are rejected.

### Mapping configuration

The same derived model can be prepared with independent configurations:

```python
from mountainash_structuremap import MappingSpec, Naming, UseOverride

mapping = MappingSpec(
    defaults={"naming": Naming(metadata_namespace="recon_")},
    overrides={
        "people": UseOverride(table="persons"),
        "people/name": UseOverride(column="display_name"),
    },
)
configured = prepare(derive(Document), mapping)
rows = decompose(source, configured)
assert rows["persons"][0]["display_name"] == "Ada"
assert reconstruct(rows, configured) == source
```

Paths follow native member names with transparent list/map membership; fixed
tuple slots use ordinal segments. `""` selects the root. Table names apply to
root, collection and recursive-link rows; nonrecursive records remain inline.
`column` names scalar projections, and `prefix` scopes a record's projections.
Explicit names preserve their spelling. A collection rename changes its anonymous
`__item` descendants, not independently named descendant uses.

Finite deeper recursive overrides apply only at their authored uses. Once those
paths are exhausted, execution reuses an ordinary recursive role. Prepared
programs do not retain mutable mapping input. Unknown paths, inapplicable options,
and physical table/column collisions fail before data is read.

Generated keys, strict positions, ordered collections, separate tables and
normalized presence remain defaults. Source keys, shared collection roles,
lenient ordering and type casts are explicit policies. Canonical strategy types
live in `mountainash_structuremap.mapping`. Custom expression casts, EAV,
Shared/Widen and preserved supplied-field presence remain unavailable and fail
during preparation rather than silently falling back.

### Composable mappings and independent schemas

`prepare(model, mapping, *, relations=None, components=(), directions=...)` uses
one compiler and shared executor for default mappings and external components.
`relations` is a table-name-to-Mountainash-`TypeSpec` mapping. When supplied, its
table/column sets and physical types constrain preparation; data never determines
the schema, including empty collections and all-null fields. Preparation snapshots
the declarations and schemas rather than retaining caller-owned mutable inputs.

`MappingUse(component=..., config=..., children=...)` selects an exact component
identity. Supply implementations explicitly in `components`; loading canonical
declarations does not import application modules or discover plugins. Missing and
duplicate implementations fail before data access. `MappingSpec.use=None` uses
the built-in component, and omitted children use their built-ins.

Components implement `MappingComponent.prepare(use, context)` from
`mountainash_structuremap.components`. They declare their `identity` and supported
`directions`. `PreparationContext` exposes the shape, diagnostic path, table,
requested directions and an isolated `field(column, table=...)` view of an
independently declared field. Its operations are:

- `value(codec=..., options=...)` for one or several physical cells;
- `record(children, options=...)` for named members;
- `sequence(item, options=...)` and `map(key=..., value=..., options=...)`;
- `positional(items, options=...)` for integer-indexed slots;
- `alternatives(branches, options=...)` for declared branch keys;
- `apply(node)` to delegate preparation at the same structural use.

Each use contributes one structural operation. Child operations go through shared
preparation, not a component-owned traversal. Nullable wrappers retain core null
semantics. Components must validate accepted shapes, configuration and child names,
and document normalization and buffering requirements. Conflicting component and
selector choices fail rather than silently taking precedence.

Built-in identities are `core.value`, `core.record`, `core.sequence`, `core.map`,
`core.positional` and `core.alternatives`. Their config uses `UseOverride` fields;
children are native member names, `item`, `key`/`value`, numeric slot strings or
alternative keys respectively.

`MappingRef(ref=..., config=..., children=...)` reuses a declaration from
`MappingSpec.definitions`; overlays stay local to that use. Alias-only cycles
and non-structural component-delegation cycles fail. Structural self/mutual
recursion remains finite. `@Address` selects every use of that definition;
`@Address/city` and longer member suffixes select descendants. Use qualified
definition names when a short name is ambiguous. Exact native-path overrides
win over definition selectors; equally specific conflicting selectors fail.

`RowLayout` selects physical relationship columns per table. For example:

```python
from mountainash_structuremap import RowLayout

layouts = {
    "orders": RowLayout(key=("order_id",)),
    "lines": RowLayout(
        key=("order_id", "line_no"), parent=("order_id",), position="line_no",
    ),
}
```

Use these in `MappingSpec(layouts=layouts)` alongside explicit table placement.
Source/composite keys must be non-null and unique within their table and role.
`SourceKey(columns=(...))` can carry mapped ancestor fields into a child key.
Multiple ancestor origins with the same column name are ambiguous: rename the
source columns explicitly. Parent columns correspond in order and physical type
to the parent key. Parent/position columns may overlap payload only when values
agree. Both directions reject contradictory carry values; keys use exact typed
identity, so `True` and `1` are distinct.

Generated keys remain the default. Explicit layouts rename key, parent, position,
map-key, null-marker, role and parent-table metadata. Sufficient source layouts
need no extra `_sm_*` columns. Map keys remain native strings; key codecs must use
one physical STRING column.

`SharedCollection(table=..., role_column=..., role=...)` places distinct collection
uses in one table. The role defaults to the native use path; use explicit roles
when sharing alternative branches. Roles retain independent keys and position
domains; equal application IDs never merge occurrences. Configured parent-table
selectors must agree with the role's permitted parent tables.

`TypeCast(to=..., on_failure="throw"|"null")` uses Mountainash's dtype vocabulary
and projections. Preparation declares post-cast physical schemas without sampling
data. Execution batches each scalar use's owned columns, then validates finalized
keys; casting `"01"` and `"1"` to the same source key is an error. Reconstruction
applies the reverse native-column projection before native construction. Casts
can normalize values (`"01"` → `1` → `"1"`); they do not promise lexical identity.

Cast schemas retain canonical numeric widths through Mountainash
`FieldSpec(dtype=...)`, descriptor reload and Arrow materialization, including
unsigned 64-bit values. Explicit native overrides retain Mountainash precedence
in physical schema comparison and reverse projections.

`EncodingCodec` needs `fields` and `encode`; `DecodingCodec` needs `fields`, `check`
and `decode`. `ScalarCodec` combines them. Codecs use tuples of cells and
`CellLocation` from the public `codecs` module. An encode-only or decode-only codec
does not need unused methods. Select `directions=frozenset({"decompose"})` or
`frozenset({"reconstruct"})`; the default prepares both. Execution rejects an
unprepared direction before touching input. Reconstruction checks scalar cells
before constructing native records.

Execution buffers rows, scalar projection batches and relationship indexes; this
is not a streaming claim. See `examples/composable_mapping.py` for independent
schemas/rows, reusable address components, a three-integer date codec, source
keys, recursive references, shared roles and branch-specific cast reconstruction.

### Portable authoring

JSON, safe YAML and the fluent builder produce the same canonical `MappingSpec`;
they do not introduce a second preparation or execution engine.

```python
from mountainash_structuremap import (
    MappingBuilder, MappingUse, dump_mapping, load_mapping, mapping_schema,
)

mapping = load_mapping(
    '{"use":{"component":"core.record"},"overrides":{"city":{"column":"town"}}}',
    format="json",
)
equivalent = (
    MappingBuilder()
    .use(MappingUse(component="core.record"))
    .override("city", column="town")
    .build()
)
yaml_text = dump_mapping(mapping, format="yaml")
schema = mapping_schema()  # generated JSON Schema, not a separately maintained schema
```

The envelope is unversioned and uses `root`, `use`, `definitions`, `overrides`,
`defaults` and `layouts`. A component use has `component`, JSON-shaped `config`
and an optional `children` mapping keyed by that component's child names. A
reference uses `ref`, with optional `config` and `children` overlays.
Explicit null overrides survive dumping and loading: resetting a selector's cast
with `cast: null` is different from omitting `cast`. Duplicate keys, non-JSON
values, executable YAML tags and unknown declaration fields fail rather than
being silently normalized or ignored. Loading accepts unresolved references and
component names for inspection; `prepare` requires their explicit implementations.
No loader imports native classes, activates a binding or fetches resources.

`MappingBuilder` mutates fluently through `defaults`, `use`, `define`, `override`
and `layout`; `build()` returns a detached snapshot. Builder inputs and previously
built mappings do not share mutable declaration dictionaries.

`StructureDocument(namespace=..., structure=...)`, `load_structure`,
`dump_structure` and `structure_schema` support standalone structural documents.
`assemble_structure(documents, root=...)` prefixes definition names with each
document's namespace unless already so qualified, rewrites finite references and
rejects duplicate qualified names. An unqualified reference must resolve uniquely
across all supplied documents; ambiguity reports qualified candidates. Selectors
use the same exact/unique-short-name rule. Recursive references stay finite.
To execute an assembled structure, explicitly supply its native associations:

```python
from mountainash_structuremap import assemble_structure
from mountainash_structuremap.definitions import DerivedModel

model = DerivedModel(
    structure=assemble_structure(documents, root="addressbook.Directory"),
    native_types={
        "addressbook.Directory": Directory,
        "addressbook.Address": Address,
    },
    binding="dataclass",
)
```

`AuthoringBundle(structure=..., mapping=..., relations=...)` packages an optional
structure document, mapping and table-name-to-`TypeSpec` declarations.
`dump_bundle`/`load_bundle` accept explicit `format="json"` or `"yaml"`.
Relations retain Mountainash Frictionless descriptors, including parameterized
dtypes and missing-value settings; bundles contain no executable native bindings.
Supply the loaded relations to `prepare(..., relations=bundle.relations)` rather
than inferring a schema from rows.

The portable-authoring section of `examples/composable_mapping.py` executes JSON,
YAML and fluent routes over independently declared schemas and shared-role rows,
then reloads retained bundles before reconstruction. This remains buffered table
execution, not a filesystem dataset or incremental-processing interface.

### Alternatives

Declared dataclass unions and supported native XML choices use frozen alternative
associations, not inference from populated payload columns. Unknown runtime
classes or QNames cannot extend a prepared schema.

- Repeated `variant_layout="split"` uses separate alternative tables with one
  shared position domain per collection occurrence. Strict positions may have
  gaps but cannot repeat across alternative tables. Lenient null/tied positions
  follow alternative declaration order, then delivery order within each table.
- `variant_layout="prefixed"` puts alternatives in one occurrence row with
  disjoint payload projections. Inactive payload cells and descendants must be
  empty. Singular alternatives use inline projections or finite recursive links.
- `variant_layout="shared"` merges compatible common columns using fully declared
  post-cast physical types (Shared/Exact). Branch-only fields and descendants must
  be empty for inactive alternatives.
- Recorded discriminators configure string columns and tags. A
  `SourceDiscriminator(field=...)` retains a scalar source field with finite,
  disjoint declared tag domains, such as primitive `Literal` values. It supports
  Split, Prefixed and Shared, preserves numeric tags, and rejects discriminator
  casts, ambiguous domains and unexpected runtime tags. EAV remains unavailable.
- `UseOverride.alternatives` configures each declared key's `prefix`, and its
  `table` for repeated Split. The reserved `$null` layout names the separate
  null-occurrence table; a selected nillable XML payload is distinct from that
  null occurrence and from a present all-null record.
- Native member paths traverse matching alternatives, including element choices
  containing `xsi:type` alternatives. Per-use overrides do not leak into unrelated
  branches or recursive depths. Explicit collisions fail with configuration
  guidance.

The XML adapter supports retained `DerivedElement` QNames, declared local unions,
native `xsi:type` subclasses/sibling substitutions, and distinct token item types.
An ambiguous empty token choice requires a retained explicit QName; the binding
does not recover information discarded by native parsing.

### XML documents

Install `mountainash-structuremap[xml]` for the xsdata 26.2 binding. XML-shaped
dataclasses select it automatically; `derive(Model, binding="xsdata")` selects
it explicitly. Missing XML dependencies fail with installation guidance rather
than falling back to a plain dataclass interpretation.

Users author the xsdata models supplied to StructureMap and own any generation
from XSD. StructureMap consumes those classes and their metadata; it does not
generate models, orchestrate the generator or repair model metadata. Compatible
handwritten models are also supported within the declared binding capabilities.

```python
from mountainash_structuremap import parse, serialize

prepared = prepare(derive(XmlDocument))
native = parse(xml_text_or_bytes, prepared)
tables = decompose(native, prepared)
rebuilt = reconstruct(tables, prepare(derive(XmlDocument)))
xml_text = serialize(rebuilt, prepared)
```

`parse` accepts complete XML text or bytes, not filenames or streams.
`serialize` returns XML text. Both use xsdata; prefixes and byte-identical output
are not preserved. Run `hatch run test:python examples/xml_roundtrip.py` for a
complete native-document and independently authored table example.

Unicode text uses xsdata's native stream entrypoint with `StringIO`; bytes retain
their declared encoding through `from_bytes`. The standard-library
`XmlEventHandler` is selected explicitly, independent of whether lxml is installed.
This avoids xsdata 26.2's `from_string` re-encoding defect, tracked separately in
[issue #4](https://github.com/mountainash-io/mountainash-structuremap/issues/4).

The XML binding supports scalar and enum fields, nullable values, inline/empty
records, repeated elements, token collections and wrappers. Native list/tuple
containers and repeated nil members retain their occurrences; nil members do not
make an empty required collection null. XML names, namespaces and
element/attribute/text kinds are retained.

Columns default to XML local names; same-local-name collisions require explicit
`column` overrides addressed by native attribute paths. Overrides change table
layout, not native XML names.

Preparation invokes neither native constructors nor default factories.
Reconstruction restores captured values after validating all table input.
Parsing and serialization retain xsdata's native normalization: for example,
an empty optional string with a `None` default can parse as `None`. This is not
source-presence tracking or full XSD validation. Converter failures and native
constructor/parser/serializer failures retain their causes.

Compound choices with overlapping Python types that xsdata cannot bind fail
during derivation/preparation, naming the field and conflicting alternatives.
Use distinct native intermediate types/subclasses supported by xsdata; differing
QNames or `DerivedElement` do not remove that native limitation.

### XML scalar representations

`value_space="typed"` or `"lexical"` can be selected in mapping defaults or at a
native field path. Representation is fixed at preparation, including for empty
and all-null tables. Unsupported Typed requests fail with Lexical guidance;
values outside a prepared domain never trigger a fallback or a rounding cast.

| Source datatype | Prepared representation |
|---|---|
| String, boolean, float | Native scalar carriers by default. Explicit Lexical uses canonical boolean/double text, including infinities, NaN and signed zero. Empty strings are data. |
| Integer | Canonical decimal text unless declared bounds prove signed-i64 fit. Integer enums can use their finite native domain as that proof. Conversion does not change Python's process-wide digit limit. |
| Decimal | Exact Decimal cells with `DecimalDtype(p,s)` when retained total/fraction facets prove a portable domain; otherwise canonical decimal text. Absent tighter bounds, `p=total_digits+fraction_digits`; portable precision is at most 38. Source facets are checked even when a value fits the wider storage declaration. |
| `XmlDateTime` | Naive UTC `timestamp[us]` plus an offset-minute companion. A null offset means timezone absent and the datetime is wall time; zero means an explicit UTC offset. |
| `XmlDate`, `XmlTime` | Local date or `time64[us]`, plus an offset-minute companion. Local fields are not UTC-normalized. |
| `XmlPeriod`, `XmlDuration` | Canonical lexical text. Partial-date components/subtypes and calendar months are not replaced with invented complete dates or fixed timedeltas. |
| Binary, QName | Declared `base16`/`base64` text or expanded QName identity. No portable Typed carrier is selected; explicit Typed fails. |
| Enum | The value's scalar representation plus a frozen native member association; inverse selection does not invoke enum lookup hooks. |

Offset companions use the configured metadata namespace: `when` produces
`_sm_when_offset` by default. Typed datetime/time reject hour 24, submicrosecond
precision and out-of-range Python carriers. Select Lexical for those values.
Lexical temporal canonicalization follows XSD 1.1: local fields and the retained
offset are preserved, zero offsets become `Z`, and valid `24:00:00` normalizes to
midnight. Independently supplied text must already be canonical.

Numeric facets and partial-date format declarations come only from retained
native metadata. The binding does not recover restrictions from an XSD document
or infer them from field names or sample values. This is exact conversion under
the declared native contract, not full XSD validation.

The example exercises exact large decimals, temporal offsets and Lexical
overrides. Prepared declarations also support Mountainash Frictionless
persistence and Arrow schema/value conversion; this does not add a file writer.


### XML open content

`open_content="fragment"` preserves native wildcard and mixed-content values.
It is the default for xsdata open fields. Set `open_content="reject"` to refuse
those fields during preparation. A field-level `UseOverride` takes precedence
over the mapping default; `structured` and `drop` are unavailable.

| Native field | Fragment representation |
|---|---|
| Repeated wildcard or mixed content | One STRING cell per retained native occurrence, with ordinary collection keys and positions. |
| Compound choices containing wildcards | Opaque fragment occurrences retaining declared choice QNames/types and admitted open elements. |
| Singular wildcard | One cell, including a native nameless `AnyElement` that aggregates several children. Nullable absence remains null. |
| Attribute wildcard | One cell containing the complete retained attribute mapping. |

Each cell is a complete XML document rooted at
`{urn:mountainash:structuremap:fragment}fragment`. The codec owns this envelope;
it is not an application model. For independently authored mixed-content rows,
text and empty text are represented as follows:

```xml
<sm:fragment xmlns:sm="urn:mountainash:structuremap:fragment">text</sm:fragment>
<sm:fragment xmlns:sm="urn:mountainash:structuremap:fragment"/>
```

Element fragments retain nested text, generic element tails, expanded names,
attributes and available QName namespace context. Typed elements and their outer
trailing text occupy separate native mixed-content occurrences and therefore
separate rows; a typed fragment cell containing an outer tail is rejected.
Repeated fields reject multiple occurrences in one envelope. Mixed fields must
use a native `list[object]` or `tuple[object, ...]`; scalar mixed annotations are
refused with that model configuration guidance.

Fragment syntax and role checks use a package-restricted generic xsdata parser
that cannot discover application classes. Metadata-only checks resolve typed
tail admissibility without constructing parser nodes or invoking factories.
Application XML binding runs only after all table, position and reachability
checks pass. Native constructor failures during that final binding retain their
causes and cannot roll back application side effects.

The contract preserves what xsdata retains. It does not recover discarded
namespace declarations, source prefixes, source lexemes or original bytes.
The combined XML example exercises choices, exact numbers, temporal offsets and
mixed fragments in one document.

### Pydantic model data

Ordinary Pydantic `BaseModel` record classes select the native binding automatically;
`derive(Model, binding="pydantic")` selects it explicitly. Field discovery uses
`model_fields`, reads use attributes, and reconstruction uses native validated
construction through the same prepared table executor.

Mapping paths use Python member names. For `quantity: int = Field(alias="qty")`,
`UseOverride(column="quantity_count")` maps `quantity` to the table column
`quantity_count`. Reconstruction passes canonical member dictionaries to
`model_validate(..., by_name=True, by_alias=False)`; it does not synthesize alias
paths, retry aliases or change model configuration. Native before-validators must
accept that canonical input. Native failures retain their exception causes.

The supported data shapes use the shared table compiler and executor:

| Native shape | Supported behavior |
| --- | --- |
| Primitive fields | `str`, `bool`, signed-i64 `int`, `float`, nullable values and homogeneous primitive `Literal` annotations. |
| Records and containers | Nested records, lists, string-key maps, fixed tuples (including empty tuples), and finite recursive membership. Empty/null/all-null values follow Normalize below. |
| Native model composition | Inherited fields/validators, resolved generic specializations and nested ordinary dataclasses. |
| Variants | Singular and repeated Split/Prefixed layouts, recorded tags, and Literal-tagged discriminated model unions. Table payload validation precedes native construction. |

`Annotated` metadata is left to native validation, including constraints nested
inside containers. Homogeneous primitive Literal domains are retained and checked
by StructureMap, including nested plain dataclasses; this does not claim general
dataclass validation. Native `date` and exact Decimal fields use the shared scalar
codecs. Enums and binary fields remain outside this Pydantic binding's coverage.

Direct reads retain fields excluded from native serialization and captured
default/factory values. Computed properties are not projected into columns, and
supplied-field history is not restored. Native validation may normalize values or
reselect a union after table dispatch: for example, a recorded string `"456"` can
become integer `456` under native `int | str` left-to-right validation.

`construction="validated"` is available for Pydantic record targets; it does not
claim validation for nested plain dataclass constructors. Custom `__init__` models
are rejected, including nested ones: their initialization can silently discard
canonical values across native revalidation. Use standard model initialization
and compatible field/model validators. Retained extras and RootModel remain
unsupported rather than being silently flattened or discarded. Separate
`validate_native=True`, unvalidated construction and Preserve presence are not
available for this binding.

`parse(json_text_or_bytes, prepared)` delegates to native `model_validate_json`;
`serialize(value, prepared)` delegates to `model_dump_json`. Native aliases,
validators and serialization configuration govern documents, independently of
table column names. For alias-oriented output, configure the native model with
`ConfigDict(serialize_by_alias=True)`. Document parsing does not force canonical
names or retry aliases. Native parse/serialization errors remain chained causes.

Run `hatch run test:python examples/pydantic_roundtrip.py` to see renamed columns,
reordered delivery, independent table reconstruction, native validation errors,
recorded Split variants and native alias-oriented JSON.

### Native boundary and normalization

The plain-dataclass binding supports introspection, reading and construction,
not wire parsing, serialization, native validation or supplied-field presence.
Construction uses the class normally, including its `__post_init__`; it is not
advertised as validation. Preparation rejects mapped `init=False` fields and
unsupplied required constructor-only arguments. Optional constructor-only
arguments retain their native defaults.

An optional inline record with only null projections and no descendants
normalizes to `None`. Descendants establish that the containing record exists.
An optional empty collection normalizes to `None`; required empty collections
remain empty. Collection members themselves are not removed by normalization.

Detected selection recognizes XML-shaped metadata and Pydantic models and
requires their native bindings rather than silently dropping their semantics.
If metadata has another meaning, `derive(Model, binding="dataclass")` explicitly
selects the plain-dataclass contract; this does not claim XML capabilities.

Invalid canonical declarations raise Pydantic `ValidationError`. Derivation,
preparation and execution errors use `StructureMapError`, with a code, owner,
source path, remedy and applicable table/row/column location. Native constructor
exceptions remain the chained cause.

### Not yet executable

Presence preservation, EAV alternatives, Widen/Combined shared projections and
custom cast implementations remain unavailable. Preparation refuses unavailable
strategies rather than weakening them. Native-binding limits are described above.

Reconstruction remains in-memory. Incremental JSON and incremental reconstruction
are unavailable.

The former namespace blocker in
[issue #5](https://github.com/mountainash-io/mountainash-structuremap/issues/5)
was withdrawn: models generated for that namespace structure passed native and
StructureMap round-trips. The failing handwritten fixture omitted namespace
metadata emitted by the generator. Qualification includes supplied generated
models; generator use is test setup, not framework functionality.

## Typed storage and local output

Import the execution operations from `mountainash_structuremap.execution`:

- `decompose_arrow(value, prepared)` returns Arrow tables with the prepared
  schemas, including empty tables.
- `decompose_to(value, prepared, output, format="parquet", buffer_rows=10_000)`
  publishes a managed Parquet dataset; `format="arrow"` selects Arrow IPC files.
- `decompose_xml_to(source, prepared, output, record_path="groups/entries",
  format="parquet", buffer_rows=10_000)` reads XML incrementally through the native
  xsdata binding and publishes the same managed typed dataset.
- `reconstruct_from(source, prepared)` reconstructs from a managed dataset `Path`
  or explicit logical-name bindings to Arrow tables and/or `FileSourceSpec`s.
- `reconstruct_to(source, prepared, output, packaging="standalone")` publishes
  native XML/JSON through the selected binding. `packaging="managed"` includes
  a completion manifest alongside the native `document` payload.

`OutputTarget(root: Path, name, namespace=None, completion="atomic", replace=False)`
resolves to `root / namespace / name`. Namespace is optional; it does not alter
logical table names or enable replacement. Names are single path components;
namespaces must remain within the root without `.` or `..` components.
`ExecutionResult` contains the published `path`, `kind` and immutable `row_counts`;
native document results have no output tables and therefore empty counts.

The existing `decompose`/`reconstruct` row APIs retain their signatures. One
document-scoped session owns their traversal, casts, keys and finalized rows.
Writers flush synchronously with at most `buffer_rows` pending rows per table.
This does not make native-object decomposition or reconstruction bounded-memory:
the full native value/current mapped record may already be materialized.
Declared source-key identity tracking grows with document cardinality; generated
keys use a counter. Generated keys are opaque identities, not stable identifiers
between different traversal schedules.

### Typed input

Files-based reads require `[files]`. In-memory Arrow operations and local output
writers do not import that optional integration. Explicit files require no
StructureMap manifest. Their physical column names/order/types must match the
prepared schemas; incompatible data is not silently cast. Arrow carrier
nullability and unrelated metadata do not replace the mapping's own checks.
Parquet inspection and reading use Mountainash Files; IPC uses its random-access
stream acquisition and PyArrow's IPC file reader.

Each `FileSourceSpec` must identify one concrete source: no unresolved templates,
globs, archive selectors or `on_missing="skip"`. Resolve those selections first.
Inspection/read cardinality and actual schemas are checked; this does not
establish source immutability between reads. Unsupported random-access transforms
fail rather than triggering a buffering fallback.

### Incremental XML

Install `[xml,files]` and supply one concrete XML `FileSourceSpec`. Acquisition uses
Files' sequential stream; native xsdata metadata and parsing interpret the XML.
Omit the source format or use `"xml"`/an unconfigured `XmlSpec`. Files XPath and
encoding/parse-policy overrides are not accepted: `record_path` selects mapped
members, not an XPath. Internal entity content is preserved; external XML files
and URLs are not resolved.

`record_path` is an exact slash-separated authored member path. Direct and wrapped
collections, nested inline records and repeated record ancestors are supported.
Native wrappers remain transparent: mixed wrapped and direct occurrences retain
their order and parent scopes. Selection uses expanded QNames and structural
ancestry, not local-name matching.
Repeated alternatives support Split, Prefixed and Shared layouts. Complete
records retain native choices, `xsi:type`, inline children, nested collections,
scalar codecs and mixed/wildcard fragments, including text split across reads.
Nil values follow the native model: nullable choice items require a nullable
item annotation when xsdata returns `None`; execution does not invent a selected
QName wrapper absent from native parsing.

Root/ancestor attributes are bound at scope start. Other scalar and complex
headers, including content after the selected records, are retained until that
scope closes. Empty collections and omitted members use native defaults and
required-member semantics. Complete records are constructed natively; envelope
members enter the shared mapping session without constructing a partial ancestor.

Generated keys and source keys derived from ancestor attributes, including casts,
defaults and composite ancestor carry, can be ready before record emission.
Element/header key dependencies fail **before input is opened**: native field
declaration order does not guarantee XML arrival order. Select a compatible
record boundary or use in-memory decomposition. Paths through an alternative
ancestor are unavailable; select the enclosing repeated alternative as the
complete-record boundary instead. These restrictions do not apply to supported
constructs inside a complete record.

Rows can flush before document end. Memory includes live ancestor/header context,
the current native record and its mapped rows, bounded per-table writer buffers,
parser read-ahead and source-key identity tracking. This is not a total
constant-memory guarantee. Local untransformed files are the bounded-input
qualification target; delegated transport transforms may buffer.

### Publication and failure

Destination preflight runs before reading input. Existing output requires explicit
replacement permission; managed output may replace only an empty directory,
never an existing nonempty result. Standalone output may replace a regular file.
No implicit renaming, backup swap or deletion of prior successful results occurs.

Output is staged beside its destination. Managed table results contain
`manifest.json` and ordinal paths such as `tables/0000.parquet`; manifest entries
retain the logical names verbatim. Manifest version 1 records `status`, `kind`
and table `{name, path, rows, format}` entries, or `document: {path}`.
Writers close before the complete manifest and final rename. This is local staged
publication, not crash durability or concurrent-writer isolation.

On failure, `atomic` attempts staging removal; `keep_partial` retains diagnostic
files with failed metadata and successfully written row counts. Secondary cleanup
or metadata errors are attached to the original failure. Reserved
`.structuremap-staging-` directories cannot be read as completed managed inputs,
even if failure metadata could not be written. Explicit file bindings can inspect
retained data. A standalone publication followed by cleanup failure reports the
already published target and leftover staging; it does not roll back the file.

Run `hatch run test:python examples/execution_roundtrip.py` for independently
authored Parquet → native XML → incremental Parquet/IPC → fresh reconstruction
with buffer sizes 1 and 10,000, plus both native output packages.

## Development

Requires Python 3.12+, Git and Hatch 1.14+; Hatch manages its configured uv
installer. The package requires Pydantic 2.11+ for native name-only validation.

Mountainash is sourced directly from GitHub at a pinned commit:
`85a90689f16084919cf13afc0d3ca10852843aba` (the composable-field-declarations
cutover, pending its merge to Mountainash `develop`). The dependency in `pyproject.toml`
is the single pin used by source environments, wheel installation and CI.
No package-index publication or sibling checkout is required.

Configure authenticated Git access to `mountainash-io/mountainash` before the
first installation. For GitHub CLI users, `gh auth setup-git` configures Git to
use the existing login. The built wheel retains the pinned GitHub dependency,
so installing it also requires Git and read access to that repository.

PyArrow is a direct dependency. The optional `[files]` extra installs immutable
Git revisions of Mountainash Files, Transport, Auth Client and Settings; Git read
access to those repositories is required as well. Source tests select `[xml,files]`.
The `[xml]` extra includes xsdata 26.2 and lxml. Neither optional integration is
required for base native row-table execution.

```bash
hatch run test:test
hatch run test:python examples/dataclass_roundtrip.py
hatch run test:python examples/xml_roundtrip.py
hatch run test:python examples/pydantic_roundtrip.py
hatch run test:python examples/composable_mapping.py
hatch run test:python examples/execution_roundtrip.py
hatch run ruff:check
hatch run mypy:check
hatch build
hatch run test:wheel-smoke
```

The example prints emitted tables and reconstructs reordered delivery through
a fresh preparation. No central documentation checkout is needed to build or
run this package. Product work uses feature branches targeting protected
`develop`; releases go through a release branch to `main`.

The wheel smoke builds a new wheel and installs `[xml,files]` and base-only
packages in separate temporary environments, checking dependencies with
`pip check`. Current Pydantic and the minimum `pydantic==2.11.0` retain native
JSON, independent-row and constrained-input coverage. Base-only execution
imports the execution API, performs a real Arrow round trip, writes managed
Parquet, and proves that reading it requests the missing `[files]` extra.
Detected/explicit missing-XML guidance is checked separately.

Separate isolated writer/reader processes qualify both Parquet and IPC through
nine cases per format: XML Split/recursive/fragments and temporal companions;
external directional multi-column codecs with populated, empty and all-null
values; shared roles; composite ancestor keys; Prefixed alternatives;
Shared/Exact alternatives; and Pydantic models. Readers consume retained
declarations, mapping and schemas with explicitly supplied application classes,
and reconstruct both managed results and independently authored files.

The installed XML path also streams a native document into each storage format
and reconstructs standalone and managed XML in a fresh process. Assertions cover
exact Decimal values, temporal offset presence, namespaces, order and mixed
text/tails. Pydantic storage reconstructs native JSON with independently checked
aliases. No prepared objects or decomposition caches cross process boundaries.

All runs use `python -I` and the installed package outside the checkout; only
copied application fixtures are added to the import path. No source overlay or
central documentation checkout is used. Temporary environments and transport
files are removed afterward.

CI runs `foundation-tests`, `foundation-wheel`, `foundation-lint` and
`foundation-types`. Source, wheel and CI qualification use the same Mountainash
revision. Private dependency fetches use the organization secrets `CI_APP_ID`
and `CI_APP_PRIVATE_KEY`; the app must have read-only contents access to
`mountainash`, `mountainash-files`, `mountainash-transport`,
`mountainash-auth-client` and `mountainash-settings` in `mountainash-io`—the exact
private dependency chain declared in `pyproject.toml`.
