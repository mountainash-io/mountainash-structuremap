# Changelog

## Unreleased

- Add the plain-dataclass in-memory `derive`, `prepare`, `decompose` and `reconstruct` pipeline with prepared Mountainash TypeSpecs.
- Preserve inline records, scalar/record list membership, null and empty scalar values, duplicate members, required empty records and captured default-factory output.
- Attach collection descendants to their actual enclosing row, including optional inline records whose presence is established by descendants.
- Accept independently supplied occurrence keys and gapped strict positions; sort reconstruction metadata through Mountainash without inferring business-cell types.
- Add contextual native/configuration/data errors, explicit unsupported-capability refusals, typed package exports and an executable round-trip example.
- Resolve postponed annotations against the dataclass namespace, retaining nested record classes and class-local scalar aliases.
- Add finite recursive role tables and explicit-stack traversal, distinguishing containment cycles from repeated use of the same object.
- Preserve nested lists, insertion-ordered string maps, fixed tuples, and nullable structured members with explicit null markers.
- Retain declaring-class annotation namespaces under inheritance and support empty fixed tuples.
- Reject detached recursive rows and contradictory null members without discarding business columns named like inactive metadata.
