# pyright: reportMissingImports=false

"""measurement_definition 単位の schedule service を検証するテスト群。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from meter_reading_mini.core.contract_models import ReadingCycle, ReadingSession
from meter_reading_mini.core.meter_reading_sync_repositories import (
    DailySchedule,
    EveryTwoMonthsSchedule,
    MonthlySchedule,
    MonthlyScheduleSelectorType,
    MonthParity,
    ScheduleItem,
    WeeklySchedule,
)
from meter_reading_mini.core.schedule_settings import (
    DEFAULT_SCHEDULES,
    MeasurementDefinitionScheduleService,
    ScheduleSettingsService,
    ScheduleValidationError,
    validate_schedule_type_for_reading_cycle,
)


@dataclass
class FakeMeasurementDefinitionScheduleRepository:
    """measurement_definition_schedules の読み書きを模擬する fake repository。"""

    schedules: dict[str, str] = field(default_factory=dict)

    def get_measurement_definition_schedule(self, measurement_definition_id: str) -> str | None:
        return self.schedules.get(measurement_definition_id)

    def upsert_measurement_definition_schedule(
        self, measurement_definition_id: str, schedule_json: str
    ) -> None:
        self.schedules[measurement_definition_id] = schedule_json


@dataclass
class FakePlannedReadingRecompositionRepository:
    """future planned reading 再構成を模擬する fake repository。"""

    calls: list[tuple[str, ReadingCycle, object, date]] = field(default_factory=list)

    def recompose_future_planned_readings(
        self,
        measurement_definition_id: str,
        reading_cycle: ReadingCycle,
        schedule,
        *,
        from_business_date: date | None = None,
    ) -> tuple[int, int]:
        self.calls.append(
            (measurement_definition_id, reading_cycle, schedule, from_business_date or date.min)
        )
        return (2, 3)


@pytest.mark.parametrize("reading_cycle", list(ReadingCycle))
def test_measurement_definition_schedule_service_round_trips_default_schedules(
    reading_cycle: ReadingCycle,
) -> None:
    """default schedule が save/get で round-trip することを確認する。"""

    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)
    default_schedule = DEFAULT_SCHEDULES[reading_cycle]

    service.save_schedule("md-001", reading_cycle, default_schedule)
    loaded = service.get_schedule("md-001", reading_cycle)

    assert loaded == default_schedule
    assert repository.schedules["md-001"] == ScheduleSettingsService.serialize_default(
        reading_cycle
    )


def test_measurement_definition_schedule_service_triggers_recomposition_on_save() -> None:
    """schedule 保存時に future planned reading 再構成が呼ばれることを確認する。"""

    repository = FakeMeasurementDefinitionScheduleRepository()
    recomposition_repository = FakePlannedReadingRecompositionRepository()
    service = MeasurementDefinitionScheduleService(
        repository,
        recomposition_repository=recomposition_repository,
        current_business_date_provider=lambda: date(2026, 4, 2),
    )
    schedule = WeeklySchedule(weekday=2, reading_session=ReadingSession.SESSION_2200)

    service.save_schedule("md-001", ReadingCycle.WEEKLY, schedule)

    assert recomposition_repository.calls == [
        ("md-001", ReadingCycle.WEEKLY, schedule, date(2026, 4, 2)),
    ]


@pytest.mark.parametrize(
    "reading_cycle, schedule",
    [
        (
            ReadingCycle.DAILY_1X,
            DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0800),)),
        ),
        (
            ReadingCycle.WEEKLY,
            WeeklySchedule(weekday=1, reading_session=ReadingSession.SESSION_0800),
        ),
        (
            ReadingCycle.MONTHLY,
            MonthlySchedule(
                selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
                day_of_month=1,
                reading_session=ReadingSession.SESSION_0800,
            ),
        ),
        (
            ReadingCycle.EVERY_TWO_MONTHS,
            EveryTwoMonthsSchedule(
                selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
                month_parity=MonthParity.ODD,
                day_of_month=1,
                reading_session=ReadingSession.SESSION_0800,
                day_offset=0,
            ),
        ),
    ],
)
def test_validate_schedule_type_for_reading_cycle_accepts_matching_types(
    reading_cycle: ReadingCycle, schedule
) -> None:
    """reading_cycle と schedule 型が一致すると検証を通す。"""

    validate_schedule_type_for_reading_cycle(reading_cycle, schedule)


@pytest.mark.parametrize(
    "reading_cycle, schedule",
    [
        (
            ReadingCycle.DAILY_1X,
            WeeklySchedule(weekday=1, reading_session=ReadingSession.SESSION_0800),
        ),
        (
            ReadingCycle.WEEKLY,
            DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0800),)),
        ),
        (
            ReadingCycle.MONTHLY,
            DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0800),)),
        ),
        (
            ReadingCycle.EVERY_TWO_MONTHS,
            MonthlySchedule(
                selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
                day_of_month=1,
                reading_session=ReadingSession.SESSION_0800,
                day_offset=0,
            ),
        ),
    ],
)
def test_measurement_definition_schedule_service_rejects_mismatched_schedule_types(
    reading_cycle: ReadingCycle,
    schedule,
) -> None:
    """reading_cycle と schedule 型が一致しないと validation error を返す。"""

    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)

    with pytest.raises(ValueError):
        service.save_schedule("md-001", reading_cycle, schedule)


def test_measurement_definition_schedule_service_ensures_default_schedule_when_missing() -> None:
    """未作成の schedule row は default で作成される。"""

    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)

    loaded = service.ensure_default_schedule("md-002", ReadingCycle.WEEKLY)

    assert loaded == DEFAULT_SCHEDULES[ReadingCycle.WEEKLY]
    assert repository.schedules["md-002"] == ScheduleSettingsService.serialize_default(
        ReadingCycle.WEEKLY
    )

    repository.schedules["md-002"] = '{"weekday": "monday", "reading_session": "08:00"}'
    loaded_again = service.ensure_default_schedule("md-002", ReadingCycle.WEEKLY)

    assert loaded_again == WeeklySchedule(weekday=0, reading_session=ReadingSession.SESSION_0800)


def test_measurement_definition_schedule_service_accepts_legacy_daily_json_without_day_offset() -> (
    None
):
    """旧 daily JSON の reading_sessions 配列も day_offset=0 として読める。"""

    repository = FakeMeasurementDefinitionScheduleRepository(
        schedules={"md-legacy": '{"reading_sessions": ["08:00", "22:00"]}'},
    )
    service = MeasurementDefinitionScheduleService(repository)

    loaded = service.get_schedule("md-legacy", ReadingCycle.DAILY_2X)

    assert loaded == DailySchedule(
        reading_items=(
            ScheduleItem(ReadingSession.SESSION_0800),
            ScheduleItem(ReadingSession.SESSION_2200),
        ),
    )


def test_save_rejects_daily_1x_with_zero_items() -> None:
    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)

    empty = DailySchedule(reading_items=())

    with pytest.raises(ScheduleValidationError) as exc:
        service.save_schedule("md-001", ReadingCycle.DAILY_1X, empty)

    assert any(ReadingCycle.DAILY_1X.value in msg for msg in exc.value.errors)


def test_save_rejects_daily_2x_with_one_item() -> None:
    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)

    schedule = DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0800),))

    with pytest.raises(ScheduleValidationError) as exc:
        service.save_schedule("md-002", ReadingCycle.DAILY_2X, schedule)

    assert any(ReadingCycle.DAILY_2X.value in msg for msg in exc.value.errors)


def test_save_rejects_daily_2x_with_three_items() -> None:
    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)

    schedule = DailySchedule(
        reading_items=(
            ScheduleItem(ReadingSession.SESSION_0800),
            ScheduleItem(ReadingSession.SESSION_2200),
            ScheduleItem(ReadingSession.SESSION_1400),
        )
    )

    with pytest.raises(ScheduleValidationError) as exc:
        service.save_schedule("md-003", ReadingCycle.DAILY_2X, schedule)

    assert any(ReadingCycle.DAILY_2X.value in msg for msg in exc.value.errors)


def test_save_rejects_daily_3x_with_two_items() -> None:
    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)

    schedule = DailySchedule(
        reading_items=(
            ScheduleItem(ReadingSession.SESSION_0600),
            ScheduleItem(ReadingSession.SESSION_1400),
        )
    )

    with pytest.raises(ScheduleValidationError) as exc:
        service.save_schedule("md-004", ReadingCycle.DAILY_3X, schedule)

    assert any(ReadingCycle.DAILY_3X.value in msg for msg in exc.value.errors)


def test_save_rejects_duplicate_reading_sessions() -> None:
    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)

    schedule = DailySchedule(
        reading_items=(
            ScheduleItem(ReadingSession.SESSION_0800),
            ScheduleItem(ReadingSession.SESSION_0800),
        )
    )

    with pytest.raises(ScheduleValidationError) as exc:
        service.save_schedule("md-dup", ReadingCycle.DAILY_2X, schedule)

    assert any("重複" in msg for msg in exc.value.errors)


def test_save_accepts_expected_daily_count() -> None:
    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)

    schedule = DailySchedule(
        reading_items=(
            ScheduleItem(ReadingSession.SESSION_0800),
            ScheduleItem(ReadingSession.SESSION_2200),
        )
    )

    service.save_schedule("md-ok", ReadingCycle.DAILY_2X, schedule)

    loaded = service.get_schedule("md-ok", ReadingCycle.DAILY_2X)
    assert loaded == schedule


@pytest.mark.parametrize(
    "reading_cycle, sessions",
    [
        (ReadingCycle.DAILY_1X, ()),
        (ReadingCycle.DAILY_2X, (ScheduleItem(ReadingSession.SESSION_0800),)),
        (
            ReadingCycle.DAILY_3X,
            (ScheduleItem(ReadingSession.SESSION_0600), ScheduleItem(ReadingSession.SESSION_1400)),
        ),
    ],
)
def test_parametrized_expected_count_rejections(reading_cycle: ReadingCycle, sessions) -> None:
    repository = FakeMeasurementDefinitionScheduleRepository()
    service = MeasurementDefinitionScheduleService(repository)

    schedule = DailySchedule(reading_items=tuple(sessions))

    with pytest.raises(ScheduleValidationError):
        service.save_schedule("md-param", reading_cycle, schedule)
