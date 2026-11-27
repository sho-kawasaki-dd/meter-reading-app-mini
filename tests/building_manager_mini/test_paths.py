from __future__ import annotations

from pathlib import Path

import pytest

from building_manager_mini import paths


def test_paths_resolve_to_the_shared_directory_layout(tmp_path: Path) -> None:
    resolved = paths.AppPaths.from_root(tmp_path / "data")

    assert resolved.lock_path == resolved.data_dir / "app.lock"
    assert resolved.ui_settings_path == resolved.data_dir / "ui_settings.json"
    assert resolved.common_db_path == resolved.data_dir / "common" / "common.db"
    assert resolved.db_path == resolved.data_dir / "meter_reading" / "meter_reading.db"
    assert resolved.work_log_db_path == resolved.data_dir / "work_log" / "work_log.db"
    assert resolved.backup_root == resolved.data_dir / "backups"
    assert resolved.backup_dir == resolved.backup_root / "meter_reading"
    assert resolved.log_path == resolved.data_dir / "logs" / "app.log"


def test_resolve_startup_paths_uses_old_default_only_for_default_new_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        paths,
        "user_data_dir",
        lambda name, **kwargs: str(tmp_path / name),
    )
    monkeypatch.setattr(
        paths,
        "user_log_dir",
        lambda name, **kwargs: str(tmp_path / f"{name}-logs"),
    )

    default = paths.resolve_startup_paths({})
    overridden = paths.resolve_startup_paths({paths.DATA_DIR_ENV: str(tmp_path / "custom")})
    both_overridden = paths.resolve_startup_paths(
        {
            paths.DATA_DIR_ENV: str(tmp_path / "custom"),
            paths.LEGACY_DATA_DIR_ENV: str(tmp_path / "old-custom"),
        }
    )

    assert default.paths.data_dir == (tmp_path / paths.APP_NAME).resolve()
    assert (
        default.legacy_meter_reading_db_path
        == (tmp_path / paths.LEGACY_APP_NAME / "meter_reading.db").resolve()
    )
    assert overridden.legacy_meter_reading_db_path is None
    assert (
        both_overridden.legacy_meter_reading_db_path
        == (tmp_path / "old-custom" / "meter_reading.db").resolve()
    )


def test_legacy_environment_alone_is_rejected_before_path_creation(tmp_path: Path) -> None:
    with pytest.raises(paths.PathConfigurationError, match=paths.DATA_DIR_ENV):
        paths.resolve_startup_paths({paths.LEGACY_DATA_DIR_ENV: str(tmp_path / "old")})

    assert not (tmp_path / "old").exists()


@pytest.mark.parametrize("protected", ["legacy-data", "legacy-logs", "explicit-legacy"])
def test_new_root_cannot_overlap_legacy_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, protected: str
) -> None:
    legacy_data = tmp_path / "legacy-data"
    legacy_logs = tmp_path / "legacy-logs"
    monkeypatch.setattr(
        paths,
        "user_data_dir",
        lambda name, **kwargs: str(
            legacy_data if name == paths.LEGACY_APP_NAME else tmp_path / "new-default"
        ),
    )
    monkeypatch.setattr(paths, "user_log_dir", lambda name, **kwargs: str(legacy_logs))
    root = {
        "legacy-data": legacy_data / "nested",
        "legacy-logs": legacy_logs,
        "explicit-legacy": tmp_path / "explicit" / "child",
    }[protected]
    env = {paths.DATA_DIR_ENV: str(root)}
    if protected == "explicit-legacy":
        env[paths.LEGACY_DATA_DIR_ENV] = str(tmp_path / "explicit")

    with pytest.raises(paths.PathConfigurationError, match="重なっています"):
        paths.resolve_startup_paths(env)


def test_paths_are_case_insensitive_when_checking_overlap() -> None:
    assert paths._overlaps(Path("C:/App/Data/Child"), Path("c:/app/data"))
    assert not paths._overlaps(Path("C:/App/Data2"), Path("c:/app/data"))


def test_ensure_dirs_does_not_create_reserved_application_databases(tmp_path: Path) -> None:
    resolved = paths.AppPaths.from_root(tmp_path / "data")

    paths.ensure_dirs(resolved)

    assert resolved.backup_root.is_dir()
    assert resolved.backup_dir.is_dir()
    assert resolved.log_dir.is_dir()
    assert not resolved.common_db_path.parent.exists()
    assert not resolved.work_log_db_path.parent.exists()
    assert not resolved.common_db_path.exists()
    assert not resolved.work_log_db_path.exists()
