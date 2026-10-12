# Changelog

## Unreleased

- Add the plain-dataclass in-memory `derive`, `prepare`, `decompose` and `reconstruct` pipeline with prepared Mountainash TypeSpecs.
- Preserve inline records, scalar/record list membership, null and empty scalar values, duplicate members, required empty records and captured default-factory output.
- Attach collection descendants to their actual enclosing row, including optional inline records whose presence is established by descendants.
- Accept independently supplied occurrence keys and gapped strict positions; sort reconstruction metadata through Mountainash without inferring business-cell types.
- Add contextual native/configuration/data errors, explicit unsupported-capability refusals, typed package exports and an executable round-trip example.
