"""版と SHA256 を固定した依存ソースを取得する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote, unquote, urlsplit
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packaging" / "dependency-sources" / "manifest.json"
LOCK = ROOT / "uv.lock"
SOURCE_DIRECTORY = MANIFEST.parent
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Source:
    component: str
    version: str
    filename: str
    url: str
    sha256: str
    license: str


def _text(entry: object, key: str) -> str:
    if not isinstance(entry, dict):
        raise ValueError(f"ソース項目が不正です: {key}")
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"ソース項目に {key} が必要です。")
    return value


def _validate_source(source: Source) -> None:
    if (
        source.filename in {".", ".."}
        or "/" in source.filename
        or "\\" in source.filename
        or ":" in source.filename
    ):
        raise ValueError(f"ソースのファイル名が不正です: {source.filename}")
    parts = urlsplit(source.url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"ソースの取得元は HTTPS を指定してください: {source.url}")
    if not re.fullmatch(r"[0-9a-f]{64}", source.sha256):
        raise ValueError(f"SHA256 が不正です: {source.filename}")


def read_sources(manifest: Path = MANIFEST, lock: Path = LOCK) -> tuple[str, str, list[Source]]:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        locked = tomllib.loads(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"依存ソース一覧を読み込めません: {error}") from error
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("依存ソース一覧の版が不正です。")
    audit = _text(data, "audit_status")
    if audit not in {"pending", "complete"}:
        raise ValueError("依存ソース一覧の監査状態が不正です。")
    sandbox_validation = _text(data, "sandbox_validation_status")
    if sandbox_validation not in {"pending", "complete"}:
        raise ValueError("Sandbox / VM 検証の状態が不正です。")
    external = data.get("external")
    pypi = data.get("pypi")
    packages = locked.get("package")
    if (
        not isinstance(external, list)
        or not isinstance(pypi, list)
        or not isinstance(packages, list)
    ):
        raise ValueError("依存ソースまたはロック済みパッケージの一覧が不正です。")

    sources = [
        Source(
            component=_text(item, "component"),
            version=_text(item, "version"),
            filename=_text(item, "filename"),
            url=_text(item, "url"),
            sha256=_text(item, "sha256"),
            license=_text(item, "license"),
        )
        for item in external
    ]
    for item in pypi:
        name = _text(item, "name")
        version = _text(item, "version")
        matches = [
            package
            for package in packages
            if isinstance(package, dict)
            and package.get("name") == name
            and package.get("version") == version
        ]
        if len(matches) != 1:
            raise ValueError(f"依存ソースの版が uv.lock と一致しません: {name} {version}")
        sdist = matches[0].get("sdist")
        url = _text(sdist, "url")
        digest = _text(sdist, "hash")
        if not digest.startswith("sha256:"):
            raise ValueError(f"ソースの SHA256 がありません: {name}")
        sources.append(
            Source(
                component=name,
                version=version,
                filename=unquote(urlsplit(url).path.rsplit("/", 1)[-1]),
                url=url,
                sha256=digest.removeprefix("sha256:"),
                license=_text(item, "license"),
            )
        )

    if not sources:
        raise ValueError("依存ソースがありません。")
    filenames: set[str] = set()
    components: set[str] = set()
    for source in sources:
        _validate_source(source)
        if source.filename.casefold() in filenames or source.component.casefold() in components:
            raise ValueError(f"依存ソースが重複しています: {source.component}")
        filenames.add(source.filename.casefold())
        components.add(source.component.casefold())
    return audit, sandbox_validation, sources


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sources(sources: list[Source], directory: Path) -> None:
    for source in sources:
        path = directory / source.filename
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"依存ソースがありません: {path}")
        if sha256_file(path) != source.sha256:
            raise ValueError(f"依存ソースの SHA256 が一致しません: {path}")


def read_cargo_sources(sources: list[Source], directory: Path) -> list[Source]:
    matches = [source for source in sources if source.component == "pydantic-core"]
    if len(matches) != 1:
        raise ValueError("pydantic-core の対応ソースがありません。")
    core = matches[0]
    verify_sources([core], directory)
    if not core.filename.endswith(".tar.gz"):
        raise ValueError("pydantic-core のソース形式が不正です。")
    lock_name = f"{core.filename.removesuffix('.tar.gz')}/Cargo.lock"
    try:
        with tarfile.open(directory / core.filename, "r:gz") as archive:
            members = [member for member in archive if member.name == lock_name]
            if len(members) != 1 or not members[0].isfile() or members[0].size > 2 * CHUNK_SIZE:
                raise ValueError("pydantic-core の Cargo.lock が不正です。")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ValueError("pydantic-core の Cargo.lock を読み込めません。")
            with stream:
                lock = tomllib.loads(stream.read().decode("utf-8"))
    except (OSError, UnicodeError, tarfile.TarError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"pydantic-core の Cargo.lock を読み込めません: {error}") from error

    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError("Cargo.lock のパッケージ一覧が不正です。")
    result: list[Source] = []
    seen: set[str] = set()
    root_count = 0
    for package in packages:
        name = _text(package, "name")
        version = _text(package, "version")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.+_-]*", version
        ):
            raise ValueError(f"Cargo パッケージ名または版が不正です: {name} {version}")
        source = package.get("source") if isinstance(package, dict) else None
        if source is None and name == "pydantic-core" and version == core.version:
            root_count += 1
            continue
        if source != "registry+https://github.com/rust-lang/crates.io-index":
            raise ValueError(f"未対応の Cargo ソースがあります: {name} {version}")
        filename = f"{name}-{version}.crate"
        item = Source(
            component=f"cargo:{name}@{version}",
            version=version,
            filename=filename,
            url=f"https://static.crates.io/crates/{quote(name)}/{quote(filename)}",
            sha256=_text(package, "checksum"),
            license="Cargo.toml と同梱通知を参照 (未監査)",
        )
        _validate_source(item)
        if filename.casefold() in seen:
            raise ValueError(f"Cargo ソースが重複しています: {filename}")
        seen.add(filename.casefold())
        result.append(item)
    if root_count != 1 or not result:
        raise ValueError("Cargo.lock のルートまたは依存関係が不正です。")
    return result


def _open_url(url: str) -> BinaryIO:
    return urlopen(url, timeout=120)


def fetch_sources(
    sources: list[Source],
    directory: Path,
    *,
    opener: Callable[[str], BinaryIO] = _open_url,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for source in sources:
        _validate_source(source)
        destination = directory / source.filename
        if destination.exists() or destination.is_symlink():
            verify_sources([source], directory)
            continue
        temporary = directory / f".{source.filename}.{uuid4().hex}.part"
        try:
            digest = hashlib.sha256()
            with opener(source.url) as response, temporary.open("xb") as output:
                while chunk := response.read(CHUNK_SIZE):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != source.sha256:
                raise ValueError(f"取得したソースの SHA256 が一致しません: {source.component}")
            try:
                os.link(temporary, destination)
            except FileExistsError:
                verify_sources([source], directory)
        finally:
            temporary.unlink(missing_ok=True)


def source_records(sources: list[Source]) -> list[dict[str, str]]:
    return [asdict(source) for source in sources]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=SOURCE_DIRECTORY)
    args = parser.parse_args()
    try:
        audit, sandbox_validation, sources = read_sources()
        fetch_sources(sources, args.output_dir)
        verify_sources(sources, args.output_dir)
        cargo_sources = read_cargo_sources(sources, args.output_dir)
        fetch_sources(cargo_sources, args.output_dir)
        verify_sources(cargo_sources, args.output_dir)
    except (OSError, ValueError) as error:
        print(f"依存ソースを取得できません: {error}", file=sys.stderr)
        return 1
    print(
        f"依存ソース {len(sources)} 件と Cargo ソース {len(cargo_sources)} 件の "
        f"SHA256 を確認しました。監査状態: {audit}、Sandbox / VM 検証: {sandbox_validation}"
    )
    if audit != "complete" or sandbox_validation != "complete":
        print("監査または Sandbox / VM 検証が未完了のため配布には使用できません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
