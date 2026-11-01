"""物理メーターの登録・更新・論理削除を扱う service。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from uuid import uuid4

from meter_reading_mini.core.contract_models import Category
from meter_reading_mini.core.errors import DomainError, MasterInUseError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeterLocationRecord,
    MeterMasterCreateInput,
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


@dataclass(frozen=True, slots=True)
class PreparedMeterCreation:
    meter_name: str
    category: Category
    location: MeterLocationRecord | None
    new_location_name: str | None
    meter_serial: str | None


class MeterMasterService:
    """Repository を通じて物理メーターを管理し、更新操作ごとに transaction を持つ。"""

    def __init__(
        self,
        repository: MeterMasterWriteRepository,
        *,
        transaction_factory: TransactionFactory,
        id_provider: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._repository = repository
        self._transaction_factory = transaction_factory
        self._id_provider = id_provider

    def list_meters(self, *, show_inactive: bool = False) -> list[MeterMasterRecord]:
        return self._repository.list_meter_masters(include_inactive=show_inactive)

    def get_meter(self, meter_id: str) -> MeterMasterRecord | None:
        return self._repository.get_meter_master(meter_id)

    def get_usage(self, meter_id: str) -> MeterMasterUsageRecord:
        self._require_meter(meter_id)
        return self._repository.get_meter_master_usage(meter_id)

    def list_locations(self) -> list[MeterLocationRecord]:
        return self._repository.list_meter_locations()

    def rename_location(self, location_id: str, name: str) -> None:
        normalized_id = _required(location_id, "location_id")
        normalized_name = _required(name, "場所名")
        with self._transaction_factory():
            if self._repository.get_meter_location(normalized_id) is None:
                raise MeterValidationError([f"場所が見つかりません: {normalized_id}"])
            self._repository.rename_meter_location(normalized_id, normalized_name)
        logger.info("場所名を変更しました: %s", normalized_id)

    def create_meter(self, request: MeterMasterCreateInput) -> MeterMasterRecord:
        with self._transaction_factory():
            created = self.create_meter_within_transaction(request)
        logger.info("物理メーターを登録しました: %s", created.meter_id)
        return created

    def prepare_meter_creation(self, request: MeterMasterCreateInput) -> PreparedMeterCreation:
        try:
            category = Category(request.category)
        except (TypeError, ValueError) as exc:
            raise MeterValidationError(
                [f"category は {METER_CATEGORY_VALUES} のいずれかです。"]
            ) from exc
        meter_name = _required(request.meter_name, "meter_name")
        if (request.location_id is None) == (request.new_location_name is None):
            raise MeterValidationError(["既存の場所か新しい場所名のいずれかを指定してください。"])
        if request.location_id is not None:
            location_id = _required(request.location_id, "location_id")
            location = self._repository.get_meter_location(location_id)
            if location is None:
                raise MeterValidationError([f"場所が見つかりません: {location_id}"])
            if location.has_name_conflict or not location.location_name.strip():
                raise MeterValidationError(
                    [f"場所名が不一致です。場所名を統一してから登録してください: {location_id}"]
                )
            new_location_name = None
        else:
            assert request.new_location_name is not None
            location = None
            new_location_name = _required(request.new_location_name, "場所名")
        return PreparedMeterCreation(
            meter_name=meter_name,
            category=category,
            location=location,
            new_location_name=new_location_name,
            meter_serial=_optional(request.meter_serial),
        )

    def create_meter_within_transaction(self, request: MeterMasterCreateInput) -> MeterMasterRecord:
        """呼出し側のトランザクション内で、通常登録と同じ検証・採番を行う。"""

        prepared = self.prepare_meter_creation(request)
        meter_id = _required(self._id_provider(), "採番した meter_id")
        if self._repository.get_meter_master(meter_id) is not None:
            raise MeterValidationError([f"meter_id '{meter_id}' は既に存在します。"])
        if prepared.location is not None:
            location_id = prepared.location.location_id
            location_name = prepared.location.location_name
        else:
            assert prepared.new_location_name is not None
            location_name = prepared.new_location_name
            location_id = _required(self._id_provider(), "採番した location_id")
            if self._repository.get_meter_location(location_id) is not None:
                raise MeterValidationError([f"location_id '{location_id}' は既に存在します。"])
        normalized = MeterMasterCreateRequest(
            meter_id=meter_id,
            location_id=location_id,
            location_name=location_name,
            meter_name=prepared.meter_name,
            category=prepared.category,
            meter_serial=prepared.meter_serial,
        )
        return self._repository.create_meter_master(normalized)

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
