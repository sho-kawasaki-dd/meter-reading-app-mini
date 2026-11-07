"""バックアップ・復元サービスと永続化層の契約。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RestorePreview:
    source_path: Path
    source_schema_version: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestoreResult:
    backup_path: Path


class BackupRestoreRepository(Protocol):
    def create_backup(self, destination: Path) -> None: ...

    def preview_restore(self, source: Path) -> RestorePreview: ...

    def discard_preview(self) -> None: ...

    def restore_after_shutdown(
        self, backup_path: Path, close_connection: Callable[[], None]
    ) -> RestoreResult: ...
