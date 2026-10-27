"""スマホ用の単一 HTML 検針シートを組み立てる service。"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from decimal import Decimal

from meter_reading_mini.core.contract_models import (
    MeasurementDefinitionData,
    MeterMasterData,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    HistoricalMeasuredReadingLookupRepository,
    PlannedReadingRecord,
    PlannedReadingTaskQueryRepository,
    PlannedReadingTaskState,
    ReplacementBoundaryLookupRepository,
    SheetMasterQueryRepository,
    SheetPlannedReadingRecord,
)
from meter_reading_mini.core.mobile_reading import (
    PAYLOAD_VERSION,
    SHEET_KIND,
    SheetFilter,
    cycle_group_for,
    format_sheet_timestamps,
    render_sheet_html,
)
from meter_reading_mini.core.previous_reading import (
    describe_initial_previous_reading,
    resolve_calculation_context,
)
from meter_reading_mini.core.timeutil import utc_now, utc_now_str
from meter_reading_mini.core.timezone_settings import DEFAULT_APP_TIMEZONE
from meter_reading_mini.core.work_package_download import (
    PlannedReadingGeneratorService,
    PlannedReadingLifecycleService,
)

SHEET_TASK_STATES = (PlannedReadingTaskState.OPEN, PlannedReadingTaskState.AUTO_CLOSED)


class FutureSheetBusinessDateError(ValidationError):
    """未来の業務日を指定したことを表す。"""

    def __init__(self, target_business_date: date, current_business_date: date) -> None:
        self.target_business_date = target_business_date
        self.current_business_date = current_business_date
        super().__init__(
            f"未来の業務日の検針シートは出力できません: {target_business_date.isoformat()}"
        )


class EmptySheetError(ValidationError):
    """出力対象の検針枠が 0 件であることを表す。"""

    def __init__(self, target_business_date: date) -> None:
        self.target_business_date = target_business_date
        super().__init__(f"{target_business_date.isoformat()} に出力できる検針枠がありません。")


@dataclass(frozen=True, slots=True)
class MobileReadingSheet:
    sheet_id: str
    html: str
    filename: str
    slot_count: int


def _new_sheet_id() -> str:
    return secrets.token_hex(16)


def build_sheet_filename(business_date: date, file_time: str, sheet_filter: SheetFilter) -> str:
    """絞り込み条件に応じたシートのファイル名を返す。"""

    sessions = sheet_filter.reading_sessions
    groups = sheet_filter.cycle_groups
    if sheet_filter.is_empty:
        qualifier = ""
    elif sessions is not None and len(sessions) == 1 and groups is None:
        qualifier = f"{sessions[0].replace(':', '')}_"
    elif groups is not None and len(groups) == 1 and sessions is None:
        qualifier = f"{groups[0]}_"
    else:
        qualifier = "filtered_"
    return f"meter_reading_{business_date.strftime('%Y%m%d')}_{qualifier}{file_time}.html"


class MobileReadingSheetService:
    """補完・自動クローズ・枠取得・前回値算出を 1 トランザクションで行い HTML を作る。"""

    def __init__(
        self,
        *,
        planned_reading_generator: PlannedReadingGeneratorService,
        planned_reading_lifecycle_service: PlannedReadingLifecycleService,
        task_repository: PlannedReadingTaskQueryRepository,
        master_repository: SheetMasterQueryRepository,
        historical_reading_repository: HistoricalMeasuredReadingLookupRepository,
        boundary_repository: ReplacementBoundaryLookupRepository,
        transaction_factory: Callable[[], AbstractContextManager[object]],
        timezone_provider: Callable[[], tzinfo] | None = None,
        clock: Callable[[], datetime] = utc_now,
        sheet_id_factory: Callable[[], str] = _new_sheet_id,
    ) -> None:
        self._planned_reading_generator = planned_reading_generator
        self._planned_reading_lifecycle_service = planned_reading_lifecycle_service
        self._task_repository = task_repository
        self._master_repository = master_repository
        self._historical_reading_repository = historical_reading_repository
        self._boundary_repository = boundary_repository
        self._transaction_factory = transaction_factory
        self._timezone_provider = timezone_provider or (lambda: DEFAULT_APP_TIMEZONE)
        self._clock = clock
        self._sheet_id_factory = sheet_id_factory

    def create_sheet(
        self,
        target_business_date: date,
        *,
        reading_sessions: Collection[str] | None = None,
        cycle_groups: Collection[str] | None = None,
    ) -> MobileReadingSheet:
        sheet_filter = SheetFilter.create(reading_sessions, cycle_groups)
        business_timezone = self._timezone_provider()
        now = self._clock()
        generated_at = utc_now_str(now)
        current_business_date = now.astimezone(business_timezone).date()
        if target_business_date > current_business_date:
            raise FutureSheetBusinessDateError(target_business_date, current_business_date)

        with self._transaction_factory():
            self._planned_reading_generator.ensure_generated_through(target_business_date)
            self._planned_reading_lifecycle_service.reconcile_before_download()
            slots = [
                slot
                for slot in self._task_repository.list_planned_reading_tasks_for_sheet(
                    target_business_date, SHEET_TASK_STATES
                )
                if _matches_filter(slot, sheet_filter)
            ]
            sheet_body = self._build_sheet_body(slots) if slots else None
        if sheet_body is None:
            raise EmptySheetError(target_business_date)

        local_text, stamp = format_sheet_timestamps(generated_at, business_timezone)
        sheet_id = self._sheet_id_factory()
        sheet_data: dict[str, object] = {
            "kind": SHEET_KIND,
            "v": PAYLOAD_VERSION,
            "sheet_id": sheet_id,
            "business_date": target_business_date.isoformat(),
            "generated_at": generated_at,
            "generated_at_local": local_text,
            "filter": sheet_filter.to_json(),
            **sheet_body,
        }
        return MobileReadingSheet(
            sheet_id=sheet_id,
            html=render_sheet_html(sheet_data),
            filename=build_sheet_filename(target_business_date, stamp.split("_")[-1], sheet_filter),
            slot_count=len(slots),
        )

    def _build_sheet_body(self, slots: Sequence[SheetPlannedReadingRecord]) -> dict[str, object]:
        meters = self._master_repository.list_sheet_meters({slot.meter_id for slot in slots})
        definitions = self._master_repository.list_sheet_measurement_definitions(
            {slot.measurement_definition_id for slot in slots}
        )
        _require_masters(slots, meters, definitions)
        return {
            "locations": _build_locations(slots, meters),
            "meters": [_meter_json(meter) for meter in meters],
            "definitions": [_definition_json(definition) for definition in definitions],
            "slots": [self._slot_json(slot) for slot in slots],
        }

    def _slot_json(self, slot: SheetPlannedReadingRecord) -> dict[str, object]:
        context = resolve_calculation_context(
            self._historical_reading_repository,
            self._boundary_repository,
            PlannedReadingRecord(
                planned_reading_id=slot.planned_reading_id,
                measurement_definition_id=slot.measurement_definition_id,
                meter_id=slot.meter_id,
                scheduled_at=slot.scheduled_at,
            ),
            excluding_planned_reading_id=slot.planned_reading_id,
        )
        initial = describe_initial_previous_reading(context)
        return {
            "planned_reading_id": slot.planned_reading_id,
            "measurement_definition_id": slot.measurement_definition_id,
            "meter_id": slot.meter_id,
            "reading_session": slot.reading_session.value,
            "scheduled_at": utc_now_str(slot.scheduled_at),
            "reading_cycle": slot.reading_cycle.value,
            "task_state": slot.task_state.value,
            "initial_last_reading": _decimal_json(initial.reading),
            "initial_previous_scheduled_at": (
                None if initial.scheduled_at is None else utc_now_str(initial.scheduled_at)
            ),
            "initial_previous_planned_reading_id": initial.planned_reading_id,
            "previous_source": initial.source.value,
        }


def _matches_filter(slot: SheetPlannedReadingRecord, sheet_filter: SheetFilter) -> bool:
    if (
        sheet_filter.reading_sessions is not None
        and slot.reading_session.value not in sheet_filter.reading_sessions
    ):
        return False
    return (
        sheet_filter.cycle_groups is None
        or cycle_group_for(slot.reading_cycle) in sheet_filter.cycle_groups
    )


def _require_masters(
    slots: Sequence[SheetPlannedReadingRecord],
    meters: Sequence[MeterMasterData],
    definitions: Sequence[MeasurementDefinitionData],
) -> None:
    meter_ids = {meter.meter_id for meter in meters}
    definition_ids = {definition.measurement_definition_id for definition in definitions}
    for slot in slots:
        if slot.meter_id not in meter_ids:
            raise ValidationError(f"シートに必要な物理メーターが見つかりません: {slot.meter_id}")
        if slot.measurement_definition_id not in definition_ids:
            raise ValidationError(
                f"シートに必要な測定定義が見つかりません: {slot.measurement_definition_id}"
            )


def _build_locations(
    slots: Sequence[SheetPlannedReadingRecord], meters: Sequence[MeterMasterData]
) -> list[dict[str, str]]:
    names: dict[str, str] = {}
    for meter in meters:
        names.setdefault(meter.location_id, meter.location_name)
    locations: list[dict[str, str]] = []
    for location_id in sorted({slot.location_id for slot in slots}):
        if location_id not in names:
            raise ValidationError(f"シートに必要な場所名が見つかりません: {location_id}")
        locations.append({"location_id": location_id, "location_name": names[location_id]})
    return locations


def _decimal_json(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _meter_json(meter: MeterMasterData) -> dict[str, object]:
    return {
        "meter_id": meter.meter_id,
        "location_id": meter.location_id,
        "location_name": meter.location_name,
        "meter_name": meter.meter_name,
        "category": meter.category.value,
        "meter_serial": meter.meter_serial,
    }


def _definition_json(definition: MeasurementDefinitionData) -> dict[str, object]:
    return {
        "measurement_definition_id": definition.measurement_definition_id,
        "location_id": definition.location_id,
        "display_name": definition.display_name,
        "category": definition.category.value,
        "unit": definition.unit,
        "value_schema": definition.value_schema.value,
        "multiplier": format(definition.multiplier, "f"),
        "decimal_places": definition.decimal_places,
        "warning_threshold": _decimal_json(definition.warning_threshold),
        "rollover_value": _decimal_json(definition.rollover_value),
        "calculation_method": definition.calculation_method.value,
    }


__all__ = [
    "EmptySheetError",
    "FutureSheetBusinessDateError",
    "MobileReadingSheet",
    "MobileReadingSheetService",
    "build_sheet_filename",
]
