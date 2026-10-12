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
- Nullable scalar fields, nonrecursive inline records and scalar/record lists.
- Separate tables for collection uses, including lists inside list records.
- Required empty records, all-null record occurrences, duplicate scalar members,
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

### Native boundary and normalization

The plain-dataclass binding supports introspection, reading and construction,
not wire parsing, serialization, native validation or supplied-field presence.
Construction uses the class normally, including its `__post_init__`; it is not
advertised as validation. Mapped `init=False` fields are unsupported.

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

### Not delivered in this slice

Recursive execution, directly nested collections, maps, fixed tuples, nullable
structured collection members, variant execution, per-use overrides and the
remaining mapping policies are not available. Preparation refuses these requests.
The canonical definitions can describe more shapes than this executor supports.

XML/xsdata, native Pydantic/JSON bindings, decimal/lexical strategies, YAML/JSON
authoring, Parquet output and incremental execution are separate work. This
slice does not claim the complete Foundation or rebuild-programme contract.

## Development

Requires Python 3.12+, Hatch and uv. Mountainash is a source dependency, not a
package-index publication prerequisite. By default Hatch uses the sibling
`../mountainash` checkout. Use a clean selected `develop` revision, not another
session's working branch. Set `MOUNTAINASH_SOURCE_URI` to an absolute `file:` URI
to use a different clean checkout or source extraction.

The implementation checkpoint uses Mountainash revision
`4a4991253fb629a2b9ede26f6f1cef12755109ef`. Its extraction must include
`hatch.toml` as well as `pyproject.toml`, `src`, README and license, because
Mountainash's build/version configuration lives in `hatch.toml`.

```bash
hatch run test:test
hatch run test:python examples/dataclass_roundtrip.py
hatch run ruff:check
hatch run mypy:check
hatch build
```

The example prints emitted tables and reconstructs reordered delivery through
a fresh preparation. No central documentation checkout is needed to build or
run this package. Product work uses feature branches targeting protected
`develop`; releases go through a release branch to `main`.
