"""物理メーターの登録・更新・論理削除を扱う service。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace

from meter_reading_mini.core.contract_models import Category
from meter_reading_mini.core.errors import DomainError, MasterInUseError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeterMasterCreateRequest,
    MeterMasterRecord,
    MeterMasterUpdateRequest,
    MeterMasterUsageRecord,
    MeterMasterWriteRepository,
)

logger = logging.getLogger(__name__)

TransactionFactory = Callable[[], AbstractContextManager[object]]
METER_CATEGORY_VALUES = [category.value for category in Category]


class MeterMasterServiceError(DomainError):
    """物理メーターの業務操作に失敗したことを表す。"""


class MeterValidationError(MeterMasterServiceError):
    """物理メーター入力の検証エラー。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("物理メーターの入力が正しくありません: " + "、".join(errors))


class MeterNotFoundError(MeterMasterServiceError):
    """指定された物理メーターが存在しないことを表す。"""

    def __init__(self, meter_id: str) -> None:
        self.meter_id = meter_id
        super().__init__(f"物理メーターが見つかりません: {meter_id}")


class MeterMasterService:
    """Repository を通じて物理メーターを管理し、更新操作ごとに transaction を持つ。"""

    def __init__(
        self,
        repository: MeterMasterWriteRepository,
        *,
        transaction_factory: TransactionFactory,
    ) -> None:
        self._repository = repository
        self._transaction_factory = transaction_factory

    def list_meters(self, *, show_inactive: bool = False) -> list[MeterMasterRecord]:
        return self._repository.list_meter_masters(include_inactive=show_inactive)

    def get_meter(self, meter_id: str) -> MeterMasterRecord | None:
        return self._repository.get_meter_master(meter_id)

    def get_usage(self, meter_id: str) -> MeterMasterUsageRecord:
        self._require_meter(meter_id)
        return self._repository.get_meter_master_usage(meter_id)

    def create_meter(self, request: MeterMasterCreateRequest) -> MeterMasterRecord:
        try:
            category = Category(request.category)
        except (TypeError, ValueError) as exc:
            raise MeterValidationError(
                [f"category は {METER_CATEGORY_VALUES} のいずれかです。"]
            ) from exc
        normalized = replace(
            request,
            category=category,
            meter_id=_required(request.meter_id, "meter_id"),
            location_id=_required(request.location_id, "location_id"),
            location_name=_required(request.location_name, "location_name"),
            meter_name=_required(request.meter_name, "meter_name"),
            meter_serial=_optional(request.meter_serial),
        )
        with self._transaction_factory():
            if self._repository.get_meter_master(normalized.meter_id) is not None:
                raise MeterValidationError([f"meter_id '{normalized.meter_id}' は既に存在します。"])
            created = self._repository.create_meter_master(normalized)
        logger.info("物理メーターを登録しました: %s", created.meter_id)
        return created

    def update_meter(self, meter_id: str, request: MeterMasterUpdateRequest) -> MeterMasterRecord:
        try:
            category = Category(request.category)
        except (TypeError, ValueError) as exc:
            raise MeterValidationError(
                [f"category は {METER_CATEGORY_VALUES} のいずれかです。"]
            ) from exc
        normalized_id = _required(meter_id, "meter_id")
        normalized = replace(
            request,
            category=category,
            location_name=_required(request.location_name, "location_name"),
            meter_name=_required(request.meter_name, "meter_name"),
            meter_serial=_optional(request.meter_serial),
        )
        with self._transaction_factory():
            self._require_meter(normalized_id)
            updated = self._repository.update_meter_master(normalized_id, normalized)
        logger.info("物理メーターを更新しました: %s", normalized_id)
        return updated

    def deactivate_meter(self, meter_id: str) -> None:
        normalized_id = _required(meter_id, "meter_id")
        with self._transaction_factory():
            self._require_meter(normalized_id)
            usage = self._repository.get_meter_master_usage(normalized_id)
            if usage.has_active_assignment or usage.has_open_planned_readings:
                raise MasterInUseError(f"使用中の物理メーターは無効化できません: {normalized_id}")
            self._repository.set_meter_master_active(normalized_id, False)
        logger.info("物理メーターを無効化しました: %s", normalized_id)

    def activate_meter(self, meter_id: str) -> None:
        normalized_id = _required(meter_id, "meter_id")
        with self._transaction_factory():
            self._require_meter(normalized_id)
            self._repository.set_meter_master_active(normalized_id, True)
        logger.info("物理メーターを再有効化しました: %s", normalized_id)

    def delete_meter(self, meter_id: str) -> None:
        """既存呼出しとの互換用に、物理削除ではなく論理削除を行う。"""

        self.deactivate_meter(meter_id)

    def _require_meter(self, meter_id: str) -> MeterMasterRecord:
        meter = self._repository.get_meter_master(meter_id)
        if meter is None:
            raise MeterNotFoundError(meter_id)
        return meter


def _required(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise MeterValidationError([f"{field_name} は必須です。"])
    return normalized


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


__all__ = [
    "METER_CATEGORY_VALUES",
    "MeterMasterService",
    "MeterMasterServiceError",
    "MeterNotFoundError",
    "MeterValidationError",
]
