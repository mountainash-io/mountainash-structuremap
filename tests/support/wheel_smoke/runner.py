from __future__ import annotations

import json
import sys
from importlib.metadata import distribution
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mountainash.typespec import TypeSpec, UniversalType  # noqa: E402
from wheel_models import Entry, Ledger, Node  # noqa: E402

from mountainash_structuremap import decompose, derive, prepare, reconstruct  # noqa: E402


def tables_for_ledger():
    prepared = prepare(derive(Ledger))
    supplied = {
        "root": [{"_sm_key": "header"}],
        "entries": [
            {"_sm_key": "late", "_sm_parent": "header", "_sm_position": 20, "label": "end"},
            {"_sm_key": "null", "_sm_parent": "header", "_sm_position": 3, "label": None},
            {"_sm_key": "empty", "_sm_parent": "header", "_sm_position": 8, "label": ""},
        ],
    }
    return prepared, supplied


def assert_typespec(prepared, member_table: str) -> None:
    assert isinstance(prepared.tables["root"].typespec, TypeSpec)
    fields = {field.name: field.type for field in prepared.tables[member_table].typespec.fields}
    assert fields["label"] == UniversalType.STRING
    assert fields["_sm_position"] == UniversalType.INTEGER
    assert fields["_sm_key"] == UniversalType.STRING


def installed_path() -> None:
    import mountainash_structuremap

    package = Path(mountainash_structuremap.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    print(f"Installed package: {package}")
    dependency_source = distribution("mountainash").read_text("direct_url.json")
    assert dependency_source is not None
    print(f"Mountainash commit: {json.loads(dependency_source)['vcs_info']['commit_id']}")


def main(mode: str) -> None:
    installed_path()
    if mode == "independent":
        prepared, supplied = tables_for_ledger()
        assert_typespec(prepared, "entries")
        original = json.loads(json.dumps(supplied))
        rebuilt = reconstruct(supplied, prepared)
        assert rebuilt == Ledger([Entry(None), Entry(""), Entry("end")])
        normalized = decompose(rebuilt, prepared)
        assert [row["_sm_position"] for row in normalized["entries"]] == [0, 1, 2]
        assert [row["label"] for row in normalized["entries"]] == [None, "", "end"]
        assert supplied == original
        print(f"Independent reconstruction: {rebuilt}")
    elif mode == "write":
        prepared = prepare(derive(Node))
        value = Node("root", [Node("branch", [Node("leaf", [])]), Node("sibling", [])])
        tables = decompose(value, prepared)
        for rows in tables.values():
            rows.reverse()
        serialized = json.dumps(tables, ensure_ascii=False)
        Path("tables.json").write_text(serialized, encoding="utf-8")
        print(f"Recursive transport: {serialized}")
    elif mode == "read":
        prepared = prepare(derive(Node))
        tables = json.loads(Path("tables.json").read_text(encoding="utf-8"))
        rebuilt = reconstruct(tables, prepared)
        assert rebuilt == Node("root", [Node("branch", [Node("leaf", [])]), Node("sibling", [])])
        assert_typespec(prepared, "children")
        print(f"Fresh-process reconstruction: {rebuilt}")
    else:
        raise ValueError(f"unknown mode: {mode}")


if __name__ == "__main__":
    main(sys.argv[1])
