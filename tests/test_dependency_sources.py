from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from scripts.fetch_dependency_sources import Source

spec = importlib.util.spec_from_file_location(
    "fetch_dependency_sources",
    Path(__file__).resolve().parents[1] / "scripts" / "fetch_dependency_sources.py",
)
assert spec is not None and spec.loader is not None
sources = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sources
spec.loader.exec_module(sources)


def _source(content: bytes = b"source") -> Source:
    return sources.Source(
        component="test",
        version="1",
        filename="source.tar.gz",
        url="https://example.test/source.tar.gz",
        sha256=hashlib.sha256(content).hexdigest(),
        license="MIT",
    )


def _manifest(tmp_path: Path, external: list[dict[str, str]]) -> tuple[Path, Path]:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audit_status": "pending",
                "sandbox_validation_status": "pending",
                "external": external,
                "pypi": [],
            }
        ),
        encoding="utf-8",
    )
    lock = tmp_path / "uv.lock"
    lock.write_text("package = []\n", encoding="utf-8")
    return manifest, lock


def test_project_source_manifest_matches_locked_package_versions() -> None:
    audit, sandbox_validation, entries = sources.read_sources()
    assert audit == "pending"
    assert sandbox_validation == "pending"
    assert len(entries) == 30
    assert {entry.component for entry in entries} >= {
        "Qt-qtbase",
        "Qt-qtcharts",
        "PySide6-shiboken6",
        "CPython",
        "pydantic-core",
        "pyinstaller",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("filename", "../source.tar.gz"),
        ("filename", "nested\\source.tar.gz"),
        ("url", "http://example.test/source.tar.gz"),
        ("url", "https://user:password@example.test/source.tar.gz"),
        ("sha256", "0" * 63),
    ],
)
def test_manifest_rejects_unsafe_entries(tmp_path: Path, field: str, value: str) -> None:
    entry = sources.source_records([_source()])[0]
    entry[field] = value
    manifest, lock = _manifest(tmp_path, [entry])
    with pytest.raises(ValueError):
        sources.read_sources(manifest, lock)


def test_manifest_rejects_duplicate_component_and_filename(tmp_path: Path) -> None:
    entry = sources.source_records([_source()])[0]
    manifest, lock = _manifest(tmp_path, [entry, entry.copy()])
    with pytest.raises(ValueError, match="重複"):
        sources.read_sources(manifest, lock)


def test_manifest_rejects_missing_or_different_locked_source(tmp_path: Path) -> None:
    manifest, lock = _manifest(tmp_path, [])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["pypi"] = [{"name": "example", "version": "1", "license": "MIT"}]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="uv.lock"):
        sources.read_sources(manifest, lock)


def test_manifest_rejects_invalid_sandbox_validation_status(tmp_path: Path) -> None:
    manifest, lock = _manifest(tmp_path, [sources.source_records([_source()])[0]])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["sandbox_validation_status"] = "unknown"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Sandbox / VM"):
        sources.read_sources(manifest, lock)


def test_fetch_verifies_hash_and_reuses_only_matching_file(tmp_path: Path) -> None:
    entry = _source()
    calls: list[str] = []

    def open_source(url: str) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(b"source")

    sources.fetch_sources([entry], tmp_path, opener=open_source)
    sources.fetch_sources([entry], tmp_path, opener=open_source)
    sources.verify_sources([entry], tmp_path)
    assert calls == [entry.url]
    assert (tmp_path / entry.filename).read_bytes() == b"source"
    assert len(list(tmp_path.iterdir())) == 1


def test_fetch_rejects_corruption_without_overwriting_or_leaving_partials(tmp_path: Path) -> None:
    entry = _source()
    with pytest.raises(ValueError, match="SHA256"):
        sources.fetch_sources([entry], tmp_path, opener=lambda url: io.BytesIO(b"incorrect"))
    assert list(tmp_path.iterdir()) == []

    existing = tmp_path / entry.filename
    existing.write_bytes(b"old")
    with pytest.raises(ValueError, match="SHA256"):
        sources.fetch_sources([entry], tmp_path, opener=lambda url: pytest.fail("再取得しました"))
    assert existing.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [existing]


def _core_with_cargo_lock(tmp_path: Path, lock: str) -> Source:
    filename = "pydantic_core-1.tar.gz"
    path = tmp_path / filename
    content = lock.encode("utf-8")
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("pydantic_core-1/Cargo.lock")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return sources.Source(
        "pydantic-core",
        "1",
        filename,
        "https://example.test/pydantic-core.tar.gz",
        sources.sha256_file(path),
        "MIT",
    )


def _cargo_lock(entry: str) -> str:
    return f'[[package]]\nname = "pydantic-core"\nversion = "1"\n[[package]]\n{entry}\n'


def test_cargo_sources_come_from_verified_pydantic_lock(tmp_path: Path) -> None:
    core = _core_with_cargo_lock(
        tmp_path,
        _cargo_lock(
            'name = "wasi"\nversion = "0.1.0+build"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            f'checksum = "{"a" * 64}"'
        ),
    )
    (item,) = sources.read_cargo_sources([core], tmp_path)
    assert item.component == "cargo:wasi@0.1.0+build"
    assert item.filename == "wasi-0.1.0+build.crate"
    assert item.url.endswith("/wasi/wasi-0.1.0%2Bbuild.crate")
    assert item.sha256 == "a" * 64

    (tmp_path / core.filename).write_bytes(b"modified")
    with pytest.raises(ValueError, match="SHA256"):
        sources.read_cargo_sources([core], tmp_path)


@pytest.mark.parametrize(
    "entry",
    [
        'name = "../secret"\nversion = "1"',
        'name = "example"\nversion = "../1"',
        'name = "example"\nversion = "1"\nsource = "git+https://example.test/repo"',
        'name = "example"\nversion = "1"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "bad"',
    ],
)
def test_cargo_sources_reject_unsafe_or_unverified_entries(tmp_path: Path, entry: str) -> None:
    core = _core_with_cargo_lock(tmp_path, _cargo_lock(entry))
    with pytest.raises(ValueError):
        sources.read_cargo_sources([core], tmp_path)


def test_cargo_sources_reject_duplicate_and_missing_root(tmp_path: Path) -> None:
    entry = (
        'name = "example"\nversion = "1"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
        f'checksum = "{"b" * 64}"'
    )
    core = _core_with_cargo_lock(tmp_path, _cargo_lock(f"{entry}\n[[package]]\n{entry}"))
    with pytest.raises(ValueError, match="重複"):
        sources.read_cargo_sources([core], tmp_path)

    core = _core_with_cargo_lock(tmp_path, f"[[package]]\n{entry}\n")
    with pytest.raises(ValueError, match="ルート"):
        sources.read_cargo_sources([core], tmp_path)
