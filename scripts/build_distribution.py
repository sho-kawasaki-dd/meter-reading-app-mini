"""Windows x64 の検証用インストーラーと対応ソース候補を作成する。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import IO, TextIO
from uuid import uuid4

import build_windows
from fetch_dependency_sources import (
    SOURCE_DIRECTORY,
    Source,
    read_cargo_sources,
    read_sources,
    sha256_file,
    source_records,
    verify_sources,
)
from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.utils.hooks import collect_data_files
from PySide6.QtCore import qVersion

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ALLOWLIST = ROOT / "packaging" / "source-files.txt"
BUNDLE_ALLOWLIST = ROOT / "packaging" / "bundle-files.txt"
INSTALLER = ROOT / "packaging" / "meter_reading_mini.iss"
LAUNCHER = ROOT / "packaging" / "windows_launcher.cs"
APP_NAME = "meter-reading-mini"
REQUIRED_ROOT_FILES = (
    "LICENSE",
    "DISTRIBUTION_LICENSE.txt",
    "THIRD_PARTY_NOTICES.txt",
    "SOURCE_CODE.txt",
)
AUDITED_MODULES = frozenset(
    {
        "meter_reading_mini",
        "PySide6",
        "shiboken6",
        "pydantic",
        "pydantic_core",
        "annotated_types",
        "typing_extensions",
        "typing_inspection",
        "tzdata",
        "platformdirs",
        "packaging",
        "setuptools",
        "_distutils_hack",
        "_pyi_rth_utils",
    }
)


def _sha256_stream(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def read_allowlist(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError(f"許可リストを読み込めません: {path}") from error
    names: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = PurePosixPath(line)
        if (
            line != line.strip()
            or "\\" in line
            or ":" in line
            or parts.is_absolute()
            or parts.as_posix() != line
            or any(part in {"", ".", ".."} for part in parts.parts)
            or line.casefold() in seen
        ):
            raise ValueError(f"許可リストのパスが不正または重複しています: {line}")
        names.append(line)
        seen.add(line.casefold())
    if not names:
        raise ValueError(f"許可リストが空です: {path}")
    return names


def _reject_link(path: Path, root: Path) -> None:
    current = path
    while current != root:
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"リンクを成果物に含められません: {current}")
        current = current.parent


def snapshot_source_files(root: Path = ROOT, allowlist: Path = SOURCE_ALLOWLIST) -> dict[str, str]:
    names = read_allowlist(allowlist)
    if "packaging/source-files.txt" not in names:
        raise ValueError("ソース許可リスト自身が含まれていません。")
    snapshot: dict[str, str] = {}
    for name in names:
        path = root.joinpath(*PurePosixPath(name).parts)
        _reject_link(path, root)
        if not path.is_file():
            raise ValueError(f"許可リストのソースがありません: {name}")
        lowered = name.casefold()
        if lowered.endswith(
            (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".bak", ".log")
        ) or any(part.casefold() in {"backups", "logs"} for part in PurePosixPath(name).parts):
            raise ValueError(f"業務データをソースに含められません: {name}")
        snapshot[name] = sha256_file(path)

    expected = set(names)
    for subtree in ("src", "tests", "scripts", "packaging"):
        directory = root / subtree
        if not directory.is_dir():
            raise ValueError(f"ソースディレクトリがありません: {subtree}")
        for path in directory.rglob("*"):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or relative.suffix == ".pyc":
                continue
            if (
                relative.parts[:2] == ("packaging", "dependency-sources")
                and relative.name != "manifest.json"
            ):
                continue
            _reject_link(path, root)
            if path.is_file() and relative.as_posix() not in expected:
                raise ValueError(f"許可リストにないソースがあります: {relative}")
    return snapshot


def _project_version() -> str:
    try:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"アプリの版を読み込めません: {error}") from error
    version = project.get("project", {}).get("version")
    if (
        not isinstance(version, str)
        or not version
        or not all(part.isdigit() for part in version.split("."))
    ):
        raise ValueError("アプリの版は数字とピリオドで指定してください。")
    return version


def verify_environment(sources: list[Source]) -> dict[str, str]:
    build_windows._require_windows_x64()
    if sys.version_info[:3] != (3, 13, 12):
        raise ValueError("監査対象の Python 3.13.12 が必要です。")
    build_file = Path(sys.base_prefix) / "BUILD"
    if not build_file.is_file() or build_file.read_text(encoding="ascii").strip() != "20260211":
        raise ValueError("監査対象の python-build-standalone 20260211 が必要です。")
    if qVersion() != "6.12.0":
        raise ValueError(f"監査対象外の Qt です: {qVersion()}")
    if sqlite3.sqlite_version != "3.50.4" or not ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.5 "):
        raise ValueError("監査対象外の SQLite または OpenSSL が含まれています。")
    dependencies: dict[str, str] = {
        "python": platform.python_version(),
        "python-build-standalone": "20260211",
        "qt": qVersion(),
        "sqlite": sqlite3.sqlite_version,
        "openssl": ssl.OPENSSL_VERSION,
    }
    for source in sources:
        if source.url.startswith("https://files.pythonhosted.org/"):
            try:
                installed = importlib.metadata.version(source.component)
            except importlib.metadata.PackageNotFoundError as error:
                raise ValueError(f"依存パッケージがありません: {source.component}") from error
            if installed != source.version:
                raise ValueError(
                    f"依存パッケージの版が一致しません: {source.component} "
                    f"(期待 {source.version}、実際 {installed})"
                )
            dependencies[source.component] = installed
    for name in ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6"):
        installed = importlib.metadata.version(name)
        if installed != "6.12.0":
            raise ValueError(f"監査対象外の {name} です: {installed}")
        dependencies[name] = installed
    return dependencies


def tzdata_files() -> dict[str, Path]:
    result: dict[str, Path] = {}
    for source, destination in collect_data_files("tzdata"):
        source_path = Path(source)
        relative = (Path("_internal") / destination / source_path.name).as_posix()
        if relative in result:
            raise ValueError(f"タイムゾーン資産が重複しています: {relative}")
        result[relative] = source_path
    if not result:
        raise ValueError("タイムゾーン資産を確認できません。")
    return result


def _verify_pyz(executable: Path) -> None:
    archive = CArchiveReader(str(executable)).open_embedded_archive("PYZ.pyz")
    top_level = {name.split(".", 1)[0] for name in archive.toc}
    unknown = top_level - set(sys.stdlib_module_names) - AUDITED_MODULES
    if unknown:
        raise ValueError(f"監査対象外の Python モジュールがあります: {', '.join(sorted(unknown))}")


def verify_bundle(
    directory: Path,
    snapshot: dict[str, str],
    *,
    allowlist: Path = BUNDLE_ALLOWLIST,
) -> dict[str, str]:
    build_windows._validate_distribution(directory / "app")
    build_windows._validate_executable(directory / f"{APP_NAME}.exe")
    app_files = read_allowlist(allowlist)
    tzdata = tzdata_files()
    expected: set[str] = set(REQUIRED_ROOT_FILES)
    expected.add(f"{APP_NAME}.exe")
    expected.add("licenses/Qt-third-party-notices.txt")
    expected.add("licenses/Rust-third-party-notices.txt")
    expected.update(f"app/{name}" for name in app_files)
    expected.update(f"app/{name}" for name in tzdata)
    expected.update(
        name.removeprefix("packaging/")
        for name in snapshot
        if name.startswith("packaging/licenses/")
    )
    actual: set[str] = set()
    expected_directories = {
        parent.as_posix()
        for name in expected
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    for path in directory.rglob("*"):
        _reject_link(path, directory)
        name = path.relative_to(directory).as_posix()
        if path.is_file():
            actual.add(name)
        elif path.is_dir() and name not in expected_directories:
            raise ValueError(f"許可リストにない同梱ディレクトリがあります: {name}")
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(f"同梱ファイルの不一致: 不足={missing}、許可対象外={unexpected}")
    for relative, source in tzdata.items():
        if sha256_file(directory / "app" / relative) != sha256_file(source):
            raise ValueError(f"タイムゾーン資産が一致しません: {relative}")
    for name, digest in snapshot.items():
        if name.startswith("packaging/licenses/"):
            bundled = directory / name.removeprefix("packaging/")
            if sha256_file(bundled) != digest:
                raise ValueError(f"ライセンス原文が一致しません: {name}")
    for name in REQUIRED_ROOT_FILES:
        source_name = name if name == "LICENSE" else f"packaging/{name}"
        if sha256_file(directory / name) != snapshot[source_name]:
            raise ValueError(f"配布通知が一致しません: {name}")
    _verify_pyz(directory / "app" / f"{APP_NAME}.exe")
    return {name: sha256_file(directory / name) for name in sorted(actual)}


def write_qt_notices(sources: list[Source], source_dir: Path, destination: Path) -> None:
    qt_sources = [
        source for source in sources if source.component in {"Qt-qtbase", "Qt-qtsvg", "Qt-qtcharts"}
    ]
    if len(qt_sources) != 3:
        raise ValueError("Qt の対応ソースが不足しています。")
    with destination.open("w", encoding="utf-8", newline="\n") as output:
        for source in qt_sources:
            count = 0
            with tarfile.open(source_dir / source.filename, "r:xz") as archive:
                for member in archive:
                    name = PurePosixPath(member.name)
                    if not member.isfile() or name.is_absolute() or ".." in name.parts:
                        continue
                    base = name.name.casefold()
                    if not (
                        base == "qt_attribution.json"
                        or base in {"license", "license.txt", "copying", "copying.txt"}
                        or "LICENSES" in name.parts
                    ):
                        continue
                    if member.size > 2 * 1024 * 1024:
                        raise ValueError(f"Qt 通知が大きすぎます: {member.name}")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError(f"Qt 通知を読み込めません: {member.name}")
                    with stream:
                        content = stream.read().decode("utf-8")
                    output.write(f"===== {source.component}/{member.name} =====\n")
                    output.write(content)
                    output.write("\n\n")
                    count += 1
            if not count:
                raise ValueError(f"Qt の第三者通知がありません: {source.component}")


def _cargo_upstream_notices(
    crate: tarfile.TarFile,
    source: Source,
    sources: list[Source],
    source_dir: Path,
    output: TextIO,
) -> int:
    component = source.component.replace("cargo:", "cargo-vcs:", 1)
    matches = [item for item in sources if item.component == component]
    if not matches:
        return 0
    if len(matches) != 1 or not matches[0].filename.endswith(".tar.gz"):
        raise ValueError(f"Cargo の上流ソースが不正です: {source.component}")
    upstream = matches[0]
    crate_root = source.filename.removesuffix(".crate")
    metadata_name = f"{crate_root}/.cargo_vcs_info.json"
    metadata_members = [member for member in crate if member.name == metadata_name]
    if (
        len(metadata_members) != 1
        or not metadata_members[0].isfile()
        or metadata_members[0].size > 1024 * 1024
    ):
        raise ValueError(f"Cargo の上流コミット情報が不正です: {source.filename}")
    stream = crate.extractfile(metadata_members[0])
    if stream is None:
        raise ValueError(f"Cargo の上流コミット情報を読み込めません: {source.filename}")
    with stream:
        metadata = json.load(stream)
    git = metadata.get("git") if isinstance(metadata, dict) else None
    path_in_vcs = metadata.get("path_in_vcs") if isinstance(metadata, dict) else None
    if (
        not isinstance(git, dict)
        or git.get("sha1") != upstream.version
        or git.get("dirty", False) is not False
        or not isinstance(path_in_vcs, str)
        or "\\" in path_in_vcs
        or ":" in path_in_vcs
        or (path_in_vcs and PurePosixPath(path_in_vcs).as_posix() != path_in_vcs)
        or ".." in PurePosixPath(path_in_vcs).parts
        or PurePosixPath(path_in_vcs).is_absolute()
    ):
        raise ValueError(f"Cargo の上流コミットまたはパスが一致しません: {source.filename}")

    crate_files = [
        member
        for member in crate
        if member.isfile()
        and member.name.startswith(f"{crate_root}/")
        and member.name.endswith(".rs")
    ]
    if not crate_files:
        raise ValueError(f"Cargo の Rust ソースがありません: {source.filename}")
    upstream_root = upstream.filename.removesuffix(".tar.gz")
    with tarfile.open(source_dir / upstream.filename, "r:gz") as archive:
        members = [member for member in archive if member.isfile()]
        by_name = {member.name: member for member in members}
        if len(by_name) != len(members):
            raise ValueError(f"Cargo の上流ソースに重複があります: {upstream.filename}")
        for member in crate_files:
            relative = PurePosixPath(member.name.removeprefix(f"{crate_root}/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Cargo のソースパスが不正です: {member.name}")
            name = PurePosixPath(upstream_root, path_in_vcs, relative).as_posix()
            other = by_name.get(name)
            if other is None or member.size != other.size:
                raise ValueError(f"Cargo の上流ソースが一致しません: {member.name}")
            local_stream = crate.extractfile(member)
            upstream_stream = archive.extractfile(other)
            if local_stream is None or upstream_stream is None:
                raise ValueError(f"Cargo の上流ソースを読み込めません: {member.name}")
            with local_stream, upstream_stream:
                if _sha256_stream(local_stream) != _sha256_stream(upstream_stream):
                    raise ValueError(f"Cargo の上流ソースが一致しません: {member.name}")

        count = 0
        for member in members:
            name = PurePosixPath(member.name)
            if (
                len(name.parts) != 2
                or name.parts[0] != upstream_root
                or not name.name.casefold().startswith(
                    ("license", "copying", "notice", "copyright", "authors")
                )
            ):
                continue
            if member.size > 2 * 1024 * 1024:
                raise ValueError(f"Cargo の上流通知が大きすぎます: {member.name}")
            notice = archive.extractfile(member)
            if notice is None:
                raise ValueError(f"Cargo の上流通知を読み込めません: {member.name}")
            with notice:
                content = notice.read().decode("utf-8")
            output.write(f"--- {upstream.component}/{member.name} ---\n{content}\n")
            count += 1
        if not count:
            raise ValueError(f"Cargo の上流通知がありません: {upstream.filename}")
        return count


def write_cargo_notices(sources: list[Source], source_dir: Path, destination: Path) -> None:
    cargo_sources = [source for source in sources if source.component.startswith("cargo:")]
    if not cargo_sources:
        raise ValueError("Cargo の対応ソースがありません。")
    with destination.open("w", encoding="utf-8", newline="\n") as output:
        output.write(
            "Cargo.lock から取得した Rust 依存の通知候補です。配布前の監査が必要です。\n\n"
        )
        for source in cargo_sources:
            root = source.filename.removesuffix(".crate")
            with tarfile.open(source_dir / source.filename, "r:gz") as archive:
                members = archive.getmembers()
                manifests = [member for member in members if member.name == f"{root}/Cargo.toml"]
                if (
                    len(manifests) != 1
                    or not manifests[0].isfile()
                    or manifests[0].size > 2 * 1024 * 1024
                ):
                    raise ValueError(f"Cargo.toml が不正です: {source.filename}")
                stream = archive.extractfile(manifests[0])
                if stream is None:
                    raise ValueError(f"Cargo.toml を読み込めません: {source.filename}")
                with stream:
                    package = tomllib.loads(stream.read().decode("utf-8")).get("package")
                if (
                    not isinstance(package, dict)
                    or f"cargo:{package.get('name')}@{package.get('version')}" != source.component
                    or package.get("version") != source.version
                ):
                    raise ValueError(f"Cargo パッケージ情報が一致しません: {source.filename}")
                license_name = package.get("license")
                license_file = package.get("license-file")
                if not isinstance(license_name, str) and not isinstance(license_file, str):
                    raise ValueError(f"Cargo のライセンス情報がありません: {source.filename}")
                output.write(f"===== {source.component} =====\n")
                output.write(f"SHA256: {source.sha256}\n")
                output.write(f"取得元: {source.url}\n")
                if isinstance(license_name, str):
                    output.write(f"Cargo.toml のライセンス: {license_name}\n")
                count = 0
                for member in members:
                    name = PurePosixPath(member.name)
                    if (
                        not member.isfile()
                        or name.is_absolute()
                        or ".." in name.parts
                        or not name.parts
                        or name.parts[0] != root
                    ):
                        continue
                    base = name.name.casefold()
                    if not base.startswith(
                        ("license", "copying", "notice", "copyright", "authors")
                    ) and (
                        not isinstance(license_file, str)
                        or name.as_posix() != f"{root}/{license_file}"
                    ):
                        continue
                    if member.size > 2 * 1024 * 1024:
                        raise ValueError(f"Cargo 通知が大きすぎます: {member.name}")
                    notice = archive.extractfile(member)
                    if notice is None:
                        raise ValueError(f"Cargo 通知を読み込めません: {member.name}")
                    with notice:
                        content = notice.read().decode("utf-8")
                    output.write(f"--- {member.name} ---\n{content}\n")
                    count += 1
                if not count:
                    count = _cargo_upstream_notices(archive, source, sources, source_dir, output)
                if not count:
                    output.write("アーカイブ内に通知原文がなく、配布前の個別確認が必要です。\n")
                output.write("\n")


def _compiler_path() -> Path:
    windows = os.environ.get("WINDIR")
    if not windows:
        raise ValueError("Windows のシステムディレクトリを確認できません。")
    compiler = Path(windows) / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe"
    if not compiler.is_file():
        raise ValueError(f"C# コンパイラーがありません: {compiler}")
    return compiler


def _inno_version(compiler: Path) -> str:
    import winreg

    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                root = winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view)
            except FileNotFoundError:
                continue
            with root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    with winreg.OpenKey(root, winreg.EnumKey(root, index)) as entry:
                        try:
                            location, _ = winreg.QueryValueEx(entry, "InstallLocation")
                            version, _ = winreg.QueryValueEx(entry, "DisplayVersion")
                        except FileNotFoundError:
                            continue
                        if (
                            isinstance(location, str)
                            and isinstance(version, str)
                            and Path(location).resolve() == compiler.parent.resolve()
                        ):
                            return version
    raise ValueError(f"Inno Setup の登録版を確認できません: {compiler}")


def _find_inno() -> Path:
    locations = [
        Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Inno Setup 6"
        / "ISCC.exe",
    ]
    for location in locations:
        if location.is_file():
            return location
    raise ValueError("Inno Setup 6.7.3 の ISCC.exe がありません。")


def _assemble_bundle(
    distribution: Path,
    stage: Path,
    snapshot: dict[str, str],
    sources: list[Source],
    source_dir: Path,
) -> None:
    for path in distribution.rglob("*"):
        _reject_link(path, distribution)
    stage.mkdir()
    shutil.copytree(distribution, stage / "app")
    for name in REQUIRED_ROOT_FILES:
        source = ROOT / name if name == "LICENSE" else ROOT / "packaging" / name
        shutil.copyfile(source, stage / name)
    for name in snapshot:
        if name.startswith("packaging/licenses/"):
            destination = stage / name.removeprefix("packaging/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
    write_qt_notices(sources, source_dir, stage / "licenses" / "Qt-third-party-notices.txt")
    write_cargo_notices(sources, source_dir, stage / "licenses" / "Rust-third-party-notices.txt")
    subprocess.run(
        [
            str(_compiler_path()),
            "/nologo",
            "/codepage:65001",
            "/target:winexe",
            "/platform:x64",
            "/optimize+",
            f"/out:{stage / f'{APP_NAME}.exe'}",
            str(LAUNCHER),
        ],
        cwd=ROOT,
        check=True,
    )


def write_source_archive(
    destination: Path, snapshot: dict[str, str], sources: list[Source], source_dir: Path
) -> None:
    expected = dict(snapshot)
    for source in sources:
        name = f"packaging/dependency-sources/{source.filename}"
        if name in expected:
            raise ValueError(f"ソースアーカイブの名前が重複しています: {name}")
        expected[name] = source.sha256
    with zipfile.ZipFile(destination, "x", allowZip64=True) as archive:
        for name in sorted(snapshot):
            archive.write(ROOT / name, name, compress_type=zipfile.ZIP_DEFLATED)
        for source in sources:
            archive.write(
                source_dir / source.filename,
                f"packaging/dependency-sources/{source.filename}",
                compress_type=zipfile.ZIP_STORED,
            )
    with zipfile.ZipFile(destination) as archive:
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != set(expected):
            raise ValueError("ソースアーカイブの内容が許可リストと一致しません。")
        for name, digest in expected.items():
            with archive.open(name) as stream:
                if _sha256_stream(stream) != digest:
                    raise ValueError(f"ソースアーカイブの内容が一致しません: {name}")


def build(
    output_root: Path,
    source_dir: Path,
    *,
    verification_only: bool,
    inno_compiler: Path | None = None,
) -> Path:
    audit, sandbox_validation, sources = read_sources()
    if not verification_only:
        if audit != "complete":
            raise ValueError("対応ソースの監査が未完了です。配布用ビルドはできません。")
        if sandbox_validation != "complete":
            raise ValueError("Sandbox / VM 検証が未完了です。配布用ビルドはできません。")
    snapshot = snapshot_source_files()
    verify_sources(sources, source_dir)
    sources.extend(read_cargo_sources(sources, source_dir))
    verify_sources(sources, source_dir)
    dependencies = verify_environment(sources)
    inno = (inno_compiler or _find_inno()).resolve()
    if not inno.is_file() or _inno_version(inno) != "6.7.3":
        raise ValueError("Inno Setup 6.7.3 が必要です。")
    version = _project_version()
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_directory = (
        output_root
        / f"{APP_NAME}-{'verification' if verification_only else 'release'}-{uuid4().hex}"
    )
    run_directory.mkdir()
    with ExitStack() as cleanup:
        cleanup.callback(shutil.rmtree, run_directory)
        marker = run_directory / ".build-in-progress"
        marker.touch(exist_ok=False)
        prefix = f"{APP_NAME}-{version}" + ("-verification" if verification_only else "")
        setup = run_directory / f"{prefix}-setup.exe"
        source_archive = run_directory / f"{prefix}-sources.zip"
        manifest_path = run_directory / f"{prefix}-manifest.json"
        with tempfile.TemporaryDirectory(prefix=".work-", dir=run_directory) as work:
            working = Path(work)
            distribution = build_windows.build(working)
            stage = working / "bundle"
            _assemble_bundle(distribution, stage, snapshot, sources, source_dir)
            bundled = verify_bundle(stage, snapshot)
            subprocess.run(
                [
                    str(inno),
                    "/Q",
                    f"/DAppVersion={version}",
                    f"/DOutputName={setup.stem}",
                    f"/DBundleDir={stage}",
                    f"/O{run_directory}",
                    str(INSTALLER),
                ],
                cwd=ROOT,
                check=True,
            )
            if not setup.is_file() or setup.stat().st_size == 0:
                raise ValueError("インストーラーの作成を確認できません。")
            write_source_archive(source_archive, snapshot, sources, source_dir)
            verify_sources(sources, source_dir)
            if snapshot_source_files() != snapshot:
                raise ValueError("ビルド中にソースが変更されました。")
        manifest = {
            "schema_version": 1,
            "build_type": "local-verification" if verification_only else "release",
            "source_audit": audit,
            "sandbox_validation": sandbox_validation,
            "application_version": version,
            "architecture": "windows-x64",
            "inno_setup": "6.7.3",
            "dependencies": dependencies,
            "dependency_sources": source_records(sources),
            "application_source_sha256": snapshot,
            "bundled_files_sha256": bundled,
            "setup_sha256": sha256_file(setup),
            "source_archive_sha256": sha256_file(source_archive),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        sums = run_directory / "SHA256SUMS.txt"
        sums.write_text(
            "".join(
                f"{sha256_file(path)}  {path.name}\n"
                for path in (setup, source_archive, manifest_path)
            ),
            encoding="ascii",
        )
        expected = {setup.name, source_archive.name, manifest_path.name, sums.name, marker.name}
        if {path.name for path in run_directory.iterdir()} != expected:
            raise ValueError("今回の成果物以外が出力先に含まれています。")
        marker.unlink()
        cleanup.pop_all()
    return run_directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification-only", action="store_true", help="配布禁止の検証用ビルド")
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIRECTORY)
    parser.add_argument("--output-root", type=Path, default=ROOT / "dist" / "distribution")
    parser.add_argument("--inno-compiler", type=Path)
    args = parser.parse_args()
    try:
        destination = build(
            args.output_root,
            args.source_dir,
            verification_only=args.verification_only,
            inno_compiler=args.inno_compiler,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"配布資産のビルドに失敗しました: {error}", file=sys.stderr)
        return 1
    print(f"ビルドが完了しました: {destination}")
    if args.verification_only:
        print("検証用です。監査と Sandbox / VM 確認が終わるまで配布しないでください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
