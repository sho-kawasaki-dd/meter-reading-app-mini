"""予定枠の補完と自動クローズをまとめる。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

from meter_reading_mini.core.work_package_download import (
    PlannedReadingGeneratorService,
    PlannedReadingLifecycleService,
)


def reconcile_planned_readings(
    generator: PlannedReadingGeneratorService,
    lifecycle: PlannedReadingLifecycleService,
    business_date_provider: Callable[[], date],
) -> None:
    """呼出し元のトランザクション内で補完と自動クローズを行う。"""

    generator.ensure_generated_through(business_date_provider())
    lifecycle.reconcile_before_download()


class PlannedReadingMaintenanceService:
    """予定枠の更新全体を一つのトランザクションとして実行する。"""

    def __init__(
        self,
        generator: PlannedReadingGeneratorService,
        lifecycle: PlannedReadingLifecycleService,
        *,
        transaction_factory: Callable[[], AbstractContextManager[object]],
        business_date_provider: Callable[[], date],
    ) -> None:
        self._generator = generator
        self._lifecycle = lifecycle
        self._transaction_factory = transaction_factory
        self._business_date_provider = business_date_provider

    def refresh(self) -> None:
        with self._transaction_factory():
            reconcile_planned_readings(
                self._generator,
                self._lifecycle,
                self._business_date_provider,
            )


__all__ = ["PlannedReadingMaintenanceService"]
