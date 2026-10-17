import json
from contextlib import contextmanager
from io import BytesIO

import pyarrow.parquet as pq
import pytest
from mountainash_files import FileSourceSpec
from mountainash_files.formats.xml import XmlSpec

from mountainash_structuremap import StructureMapError, derive, prepare
from tests.fixtures.execution_models import Document, execution_mapping


def prepared_document():
    return prepare(derive(Document), execution_mapping())


@pytest.mark.parametrize(
    "source",
    [
        FileSourceSpec(path="missing.xml", glob="*.xml"),
        FileSourceSpec(path="{document}.xml"),
        FileSourceSpec(path="missing.xml", on_missing="skip"),
    ],
)
def test_xml_rejects_nonconcrete_source_before_output_creation(tmp_path, source):
    from mountainash_structuremap import execution

    with pytest.raises(StructureMapError) as caught:
        execution.decompose_xml_to(
            source, prepared_document(), execution.OutputTarget(tmp_path, "out"), record_path="entries"
        )
    assert caught.value.code == "nonconcrete_source"
    assert caught.value.path == source.path
    assert caught.value.remedy
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("format", [XmlSpec(xpath="./entry"), XmlSpec(encoding="latin-1"), "json"])
def test_xml_rejects_files_parse_policy_before_input(tmp_path, format):
    from mountainash_structuremap import execution

    with pytest.raises(StructureMapError) as caught:
        execution.decompose_xml_to(
            FileSourceSpec(path="missing.xml", format=format),
            prepared_document(),
            execution.OutputTarget(tmp_path, "out"),
            record_path="entries",
        )
    assert caught.value.code == "invalid_source"
    assert caught.value.remedy
    assert list(tmp_path.iterdir()) == []


def test_xml_destination_collision_precedes_input_acquisition(tmp_path):
    from mountainash_structuremap import execution

    target = tmp_path / "out"
    target.mkdir()
    (target / "saved").write_text("previous output")
    with pytest.raises(StructureMapError) as caught:
        execution.decompose_xml_to(
            FileSourceSpec(path="missing.xml"),
            prepared_document(),
            execution.OutputTarget(tmp_path, "out"),
            record_path="entries",
        )
    assert caught.value.code == "output_collision"
    assert (target / "saved").read_text() == "previous output"
    assert not list(tmp_path.glob(".structuremap-staging-*"))


@pytest.mark.parametrize("completion", ["atomic", "keep_partial"])
def test_xml_parse_failure_closes_stream_and_never_publishes_partial_document(tmp_path, monkeypatch, completion):
    import mountainash_files

    from mountainash_structuremap import execution

    handle = BytesIO(b'<document label="native"><entry><value>7</value></entry>')

    @contextmanager
    def source_stream(source, *, access):
        with handle:
            yield handle

    monkeypatch.setattr(mountainash_files, "stream", source_stream)
    with pytest.raises(StructureMapError) as caught:
        execution.decompose_xml_to(
            FileSourceSpec(path="controlled.xml"),
            prepared_document(),
            execution.OutputTarget(tmp_path, "out", completion=completion),
            record_path="entries",
            buffer_rows=1,
        )
    assert handle.closed
    assert caught.value.__cause__ is not None
    assert not (tmp_path / "out").exists()
    staging = list(tmp_path.glob(".structuremap-staging-*"))
    if completion == "atomic":
        assert staging == []
    else:
        manifest = json.loads((staging[0] / "manifest.json").read_text())
        assert manifest["status"] == "failed"
        entries = {entry["name"]: entry for entry in manifest["tables"]}
        assert entries["entries"]["rows"] == 1
        assert entries["root"]["rows"] == 0
        table = pq.read_table(staging[0] / entries["entries"]["path"])
        assert table.column("value").to_pylist() == [7]


def test_xml_close_error_does_not_replace_native_parse_failure(tmp_path, monkeypatch):
    import mountainash_files

    from mountainash_structuremap import execution

    handle = BytesIO(b'<document label="native"><unknown/></document>')
    close_error = OSError("input close failed")

    @contextmanager
    def source_stream(source, *, access):
        try:
            yield handle
        finally:
            handle.close()
            raise close_error

    monkeypatch.setattr(mountainash_files, "stream", source_stream)
    with pytest.raises(StructureMapError) as caught:
        execution.decompose_xml_to(
            FileSourceSpec(path="controlled.xml"),
            prepared_document(),
            execution.OutputTarget(tmp_path, "out"),
            record_path="entries",
        )
    assert caught.value.code == "native_parse_failed"
    assert caught.value.__cause__ is not close_error
    assert "input close failed" in " ".join(caught.value.__notes__)
    assert handle.closed
    assert not list(tmp_path.glob(".structuremap-staging-*"))
