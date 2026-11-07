from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from meter_reading_mini import restart
from meter_reading_mini.config import DATA_DIR_ENV, AppPaths


class RunningProcess:
    def poll(self) -> int | None:
        return None


class ExitedProcess:
    def poll(self) -> int | None:
        return 2


def test_restart_commands_follow_python_and_frozen_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "executable", "executable")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert restart.restart_command() == ["executable", "-m", "meter_reading_mini"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert restart.restart_command() == ["executable"]


def test_restart_preserves_paths_and_requires_the_child_ready_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "separate-logs")
    paths.data_dir.mkdir()
    monkeypatch.setenv(DATA_DIR_ENV, str(paths.data_dir))
    backup = paths.backup_dir / "pre-restore.db"
    captured: list[restart.RestartNotice] = []

    def launch(command: list[str], *, env: Mapping[str, str]) -> RunningProcess:
        assert command == restart.restart_command()
        assert Path(env[DATA_DIR_ENV]) == paths.data_dir.resolve()
        copied = dict(env)
        notice = restart.take_restart_notice(copied, paths.data_dir)
        assert notice is not None
        assert notice.backup_path == backup.resolve()
        assert notice.log_dir == paths.log_dir.resolve()
        assert restart.take_restart_notice(copied, paths.data_dir) is None
        restart.signal_restart_ready(notice)
        captured.append(notice)
        return RunningProcess()

    restart.restart_application(paths, backup, launcher=launch, timeout=1)
    assert len(captured) == 1
    assert not captured[0].ready_path.exists()
    assert not list(paths.data_dir.glob(".meter-reading-restart-*"))


def test_explicit_paths_are_propagated_without_an_existing_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
    paths.data_dir.mkdir()
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)

    def launch(command: list[str], *, env: Mapping[str, str]) -> RunningProcess:
        assert env[DATA_DIR_ENV] == str(paths.data_dir.resolve())
        notice = restart.take_restart_notice(dict(env), paths.data_dir)
        assert notice is not None
        restart.signal_restart_ready(notice)
        return RunningProcess()

    restart.restart_application(paths, paths.backup_dir / "saved.db", launcher=launch, timeout=1)


def test_frozen_restart_uses_the_executable_and_preserves_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
    paths.data_dir.mkdir()
    monkeypatch.setenv(DATA_DIR_ENV, str(paths.data_dir))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "meter-reading-mini.exe")

    def launch(command: list[str], *, env: Mapping[str, str]) -> RunningProcess:
        assert command == ["meter-reading-mini.exe"]
        assert env[DATA_DIR_ENV] == str(paths.data_dir.resolve())
        notice = restart.take_restart_notice(dict(env), paths.data_dir)
        assert notice is not None
        restart.signal_restart_ready(notice)
        return RunningProcess()

    restart.restart_application(paths, paths.backup_dir / "saved.db", launcher=launch, timeout=1)


def test_process_exit_and_missing_ready_signal_are_failures(tmp_path: Path) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
    paths.data_dir.mkdir()
    backup = paths.backup_dir / "saved.db"
    with pytest.raises(RuntimeError, match="終了コード: 2"):
        restart.restart_application(
            paths, backup, launcher=lambda command, *, env: ExitedProcess(), timeout=1
        )
    with pytest.raises(TimeoutError, match="確認できません"):
        restart.restart_application(
            paths, backup, launcher=lambda command, *, env: RunningProcess(), timeout=0.05
        )
    assert not list(paths.data_dir.glob(".meter-reading-restart-*"))


def test_signal_without_a_live_child_is_not_success(tmp_path: Path) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
    paths.data_dir.mkdir()

    class ExitsAfterSignal:
        calls = 0

        def poll(self) -> int | None:
            self.calls += 1
            return None if self.calls == 1 else 0

    def launch(command: list[str], *, env: Mapping[str, str]) -> ExitsAfterSignal:
        notice = restart.take_restart_notice(dict(env), paths.data_dir)
        assert notice is not None
        restart.signal_restart_ready(notice)
        return ExitsAfterSignal()

    with pytest.raises(RuntimeError, match="起動後に終了"):
        restart.restart_application(
            paths, paths.backup_dir / "saved.db", launcher=launch, timeout=1
        )


def test_invalid_or_partial_restart_handoff_is_rejected(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(RuntimeError, match="不完全"):
        restart.take_restart_notice({restart._READY_PATH_ENV: str(data / "ready")}, data)

    environment = {
        restart._READY_PATH_ENV: str(tmp_path / "outside" / "ready"),
        restart._READY_TOKEN_ENV: "a" * 32,
        restart._BACKUP_PATH_ENV: str(data / "backup.db"),
        restart._LOG_DIR_ENV: str(data / "logs"),
    }
    with pytest.raises(RuntimeError, match="不正"):
        restart.take_restart_notice(environment, data)
    assert environment == {}


def test_ready_file_is_created_exclusively(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    ready.write_text("existing", encoding="ascii")
    notice = restart.RestartNotice(ready, "a" * 32, tmp_path / "backup.db", tmp_path)
    with pytest.raises(FileExistsError):
        restart.signal_restart_ready(notice)
    assert ready.read_text(encoding="ascii") == "existing"
