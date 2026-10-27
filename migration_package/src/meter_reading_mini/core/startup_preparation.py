"""アプリ起動時に既定値投入と予定枠準備を統括する。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

from meter_reading_mini.core.work_package_download import (
    PlannedReadingGeneratorService,
    PlannedReadingLifecycleService,
)


class StartupPreparationService:
    """起動時の初期設定・補完・自動クローズを一括して実行する。"""

    def __init__(
        self,
        *,
        bootstrapper: Callable[[], object],
        planned_reading_generator: PlannedReadingGeneratorService,
        planned_reading_lifecycle_service: PlannedReadingLifecycleService,
        transaction_factory: Callable[[], AbstractContextManager[object]],
        business_date_provider: Callable[[], date],
    ) -> None:
        self._bootstrapper = bootstrapper
        self._planned_reading_generator = planned_reading_generator
        self._planned_reading_lifecycle_service = planned_reading_lifecycle_service
        self._transaction_factory = transaction_factory
        self._business_date_provider = business_date_provider

    def prepare(self) -> None:
        """起動準備全体を単一トランザクションで確定する。"""

        with self._transaction_factory():
            self._bootstrapper()
            self._planned_reading_generator.ensure_generated_through(self._business_date_provider())
            self._planned_reading_lifecycle_service.reconcile_before_download()


__all__ = ["StartupPreparationService"]
