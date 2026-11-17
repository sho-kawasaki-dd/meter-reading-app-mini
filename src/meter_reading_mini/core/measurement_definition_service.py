"""測定定義の登録・更新・論理削除と初期割当を扱う service。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from meter_reading_mini.core.contract_models import CalculationMethod, Purpose, ValueSchema
from meter_reading_mini.core.errors import FormulaReferencedDefinitionError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateInput,
    MeasurementDefinitionCreateRequest,
    MeasurementDefinitionManagementRepository,
    MeasurementDefinitionRecord,
    MeasurementDefinitionUpdateRequest,
    MeterAssignmentRecord,
    MeterAssignmentRepository,
    MeterMasterWriteRepository,
)
from meter_reading_mini.core.reading_values import validate_initial_reading
from meter_reading_mini.core.schedule_settings import (
    deserialize_schedule,
    validate_expected_daily_count,
    validate_schedule_type_for_reading_cycle,
)
from meter_reading_mini.core.timezone_settings import normalize_to_utc

logger = logging.getLogger(__name__)

TransactionFactory = Callable[[], AbstractContextManager[object]]


@dataclass(frozen=True, slots=True)
class InitialReadingState:
    value: Decimal | None
    unavailable_reason: str | None


class MeasurementDefinitionService:
    """登録・更新・再有効化などの業務境界で transaction を所有する。"""

    def __init__(
        self,
        repository: MeasurementDefinitionManagementRepository,
        assignment_repository: MeterAssignmentRepository,
        meter_repository: MeterMasterWriteRepository,
        *,
        transaction_factory: TransactionFactory,
        id_provider: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._repository = repository
        self._assignment_repository = assignment_repository
        self._meter_repository = meter_repository
        self._transaction_factory = transaction_factory
        self._id_provider = id_provider

    def get_measurement_definition(
        self, measurement_definition_id: str
    ) -> MeasurementDefinitionRecord | None:
        return self._repository.get_measurement_definition(measurement_definition_id)

    def list_measurement_definitions(
        self, *, show_inactive: bool = False
    ) -> list[MeasurementDefinitionRecord]:
        return self._repository.list_measurement_definitions(show_inactive=show_inactive)

    def create_measurement_definition(
        self, request: MeasurementDefinitionCreateInput
    ) -> MeasurementDefinitionRecord:
        normalized = _normalize_create_request(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id=_required(self._id_provider(), "採番した測定定義 ID"),
                location_id=request.location_id,
                display_name=request.display_name,
                category=request.category,
                unit=request.unit,
                value_schema=request.value_schema,
                multiplier=request.multiplier,
                decimal_places=request.decimal_places,
                warning_threshold=request.warning_threshold,
                rollover_value=request.rollover_value,
                calculation_method=request.calculation_method,
                reading_cycle=request.reading_cycle,
                purpose=request.purpose,
                generation_start_date=request.generation_start_date,
                initial_schedule_json=request.initial_schedule_json,
                initial_meter_id=request.initial_meter_id,
                initial_reading=request.initial_reading,
            )
        )
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
            if (
                current.unit != normalized.unit or current.category != normalized.category
            ) and self._repository.is_measurement_definition_referenced_by_formula(normalized_id):
                raise FormulaReferencedDefinitionError(normalized_id)
            self._validate_precision_change(normalized_id, normalized.decimal_places)
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

    def _initial_reading_state(
        self, definition: MeasurementDefinitionRecord
    ) -> tuple[MeterAssignmentRecord | None, str | None]:
        assignments = self._assignment_repository.list_meter_assignments(
            definition.measurement_definition_id
        )
        assignment = assignments[0] if assignments else None
        if not definition.is_active:
            return assignment, "無効な測定定義は変更できません。"
        if (
            definition.calculation_method is not CalculationMethod.DELTA
            or definition.value_schema is not ValueSchema.DECIMAL_STRING
        ):
            return assignment, "この測定定義には読値初期値を設定できません。"
        if len(assignments) != 1 or assignment is None:
            return assignment, "交換前の初期割当が必要です。"
        if (
            assignment.needs_review
            or self._assignment_repository.has_assignment_measured_readings(assignment)
            or self._assignment_repository.has_definition_replacement(
                definition.measurement_definition_id
            )
        ):
            return assignment, "実測後・交換後・割当確認待ちの場合は変更できません。"
        return assignment, None

    def get_initial_reading_state(self, measurement_definition_id: str) -> InitialReadingState:
        definition_id = _required(measurement_definition_id, "measurement_definition_id")
        definition = self._repository.get_measurement_definition(definition_id)
        if definition is None:
            raise ValidationError("測定定義が見つかりません。")
        assignment, reason = self._initial_reading_state(definition)
        return InitialReadingState(assignment.initial_reading if assignment else None, reason)

    def set_initial_reading(self, measurement_definition_id: str, value: Decimal | None) -> None:
        definition_id = _required(measurement_definition_id, "measurement_definition_id")
        with self._transaction_factory():
            definition = self._repository.get_measurement_definition(definition_id)
            if definition is None:
                raise ValidationError("有効な測定定義が見つかりません。")
            assignment, reason = self._initial_reading_state(definition)
            if reason is not None:
                raise ValidationError(reason)
            if assignment is None:
                raise ValidationError("交換前の初期割当が必要です。")
            if value is not None:
                validate_initial_reading(
                    value,
                    decimal_places=definition.decimal_places,
                    rollover_value=assignment.rollover_value,
                )
            self._assignment_repository.set_assignment_initial_reading(assignment, value)

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

    def _validate_precision_change(
        self, measurement_definition_id: str, decimal_places: int | None
    ) -> None:
        if decimal_places is None:
            return
        for assignment in self._assignment_repository.list_meter_assignments(
            measurement_definition_id
        ):
            if assignment.initial_reading is not None:
                fractional = format(assignment.initial_reading, "f").partition(".")[2].rstrip("0")
                if len(fractional) > decimal_places:
                    raise ValidationError("既存の読値初期値を表せない小数桁数には変更できません。")
            rollover = assignment.rollover_value
            if rollover is None:
                continue
            fractional = format(rollover, "f").partition(".")[2].rstrip("0")
            if len(fractional) > decimal_places:
                raise ValidationError(
                    "小数桁数を減らす場合、既存のロールオーバー値を表せないため変更できません。"
                )


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
    if request.initial_reading is not None:
        if _optional(request.initial_meter_id) is None:
            raise ValidationError("読値初期値には初期メーターの指定が必要です。")
        if (
            request.calculation_method is not CalculationMethod.DELTA
            or request.value_schema is not ValueSchema.DECIMAL_STRING
        ):
            raise ValidationError("この測定定義には読値初期値を設定できません。")
        validate_initial_reading(
            request.initial_reading,
            decimal_places=request.decimal_places,
            rollover_value=request.rollover_value,
        )
    if request.initial_schedule_json is not None:
        try:
            schedule = deserialize_schedule(request.reading_cycle, request.initial_schedule_json)
            validate_schedule_type_for_reading_cycle(request.reading_cycle, schedule)
            validate_expected_daily_count(request.reading_cycle, schedule)
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
    _validate_optional_decimal(request.warning_threshold, "警告閾値", allow_zero=True)
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
