"""`GET /api/work-packages/today` の業務オーケストレーションを担当する service。"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, tzinfo

logger = logging.getLogger(__name__)

MAX_BACKFILL_DAYS_PER_REQUEST = 14
TASK_COUNT_WARN_THRESHOLD = 5000
MASTER_COUNT_WARN_THRESHOLD = 2000

from meter_reading_mini.core.contract_models import (
    PlannedReadingTask,
    ReadingCycle,
    ReadingSession,
    WorkPackageResponse,
    WorkPackageTaskGroups,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    DailySchedule,
    EveryTwoMonthsSchedule,
    MeasurementDefinitionQueryRepository,
    MeterMasterQueryRepository,
    MonthlySchedule,
    MonthlyScheduleSelectorType,
    MonthParity,
    PlannedReadingCloseReason,
    PlannedReadingCreateRequest,
    PlannedReadingGenerationRepository,
    PlannedReadingLifecycleRepository,
    PlannedReadingTaskQueryRepository,
    ReleaseDateCalculator,
    ScheduleSettings,
    WeeklySchedule,
)
from meter_reading_mini.core.timezone_settings import (
    DEFAULT_APP_TIMEZONE,
    build_scheduled_at,
    build_business_date_provider,
    build_current_datetime_provider,
)

BusinessDateProvider = Callable[[], date]
CurrentDateTimeProvider = Callable[[], datetime]
CutoffTimeProvider = Callable[[], time]


class WorkPackageDownloadServiceError(ValueError):
    """work package download path で扱う業務エラーの基底例外。"""


class FutureBusinessDateError(WorkPackageDownloadServiceError):
    """future business_date を要求したときに送出する。"""

    def __init__(self, target_business_date: date, current_business_date: date) -> None:
        super().__init__(
            f"Future business_date is not supported: {target_business_date.isoformat()} > {current_business_date.isoformat()}"
        )
        self.target_business_date = target_business_date
        self.current_business_date = current_business_date


def _default_cutoff_time_provider() -> time:
    """auto-close の既定 cutoff 時刻を返す。"""

    return time(hour=7, minute=0)


class PlannedReadingGeneratorService:
    """target business_date までの planned reading を backfill 生成する service。"""

    def __init__(
        self,
        repository: PlannedReadingGenerationRepository,
        *,
        release_date_calculator: ReleaseDateCalculator | None = None,
        scheduled_timezone_provider: Callable[[], tzinfo] | None = None,
    ) -> None:
        self._repository = repository
        self._release_date_calculator = release_date_calculator
        self._scheduled_timezone_provider = scheduled_timezone_provider or (
            lambda: DEFAULT_APP_TIMEZONE
        )

    def ensure_generated_through(self, target_business_date: date) -> bool:
        """不足している business_date 範囲だけを冪等に生成し、継続必要時は True を返す。"""

        definitions = self._repository.list_planned_reading_generation_definitions()
        skipped_definitions = (
            self._repository.list_measurement_definitions_without_active_assignment()
        )
        for skipped_definition in skipped_definitions:
            logger.warning(
                "Skipped generation for measurement_definition_id=%s: No active meter assignment",
                skipped_definition.measurement_definition_id,
            )
        if not definitions:
            return False

        latest_business_date = self._repository.get_latest_planned_business_date()
        resolved_target_business_date = target_business_date
        backfill_pending = False
        if latest_business_date is not None:
            max_target_business_date = latest_business_date + timedelta(
                days=MAX_BACKFILL_DAYS_PER_REQUEST
            )
            if target_business_date > max_target_business_date:
                resolved_target_business_date = max_target_business_date
                backfill_pending = True

        logger.info(
            "Backfill target resolved: requested=%s resolved=%s latest=%s backfill_pending=%s",
            target_business_date.isoformat(),
            resolved_target_business_date.isoformat(),
            latest_business_date.isoformat() if latest_business_date is not None else "none",
            backfill_pending,
        )

        start_business_date = (
            resolved_target_business_date
            if latest_business_date is None
            else latest_business_date + timedelta(days=1)
        )
        if start_business_date > resolved_target_business_date:
            return backfill_pending

        planned_reading_requests: list[PlannedReadingCreateRequest] = []
        current_business_date = start_business_date
        scheduled_tzinfo = self._scheduled_timezone_provider()
        while current_business_date <= resolved_target_business_date:
            for definition in definitions:
                schedule = definition.schedule
                if schedule is None:
                    raise ValueError(
                        f"Missing schedule for measurement_definition_id={definition.measurement_definition_id}"
                    )
                for reading_session, day_offset in _resolve_schedule_items(
                    definition.reading_cycle, current_business_date, schedule
                ):
                    effective_business_date = current_business_date + timedelta(days=day_offset)
                    release_date = effective_business_date
                    if self._release_date_calculator is not None:
                        release_date = self._release_date_calculator.compute_release_date(
                            effective_business_date, reading_session
                        )
                    planned_reading_requests.append(
                        PlannedReadingCreateRequest(
                            planned_reading_id=_build_planned_reading_id(
                                definition.measurement_definition_id,
                                current_business_date,
                                reading_session,
                            ),
                            measurement_definition_id=definition.measurement_definition_id,
                            meter_id=definition.meter_id,
                            location_id=definition.location_id,
                            business_date=current_business_date,
                            release_date=release_date,
                            reading_session=reading_session,
                            scheduled_at=build_scheduled_at(
                                current_business_date,
                                reading_session.value,
                                day_offset=day_offset,
                                tzinfo=scheduled_tzinfo,
                            ),
                            day_offset=day_offset,
                        )
                    )
            current_business_date += timedelta(days=1)

        generated_count = self._repository.create_planned_readings_if_missing(
            planned_reading_requests
        )
        logger.info(
            "Backfill generated %d planned reading(s) for %s through %s",
            generated_count,
            start_business_date.isoformat(),
            resolved_target_business_date.isoformat(),
        )
        return backfill_pending


class PlannedReadingLifecycleService:
    """download 前に planned reading の internal lifecycle を整える service。"""

    def __init__(
        self,
        repository: PlannedReadingLifecycleRepository,
        *,
        current_datetime_provider: CurrentDateTimeProvider | None = None,
        cutoff_time_provider: CutoffTimeProvider | None = None,
    ) -> None:
        self._repository = repository
        self._current_datetime_provider = (
            current_datetime_provider or build_current_datetime_provider()
        )
        self._cutoff_time_provider = cutoff_time_provider or _default_cutoff_time_provider

    def reconcile_before_download(self, target_business_date: date) -> int:
        """requested business_date を返す前に、過去日の open task を auto-close する。"""

        current_datetime = self._current_datetime_provider()
        through_business_date = _resolve_auto_close_through_business_date(
            target_business_date=target_business_date,
            current_datetime=current_datetime,
            cutoff_time=self._cutoff_time_provider(),
        )
        closed_count = self._repository.auto_close_open_planned_readings(
            through_business_date,
            closed_at=current_datetime,
            close_reason=PlannedReadingCloseReason.CUTOFF_ELAPSED,
        )
        if closed_count:
            logger.info(
                "Auto-closed %d planned reading(s) through %s",
                closed_count,
                through_business_date.isoformat(),
            )
        return closed_count


class WorkPackageDownloadService:
    """planned reading の生成と download response 組み立てをまとめる service。"""

    def __init__(
        self,
        planned_reading_generator: PlannedReadingGeneratorService,
        planned_reading_task_repository: PlannedReadingTaskQueryRepository,
        measurement_definition_repository: MeasurementDefinitionQueryRepository,
        meter_master_repository: MeterMasterQueryRepository,
        *,
        planned_reading_lifecycle_service: PlannedReadingLifecycleService | None = None,
        generation_look_ahead_days: int = 0,
        business_date_provider: BusinessDateProvider | None = None,
    ) -> None:
        self._planned_reading_generator = planned_reading_generator
        self._planned_reading_task_repository = planned_reading_task_repository
        self._measurement_definition_repository = measurement_definition_repository
        self._meter_master_repository = meter_master_repository
        self._planned_reading_lifecycle_service = planned_reading_lifecycle_service
        self._generation_look_ahead_days = generation_look_ahead_days
        self._business_date_provider = business_date_provider or build_business_date_provider()

    def download_today(self) -> WorkPackageResponse:
        """当日の work package を返す前に、必要な backfill 生成を済ませる。"""

        current_business_date = self._business_date_provider()
        return self._download_for_today(current_business_date=current_business_date)

    def download_for_business_date(self, target_business_date: date) -> WorkPackageResponse:
        """指定された business_date の work package を返す。"""

        current_business_date = self._business_date_provider()
        return self._download_for_business_date(
            target_business_date=target_business_date, current_business_date=current_business_date
        )

    def _download_for_today(self, *, current_business_date: date) -> WorkPackageResponse:
        """today path の共通処理。"""

        return self._download_for_business_date(
            target_business_date=current_business_date,
            current_business_date=current_business_date,
            generation_target_business_date=current_business_date
            + timedelta(days=self._generation_look_ahead_days),
            query_by_release_date=True,
        )

    def _download_for_business_date(
        self,
        *,
        target_business_date: date,
        current_business_date: date,
        generation_target_business_date: date | None = None,
        query_by_release_date: bool = False,
    ) -> WorkPackageResponse:
        """today と explicit business_date download の共通処理。"""

        if target_business_date > current_business_date:
            raise FutureBusinessDateError(target_business_date, current_business_date)

        if generation_target_business_date is None:
            generation_target_business_date = target_business_date
        self._planned_reading_generator.ensure_generated_through(generation_target_business_date)
        if self._planned_reading_lifecycle_service is not None:
            self._planned_reading_lifecycle_service.reconcile_before_download(target_business_date)
        if query_by_release_date:
            tasks = self._planned_reading_task_repository.list_open_planned_reading_tasks_for_release_date(
                target_business_date
            )
        else:
            tasks = self._planned_reading_task_repository.list_open_planned_reading_tasks_for_business_date(
                target_business_date
            )
        grouped_tasks = _group_tasks(tasks)

        task_count = (
            len(grouped_tasks.daily_tasks)
            + len(grouped_tasks.weekly_tasks)
            + len(grouped_tasks.monthly_tasks)
            + len(grouped_tasks.every_two_months_tasks)
        )
        meters = self._meter_master_repository.list_meter_master_data()
        measurement_definitions = (
            self._measurement_definition_repository.list_measurement_definition_data()
        )
        master_count = len(meters) + len(measurement_definitions)
        if task_count > TASK_COUNT_WARN_THRESHOLD:
            logger.warning(
                "WorkPackage has high task count: %d (threshold: %d)",
                task_count,
                TASK_COUNT_WARN_THRESHOLD,
            )
        if master_count > MASTER_COUNT_WARN_THRESHOLD:
            logger.warning(
                "WorkPackage has high master count: %d (threshold: %d)",
                master_count,
                MASTER_COUNT_WARN_THRESHOLD,
            )
        logger.info(
            "Delivering work package for %s: %d task(s)",
            target_business_date.isoformat(),
            task_count,
        )

        return WorkPackageResponse(
            meters=meters,
            measurement_definitions=measurement_definitions,
            work_package=grouped_tasks,
        )


def _resolve_auto_close_through_business_date(
    *,
    target_business_date: date,
    current_datetime: datetime,
    cutoff_time: time,
) -> date:
    """requested business_date を開いたままにしつつ、auto-close の上限日を返す。"""

    current_business_date = current_datetime.date()
    cutoff_boundary = datetime.combine(
        current_business_date,
        cutoff_time,
        tzinfo=current_datetime.tzinfo,
    )
    closable_through_business_date = current_business_date - timedelta(days=1)
    if current_datetime < cutoff_boundary:
        closable_through_business_date = current_business_date - timedelta(days=2)
    return min(closable_through_business_date, target_business_date - timedelta(days=1))


def _resolve_schedule_items(
    reading_cycle: ReadingCycle,
    business_date: date,
    schedule: ScheduleSettings,
) -> tuple[tuple[ReadingSession, int], ...]:
    """reading_cycle と business_date から生成すべき schedule item 群を返す。"""

    if isinstance(schedule, DailySchedule):
        return tuple((item.reading_session, item.day_offset) for item in schedule.reading_items)

    if isinstance(schedule, WeeklySchedule):
        if business_date.weekday() != schedule.weekday:
            return ()
        return ((schedule.reading_session, schedule.day_offset),)

    if isinstance(schedule, MonthlySchedule):
        if not _matches_monthly_selector(business_date, schedule):
            return ()
        return ((schedule.reading_session, schedule.day_offset),)

    if isinstance(schedule, EveryTwoMonthsSchedule):
        if not _matches_month_parity(business_date, schedule.month_parity):
            return ()
        # every_two_months は月次 selector と同じ日付ルールを使う
        monthly_proxy = MonthlySchedule(
            selector_type=schedule.selector_type,
            day_of_month=schedule.day_of_month,
            nth=schedule.nth,
            nth_weekday=schedule.nth_weekday,
            edge=schedule.edge,
            reading_session=schedule.reading_session,
        )
        if not _matches_monthly_selector(business_date, monthly_proxy):
            return ()
        return ((schedule.reading_session, schedule.day_offset),)

    return ()  # pragma: no cover


def _matches_monthly_selector(business_date: date, schedule: MonthlySchedule) -> bool:
    """business_date が monthly selector に合致するかを判定する。"""

    if schedule.selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
        assert schedule.day_of_month is not None
        _, last_day = calendar.monthrange(business_date.year, business_date.month)
        target_day = min(schedule.day_of_month, last_day)
        return business_date.day == target_day

    if schedule.selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
        assert schedule.nth is not None and schedule.nth_weekday is not None
        if business_date.weekday() != schedule.nth_weekday:
            return False
        # n 番目の該当曜日か判定
        week_number = (business_date.day - 1) // 7 + 1
        return week_number == schedule.nth

    if schedule.selector_type is MonthlyScheduleSelectorType.MONTH_EDGE:
        assert schedule.edge is not None
        if schedule.edge == "first":
            return business_date.day == 1
        if schedule.edge == "last":
            _, last_day = calendar.monthrange(business_date.year, business_date.month)
            return business_date.day == last_day

    return False  # pragma: no cover


def _matches_month_parity(business_date: date, month_parity: MonthParity) -> bool:
    """business_date の月が指定の偶奇に合致するかを判定する。"""

    if month_parity is MonthParity.ODD:
        return business_date.month % 2 == 1
    return business_date.month % 2 == 0


def _build_planned_reading_id(
    measurement_definition_id: str,
    business_date: date,
    reading_session: ReadingSession,
) -> str:
    """planned reading の stable identifier を決定的に組み立てる。"""

    return (
        f"plan-{measurement_definition_id}-{business_date.isoformat()}-"
        f"{reading_session.value.replace(':', '')}"
    )


def _group_tasks(tasks: list[PlannedReadingTask]) -> WorkPackageTaskGroups:
    """reading_cycle に基づいて UI 用 group へ分類する。"""

    sorted_tasks = sorted(
        tasks,
        key=lambda task: (
            task.business_date,
            task.reading_session.value,
            task.location_id,
            task.measurement_definition_id,
        ),
    )
    return WorkPackageTaskGroups(
        daily_tasks=[task for task in sorted_tasks if task.reading_cycle in _DAILY_READING_CYCLES],
        weekly_tasks=[task for task in sorted_tasks if task.reading_cycle is ReadingCycle.WEEKLY],
        monthly_tasks=[task for task in sorted_tasks if task.reading_cycle is ReadingCycle.MONTHLY],
        every_two_months_tasks=[
            task for task in sorted_tasks if task.reading_cycle is ReadingCycle.EVERY_TWO_MONTHS
        ],
    )


_DAILY_READING_CYCLES = frozenset(
    {
        ReadingCycle.DAILY_1X,
        ReadingCycle.DAILY_2X,
        ReadingCycle.DAILY_3X,
    }
)


__all__ = [
    "BusinessDateProvider",
    "CutoffTimeProvider",
    "CurrentDateTimeProvider",
    "FutureBusinessDateError",
    "PlannedReadingGeneratorService",
    "PlannedReadingLifecycleService",
    "WorkPackageDownloadServiceError",
    "WorkPackageDownloadService",
]
