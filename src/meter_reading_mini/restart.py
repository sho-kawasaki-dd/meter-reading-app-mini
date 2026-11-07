"""復元後の新プロセス起動と起動完了の確認。"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from meter_reading_mini.config import DATA_DIR_ENV, AppPaths, resolve_paths

_READY_PATH_ENV = "_METER_READING_MINI_RESTART_READY"
_READY_TOKEN_ENV = "_METER_READING_MINI_RESTART_TOKEN"
_BACKUP_PATH_ENV = "_METER_READING_MINI_RESTART_BACKUP"
_LOG_DIR_ENV = "_METER_READING_MINI_RESTART_LOG_DIR"
_RESTART_DIR_PREFIX = ".meter-reading-restart-"


@dataclass(frozen=True, slots=True)
class RestartNotice:
    ready_path: Path
    token: str
    backup_path: Path
    log_dir: Path


class RestartProcess(Protocol):
    def poll(self) -> int | None: ...


class RestartLauncher(Protocol):
    def __call__(self, command: list[str], *, env: Mapping[str, str]) -> RestartProcess: ...


def restart_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "meter_reading_mini"]


def take_restart_notice(
    environment: MutableMapping[str, str], data_dir: Path
) -> RestartNotice | None:
    values = tuple(
        environment.pop(key, None)
        for key in (_READY_PATH_ENV, _READY_TOKEN_ENV, _BACKUP_PATH_ENV, _LOG_DIR_ENV)
    )
    if all(value is None for value in values):
        return None
    ready, token, backup, log_dir = values
    if not all(values) or ready is None or token is None or backup is None or log_dir is None:
        raise RuntimeError("再起動の確認情報が不完全です。")
    ready_path = Path(ready)
    if (
        not ready_path.is_absolute()
        or ready_path.name != "ready"
        or not ready_path.parent.name.startswith(_RESTART_DIR_PREFIX)
        or ready_path.parent.parent.resolve() != data_dir.resolve()
        or len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise RuntimeError("再起動の確認情報が不正です。")
    return RestartNotice(ready_path, token, Path(backup), Path(log_dir))


def signal_restart_ready(notice: RestartNotice) -> None:
    with notice.ready_path.open("x", encoding="ascii") as output:
        output.write(notice.token)


def _launch_process(command: list[str], *, env: Mapping[str, str]) -> RestartProcess:
    return subprocess.Popen(command, env=env)


def restart_application(
    paths: AppPaths,
    backup_path: Path,
    *,
    launcher: RestartLauncher | None = None,
    timeout: float = 30.0,
) -> None:
    if timeout <= 0:
        raise ValueError("再起動の確認時間は正の値にしてください。")
    environment = os.environ.copy()
    configured_paths = resolve_paths(environment)
    if (
        DATA_DIR_ENV in environment
        or configured_paths.data_dir.resolve() != paths.data_dir.resolve()
    ):
        environment[DATA_DIR_ENV] = str(paths.data_dir.resolve())

    with tempfile.TemporaryDirectory(prefix=_RESTART_DIR_PREFIX, dir=paths.data_dir) as directory:
        ready_path = Path(directory).resolve() / "ready"
        token = secrets.token_hex(16)
        environment.update(
            {
                _READY_PATH_ENV: str(ready_path),
                _READY_TOKEN_ENV: token,
                _BACKUP_PATH_ENV: str(backup_path.resolve()),
                _LOG_DIR_ENV: str(paths.log_dir.resolve()),
            }
        )
        process = (launcher or _launch_process)(restart_command(), env=environment)
        deadline = time.monotonic() + timeout
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(f"再起動したプロセスが終了しました (終了コード: {exit_code})。")
            if ready_path.exists() and ready_path.read_text(encoding="ascii") == token:
                if process.poll() is not None:
                    raise RuntimeError("再起動したプロセスが起動後に終了しました。")
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("アプリの再起動を確認できませんでした。")
            time.sleep(0.05)
