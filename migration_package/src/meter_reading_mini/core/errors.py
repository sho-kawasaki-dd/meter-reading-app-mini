from __future__ import annotations

from pathlib import Path
from typing import Literal

RestoreStage = Literal["pre_backup", "overwrite", "recovery"]


class DomainError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class FileOutputError(DomainError):
    pass


class ValidationError(DomainError):
    pass


class InactiveMasterError(DomainError):
    pass


class MasterInUseError(DomainError):
    pass


class SchemaTooNewError(DomainError):
    pass


class InvalidBackupError(DomainError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = tuple(reasons)
        message = "バックアップを利用できません: " + "、".join(self.reasons)
        super().__init__(message)


class UnsupportedSchemaError(DomainError):
    pass


class MigrationError(DomainError):
    def __init__(self, message: str, backup_path: Path | None = None) -> None:
        self.backup_path = backup_path
        super().__init__(message)


class RestoreError(DomainError):
    def __init__(
        self,
        message: str,
        *,
        stage: RestoreStage,
        recovered: bool,
        backup_path: Path | None = None,
    ) -> None:
        if stage not in {"pre_backup", "overwrite", "recovery"}:
            raise ValueError(f"不正な復元段階です: {stage}")
        self.stage = stage
        self.recovered = recovered
        self.backup_path = backup_path
        super().__init__(message)
