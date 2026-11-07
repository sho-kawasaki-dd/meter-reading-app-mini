from __future__ import annotations

import importlib.util
import struct
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

spec = importlib.util.spec_from_file_location(
    "build_windows", Path(__file__).resolve().parents[1] / "scripts" / "build_windows.py"
)
assert spec is not None and spec.loader is not None
build_windows = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_windows)


def _populate_distribution(directory: Path) -> None:
    for relative in build_windows.REQUIRED_FILES:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test")
    header = bytearray(0x86)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 0x80)
    header[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", header, 0x84, 0x8664)
    (directory / f"{build_windows.APP_NAME}.exe").write_bytes(header)
    for relative in build_windows.APP_RESOURCES:
        target = directory / build_windows.INTERNAL / "meter_reading_mini" / relative
        target.write_bytes((build_windows.PACKAGE / relative).read_bytes())


@pytest.mark.parametrize(
    ("system", "machine", "pointer_size", "supported"),
    [
        ("win32", "AMD64", 8, True),
        ("win32", "x86_64", 8, True),
        ("win32", "AMD64", 4, False),
        ("win32", "x86", 4, False),
        ("win32", "ARM64", 8, False),
        ("linux", "x86_64", 8, False),
    ],
)
def test_build_requires_windows_x64_python(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    pointer_size: int,
    supported: bool,
) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(build_windows.sys, "platform", system)
        scoped.setattr(build_windows.platform, "machine", lambda: machine)
        scoped.setattr(build_windows.struct, "calcsize", lambda _format: pointer_size)
        if supported:
            build_windows._require_windows_x64()
        else:
            with pytest.raises(RuntimeError, match="Windows x64"):
                build_windows._require_windows_x64()


def test_each_build_uses_fresh_paths_and_keeps_previous_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "output"
    previous = output_root / "previous"
    previous.mkdir(parents=True)
    sentinel = previous / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    workspaces: list[Path] = []
    monkeypatch.setattr(build_windows, "_require_windows_x64", lambda: None)

    def run(command: list[str], *, cwd: Path, check: bool) -> None:
        assert check and cwd == build_windows.ROOT
        assert command[:3] == [build_windows.sys.executable, "-m", "PyInstaller"]
        assert command[-1] == str(build_windows.SPEC)
        dist = Path(command[command.index("--distpath") + 1])
        work = Path(command[command.index("--workpath") + 1])
        assert dist.parent == work.parent.parent == output_root
        assert dist.name.startswith(f"{build_windows.APP_NAME}-")
        assert work.parent.name.startswith(".pyinstaller-work-")
        assert (dist / ".build-in-progress").is_file()
        assert not (dist / build_windows.APP_NAME).exists() and not work.exists()
        workspaces.append(work.parent)
        _populate_distribution(dist / build_windows.APP_NAME)

    monkeypatch.setattr(build_windows.subprocess, "run", run)
    first = build_windows.build(output_root)
    second = build_windows.build(output_root)

    assert first != second
    assert first.is_dir() and second.is_dir()
    assert not (first.parent / ".build-in-progress").exists()
    assert not (second.parent / ".build-in-progress").exists()
    assert workspaces[0] != workspaces[1]
    assert all(not workspace.exists() for workspace in workspaces)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    build_windows._validate_distribution(first)
    build_windows._validate_distribution(second)


def test_failed_build_does_not_publish_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    sentinel = output_root / "previous.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(build_windows, "_require_windows_x64", lambda: None)

    def fail(command: list[str], *, cwd: Path, check: bool) -> None:
        dist = Path(command[command.index("--distpath") + 1]) / build_windows.APP_NAME
        _populate_distribution(dist)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(build_windows.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        build_windows.build(output_root)

    assert list(output_root.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_existing_destination_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "output"
    existing = output_root / f"{build_windows.APP_NAME}-{'a' * 32}"
    existing.mkdir(parents=True)
    sentinel = existing / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(build_windows, "_require_windows_x64", lambda: None)
    monkeypatch.setattr(build_windows, "uuid4", lambda: UUID("a" * 32))

    def run(command: list[str], *, cwd: Path, check: bool) -> None:
        dist = Path(command[command.index("--distpath") + 1])
        _populate_distribution(dist / build_windows.APP_NAME)

    monkeypatch.setattr(build_windows.subprocess, "run", run)
    with pytest.raises(FileExistsError):
        build_windows.build(output_root)
    assert list(output_root.iterdir()) == [existing]
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_distribution_rejects_missing_or_changed_assets(tmp_path: Path) -> None:
    _populate_distribution(tmp_path)
    schema = tmp_path / build_windows.INTERNAL / "meter_reading_mini" / "db" / "schema.sql"
    schema.unlink()
    with pytest.raises(RuntimeError, match="実行時資産が不足"):
        build_windows._validate_distribution(tmp_path)
    schema.write_text("stale", encoding="utf-8")
    with pytest.raises(RuntimeError, match="ソースと一致しません"):
        build_windows._validate_distribution(tmp_path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"not an executable", "形式が不正"),
        (b"MZ" + bytes(58) + bytes.fromhex("80000000"), "形式が不正"),
        (
            b"MZ" + bytes(58) + bytes.fromhex("80000000") + bytes(64) + b"PE\0\0" + b"\x4c\x01",
            "x64 用ではありません",
        ),
    ],
)
def test_distribution_rejects_invalid_or_non_x64_executable(
    tmp_path: Path, content: bytes, message: str
) -> None:
    _populate_distribution(tmp_path)
    (tmp_path / f"{build_windows.APP_NAME}.exe").write_bytes(content)
    with pytest.raises(RuntimeError, match=message):
        build_windows._validate_distribution(tmp_path)


@pytest.mark.parametrize(
    "relative",
    [
        Path("meter_reading.db"),
        Path("meter_reading.db-wal"),
        Path("database.sqlite3"),
        Path("backup.bak"),
        Path("app.log"),
        Path("app.log.1"),
        Path("ui_settings.json"),
        Path("meter_reading_20261107.html"),
        Path("meter_reading_draft_20261107.json"),
        Path("backups") / "copy",
        Path("logs") / "trace",
    ],
)
def test_distribution_rejects_business_data(tmp_path: Path, relative: Path) -> None:
    _populate_distribution(tmp_path)
    candidate = tmp_path / relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("business data", encoding="utf-8")
    with pytest.raises(RuntimeError, match="業務データ"):
        build_windows._validate_distribution(tmp_path)


def test_failed_validation_does_not_publish_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_windows, "_require_windows_x64", lambda: None)

    def run(command: list[str], *, cwd: Path, check: bool) -> None:
        dist = Path(command[command.index("--distpath") + 1]) / build_windows.APP_NAME
        _populate_distribution(dist)
        (dist / "meter_reading.db").write_bytes(b"private")

    monkeypatch.setattr(build_windows.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="業務データ"):
        build_windows.build(tmp_path)
    assert not list(tmp_path.iterdir())


def test_build_cli_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        build_windows.sys, "argv", ["build_windows.py", "--output-root", str(tmp_path)]
    )

    def fail(_output_root: Path) -> Path:
        raise OSError("書込失敗")

    monkeypatch.setattr(build_windows, "build", fail)
    assert build_windows.main() == 1
    assert "書込失敗" in capsys.readouterr().err


def test_build_cli_reports_the_validated_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        build_windows.sys, "argv", ["build_windows.py", "--output-root", str(tmp_path)]
    )
    destination = tmp_path / "new-build" / build_windows.APP_NAME
    monkeypatch.setattr(build_windows, "build", lambda output_root: destination)

    assert build_windows.main() == 0
    output = capsys.readouterr().out
    assert str(destination / f"{build_windows.APP_NAME}.exe") in output
    assert "開発検証用" in output
