# Mountainash StructureMap

Transform a plain Python dataclass instance into typed in-memory tables and reconstruct it independently.

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

- Dataclass record roots; `str`, `bool`, signed-i64 `int` and `float` fields.
- Nullable scalar fields, inline records, recursive dataclass links, lists,
  string-key maps and fixed tuples (including empty tuples).
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
  signed-i64 range. Gaps are accepted; subsequent decomposition renumbers from zero.
- Global `Naming` configuration for metadata namespace and `snake_case`/`preserve`
  columns. Name collisions fail rather than overwrite.

Tables use `_sm_key`, `_sm_parent` and `_sm_position` by default. Inline records
share their enclosing row's key; descendants attach to that row, not a fictitious
inline-record row. Table and row delivery order do not control reconstruction.
Source instances and supplied tables are not mutated.

Reconstruction requires the exact prepared table and column sets, including
empty tables, and exactly one root row. It validates links, positions, null
markers, active scalar cells and row reachability before calling any native
constructor. A malformed later sibling or detached row cannot cause partial
native construction. Exceptions raised by native constructors themselves retain
their cause; the package cannot roll back those constructors' external effects.

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

Explicit generated keys, strict positions, ordered collections, separate tables,
and normalized presence select the implemented policies. Known later strategies
are declarable but fail during preparation rather than being silently ignored.
Canonical strategy types live in `mountainash_structuremap.mapping`; these
declarations do not supply casts, alternate bindings or wire execution.

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

### Outside Foundation

Variants, source-backed keys, lenient/unordered collections, shared tables, casts,
presence preservation and other binding-specific strategies are not executable.
Preparation refuses those requests. The canonical definitions and policy
declarations can describe more than this executor supports.

XML/xsdata, native Pydantic/JSON bindings, decimal/lexical strategies, YAML/JSON
authoring, Parquet output and incremental execution are separate work.
Foundation covers the plain-dataclass/in-memory contract, not the full rebuild programme.

## Development

Requires Python 3.12+ and Hatch; Hatch manages its configured uv installer.
Canonical models use Pydantic 2.5+.
Mountainash is a source dependency, not a package-index publication prerequisite.
By default Hatch uses the sibling
`../mountainash` checkout. Use a clean selected `develop` revision, not another
session's working branch. Set `MOUNTAINASH_SOURCE_URI` to an absolute `file:` URI
to use a different clean checkout or source extraction.

Foundation qualification uses Mountainash revision
`c98f35ace19562dac0612fdd9f6c580a59bdc5f8`. Its extraction must include
`hatch.toml` as well as `pyproject.toml`, `src`, README and license, because
Mountainash's build/version configuration lives in `hatch.toml`.

```bash
hatch run test:test
hatch run test:python examples/dataclass_roundtrip.py
hatch run ruff:check
hatch run mypy:check
hatch build
hatch run test:wheel-smoke
```

The example prints emitted tables and reconstructs reordered delivery through
a fresh preparation. No central documentation checkout is needed to build or
run this package. Product work uses feature branches targeting protected
`develop`; releases go through a release branch to `main`.

The wheel smoke builds a new wheel and installs it with the selected Mountainash
source into a temporary virtual environment. It checks installed-package imports,
TypeSpecs and independently authored rows, then transfers primitive tables from
a recursive writer to a separate reader process. Both run outside the checkout
with Python isolated mode; only the copied fixture directory is added to their
import paths. Temporary environments and transport files are removed afterward.

CI runs `foundation-tests`, `foundation-wheel`, `foundation-lint` and
`foundation-types`. Source, wheel and CI qualification use the same Mountainash
revision. Private dependency checkout uses the organization secrets `CI_APP_ID`
and `CI_APP_PRIVATE_KEY`; the app must have read-only contents access to
`mountainash-io/mountainash`. No central documentation checkout is required.
