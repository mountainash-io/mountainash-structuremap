import json
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from mountainash_files import FileSourceSpec

from mountainash_structuremap import StructureMapError, derive, parse, prepare
from tests.fixtures.execution_models import Document, Entry, execution_mapping


def api():
    from mountainash_structuremap import execution

    return execution


def case():
    return Document("native", [Entry("-128"), Entry("127")], "done"), prepare(derive(Document), execution_mapping())


@pytest.mark.parametrize("format", ["parquet", "arrow"])
def test_public_persistence_reconstructs_with_fresh_mapping_and_namespace(tmp_path, format):
    execution = api()
    native, prepared = case()
    result = execution.decompose_to(
        native,
        prepared,
        execution.OutputTarget(tmp_path, "orders", namespace="customer/a"),
        format=format,
        buffer_rows=1,
    )
    assert result.path == tmp_path / "customer" / "a" / "orders"
    assert result.kind == "tables"
    assert result.row_counts == {"root": 1, "entries": 2}
    fresh = prepare(derive(Document), execution_mapping())
    assert execution.reconstruct_from(result.path, fresh) == native
    manifest = json.loads((result.path / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert {entry["name"]: entry["rows"] for entry in manifest["tables"]} == {"root": 1, "entries": 2}


def test_arrow_operations_accept_independent_tables(tmp_path):
    execution = api()
    native, prepared = case()
    tables = execution.decompose_arrow(native, prepared)
    assert tables["entries"].schema.field("value").type == pa.int8()
    assert tables["entries"].column("value").to_pylist() == [-128, 127]
    independent = {
        "root": pa.table({"_sm_key": ["external"], "label": ["other"], "footer": ["end"]}),
        "entries": pa.Table.from_pylist(
            [
                {"_sm_key": "b", "_sm_parent": "external", "_sm_position": 1, "value": 127},
                {"_sm_key": "a", "_sm_parent": "external", "_sm_position": 0, "value": -128},
            ],
            schema=pa.schema(
                [
                    ("_sm_key", pa.string()),
                    ("_sm_parent", pa.string()),
                    ("_sm_position", pa.int64()),
                    ("value", pa.int8()),
                ]
            ),
        ),
    }
    pq.write_table(independent["entries"], tmp_path / "external.parquet")
    independent["entries"] = FileSourceSpec(path=str(tmp_path / "external.parquet"))
    assert execution.reconstruct_from(independent, prepared) == Document("other", [Entry("-128"), Entry("127")], "end")


@pytest.mark.parametrize("packaging", ["standalone", "managed"])
def test_native_output_packages_use_binding_serialization(tmp_path, packaging):
    execution = api()
    native, prepared = case()
    target = execution.OutputTarget(tmp_path, "restored.xml" if packaging == "standalone" else "restored")
    result = execution.reconstruct_to(
        execution.decompose_arrow(native, prepared), prepared, target, packaging=packaging
    )
    document = result.path if packaging == "standalone" else result.path / "document"
    assert parse(document.read_text(), prepared) == native
    assert result.kind == "document"
    if packaging == "managed":
        assert json.loads((result.path / "manifest.json").read_text())["document"] == {"path": "document"}
    else:
        assert sorted(path.name for path in tmp_path.iterdir()) == ["restored.xml"]


@pytest.mark.parametrize("packaging", ["standalone", "managed"])
def test_destination_collision_precedes_input_and_preserves_existing_result(tmp_path, packaging):
    execution = api()
    _, prepared = case()
    target = tmp_path / "existing"
    if packaging == "managed":
        target.mkdir()
        payload = target / "saved"
    else:
        payload = target
    payload.write_text("previous successful output")
    with pytest.raises(StructureMapError) as caught:
        execution.reconstruct_to(
            tmp_path / "nonexistent-input", prepared, execution.OutputTarget(tmp_path, target.name), packaging=packaging
        )
    assert caught.value.code == "output_collision"
    assert payload.read_text() == "previous successful output"
    assert not list(tmp_path.glob(".structuremap-staging-*"))


def test_explicit_replacement_allows_file_and_empty_directory_not_nonempty_directory(tmp_path):
    execution = api()
    native, prepared = case()
    tables = execution.decompose_arrow(native, prepared)
    (tmp_path / "doc.xml").write_text("old")
    execution.reconstruct_to(tables, prepared, execution.OutputTarget(tmp_path, "doc.xml", replace=True))
    assert parse((tmp_path / "doc.xml").read_text(), prepared) == native
    (tmp_path / "tables").mkdir()
    execution.decompose_to(native, prepared, execution.OutputTarget(tmp_path, "tables", replace=True))
    with pytest.raises(StructureMapError) as caught:
        execution.decompose_to(native, prepared, execution.OutputTarget(tmp_path, "tables", replace=True))
    assert caught.value.code == "output_collision"
    assert execution.reconstruct_from(tmp_path / "tables", prepared) == native


def test_preflight_filesystem_failure_retains_output_context_and_cause(tmp_path, monkeypatch):
    execution = api()
    native, prepared = case()
    target = tmp_path / "tables"
    target.mkdir()
    original = Path.iterdir
    failure = PermissionError("cannot inspect destination")

    def unreadable(path):
        if path == target:
            raise failure
        return original(path)

    monkeypatch.setattr(Path, "iterdir", unreadable)
    with pytest.raises(StructureMapError) as caught:
        execution.decompose_to(native, prepared, execution.OutputTarget(tmp_path, "tables", replace=True))
    assert caught.value.__cause__ is failure
    assert caught.value.path == str(target)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tables"]


@pytest.mark.parametrize(
    ("name", "namespace"),
    [
        ("../escape", None),
        ("nested/", None),
        ("nested/.", None),
        ("safe", "../escape"),
        ("safe", "/absolute"),
        ("safe", "a/./b"),
        (".structuremap-staging-forged", None),
    ],
)
def test_invalid_output_location_fails_before_creating_staging(tmp_path, name, namespace):
    execution = api()
    native, prepared = case()
    with pytest.raises(StructureMapError) as caught:
        execution.decompose_to(native, prepared, execution.OutputTarget(tmp_path, name, namespace=namespace))
    assert caught.value.code == "invalid_output"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("completion", ["atomic", "keep_partial"])
def test_writer_close_failure_retains_primary_cause_and_observed_progress(tmp_path, monkeypatch, completion):
    execution = api()
    from mountainash_structuremap.table_io import FileTableSink

    native, prepared = case()
    original = FileTableSink.close
    failure = OSError("writer footer failed")

    def fail_close(self, *, primary_error=None):
        original(self, primary_error=primary_error)
        if primary_error is None:
            raise failure

    monkeypatch.setattr(FileTableSink, "close", fail_close)
    with pytest.raises(StructureMapError) as caught:
        execution.decompose_to(
            native, prepared, execution.OutputTarget(tmp_path, "tables", completion=completion), buffer_rows=1
        )
    assert caught.value.__cause__ is failure
    assert not (tmp_path / "tables").exists()
    retained = list(tmp_path.glob(".structuremap-staging-*"))
    if completion == "atomic":
        assert retained == []
    else:
        manifest = json.loads((retained[0] / "manifest.json").read_text())
        assert manifest["status"] == "failed"
        assert {entry["name"]: entry["rows"] for entry in manifest["tables"]} == {"root": 1, "entries": 2}
        with pytest.raises(StructureMapError):
            execution.reconstruct_from(retained[0], prepared)


@pytest.mark.parametrize("completion", ["atomic", "keep_partial"])
def test_unpublished_complete_manifest_cannot_be_read_after_secondary_failure(tmp_path, monkeypatch, completion):
    execution = api()
    from mountainash_structuremap import publication

    native, prepared = case()
    failure = OSError("publication failed")
    original_write = Path.write_text

    def fail_replace(source, destination):
        raise failure

    def fail_cleanup(path, *args, **kwargs):
        raise OSError("cleanup failed")

    def fail_failed_metadata(path, data, *args, **kwargs):
        if path.name == "manifest.json" and json.loads(data)["status"] == "failed":
            raise OSError("failed metadata write failed")
        return original_write(path, data, *args, **kwargs)

    monkeypatch.setattr(publication.os, "replace", fail_replace)
    if completion == "atomic":
        monkeypatch.setattr(publication.shutil, "rmtree", fail_cleanup)
    else:
        monkeypatch.setattr(Path, "write_text", fail_failed_metadata)
    with pytest.raises(StructureMapError) as caught:
        execution.decompose_to(native, prepared, execution.OutputTarget(tmp_path, "tables", completion=completion))
    assert caught.value.__cause__ is failure
    assert caught.value.__notes__
    retained = list(tmp_path.glob(".structuremap-staging-*"))
    assert json.loads((retained[0] / "manifest.json").read_text())["status"] == "complete"
    with pytest.raises(StructureMapError) as rejected:
        execution.reconstruct_from(retained[0], prepared)
    assert rejected.value.code == "incomplete_output"
    assert not (tmp_path / "tables").exists()


@pytest.mark.parametrize("mutation", ["escape", "duplicate", "failed", "format_array", "format_object"])
def test_managed_reader_rejects_invalid_manifest_before_following_sources(tmp_path, mutation):
    execution = api()
    native, prepared = case()
    result = execution.decompose_to(native, prepared, execution.OutputTarget(tmp_path, "tables"))
    path = result.path / "manifest.json"
    manifest = json.loads(path.read_text())
    if mutation == "escape":
        manifest["tables"][0]["path"] = "../outside.parquet"
    elif mutation == "duplicate":
        manifest["tables"].append(dict(manifest["tables"][0]))
    elif mutation in {"format_array", "format_object"}:
        manifest["tables"][0]["format"] = [] if mutation == "format_array" else {}
    else:
        manifest["status"] = "failed"
    path.write_text(json.dumps(manifest))
    with pytest.raises(StructureMapError) as caught:
        execution.reconstruct_from(result.path, prepared)
    assert caught.value.code in {"invalid_manifest", "incomplete_output"}


def test_unavailable_document_codec_and_direction_fail_before_input(tmp_path):
    execution = api()

    @dataclass
    class Plain:
        value: str

    plain = prepare(derive(Plain))
    with pytest.raises(StructureMapError) as caught:
        execution.reconstruct_to(tmp_path / "missing", plain, execution.OutputTarget(tmp_path, "output"))
    assert caught.value.code == "unavailable_capability"
    native, _ = case()
    only_reconstruct = prepare(derive(Document), execution_mapping(), directions=frozenset({"reconstruct"}))
    with pytest.raises(StructureMapError) as caught:
        execution.decompose_to(native, only_reconstruct, execution.OutputTarget(tmp_path, "output"))
    assert caught.value.code == "unavailable_direction"
    assert list(tmp_path.iterdir()) == []


def test_native_serialization_failure_retains_diagnostic_output_and_old_file(tmp_path, monkeypatch):
    execution = api()
    native, prepared = case()
    target = tmp_path / "document.xml"
    target.write_text("previous successful document")
    failure = ValueError("native serializer failed")

    def fail_serialize(value, mapping):
        raise failure

    monkeypatch.setattr(execution, "serialize", fail_serialize)
    with pytest.raises(StructureMapError) as caught:
        execution.reconstruct_to(
            execution.decompose_arrow(native, prepared),
            prepared,
            execution.OutputTarget(tmp_path, target.name, replace=True, completion="keep_partial"),
        )
    assert caught.value.__cause__ is failure
    assert target.read_text() == "previous successful document"
    (staging,) = tmp_path.glob(".structuremap-staging-*")
    assert json.loads((staging / "manifest.json").read_text())["status"] == "failed"


def test_standalone_publication_failure_preserves_previous_file(tmp_path, monkeypatch):
    execution = api()
    from mountainash_structuremap import publication

    native, prepared = case()
    target = tmp_path / "document.xml"
    target.write_text("previous successful document")
    failure = OSError("final rename failed")

    def fail_replace(source, destination):
        raise failure

    monkeypatch.setattr(publication.os, "replace", fail_replace)
    with pytest.raises(StructureMapError) as caught:
        execution.reconstruct_to(
            execution.decompose_arrow(native, prepared),
            prepared,
            execution.OutputTarget(tmp_path, target.name, replace=True),
        )
    assert caught.value.__cause__ is failure
    assert target.read_text() == "previous successful document"
    assert not list(tmp_path.glob(".structuremap-staging-*"))


def test_cleanup_failure_after_standalone_publication_reports_installed_document(tmp_path, monkeypatch):
    execution = api()
    from mountainash_structuremap import publication

    native, prepared = case()
    target = tmp_path / "document.xml"
    failure = OSError("staging cleanup failed")

    def fail_cleanup(path, *args, **kwargs):
        raise failure

    monkeypatch.setattr(publication.shutil, "rmtree", fail_cleanup)
    with pytest.raises(StructureMapError) as caught:
        execution.reconstruct_to(
            execution.decompose_arrow(native, prepared), prepared, execution.OutputTarget(tmp_path, target.name)
        )
    assert caught.value.__cause__ is failure
    assert parse(target.read_text(), prepared) == native
    assert any(str(target) in note for note in caught.value.__notes__)
    (staging,) = tmp_path.glob(".structuremap-staging-*")
    assert any(str(staging) in note for note in caught.value.__notes__)
