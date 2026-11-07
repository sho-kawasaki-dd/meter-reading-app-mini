"""Windows x64 用の隔離された PyInstaller ビルドを作成する。"""

from __future__ import annotations

import argparse
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "meter_reading_mini.spec"
APP_NAME = "meter-reading-mini"
PACKAGE = ROOT / "src" / "meter_reading_mini"
INTERNAL = Path("_internal")
APP_RESOURCES = (
    *(Path("db") / name for name in ("schema.sql", "schema_v1.sql", "schema_v3.sql")),
    *(
        Path("core") / "mobile_reading_assets" / name
        for name in ("sheet.html", "sheet.css", "sheet.js")
    ),
)
REQUIRED_FILES = (
    Path(f"{APP_NAME}.exe"),
    *(INTERNAL / "meter_reading_mini" / name for name in APP_RESOURCES),
    INTERNAL / "tzdata" / "zoneinfo" / "Asia" / "Tokyo",
    INTERNAL / "tzdata" / "zoneinfo" / "America" / "New_York",
    INTERNAL / "PySide6" / "Qt6Charts.dll",
    INTERNAL / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
)


def _validate_executable(path: Path) -> None:
    with path.open("rb") as executable:
        dos_header = executable.read(64)
        if len(dos_header) != 64 or dos_header[:2] != b"MZ":
            raise RuntimeError(f"実行可能ファイルの形式が不正です: {path}")
        pe_offset = struct.unpack_from("<I", dos_header, 60)[0]
        executable.seek(pe_offset)
        pe_header = executable.read(6)
    if len(pe_header) != 6 or pe_header[:4] != b"PE\0\0":
        raise RuntimeError(f"実行可能ファイルの形式が不正です: {path}")
    if struct.unpack_from("<H", pe_header, 4)[0] != 0x8664:
        raise RuntimeError(f"実行可能ファイルが Windows x64 用ではありません: {path}")


def _validate_distribution(directory: Path) -> None:
    missing = [name for name in REQUIRED_FILES if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(f"実行時資産が不足しています: {', '.join(map(str, missing))}")

    _validate_executable(directory / f"{APP_NAME}.exe")
    for name in APP_RESOURCES:
        bundled = directory / INTERNAL / "meter_reading_mini" / name
        if bundled.read_bytes() != (PACKAGE / name).read_bytes():
            raise RuntimeError(f"実行時資産が現在のソースと一致しません: {bundled}")

    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.casefold()
        if (
            name.endswith(
                (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".bak")
            )
            or re.search(r"\.log(?:\.\d+)?$", name)
            or name == "ui_settings.json"
            or (name.startswith("meter_reading_") and name.endswith((".html", ".json")))
            or any(
                part.casefold() in {"backups", "logs"} for part in path.relative_to(directory).parts
            )
        ):
            raise RuntimeError(f"業務データの可能性があるファイルを検出しました: {path}")


def _require_windows_x64() -> None:
    if (
        sys.platform != "win32"
        or platform.machine().casefold() not in {"amd64", "x86_64"}
        or struct.calcsize("P") != 8
    ):
        raise RuntimeError("ビルドには Windows x64 の Python が必要です。")


def build(output_root: Path) -> Path:
    _require_windows_x64()
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_directory = output_root / f"{APP_NAME}-{uuid4().hex}"
    run_directory.mkdir()
    with ExitStack() as cleanup:
        cleanup.callback(shutil.rmtree, run_directory)
        marker = run_directory / ".build-in-progress"
        marker.touch(exist_ok=False)
        with tempfile.TemporaryDirectory(prefix=".pyinstaller-work-", dir=output_root) as temporary:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "PyInstaller",
                    "--noconfirm",
                    "--distpath",
                    str(run_directory),
                    "--workpath",
                    str(Path(temporary) / "work"),
                    str(SPEC),
                ],
                cwd=ROOT,
                check=True,
            )
            distribution = run_directory / APP_NAME
            _validate_distribution(distribution)
        marker.unlink()
        cleanup.pop_all()
    return distribution


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "dist" / "pyinstaller",
        help="ビルドごとに新しい成果物ディレクトリを作る親ディレクトリ",
    )
    args = parser.parse_args()
    try:
        destination = build(args.output_root)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ビルドに失敗しました: {error}", file=sys.stderr)
        return 1
    print(f"ビルドが完了しました: {destination / f'{APP_NAME}.exe'}")
    print("これは開発検証用です。配布には法的監査とインストール検証が必要です。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
