"""Independent Parquet -> native XML -> managed Arrow and Parquet results."""

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq
from mountainash_files import FileSourceSpec

from mountainash_structuremap import MappingSpec, UseOverride, derive, parse, prepare
from mountainash_structuremap.execution import OutputTarget, decompose_to, reconstruct_from, reconstruct_to
from mountainash_structuremap.mapping import TypeCast


@dataclass
class Line:
    quantity: str = field(metadata={"type": "Element"})


@dataclass
class Order:
    class Meta:
        name = "order"

    reference: str = field(metadata={"type": "Attribute"})
    lines: list[Line] = field(default_factory=list, metadata={"type": "Element", "name": "line"})


def mapping():
    return prepare(derive(Order), MappingSpec(overrides={"lines/quantity": UseOverride(cast=TypeCast(to="i8"))}))


def main() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        # These source files are authored independently, not by StructureMap.
        pq.write_table(pa.table({"_sm_key": ["order-42"], "reference": ["external"]}), root / "orders.parquet")
        pq.write_table(
            pa.Table.from_pylist(
                [
                    {"_sm_key": "second", "_sm_parent": "order-42", "_sm_position": 1, "quantity": 127},
                    {"_sm_key": "first", "_sm_parent": "order-42", "_sm_position": 0, "quantity": -128},
                ],
                schema=pa.schema(
                    [
                        ("_sm_key", pa.string()),
                        ("_sm_parent", pa.string()),
                        ("_sm_position", pa.int64()),
                        ("quantity", pa.int8()),
                    ]
                ),
            ),
            root / "lines.parquet",
        )
        sources = {
            "root": FileSourceSpec(path=str(root / "orders.parquet")),
            "lines": FileSourceSpec(path=str(root / "lines.parquet")),
        }
        document = reconstruct_to(sources, mapping(), OutputTarget(root, "order.xml"))
        native = parse(document.path.read_text(), mapping())
        assert native == Order("external", [Line("-128"), Line("127")])
        print("Independent Parquet -> native XML:", document.path.read_text().strip())
        for format in ("parquet", "arrow"):
            result = decompose_to(
                native, mapping(), OutputTarget(root, format, namespace="results"), format=format, buffer_rows=1
            )
            assert reconstruct_from(result.path, mapping()) == native
            print(f"Native value -> {format} -> fresh native reconstruction:", dict(result.row_counts))
        managed = reconstruct_to(sources, mapping(), OutputTarget(root, "document"), packaging="managed")
        assert parse((managed.path / "document").read_text(), mapping()) == native
        print("Standalone and managed native document publication passed")


if __name__ == "__main__":
    main()
