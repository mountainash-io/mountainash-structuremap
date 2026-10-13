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
- Reject detached recursive rows and contradictory null members; retain inactive metadata-like names as business columns in both execution directions.
- Add canonical per-use table, column and prefix overrides, finite recursive override contexts, and configuration-isolated preparation.
- Validate native constructor signatures and reject unsupported mapping policies before reading values or calling constructors.
- Keep anonymous collection levels transparent to named selectors, reserve container roles before recursive expansion, and preserve ordinary sibling role names during compilation.
- Keep future cast declarations portable without introducing a separate dtype or expression vocabulary; require Pydantic 2.5+ for JSON-compatible declaration values.
- Validate logical scalar activity and complete row reachability before native construction, including malformed later siblings and detached recursive components.
- Add independent table acceptance, isolated installed-wheel/fresh-process reconstruction, and Foundation CI jobs pinned to one Mountainash development revision.
- Resolve function-local mutual references through already known native classes in the same lexical scope without replacing declaring-class aliases.
