from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from platformdirs import user_data_dir

from meter_reading_mini.config import APP_NAME

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_SHARING_ERRORS = frozenset({32, 33})
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class InstallationGuardError(RuntimeError):
    pass


class InstallationGuard:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: int | None = None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            raise InstallationGuardError("更新・削除ガードは既に取得されています。")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
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
            handle = create_file(
                str(self._path),
                _GENERIC_READ,
                _FILE_SHARE_READ,
                None,
                _OPEN_ALWAYS,
                _FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if handle == _INVALID_HANDLE or handle is None:
                error_code = ctypes.get_last_error()
                if error_code in _SHARING_ERRORS:
                    return False
                raise ctypes.WinError(error_code)
        except OSError as error:
            raise InstallationGuardError(
                "更新・削除ガードを確保できません。保存先とアクセス権を確認してください。"
            ) from error
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        if not close_handle(self._handle):
            raise InstallationGuardError("更新・削除ガードを解放できません。") from ctypes.WinError(
                ctypes.get_last_error()
            )
        self._handle = None


def create_installation_guard() -> InstallationGuard | None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    directory = Path(user_data_dir(APP_NAME, appauthor=False, roaming=False))
    return InstallationGuard(directory / "installation.guard")
