"""Local staged publication; no crash-durability or concurrent-writer guarantee."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from mountainash_structuremap.errors import StructureMapError

STAGING_PREFIX = ".structuremap-staging-"


def output_path(root: Path, name: str, namespace: str | None) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or Path(name).is_absolute()
        or Path(name).name != name
        or name.startswith(STAGING_PREFIX)
    ):
        raise StructureMapError(
            "invalid_output",
            f"Invalid output name {name!r}",
            remedy="Choose a single nonempty name outside the reserved staging namespace",
        )
    if namespace is not None and (
        not isinstance(namespace, str)
        or not namespace
        or Path(namespace).is_absolute()
        or any(part in {"", ".", ".."} for part in namespace.split(os.sep))
    ):
        raise StructureMapError(
            "invalid_output",
            f"Invalid output namespace {namespace!r}",
            remedy="Use a relative namespace without empty, '.' or '..' components",
        )
    base = Path(root).resolve()
    target = (base / namespace / name if namespace is not None else base / name).resolve()
    if not target.is_relative_to(base) or target == base or target.name.startswith(STAGING_PREFIX):
        raise StructureMapError(
            "invalid_output",
            "Output escapes its root or uses a reserved staging name",
            path=str(target),
            remedy="Choose a destination contained within the output root",
        )
    return target


def execution_error(exc: Exception, target: Path) -> StructureMapError:
    if isinstance(exc, StructureMapError):
        return exc
    return StructureMapError(
        "execution_failed",
        f"Output execution failed: {exc}",
        path=str(target),
        remedy="Resolve the reported native, conversion or filesystem error before retrying",
    )


def write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


class Publication:
    """Own one staging directory and the final local publication step."""

    def __init__(
        self, target: Path, *, managed: bool, replace: bool, completion: Literal["atomic", "keep_partial"]
    ) -> None:
        if completion not in {"atomic", "keep_partial"}:
            raise StructureMapError(
                "invalid_output",
                f"Unknown completion policy {completion!r}",
                remedy="Select 'atomic' or 'keep_partial'",
            )
        self.target = target
        self.managed = managed
        self.replace = replace
        self.completion = completion
        self.staging: Path | None = None
        self.published = False
        self.preflight()

    def preflight(self) -> None:
        target = self.target
        if not target.exists() and not target.is_symlink():
            return
        if not self.replace:
            reason = "Replacement requires explicit permission"
        elif self.managed and (not target.is_dir() or any(target.iterdir())):
            reason = "Managed output requires an absent or empty directory"
        elif not self.managed and not target.is_file():
            reason = "Standalone document output requires an absent or regular file target"
        else:
            return
        raise StructureMapError(
            "output_collision",
            reason,
            path=str(target),
            remedy="Choose a new destination, or explicitly replace a compatible file/empty directory",
        )

    def stage(self) -> Path:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=self.target.parent))
        return self.staging

    def publish(self, manifest: dict[str, Any]) -> None:
        assert self.staging is not None
        if self.managed:
            write_manifest(self.staging, {**manifest, "version": 1, "status": "complete"})
        self.preflight()
        os.replace(self.staging if self.managed else self.staging / "document", self.target)
        self.published = True
        if not self.managed:
            shutil.rmtree(self.staging)

    def fail(self, primary: BaseException, manifest: dict[str, Any]) -> None:
        staging = self.staging
        if staging is None:
            return
        if self.published:
            primary.add_note(f"Output was published at {self.target}; staging cleanup failed at {staging}")
            return
        if self.completion == "keep_partial":
            try:
                write_manifest(staging, {**manifest, "version": 1, "status": "failed", "error": str(primary)})
            except Exception as secondary:
                primary.add_note(f"Failed to write failure metadata at {staging}: {secondary}")
            primary.add_note(f"Failed output retained at {staging}")
        else:
            try:
                shutil.rmtree(staging)
            except Exception as secondary:
                primary.add_note(f"Failed to remove staging at {staging}: {secondary}")


def table_sources(directory: Path) -> dict[str, Any]:
    """Resolve a completed managed table manifest, without guessing table names."""
    directory = directory.resolve()
    if directory.name.startswith(STAGING_PREFIX):
        raise StructureMapError(
            "incomplete_output",
            "Unpublished staging is not a completed managed input",
            path=str(directory),
            remedy="Select a published result or explicitly bind individual files",
        )
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StructureMapError(
            "invalid_manifest",
            f"Cannot read managed manifest: {exc}",
            path=str(directory),
            remedy="Select a complete managed result or supply explicit table sources",
        ) from exc
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise StructureMapError(
            "invalid_manifest",
            "Expected a version 1 manifest",
            path=str(directory),
            remedy="Use a supported managed result or explicit table sources",
        )
    if manifest.get("status") != "complete":
        raise StructureMapError(
            "incomplete_output",
            "Managed result is not complete",
            path=str(directory),
            remedy="Select a completed result or explicitly bind the required files",
        )
    if manifest.get("kind") != "tables" or not isinstance(manifest.get("tables"), list):
        raise StructureMapError(
            "invalid_manifest",
            "Managed input must contain table entries",
            path=str(directory),
            remedy="Select a table result rather than a native document result",
        )
    entries: dict[str, tuple[Path, str]] = {}
    for entry in manifest["tables"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("path"), str)
            or not entry["path"]
            or entry.get("format") not in ("parquet", "arrow")
            or type(entry.get("rows")) is not int
            or entry["rows"] < 0
        ):
            raise StructureMapError(
                "invalid_manifest",
                "Malformed table entry",
                path=str(directory),
                remedy="Provide table name, relative path, supported format and nonnegative row count",
            )
        name, relative = entry["name"], Path(entry["path"])
        resolved = (directory / relative).resolve()
        if (
            name in entries
            or relative.is_absolute()
            or ".." in relative.parts
            or not resolved.is_relative_to(directory)
        ):
            raise StructureMapError(
                "invalid_manifest",
                f"Duplicate table or escaping source path for {name!r}",
                path=str(directory),
                table=name,
                remedy="Use unique logical table names and source paths contained in the result",
            )
        entries[name] = (resolved, entry["format"])
    try:
        from mountainash_files import FileSourceSpec
    except ImportError as exc:
        raise StructureMapError(
            "unavailable_capability",
            "Persisted input requires Mountainash Files",
            remedy="Install mountainash-structuremap[files]",
        ) from exc
    return {name: FileSourceSpec(path=str(path), format=format) for name, (path, format) in entries.items()}
