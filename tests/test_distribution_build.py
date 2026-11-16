from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
spec = importlib.util.spec_from_file_location(
    "build_distribution", SCRIPTS / "build_distribution.py"
)
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
original_path = sys.path[:]
try:
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(builder)
finally:
    sys.path[:] = original_path


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file(path: Path, content: bytes = b"content") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _source_tree(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    root = tmp_path / "source"
    names = [
        "src/module.py",
        "tests/test_module.py",
        "scripts/build.py",
        "packaging/licenses/license.txt",
        "packaging/dependency-sources/manifest.json",
        "packaging/source-files.txt",
        "README.md",
    ]
    for name in names:
        _file(root / name)
    manifest = root / "packaging" / "source-files.txt"
    manifest.write_text("\n".join(names) + "\n", encoding="utf-8")
    return root, manifest, names


@pytest.mark.parametrize(
    "name",
    ["../secret", "/absolute", "sub\\file", "C:/file", "sub//file", " name", "name "],
)
def test_allowlist_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    path = tmp_path / "files.txt"
    path.write_text(f"{name}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="パスが不正"):
        builder.read_allowlist(path)


def test_allowlist_rejects_case_insensitive_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "files.txt"
    path.write_text("dir/File\ndir/file\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重複"):
        builder.read_allowlist(path)


def test_source_snapshot_rejects_unlisted_and_business_files(tmp_path: Path) -> None:
    root, manifest, names = _source_tree(tmp_path)
    snapshot = builder.snapshot_source_files(root, manifest)
    assert set(snapshot) == set(names)
    _file(root / "packaging" / "dependency-sources" / "downloaded.tar.gz")
    assert builder.snapshot_source_files(root, manifest) == snapshot

    _file(root / "src" / "unexpected.json")
    with pytest.raises(ValueError, match="許可リストにないソース"):
        builder.snapshot_source_files(root, manifest)
    (root / "src" / "unexpected.json").unlink()
    _file(root / "src" / "customer.db")
    manifest.write_text("\n".join([*names, "src/customer.db"]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="業務データ"):
        builder.snapshot_source_files(root, manifest)


def _fake_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str], Path]:
    stage = tmp_path / "stage"
    allowlist = tmp_path / "bundle-files.txt"
    allowlist.write_text("meter-reading-mini.exe\n_internal/python313.dll\n", encoding="utf-8")
    _file(stage / "app" / "meter-reading-mini.exe")
    _file(stage / "app" / "_internal" / "python313.dll")
    _file(stage / "meter-reading-mini.exe")
    _file(stage / "licenses" / "Qt-third-party-notices.txt")
    _file(stage / "licenses" / "Rust-third-party-notices.txt")
    _file(stage / "app" / "_internal" / "tzdata" / "zoneinfo" / "Asia" / "Tokyo", b"zone")
    timezone = tmp_path / "timezone"
    timezone.write_bytes(b"zone")
    snapshot = {}
    for name in builder.REQUIRED_ROOT_FILES:
        content = name.encode()
        _file(stage / name, content)
        key = name if name == "LICENSE" else f"packaging/{name}"
        snapshot[key] = _digest(content)
    _file(stage / "licenses" / "original.txt", b"license")
    snapshot["packaging/licenses/original.txt"] = _digest(b"license")
    monkeypatch.setattr(builder.build_windows, "_validate_distribution", lambda path: None)
    monkeypatch.setattr(builder.build_windows, "_validate_executable", lambda path: None)
    monkeypatch.setattr(builder, "_verify_pyz", lambda path: None)
    monkeypatch.setattr(
        builder, "tzdata_files", lambda: {"_internal/tzdata/zoneinfo/Asia/Tokyo": timezone}
    )
    return stage, snapshot, allowlist


def test_bundle_requires_exact_files_and_verified_timezone_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage, snapshot, allowlist = _fake_bundle(tmp_path, monkeypatch)
    result = builder.verify_bundle(stage, snapshot, allowlist=allowlist)
    assert result["app/_internal/tzdata/zoneinfo/Asia/Tokyo"] == _digest(b"zone")

    unexpected = stage / "app" / "data.db"
    _file(unexpected, b"private")
    with pytest.raises(ValueError, match="許可対象外"):
        builder.verify_bundle(stage, snapshot, allowlist=allowlist)
    unexpected.unlink()
    (stage / "app" / "_internal" / "tzdata" / "zoneinfo" / "Asia" / "Tokyo").write_bytes(b"old")
    with pytest.raises(ValueError, match="タイムゾーン資産"):
        builder.verify_bundle(stage, snapshot, allowlist=allowlist)


def test_bundle_rejects_missing_or_changed_legal_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage, snapshot, allowlist = _fake_bundle(tmp_path, monkeypatch)
    (stage / "licenses" / "original.txt").write_bytes(b"modified")
    with pytest.raises(ValueError, match="ライセンス原文"):
        builder.verify_bundle(stage, snapshot, allowlist=allowlist)
    (stage / "SOURCE_CODE.txt").unlink()
    with pytest.raises(ValueError, match="不足"):
        builder.verify_bundle(stage, snapshot, allowlist=allowlist)


def test_pyz_rejects_unaudited_python_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Archive:
        def __init__(self, path: str) -> None:
            pass

        def open_embedded_archive(self, name: str) -> SimpleNamespace:
            return SimpleNamespace(
                toc={"os.path": (), "meter_reading_mini.app": (), "unknown.x": ()}
            )

    monkeypatch.setattr(builder, "CArchiveReader", Archive)
    with pytest.raises(ValueError, match="unknown"):
        builder._verify_pyz(tmp_path / "app.exe")


def test_qt_notices_use_only_license_and_attribution_files(tmp_path: Path) -> None:
    entries = []
    for component in ("Qt-qtbase", "Qt-qtsvg", "Qt-qtcharts"):
        filename = f"{component}.tar.xz"
        with tarfile.open(tmp_path / filename, "w:xz") as archive:
            for name, content in (
                ("qt/qt_attribution.json", b'{"name":"test"}'),
                ("qt/LICENSES/test.txt", b"license"),
                ("qt/private.db", b"secret"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        entries.append(
            builder.Source(component, "1", filename, "https://example.test/a", "0" * 64, "GPL")
        )
    output = tmp_path / "notices.txt"
    builder.write_qt_notices(entries, tmp_path, output)
    text = output.read_text(encoding="utf-8")
    assert text.count('{"name":"test"}') == 3
    assert text.count("license\n") == 3
    assert "secret" not in text


def test_cargo_notices_record_metadata_and_only_notices(tmp_path: Path) -> None:
    filename = "example-1.crate"
    with tarfile.open(tmp_path / filename, "w:gz") as archive:
        for name, content in (
            ("example-1/Cargo.toml", b'[package]\nname="example"\nversion="1"\nlicense="MIT"\n'),
            ("example-1/LICENSE-MIT", b"license"),
            ("example-1/AUTHORS", b"author notice"),
            ("example-1/private.db", b"secret"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    entry = builder.Source(
        "cargo:example@1", "1", filename, "https://example.test/a", "0" * 64, "未監査"
    )
    output = tmp_path / "notices.txt"
    builder.write_cargo_notices([entry], tmp_path, output)
    text = output.read_text(encoding="utf-8")
    assert "cargo:example@1" in text
    assert "Cargo.toml のライセンス: MIT" in text
    assert "example-1/LICENSE-MIT" in text
    assert "example-1/AUTHORS" in text
    assert "license\n" in text
    assert "secret" not in text

    wrong_version = builder.Source(
        "cargo:example@2", "2", filename, "https://example.test/a", "0" * 64, "未監査"
    )
    with pytest.raises(ValueError, match="一致しません"):
        builder.write_cargo_notices([wrong_version], tmp_path, output)


def test_cargo_notices_flag_missing_license_file(tmp_path: Path) -> None:
    filename = "example-1.crate"
    content = b'[package]\nname="example"\nversion="1"\nlicense="MIT"\n'
    with tarfile.open(tmp_path / filename, "w:gz") as archive:
        info = tarfile.TarInfo("example-1/Cargo.toml")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    entry = builder.Source(
        "cargo:example@1", "1", filename, "https://example.test/a", "0" * 64, "未監査"
    )
    output = tmp_path / "notices.txt"
    builder.write_cargo_notices([entry], tmp_path, output)
    assert "アーカイブ内に通知原文がなく" in output.read_text(encoding="utf-8")


def test_cargo_notices_verify_upstream_sources_before_supplementing(tmp_path: Path) -> None:
    commit = "a" * 40
    filename = "example-1.crate"
    with tarfile.open(tmp_path / filename, "w:gz") as archive:
        for name, content in (
            ("example-1/Cargo.toml", b'[package]\nname="example"\nversion="1"\nlicense="MIT"\n'),
            (
                "example-1/.cargo_vcs_info.json",
                json.dumps({"git": {"sha1": commit}, "path_in_vcs": "crates/rt"}).encode(),
            ),
            ("example-1/src/lib.rs", b"fn example() {}"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    upstream_root = f"example-upstream-{commit}"
    upstream_filename = f"{upstream_root}.tar.gz"

    def write_upstream(content: bytes) -> None:
        with tarfile.open(tmp_path / upstream_filename, "w:gz") as archive:
            for name, data in (
                (f"{upstream_root}/crates/rt/src/lib.rs", content),
                (f"{upstream_root}/LICENSE-MIT", b"upstream license"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    write_upstream(b"fn example() {}")
    crate = builder.Source(
        "cargo:example@1", "1", filename, "https://example.test/a", "0" * 64, "未監査"
    )
    upstream = builder.Source(
        "cargo-vcs:example@1",
        commit,
        upstream_filename,
        "https://example.test/b",
        "0" * 64,
        "MIT",
    )
    output = tmp_path / "notices.txt"
    builder.write_cargo_notices([crate, upstream], tmp_path, output)
    text = output.read_text(encoding="utf-8")
    assert "upstream license" in text
    assert "アーカイブ内に通知原文がなく" not in text

    wrong_commit = builder.Source(
        upstream.component,
        "b" * 40,
        upstream.filename,
        upstream.url,
        upstream.sha256,
        upstream.license,
    )
    with pytest.raises(ValueError, match="上流コミット"):
        builder.write_cargo_notices([crate, wrong_commit], tmp_path, output)

    write_upstream(b"fn altered() {}")
    with pytest.raises(ValueError, match="上流ソースが一致しません"):
        builder.write_cargo_notices([crate, upstream], tmp_path, output)


def test_source_archive_contains_only_allowlisted_inputs_and_checks_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, manifest, _ = _source_tree(tmp_path)
    snapshot = builder.snapshot_source_files(root, manifest)
    source_dir = tmp_path / "dependencies"
    _file(source_dir / "dep.tar.gz", b"dependency")
    source = builder.Source(
        "dependency", "1", "dep.tar.gz", "https://example.test/dep", _digest(b"dependency"), "MIT"
    )
    monkeypatch.setattr(builder, "ROOT", root)
    destination = tmp_path / "sources.zip"
    builder.write_source_archive(destination, snapshot, [source], source_dir)
    assert destination.is_file()
    (source_dir / "dep.tar.gz").write_bytes(b"modified")
    with pytest.raises(ValueError, match="一致しません"):
        builder.write_source_archive(tmp_path / "changed.zip", snapshot, [source], source_dir)


def test_release_is_refused_when_source_audit_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder, "read_sources", lambda: ("pending", "pending", []))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="監査が未完了"):
        builder.build(output, tmp_path, verification_only=False)
    assert not output.exists()


def test_release_is_refused_when_sandbox_validation_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder, "read_sources", lambda: ("complete", "pending", []))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="Sandbox / VM 検証が未完了"):
        builder.build(output, tmp_path, verification_only=False)
    assert not output.exists()


def test_failed_build_removes_only_new_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    _file(output / "previous" / "sentinel.txt", b"keep")
    inno = tmp_path / "ISCC.exe"
    _file(inno)
    monkeypatch.setattr(builder, "read_sources", lambda: ("pending", "pending", []))
    monkeypatch.setattr(builder, "read_cargo_sources", lambda sources, directory: [])
    monkeypatch.setattr(builder, "snapshot_source_files", lambda: {})
    monkeypatch.setattr(builder, "verify_sources", lambda sources, directory: None)
    monkeypatch.setattr(builder, "verify_environment", lambda sources: {})
    monkeypatch.setattr(builder, "_inno_version", lambda compiler: "6.7.3")
    monkeypatch.setattr(builder, "_project_version", lambda: "0.1.0")
    monkeypatch.setattr(builder.build_windows, "build", lambda working: working / "new")
    monkeypatch.setattr(builder, "_assemble_bundle", lambda *args: None)
    monkeypatch.setattr(builder, "verify_bundle", lambda *args: {})

    def fail(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["ISCC"])

    monkeypatch.setattr(builder.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        builder.build(output, tmp_path, verification_only=True, inno_compiler=inno)
    assert list(output.iterdir()) == [output / "previous"]
    assert (output / "previous" / "sentinel.txt").read_bytes() == b"keep"


def test_successful_verification_output_has_exact_artifacts_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    _file(output / "previous" / "sentinel.txt", b"keep")
    inno = tmp_path / "ISCC.exe"
    _file(inno)
    monkeypatch.setattr(builder, "read_sources", lambda: ("pending", "pending", []))
    monkeypatch.setattr(builder, "read_cargo_sources", lambda sources, directory: [])
    monkeypatch.setattr(builder, "snapshot_source_files", lambda: {})
    monkeypatch.setattr(builder, "verify_sources", lambda sources, directory: None)
    monkeypatch.setattr(builder, "verify_environment", lambda sources: {"python": "3.13.12"})
    monkeypatch.setattr(builder, "_inno_version", lambda compiler: "6.7.3")
    monkeypatch.setattr(builder, "_project_version", lambda: "0.1.0")
    monkeypatch.setattr(builder.build_windows, "build", lambda working: working / "new")
    monkeypatch.setattr(builder, "_assemble_bundle", lambda *args: None)
    monkeypatch.setattr(builder, "verify_bundle", lambda *args: {"app/file": _digest(b"app")})
    monkeypatch.setattr(
        builder, "write_source_archive", lambda path, *args: path.write_bytes(b"source")
    )

    def compile_installer(command: list[str], **kwargs: object) -> None:
        name = next(
            arg.removeprefix("/DOutputName=") for arg in command if arg.startswith("/DOutputName=")
        )
        directory = next(arg.removeprefix("/O") for arg in command if arg.startswith("/O"))
        _file(Path(directory) / f"{name}.exe", b"installer")

    monkeypatch.setattr(builder.subprocess, "run", compile_installer)
    result = builder.build(output, tmp_path, verification_only=True, inno_compiler=inno)
    names = sorted(path.name for path in result.iterdir())
    prefix = "meter-reading-mini-0.1.0-verification"
    assert names == sorted(
        [
            "SHA256SUMS.txt",
            f"{prefix}-manifest.json",
            f"{prefix}-setup.exe",
            f"{prefix}-sources.zip",
        ]
    )
    manifest = json.loads((result / f"{prefix}-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_audit"] == "pending"
    assert manifest["sandbox_validation"] == "pending"
    assert manifest["build_type"] == "local-verification"
    sums = (result / "SHA256SUMS.txt").read_text(encoding="ascii")
    for name in names:
        if name != "SHA256SUMS.txt":
            assert f"{builder.sha256_file(result / name)}  {name}\n" in sums
    assert (output / "previous" / "sentinel.txt").read_bytes() == b"keep"
