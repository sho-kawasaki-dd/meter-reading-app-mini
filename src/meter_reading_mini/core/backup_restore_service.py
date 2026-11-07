"""バックアップ・復元の操作とファイル名を統括する。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, tzinfo
from pathlib import Path

from meter_reading_mini.core.backup_restore_contracts import (
    BackupRestoreRepository,
    RestorePreview,
    RestoreResult,
)
from meter_reading_mini.core.timeutil import local_timestamp_for_filename, utc_now


class BackupRestoreService:
    def __init__(
        self,
        repository: BackupRestoreRepository,
        backup_dir: Path,
        *,
        timezone_provider: Callable[[], tzinfo],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._backup_dir = Path(backup_dir)
        self._clock = clock
        self._timezone_provider = timezone_provider

    def _timestamp(self) -> str:
        return local_timestamp_for_filename(self._clock(), self._timezone_provider())

    def default_backup_filename(self) -> str:
        return f"meter_reading_{self._timestamp()}.db"

    def create_backup(self, destination: Path) -> None:
        self._repository.create_backup(destination)

    def preview_restore(self, source: Path) -> RestorePreview:
        return self._repository.preview_restore(source)

    def discard_preview(self) -> None:
        self._repository.discard_preview()

    def restore_after_shutdown(self, close_connection: Callable[[], None]) -> RestoreResult:
        """イベントループ終了後、ロックを保持して接続を一度だけ閉じる。"""

        backup_path = self._backup_dir / f"pre-restore_{self._timestamp()}.db"
        return self._repository.restore_after_shutdown(backup_path, close_connection)
