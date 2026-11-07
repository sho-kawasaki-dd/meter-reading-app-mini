from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows ランチャーの検証")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def compiled_launchers(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    directory = tmp_path_factory.mktemp("launcher")
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe"
    assert compiler.is_file(), f"C# コンパイラーがありません: {compiler}"
    source = ROOT / "packaging" / "windows_launcher.cs"
    testing = directory / "testing.exe"
    production = directory / "production.exe"
    common = [str(compiler), "/nologo", "/codepage:65001", "/platform:x64", "/optimize+"]
    subprocess.run(
        [*common, "/target:exe", "/define:MRM_TESTING", f"/out:{testing}", str(source)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [*common, "/target:winexe", f"/out:{production}", str(source)],
        check=True,
        capture_output=True,
    )
    child_source = directory / "child.cs"
    child_source.write_text(
        """
using System;
using System.IO;
using System.Threading;
internal static class Child {
    private static int Main() {
        File.WriteAllText(Environment.GetEnvironmentVariable("MRM_READY"), "ready");
        while (!File.Exists(Environment.GetEnvironmentVariable("MRM_FINISH")))
            Thread.Sleep(20);
        return 7;
    }
}
""",
        encoding="utf-8",
    )
    child = directory / "child.exe"
    subprocess.run(
        [*common, "/target:exe", f"/out:{child}", str(child_source)],
        check=True,
        capture_output=True,
    )
    return testing, production, child


def _exclusive_open(path: Path) -> int:
    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
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


def _environment(path: Path, child: Path, ready: Path, finish: Path) -> dict[str, str]:
    return {
        **os.environ,
        "MRM_TEST_GUARD_PATH": str(path),
        "MRM_TEST_CHILD_PATH": str(child),
        "MRM_READY": str(ready),
        "MRM_FINISH": str(finish),
    }


def _wait_for_file(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert process.poll() is None, "ランチャーがアプリ起動前に終了しました"
        assert time.monotonic() < deadline, "アプリの起動を確認できません"
        time.sleep(0.02)


@WINDOWS_ONLY
def test_launcher_guards_before_start_and_until_child_exit(
    tmp_path: Path, compiled_launchers: tuple[Path, Path, Path]
) -> None:
    launcher, _, child = compiled_launchers
    guard = tmp_path / "installation.guard"
    ready = tmp_path / "ready"
    finish = tmp_path / "finish"
    process = subprocess.Popen([str(launcher)], env=_environment(guard, child, ready, finish))
    invalid = ctypes.c_void_p(-1).value
    try:
        _wait_for_file(ready, process)
        assert _exclusive_open(guard) == invalid
        finish.touch()
        assert process.wait(timeout=10) == 7
        handle = _exclusive_open(guard)
        assert handle != invalid
        _close(handle)
    finally:
        if process.poll() is None:
            finish.touch()
            process.wait(timeout=10)


@WINDOWS_ONLY
def test_launcher_rejects_installer_guard_before_launching_app(
    tmp_path: Path, compiled_launchers: tuple[Path, Path, Path]
) -> None:
    launcher, _, child = compiled_launchers
    guard = tmp_path / "installation.guard"
    ready = tmp_path / "ready"
    handle = _exclusive_open(guard)
    assert handle != ctypes.c_void_p(-1).value
    try:
        result = subprocess.run(
            [str(launcher)],
            env=_environment(guard, child, ready, tmp_path / "finish"),
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert not ready.exists()
    finally:
        _close(handle)


@WINDOWS_ONLY
def test_launcher_releases_guard_if_child_cannot_be_started(
    tmp_path: Path, compiled_launchers: tuple[Path, Path, Path]
) -> None:
    launcher, _, _ = compiled_launchers
    guard = tmp_path / "installation.guard"
    result = subprocess.run(
        [str(launcher)],
        env=_environment(guard, tmp_path / "missing.exe", tmp_path / "ready", tmp_path / "finish"),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    handle = _exclusive_open(guard)
    assert handle != ctypes.c_void_p(-1).value
    _close(handle)


@WINDOWS_ONLY
def test_production_launcher_does_not_accept_test_path_overrides(
    compiled_launchers: tuple[Path, Path, Path],
) -> None:
    _, production, _ = compiled_launchers
    content = production.read_bytes()
    assert "MRM_TEST_GUARD_PATH".encode("utf-16-le") not in content
    assert "MRM_TEST_CHILD_PATH".encode("utf-16-le") not in content
