from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from meter_reading_mini import installation_guard

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows ファイル共有の検証")


def test_non_frozen_application_does_not_use_installation_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert installation_guard.create_installation_guard() is None


@WINDOWS_ONLY
def test_frozen_application_uses_user_local_guard_independently_of_data_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("METER_READING_MINI_DATA_DIR", str(tmp_path / "other-data"))
    calls: list[tuple[str, bool, bool]] = []

    def directory(appname: str, *, appauthor: bool, roaming: bool) -> str:
        calls.append((appname, appauthor, roaming))
        return str(tmp_path / "local")

    monkeypatch.setattr(installation_guard, "user_data_dir", directory)
    guard = installation_guard.create_installation_guard()
    assert guard is not None
    assert guard.try_acquire()
    guard.release()
    assert calls == [("meter-reading-mini", False, False)]
    assert (tmp_path / "local" / "installation.guard").is_file()
    assert not (tmp_path / "other-data").exists()


def _exclusive_open(path: Path) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    return create_file(str(path), 0xC0000000, 0, None, 4, 0x80, None)


def _close(handle: int) -> None:
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    assert close_handle(handle)


@WINDOWS_ONLY
def test_shared_app_guards_and_exclusive_installer_guard_block_each_other(tmp_path: Path) -> None:
    path = tmp_path / "local" / "installation.guard"
    first = installation_guard.InstallationGuard(path)
    second = installation_guard.InstallationGuard(path)

    assert first.try_acquire()
    assert second.try_acquire()
    with pytest.raises(installation_guard.InstallationGuardError, match="既に取得"):
        first.try_acquire()
    assert _exclusive_open(path) == installation_guard._INVALID_HANDLE
    first.release()
    first.release()
    assert _exclusive_open(path) == installation_guard._INVALID_HANDLE
    second.release()

    exclusive = _exclusive_open(path)
    assert exclusive != installation_guard._INVALID_HANDLE
    try:
        assert not first.try_acquire()
    finally:
        _close(exclusive)
    assert first.try_acquire()
    first.release()


@WINDOWS_ONLY
def test_guard_is_held_across_processes_and_released_after_exit(tmp_path: Path) -> None:
    path = tmp_path / "installation.guard"
    code = (
        "import sys; from pathlib import Path; "
        "from meter_reading_mini.installation_guard import InstallationGuard; "
        "guard = InstallationGuard(Path(sys.argv[1])); "
        "assert guard.try_acquire(); print('acquired', flush=True); sys.stdin.readline()"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "acquired"
        assert _exclusive_open(path) == installation_guard._INVALID_HANDLE
        assert process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
        exclusive = _exclusive_open(path)
        assert exclusive != installation_guard._INVALID_HANDLE
        _close(exclusive)
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)


@WINDOWS_ONLY
def test_guard_reports_file_creation_errors(tmp_path: Path) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("test", encoding="utf-8")
    guard = installation_guard.InstallationGuard(parent / "installation.guard")
    with pytest.raises(installation_guard.InstallationGuardError, match="ガードを確保できません"):
        guard.try_acquire()
