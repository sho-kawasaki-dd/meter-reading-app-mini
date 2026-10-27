from pathlib import Path

import pytest

from meter_reading_mini import config


def test_resolve_paths_uses_environment_override() -> None:
    data_dir = Path("test-data")

    paths = config.resolve_paths({config.DATA_DIR_ENV: str(data_dir)})

    assert paths == config.AppPaths(
        data_dir=data_dir,
        db_path=data_dir / "meter_reading.db",
        backup_dir=data_dir / "backups",
        lock_path=data_dir / "meter_reading.lock",
        ui_settings_path=data_dir / "ui_settings.json",
        log_dir=data_dir / "logs",
        log_path=data_dir / "logs" / "app.log",
    )


def test_resolve_paths_uses_platformdirs_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_user_data_dir(appname: str, **kwargs: object) -> str:
        calls.append(("data", appname, kwargs))
        return "test-user-data"

    def fake_user_log_dir(appname: str, **kwargs: object) -> str:
        calls.append(("log", appname, kwargs))
        return "test-user-log"

    monkeypatch.setattr(config, "user_data_dir", fake_user_data_dir)
    monkeypatch.setattr(config, "user_log_dir", fake_user_log_dir)

    paths = config.resolve_paths({})

    assert paths.data_dir == Path("test-user-data")
    assert paths.ui_settings_path == Path("test-user-data") / "ui_settings.json"
    assert paths.log_dir == Path("test-user-log")
    assert calls == [
        ("data", config.APP_NAME, {"appauthor": False, "roaming": True}),
        ("log", config.APP_NAME, {"appauthor": False}),
    ]


def test_resolve_paths_treats_empty_environment_override_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "user_data_dir", lambda *args, **kwargs: "default-data")
    monkeypatch.setattr(config, "user_log_dir", lambda *args, **kwargs: "default-log")

    paths = config.resolve_paths({config.DATA_DIR_ENV: ""})

    assert paths.data_dir == Path("default-data")
    assert paths.log_dir == Path("default-log")


def test_ensure_dirs_creates_data_backup_and_log_directories(tmp_path: Path) -> None:
    paths = config.AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")

    config.ensure_dirs(paths)

    assert paths.data_dir.is_dir()
    assert paths.backup_dir.is_dir()
    assert paths.log_dir.is_dir()
