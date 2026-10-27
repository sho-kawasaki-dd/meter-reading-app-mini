"""測定定義の登録・更新・論理削除と初期割当を扱う service。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from meter_reading_mini.core.contract_models import Purpose
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
    MeasurementDefinitionManagementRepository,
    MeasurementDefinitionRecord,
    MeasurementDefinitionUpdateRequest,
    MeterAssignmentRepository,
    MeterMasterWriteRepository,
)
from meter_reading_mini.core.schedule_settings import (
    deserialize_schedule,
    validate_schedule_type_for_reading_cycle,
)
from meter_reading_mini.core.timezone_settings import normalize_to_utc

logger = logging.getLogger(__name__)

TransactionFactory = Callable[[], AbstractContextManager[object]]


class MeasurementDefinitionService:
    """登録・更新・再有効化などの業務境界で transaction を所有する。"""

    def __init__(
        self,
        repository: MeasurementDefinitionManagementRepository,
        assignment_repository: MeterAssignmentRepository,
        meter_repository: MeterMasterWriteRepository,
        *,
        transaction_factory: TransactionFactory,
    ) -> None:
        self._repository = repository
        self._assignment_repository = assignment_repository
        self._meter_repository = meter_repository
        self._transaction_factory = transaction_factory

    def get_measurement_definition(
        self, measurement_definition_id: str
    ) -> MeasurementDefinitionRecord | None:
        return self._repository.get_measurement_definition(measurement_definition_id)

    def create_measurement_definition(
        self, request: MeasurementDefinitionCreateRequest
    ) -> MeasurementDefinitionRecord:
        normalized = _normalize_create_request(request)
        with self._transaction_factory():
            if (
                self._repository.get_measurement_definition(normalized.measurement_definition_id)
                is not None
            ):
                raise ValidationError(
                    f"測定定義 ID は既に登録されています: {normalized.measurement_definition_id}"
                )
            self._repository.create_measurement_definition(normalized)
            created = self._repository.get_measurement_definition(
                normalized.measurement_definition_id
            )
            if created is None:
                raise ValidationError("登録した測定定義を取得できません。")
        logger.info("測定定義を登録しました: %s", created.measurement_definition_id)
        return created

    def update_measurement_definition(
        self,
        measurement_definition_id: str,
        request: MeasurementDefinitionUpdateRequest,
    ) -> MeasurementDefinitionRecord:
        normalized_id = _required(measurement_definition_id, "measurement_definition_id")
        normalized = _normalize_update_request(request)
        with self._transaction_factory():
            current = self._repository.get_measurement_definition(normalized_id)
            if current is None:
                raise ValidationError(f"測定定義が見つかりません: {normalized_id}")
            self._reject_immutable_changes(current, normalized)
            self._repository.update_measurement_definition(normalized_id, normalized)
            updated = self._repository.get_measurement_definition(normalized_id)
            if updated is None:
                raise ValidationError(f"更新した測定定義を取得できません: {normalized_id}")
        logger.info("測定定義を更新しました: %s", normalized_id)
        return updated

    def set_measurement_definition_active(
        self,
        measurement_definition_id: str,
        is_active: bool,
        *,
        generation_start_date: date | None = None,
    ) -> None:
        normalized_id = _required(measurement_definition_id, "measurement_definition_id")
        with self._transaction_factory():
            self._repository.set_measurement_definition_active(
                normalized_id,
                is_active,
                generation_start_date=generation_start_date,
            )
        logger.info(
            "測定定義を%sしました: %s",
            "有効化" if is_active else "無効化",
            normalized_id,
        )

    def deactivate_measurement_definition(self, measurement_definition_id: str) -> None:
        self.set_measurement_definition_active(measurement_definition_id, False)

    def activate_measurement_definition(self, measurement_definition_id: str) -> None:
        self.set_measurement_definition_active(measurement_definition_id, True)

    def set_initial_meter_assignment(
        self,
        measurement_definition_id: str,
        meter_id: str,
        effective_from: datetime,
    ) -> None:
        definition_id = _required(measurement_definition_id, "measurement_definition_id")
        normalized_meter_id = _required(meter_id, "meter_id")
        normalized_effective_from = normalize_to_utc(effective_from)
        with self._transaction_factory():
            if self._meter_repository.get_meter_master(normalized_meter_id) is None:
                raise ValidationError(f"物理メーターが見つかりません: {normalized_meter_id}")
            self._assignment_repository.set_initial_meter_assignment(
                definition_id,
                normalized_meter_id,
                normalized_effective_from,
            )
        logger.info(
            "測定定義の初期割当を登録しました: %s -> %s",
            definition_id,
            normalized_meter_id,
        )

    @staticmethod
    def _reject_immutable_changes(
        current: MeasurementDefinitionRecord,
        request: MeasurementDefinitionUpdateRequest,
    ) -> None:
        immutable_changes = (
            (request.location_id, current.location_id, "集計場所"),
            (request.reading_cycle, current.reading_cycle, "検針周期"),
            (request.value_schema, current.value_schema, "値形式"),
            (request.calculation_method, current.calculation_method, "計算方法"),
        )
        for requested, existing, label in immutable_changes:
            if requested is not None and requested != existing:
                raise ValidationError(f"作成後に{label}は変更できません。")


def _normalize_create_request(
    request: MeasurementDefinitionCreateRequest,
) -> MeasurementDefinitionCreateRequest:
    try:
        purpose = Purpose(request.purpose)
    except (TypeError, ValueError) as exc:
        raise ValidationError("集計目的は operational / billing / audit のいずれかです。") from exc
    if not request.multiplier.is_finite() or request.multiplier <= 0:
        raise ValidationError("倍率は有限の正数で指定してください。")
    _validate_optional_decimal(request.warning_threshold, "警告閾値", allow_zero=True)
    _validate_optional_decimal(request.rollover_value, "ロールオーバー値", allow_zero=False)
    _validate_decimal_places(request.decimal_places)
    if request.initial_schedule_json is not None:
        try:
            schedule = deserialize_schedule(request.reading_cycle, request.initial_schedule_json)
            validate_schedule_type_for_reading_cycle(request.reading_cycle, schedule)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"初期スケジュールが正しくありません: {exc}") from exc
    return replace(
        request,
        purpose=purpose,
        measurement_definition_id=_required(
            request.measurement_definition_id, "measurement_definition_id"
        ),
        location_id=_required(request.location_id, "location_id"),
        display_name=_required(request.display_name, "display_name"),
        unit=_required(request.unit, "unit"),
        initial_meter_id=_optional(request.initial_meter_id),
    )


def _normalize_update_request(
    request: MeasurementDefinitionUpdateRequest,
) -> MeasurementDefinitionUpdateRequest:
    if not request.multiplier.is_finite() or request.multiplier <= 0:
        raise ValidationError("倍率は有限の正数で指定してください。")
    _validate_optional_decimal(request.warning_threshold, "警告閾値", allow_zero=True)
    _validate_optional_decimal(request.rollover_value, "ロールオーバー値", allow_zero=False)
    _validate_decimal_places(request.decimal_places)
    try:
        purpose = Purpose(request.purpose)
    except (TypeError, ValueError) as exc:
        raise ValidationError("集計目的は operational / billing / audit のいずれかです。") from exc
    return replace(
        request,
        display_name=_required(request.display_name, "display_name"),
        unit=_required(request.unit, "unit"),
        purpose=purpose,
    )


def _validate_optional_decimal(
    value: Decimal | None,
    label: str,
    *,
    allow_zero: bool,
) -> None:
    if value is None:
        return
    if not value.is_finite() or value < 0 or (not allow_zero and value == 0):
        qualifier = "0 以上" if allow_zero else "正数"
        raise ValidationError(f"{label}は有限の{qualifier}で指定してください。")


def _validate_decimal_places(value: int | None) -> None:
    if value is not None and (type(value) is not int or not 0 <= value <= 10):
        raise ValidationError("小数桁数は 0〜10 の整数で指定してください。")


def _required(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field_name} は必須です。")
    return normalized


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


__all__ = ["MeasurementDefinitionService"]
