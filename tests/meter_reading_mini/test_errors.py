from pathlib import Path

import pytest

from meter_reading_mini.core.errors import (
    DomainError,
    FileOutputError,
    InactiveMasterError,
    InvalidBackupError,
    MasterInUseError,
    MigrationError,
    RestoreError,
    SchemaTooNewError,
    UnsupportedSchemaError,
    ValidationError,
)


@pytest.mark.parametrize(
    "error_type",
    [
        ValidationError,
        FileOutputError,
        InactiveMasterError,
        MasterInUseError,
        SchemaTooNewError,
        UnsupportedSchemaError,
    ],
)
def test_domain_errors_keep_message(error_type: type[DomainError]) -> None:
    error = error_type("入力内容を確認してください")

    assert isinstance(error, DomainError)
    assert error.message == "入力内容を確認してください"
    assert str(error) == error.message


def test_invalid_backup_error_keeps_reasons() -> None:
    error = InvalidBackupError(("スキーマが一致しません", "履歴が不整合です"))

    assert error.reasons == ("スキーマが一致しません", "履歴が不整合です")
    assert error.message == "バックアップを利用できません: スキーマが一致しません、履歴が不整合です"


def test_migration_error_keeps_backup_path() -> None:
    backup_path = Path("backup.db")
    error = MigrationError("移行に失敗しました", backup_path)

    assert error.backup_path == backup_path
    assert error.message == "移行に失敗しました"


@pytest.mark.parametrize("stage", ["pre_backup", "overwrite", "recovery"])
def test_restore_error_keeps_failure_details(stage: str) -> None:
    backup_path = Path("backup.db")
    error = RestoreError(
        "復元に失敗しました",
        stage=stage,  # type: ignore[arg-type]
        recovered=False,
        backup_path=backup_path,
    )

    assert error.stage == stage
    assert error.recovered is False
    assert error.backup_path == backup_path
    assert error.message == "復元に失敗しました"


def test_restore_error_rejects_unknown_stage() -> None:
    with pytest.raises(ValueError, match="不正な復元段階"):
        RestoreError("復元に失敗しました", stage="unknown", recovered=False)  # type: ignore[arg-type]
