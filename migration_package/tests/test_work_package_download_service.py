# pyright: reportMissingImports=false

"""work package download service の backfill と grouping を検証するテスト群。"""

from __future__ import annotations

import logging
import pytest
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from api.models import CalculationMethod, Category, MeasurementDefinitionData, MeterMasterData, PlannedReadingTask, Purpose, ReadingCycle, ReadingSession, ValueSchema  # noqa: E402
from application.meter_reading_sync_repositories import PlannedReadingCloseReason, PlannedReadingCreateRequest, PlannedReadingGenerationDefinitionRecord  # noqa: E402
from application.work_package_download import (  # noqa: E402
	FutureBusinessDateError,
	MASTER_COUNT_WARN_THRESHOLD,
	PlannedReadingGeneratorService,
	PlannedReadingLifecycleService,
	TASK_COUNT_WARN_THRESHOLD,
	WorkPackageDownloadService,
)
from application.schedule_settings import (  # noqa: E402
	DailySchedule,
	DEFAULT_SCHEDULES,
	EveryTwoMonthsSchedule,
	MonthParity,
	MonthlySchedule,
	MonthlyScheduleSelectorType,
	ScheduleItem,
	WeeklySchedule,
)
from core.database import MeasurementDefinitionORM, build_scheduled_at  # noqa: E402


@dataclass
class FakeWorkPackageRepository:
	"""download path の各 protocol をまとめて再現する fake repository。"""

	latest_business_date: date | None = None
	generation_definitions: list[PlannedReadingGenerationDefinitionRecord] = field(default_factory=list)
	meter_master_data: list[MeterMasterData] = field(default_factory=list)
	measurement_definition_data: list[MeasurementDefinitionData] = field(default_factory=list)
	skipped_measurement_definitions: list[MeasurementDefinitionORM] = field(default_factory=list)
	created_requests: list[PlannedReadingCreateRequest] = field(default_factory=list)
	batch_created_requests: list[list[PlannedReadingCreateRequest]] = field(default_factory=list)
	existing_planned_reading_ids: set[str] = field(default_factory=set)
	tasks_by_date: dict[date, list[PlannedReadingTask]] = field(default_factory=dict)
	auto_close_calls: list[tuple[date, datetime, PlannedReadingCloseReason]] = field(default_factory=list)

	def get_latest_planned_business_date(self) -> date | None:
		return self.latest_business_date

	def list_planned_reading_generation_definitions(
		self,
	) -> list[PlannedReadingGenerationDefinitionRecord]:
		return [
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id=definition.measurement_definition_id,
				location_id=definition.location_id,
				meter_id=definition.meter_id,
				reading_cycle=definition.reading_cycle,
				schedule=definition.schedule or DEFAULT_SCHEDULES[definition.reading_cycle],
			)
			for definition in self.generation_definitions
		]

	def list_measurement_definitions_without_active_assignment(self) -> list[MeasurementDefinitionORM]:
		return list(self.skipped_measurement_definitions)

	def create_planned_reading_if_missing(self, request: PlannedReadingCreateRequest) -> bool:
		return self.create_planned_readings_if_missing([request]) > 0

	def create_planned_readings_if_missing(self, requests: list[PlannedReadingCreateRequest]) -> int:
		self.batch_created_requests.append(list(requests))
		created_count = 0
		for request in requests:
			if request.planned_reading_id in self.existing_planned_reading_ids:
				continue

			self.existing_planned_reading_ids.add(request.planned_reading_id)
			self.created_requests.append(request)
			created_count += 1
			if self.latest_business_date is None or request.business_date > self.latest_business_date:
				self.latest_business_date = request.business_date

			reading_cycle = next(
				definition.reading_cycle
				for definition in self.generation_definitions
				if definition.measurement_definition_id == request.measurement_definition_id
			)
			self.tasks_by_date.setdefault(request.business_date, []).append(
				PlannedReadingTask(
					planned_reading_id=request.planned_reading_id,
					measurement_definition_id=request.measurement_definition_id,
					meter_id=request.meter_id,
					location_id=request.location_id,
					business_date=request.business_date,
					release_date=request.release_date,
					reading_session=request.reading_session,
					scheduled_at=request.scheduled_at,
					reading_cycle=reading_cycle,
					required=True,
				)
			)
		return created_count

	def list_open_planned_reading_tasks_for_release_date(self, release_date: date) -> list[PlannedReadingTask]:
		return [
			task
			for tasks in self.tasks_by_date.values()
			for task in tasks
			if task.release_date <= release_date
		]

	def list_open_planned_reading_tasks_for_business_date(self, business_date: date) -> list[PlannedReadingTask]:
		return list(self.tasks_by_date.get(business_date, []))

	def list_open_planned_reading_tasks(self, business_date: date) -> list[PlannedReadingTask]:
		return self.list_open_planned_reading_tasks_for_business_date(business_date)

	def auto_close_open_planned_readings(
		self,
		through_business_date: date,
		*,
		closed_at: datetime,
		close_reason: PlannedReadingCloseReason,
	) -> int:
		self.auto_close_calls.append((through_business_date, closed_at, close_reason))
		closed_count = 0
		for business_date_key in list(self.tasks_by_date):
			if business_date_key <= through_business_date:
				closed_count += len(self.tasks_by_date[business_date_key])
				del self.tasks_by_date[business_date_key]
		return closed_count

	def list_meter_master_data(self) -> list[MeterMasterData]:
		return list(self.meter_master_data)

	def list_measurement_definition_data(self) -> list[MeasurementDefinitionData]:
		return list(self.measurement_definition_data)


class FakeReleaseDateCalculator:
	"""session ごとの release_date 計算を再現するテスト用 calculator。"""

	def compute_release_date(self, business_date: date, reading_session: ReadingSession) -> date:
		if reading_session is ReadingSession.SESSION_0000:
			return business_date - timedelta(days=1)
		return business_date


@dataclass
class SingleSessionRepository:
	"""release_date today / business_date explicit の差分確認用 repository。"""

	latest_business_date: date | None = date(2026, 2, 1)
	created_requests: list[PlannedReadingCreateRequest] = field(default_factory=list)
	batch_created_requests: list[list[PlannedReadingCreateRequest]] = field(default_factory=list)
	tasks_by_date: dict[date, list[PlannedReadingTask]] = field(default_factory=dict)
	generator_definitions: list[PlannedReadingGenerationDefinitionRecord] = field(default_factory=lambda: [
		PlannedReadingGenerationDefinitionRecord(
			measurement_definition_id="md-midnight-001",
			location_id="loc-001",
			meter_id="meter-001",
			reading_cycle=ReadingCycle.DAILY_1X,
			schedule=DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0000),)),
		),
	])

	def get_latest_planned_business_date(self) -> date | None:
		return self.latest_business_date

	def list_planned_reading_generation_definitions(self) -> list[PlannedReadingGenerationDefinitionRecord]:
		return [
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id=definition.measurement_definition_id,
				location_id=definition.location_id,
				meter_id=definition.meter_id,
				reading_cycle=definition.reading_cycle,
				schedule=definition.schedule or DEFAULT_SCHEDULES[definition.reading_cycle],
			)
			for definition in self.generator_definitions
		]

	def list_measurement_definitions_without_active_assignment(self) -> list[MeasurementDefinitionORM]:
		return []

	def create_planned_reading_if_missing(self, request: PlannedReadingCreateRequest) -> bool:
		return self.create_planned_readings_if_missing([request]) > 0

	def create_planned_readings_if_missing(self, requests: list[PlannedReadingCreateRequest]) -> int:
		self.batch_created_requests.append(list(requests))
		created_count = 0
		for request in requests:
			if any(existing.planned_reading_id == request.planned_reading_id for existing in self.created_requests):
				continue
			self.created_requests.append(request)
			created_count += 1
			if self.latest_business_date is None or request.business_date > self.latest_business_date:
				self.latest_business_date = request.business_date
			self.tasks_by_date.setdefault(request.business_date, []).append(
				PlannedReadingTask(
					planned_reading_id=request.planned_reading_id,
					measurement_definition_id=request.measurement_definition_id,
					meter_id=request.meter_id,
					location_id=request.location_id,
					business_date=request.business_date,
					release_date=request.release_date,
					reading_session=request.reading_session,
					scheduled_at=request.scheduled_at,
					reading_cycle=ReadingCycle.DAILY_1X,
					required=True,
				)
			)
		return created_count

	def list_open_planned_reading_tasks_for_release_date(self, release_date: date) -> list[PlannedReadingTask]:
		return [
			task
			for tasks in self.tasks_by_date.values()
			for task in tasks
			if task.release_date <= release_date
		]

	def list_open_planned_reading_tasks_for_business_date(self, business_date: date) -> list[PlannedReadingTask]:
		return list(self.tasks_by_date.get(business_date, []))

	def list_open_planned_reading_tasks(self, business_date: date) -> list[PlannedReadingTask]:
		return self.list_open_planned_reading_tasks_for_business_date(business_date)

	def list_meter_master_data(self) -> list[MeterMasterData]:
		return []

	def list_measurement_definition_data(self) -> list[MeasurementDefinitionData]:
		return []

	def auto_close_open_planned_readings(
		self,
		through_business_date: date,
		*,
		closed_at: datetime,
		close_reason: PlannedReadingCloseReason,
	) -> int:
		closed_count = 0
		for business_date_key in list(self.tasks_by_date):
			if business_date_key <= through_business_date:
				closed_count += len(self.tasks_by_date[business_date_key])
				del self.tasks_by_date[business_date_key]
		return closed_count


def build_repository() -> FakeWorkPackageRepository:
	"""daily / weekly / monthly を同時に検証できる fake repository を返す。"""

	return FakeWorkPackageRepository(
		latest_business_date=date(2026, 1, 30),
		generation_definitions=[
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-daily-1x-001",
				location_id="loc-100",
				meter_id="meter-daily-001",
				reading_cycle=ReadingCycle.DAILY_1X,
			),
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-daily-2x-001",
				location_id="loc-100",
				meter_id="meter-daily-001",
				reading_cycle=ReadingCycle.DAILY_2X,
			),
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-weekly-001",
				location_id="loc-200",
				meter_id="meter-utility-001",
				reading_cycle=ReadingCycle.WEEKLY,
			),
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-monthly-001",
				location_id="loc-200",
				meter_id="meter-utility-001",
				reading_cycle=ReadingCycle.MONTHLY,
			),
		],
		meter_master_data=[
			MeterMasterData(
				meter_id="meter-daily-001",
				location_id="loc-100",
				location_name="Hotel Main",
				meter_name="Daily Meter",
				category=Category.ELECTRICITY,
				meter_serial="SER-100",
			),
			MeterMasterData(
				meter_id="meter-utility-001",
				location_id="loc-200",
				location_name="Utility Yard",
				meter_name="Utility Meter",
				category=Category.WATER,
				meter_serial="SER-200",
			),
		],
		measurement_definition_data=[
			MeasurementDefinitionData(
				measurement_definition_id="md-daily-1x-001",
				location_id="loc-100",
				meter_id="meter-daily-001",
				display_name="Daily kWh",
				category=Category.ELECTRICITY,
				unit="kWh",
				value_schema=ValueSchema.DECIMAL_STRING,
				last_reading=None,
				multiplier=Decimal("1.0"),
				warning_threshold=Decimal("100.0"),
				decimal_places=1,
				rollover_value=Decimal("100000"),
				calculation_method=CalculationMethod.DELTA,
				reading_cycle=ReadingCycle.DAILY_1X,
				purpose=Purpose.OPERATIONAL,
			),
			MeasurementDefinitionData(
				measurement_definition_id="md-daily-2x-001",
				location_id="loc-100",
				meter_id="meter-daily-001",
				display_name="Daily Flow",
				category=Category.ELECTRICITY,
				unit="kWh",
				value_schema=ValueSchema.DECIMAL_STRING,
				last_reading=None,
				multiplier=Decimal("1.0"),
				warning_threshold=None,
				decimal_places=0,
				rollover_value=Decimal("100000"),
				calculation_method=CalculationMethod.DELTA,
				reading_cycle=ReadingCycle.DAILY_2X,
				purpose=Purpose.OPERATIONAL,
			),
			MeasurementDefinitionData(
				measurement_definition_id="md-weekly-001",
				location_id="loc-200",
				meter_id="meter-utility-001",
				display_name="Weekly Water",
				category=Category.WATER,
				unit="m3",
				value_schema=ValueSchema.DECIMAL_STRING,
				last_reading=None,
				multiplier=Decimal("1.0"),
				warning_threshold=None,
				rollover_value=None,
				calculation_method=CalculationMethod.DELTA,
				reading_cycle=ReadingCycle.WEEKLY,
				purpose=Purpose.OPERATIONAL,
			),
			MeasurementDefinitionData(
				measurement_definition_id="md-monthly-001",
				location_id="loc-200",
				meter_id="meter-utility-001",
				display_name="Monthly Water",
				category=Category.WATER,
				unit="m3",
				value_schema=ValueSchema.DECIMAL_STRING,
				last_reading=None,
				multiplier=Decimal("1.0"),
				warning_threshold=None,
				rollover_value=None,
				calculation_method=CalculationMethod.DELTA,
				reading_cycle=ReadingCycle.MONTHLY,
				purpose=Purpose.OPERATIONAL,
			),
		],
	)


def build_single_definition_repository(
	*,
	measurement_definition_id: str,
	location_id: str,
	meter_id: str,
	reading_cycle: ReadingCycle,
	schedule,
	latest_business_date: date | None,
) -> FakeWorkPackageRepository:
	"""単一 measurement_definition の schedule 挙動を検証する repository を返す。"""

	return FakeWorkPackageRepository(
		latest_business_date=latest_business_date,
		generation_definitions=[
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id=measurement_definition_id,
				location_id=location_id,
				meter_id=meter_id,
				reading_cycle=reading_cycle,
				schedule=schedule,
			),
		],
	)


def test_generator_backfills_missing_dates_and_remains_idempotent() -> None:
	"""target 日までの欠損だけを生成し、再実行で重複しないことを確認する。"""

	repository = build_repository()
	generator = PlannedReadingGeneratorService(repository)
	target_business_date = date(2026, 2, 1)

	generator.ensure_generated_through(target_business_date)
	first_created = list(repository.created_requests)
	generator.ensure_generated_through(target_business_date)

	assert len(first_created) == 8
	assert len(repository.created_requests) == 8
	assert {request.business_date for request in first_created} == {
		date(2026, 1, 31),
		date(2026, 2, 1),
	}
	assert len(repository.batch_created_requests) == 1
	assert len(repository.batch_created_requests[0]) == 8


def test_generator_caps_backfill_to_fourteen_days_and_reports_pending(caplog: pytest.LogCaptureFixture) -> None:
	"""backfill が 14 日を超えると cap され、pending が返ることを確認する。"""

	repository = build_single_definition_repository(
		measurement_definition_id="md-daily-cap",
		location_id="loc-cap",
		meter_id="meter-cap",
		reading_cycle=ReadingCycle.DAILY_1X,
		schedule=None,
		latest_business_date=date(2026, 1, 1),
	)
	generator = PlannedReadingGeneratorService(repository)

	with caplog.at_level(logging.INFO, logger="application.work_package_download"):
		backfill_pending = generator.ensure_generated_through(date(2026, 2, 1))

	assert backfill_pending is True
	assert repository.latest_business_date == date(2026, 1, 15)
	assert {request.business_date for request in repository.created_requests} == {
		date(2026, 1, day) for day in range(2, 16)
	}
	assert len(repository.batch_created_requests) == 1
	assert "backfill_pending=True" in caplog.text


def test_generator_warns_for_active_measurement_definitions_without_active_assignment(caplog: pytest.LogCaptureFixture) -> None:
	"""active だが meter assignment を持たない definition が WARN されることを確認する。"""

	repository = build_repository()
	repository.skipped_measurement_definitions = [
		MeasurementDefinitionORM(
			measurement_definition_id="md-skipped-001",
			location_id="loc-skip",
			meter_id=None,
			display_name="Skipped Definition",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier=Decimal("1.0"),
			decimal_places=None,
			warning_threshold=None,
			rollover_value=None,
			calculation_method="delta",
			reading_cycle="daily_1x",
			purpose="operational",
			is_active=True,
		),
	]
	generator = PlannedReadingGeneratorService(repository)

	with caplog.at_level(logging.WARNING, logger="application.work_package_download"):
		generator.ensure_generated_through(date(2026, 2, 1))

	assert "Skipped generation for measurement_definition_id=md-skipped-001: No active meter assignment" in caplog.text


def test_generator_progresses_in_fourteen_day_chunks_across_repeated_calls() -> None:
	"""要求が大きくても 14 日単位で段階的に進むことを確認する。"""

	repository = build_single_definition_repository(
		measurement_definition_id="md-daily-chunk",
		location_id="loc-chunk",
		meter_id="meter-chunk",
		reading_cycle=ReadingCycle.DAILY_1X,
		schedule=None,
		latest_business_date=date(2026, 1, 1),
	)
	generator = PlannedReadingGeneratorService(repository)

	first_pending = generator.ensure_generated_through(date(2026, 2, 1))
	second_pending = generator.ensure_generated_through(date(2026, 2, 1))

	assert first_pending is True
	assert second_pending is True
	assert repository.latest_business_date == date(2026, 1, 29)
	assert {request.business_date for request in repository.created_requests} == {
		date(2026, 1, day) for day in range(2, 30)
	}
	assert len(repository.batch_created_requests) == 2
	assert len(repository.batch_created_requests[0]) == 14
	assert len(repository.batch_created_requests[1]) == 14


def test_generator_applies_release_date_calculator_when_provided() -> None:
	"""release_date calculator があると session ごとの配布日が反映される。"""

	repository = build_daily_3x_billing_repository(
		daily_3x_schedule=DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0000), ScheduleItem(ReadingSession.SESSION_0800))),
	)
	generator = PlannedReadingGeneratorService(
		repository,
		release_date_calculator=FakeReleaseDateCalculator(),
	)

	generator.ensure_generated_through(date(2026, 2, 1))

	daily_3x_requests = [
		r for r in repository.created_requests
		if r.measurement_definition_id == "md-electric-incoming-kwh"
		and r.business_date == date(2026, 2, 1)
	]
	assert {r.reading_session for r in daily_3x_requests} == {
		ReadingSession.SESSION_0000,
		ReadingSession.SESSION_0800,
	}
	assert [r.release_date for r in sorted(daily_3x_requests, key=lambda request: request.reading_session.value)] == [
		date(2026, 1, 31),
		date(2026, 2, 1),
	]


def test_generator_applies_day_offset_before_release_date_calculation() -> None:
	"""day_offset があると scheduled_at と release_date の両方に反映される。"""

	repository = SingleSessionRepository()
	repository.latest_business_date = date(2026, 1, 31)
	repository.generator_definitions[0] = PlannedReadingGenerationDefinitionRecord(
		measurement_definition_id="md-midnight-001",
		location_id="loc-001",
		meter_id="meter-001",
		reading_cycle=ReadingCycle.DAILY_1X,
		schedule=DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0000, day_offset=1),)),
	)
	generator = PlannedReadingGeneratorService(
		repository,
		release_date_calculator=FakeReleaseDateCalculator(),
	)

	generator.ensure_generated_through(date(2026, 2, 1))

	request = next(
		r
		for r in repository.created_requests
		if r.measurement_definition_id == "md-midnight-001" and r.business_date == date(2026, 2, 1)
	)
	assert request.release_date == date(2026, 2, 1)
	assert request.scheduled_at == build_scheduled_at(
		date(2026, 2, 1),
		ReadingSession.SESSION_0000.value,
		day_offset=1,
	)


def test_download_today_uses_release_date_query_and_look_ahead_generation() -> None:
	"""today は release_date ベースで next day の 00:00 task も返し、explicit business_date は返さない。"""

	repository = SingleSessionRepository()
	repository.latest_business_date = date(2026, 1, 31)
	generator = PlannedReadingGeneratorService(
		repository,
		release_date_calculator=FakeReleaseDateCalculator(),
	)
	service = WorkPackageDownloadService(
		planned_reading_generator=generator,
		planned_reading_task_repository=repository,
		measurement_definition_repository=repository,
		meter_master_repository=repository,
		planned_reading_lifecycle_service=PlannedReadingLifecycleService(
			repository,
			current_datetime_provider=lambda: datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
			cutoff_time_provider=lambda: time(hour=7, minute=0),
		),
		generation_look_ahead_days=1,
		business_date_provider=lambda: date(2026, 2, 1),
	)

	today_response = service.download_today()
	explicit_response = service.download_for_business_date(date(2026, 2, 1))

	today_business_dates = [task.business_date for task in today_response.work_package.daily_tasks]
	explicit_business_dates = [task.business_date for task in explicit_response.work_package.daily_tasks]

	assert today_business_dates == [date(2026, 2, 1), date(2026, 2, 2)]
	assert [task.release_date for task in today_response.work_package.daily_tasks] == [
		date(2026, 1, 31),
		date(2026, 2, 1),
	]
	assert explicit_business_dates == [date(2026, 2, 1)]
	assert [task.release_date for task in explicit_response.work_package.daily_tasks] == [date(2026, 1, 31)]
	assert today_response.backfill_pending is False
	assert explicit_response.backfill_pending is False


def test_download_service_groups_daily_weekly_and_monthly_tasks() -> None:
	"""download service が生成後 task を group 化して返すことを確認する。"""

	repository = build_repository()
	service = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository),
		planned_reading_task_repository=repository,
		measurement_definition_repository=repository,
		meter_master_repository=repository,
		business_date_provider=lambda: date(2026, 2, 1),
	)

	response = service.download_today()

	assert len(response.meters) == 2
	assert len(response.measurement_definitions) == 4
	assert len(response.work_package.daily_tasks) == 6
	assert len(response.work_package.weekly_tasks) == 1
	assert len(response.work_package.monthly_tasks) == 1
	assert response.work_package.every_two_months_tasks == []
	assert response.work_package.ad_hoc_tasks == []
	measurement_definitions = {
		item.measurement_definition_id: item for item in response.measurement_definitions
	}
	assert measurement_definitions["md-daily-1x-001"].decimal_places == 1
	assert measurement_definitions["md-daily-2x-001"].decimal_places == 0
	assert measurement_definitions["md-weekly-001"].decimal_places is None
	assert [task.reading_session.value for task in response.work_package.daily_tasks] == [
		"08:00",
		"08:00",
		"22:00",
		"08:00",
		"08:00",
		"22:00",
	]


def test_lifecycle_service_auto_closes_through_day_before_yesterday_before_cutoff() -> None:
	"""cutoff 前は一昨日までしか auto-close しないことを確認する。"""

	repository = build_repository()
	lifecycle_service = PlannedReadingLifecycleService(
		repository,
		current_datetime_provider=lambda: datetime(2026, 2, 1, 6, 30, tzinfo=timezone.utc),
		cutoff_time_provider=lambda: time(hour=7, minute=0),
	)

	lifecycle_service.reconcile_before_download(date(2026, 2, 1))

	assert repository.auto_close_calls[0][0] == date(2026, 1, 30)
	assert repository.auto_close_calls[0][2] is PlannedReadingCloseReason.CUTOFF_ELAPSED


def test_download_service_runs_lifecycle_reconciliation_before_listing_today_tasks() -> None:
	"""download 前に lifecycle reconciliation が走り、前日分が close されることを確認する。"""

	repository = build_repository()
	service = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository),
		planned_reading_task_repository=repository,
		measurement_definition_repository=repository,
		meter_master_repository=repository,
		planned_reading_lifecycle_service=PlannedReadingLifecycleService(
			repository,
			current_datetime_provider=lambda: datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
			cutoff_time_provider=lambda: time(hour=7, minute=0),
		),
		business_date_provider=lambda: date(2026, 2, 1),
	)

	response = service.download_today()

	assert repository.auto_close_calls[0][0] == date(2026, 1, 31)
	assert len(response.work_package.daily_tasks) == 3


def test_download_for_business_date_returns_same_payload_as_today_for_current_date() -> None:
	"""today path は release_date ベース、explicit path は business_date ベースで分かれることを確認する。"""

	repository_for_today = build_repository()
	service_for_today = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository_for_today),
		planned_reading_task_repository=repository_for_today,
		measurement_definition_repository=repository_for_today,
		meter_master_repository=repository_for_today,
		planned_reading_lifecycle_service=PlannedReadingLifecycleService(
			repository_for_today,
			current_datetime_provider=lambda: datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
			cutoff_time_provider=lambda: time(hour=7, minute=0),
		),
		business_date_provider=lambda: date(2026, 2, 1),
	)
	repository_for_explicit = build_repository()
	service_for_explicit = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository_for_explicit),
		planned_reading_task_repository=repository_for_explicit,
		measurement_definition_repository=repository_for_explicit,
		meter_master_repository=repository_for_explicit,
		planned_reading_lifecycle_service=PlannedReadingLifecycleService(
			repository_for_explicit,
			current_datetime_provider=lambda: datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
			cutoff_time_provider=lambda: time(hour=7, minute=0),
		),
		business_date_provider=lambda: date(2026, 2, 1),
	)

	today_response = service_for_today.download_today()
	explicit_response = service_for_explicit.download_for_business_date(date(2026, 2, 1))

	assert today_response.model_dump() == explicit_response.model_dump()


def test_download_for_past_business_date_returns_requested_open_tasks() -> None:
	"""current day より過去の requested business_date でも open task を返せることを確認する。"""

	repository = build_repository()
	service = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository),
		planned_reading_task_repository=repository,
		measurement_definition_repository=repository,
		meter_master_repository=repository,
		planned_reading_lifecycle_service=PlannedReadingLifecycleService(
			repository,
			current_datetime_provider=lambda: datetime(2026, 2, 2, 6, 30, tzinfo=timezone.utc),
			cutoff_time_provider=lambda: time(hour=7, minute=0),
		),
		business_date_provider=lambda: date(2026, 2, 2),
	)

	response = service.download_for_business_date(date(2026, 2, 1))

	assert len(response.work_package.daily_tasks) == 3
	assert len(response.work_package.weekly_tasks) == 1
	assert len(response.work_package.monthly_tasks) == 1
	assert all(task.business_date == date(2026, 2, 1) for task in response.work_package.daily_tasks)
	assert repository.auto_close_calls[0][0] == date(2026, 1, 31)


def test_download_for_future_business_date_is_rejected() -> None:
	"""future business_date 指定を service が reject することを確認する。"""

	repository = build_repository()
	service = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository),
		planned_reading_task_repository=repository,
		measurement_definition_repository=repository,
		meter_master_repository=repository,
		business_date_provider=lambda: date(2026, 2, 1),
	)

	try:
		service.download_for_business_date(date(2026, 2, 2))
		raise AssertionError("Expected FutureBusinessDateError to be raised.")
	except FutureBusinessDateError as exc:
		assert exc.target_business_date == date(2026, 2, 2)
		assert exc.current_business_date == date(2026, 2, 1)


def test_download_today_marks_backfill_pending_when_generation_is_capped(caplog: pytest.LogCaptureFixture) -> None:
	"""download_today が backfill cap を受けると response で pending を返す。"""

	repository = SingleSessionRepository(latest_business_date=date(2026, 1, 1))
	service = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository),
		planned_reading_task_repository=repository,
		measurement_definition_repository=repository,
		meter_master_repository=repository,
		generation_look_ahead_days=30,
		business_date_provider=lambda: date(2026, 2, 1),
	)

	with caplog.at_level(logging.INFO, logger="application.work_package_download"):
		response = service.download_today()

	assert response.backfill_pending is True
	assert repository.latest_business_date == date(2026, 1, 15)
	assert len(repository.batch_created_requests) == 1
	assert "backfill_pending=True" in caplog.text


def test_download_service_warns_when_task_count_exceeds_threshold(caplog: pytest.LogCaptureFixture) -> None:
	"""task 件数が閾値を超えると WARN を出しつつ応答は欠落させない。"""

	repository = FakeWorkPackageRepository(
		latest_business_date=date(2026, 2, 1),
		tasks_by_date={
			date(2026, 2, 1): [
				PlannedReadingTask(
					planned_reading_id=f"planned-{index}",
					measurement_definition_id=f"md-{index}",
					meter_id=f"meter-{index}",
					location_id=f"loc-{index}",
					business_date=date(2026, 2, 1),
					release_date=date(2026, 2, 1),
					reading_session=ReadingSession.SESSION_0800,
					scheduled_at=build_scheduled_at(date(2026, 2, 1), ReadingSession.SESSION_0800.value),
					reading_cycle=ReadingCycle.DAILY_1X,
					required=True,
				)
				for index in range(TASK_COUNT_WARN_THRESHOLD + 1)
			],
		},
	)
	service = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository),
		planned_reading_task_repository=repository,
		measurement_definition_repository=repository,
		meter_master_repository=repository,
		business_date_provider=lambda: date(2026, 2, 1),
	)

	with caplog.at_level(logging.WARNING, logger="application.work_package_download"):
		response = service.download_today()

	assert len(response.work_package.daily_tasks) == TASK_COUNT_WARN_THRESHOLD + 1
	assert "WorkPackage has high task count" in caplog.text
	assert str(TASK_COUNT_WARN_THRESHOLD + 1) in caplog.text


def test_download_service_warns_when_master_count_exceeds_threshold(caplog: pytest.LogCaptureFixture) -> None:
	"""master 件数が閾値を超えると WARN を出しつつ応答の master は完全に返す。"""

	repository = FakeWorkPackageRepository(
		latest_business_date=date(2026, 2, 1),
		meter_master_data=[
			MeterMasterData(
				meter_id=f"meter-{index}",
				location_id=f"loc-{index}",
				location_name=f"Location {index}",
				meter_name=f"Meter {index}",
				category=Category.ELECTRICITY,
				meter_serial=f"SN-{index}",
			)
			for index in range(1000)
		],
		measurement_definition_data=[
			MeasurementDefinitionData(
				measurement_definition_id=f"md-{index}",
				location_id=f"loc-{index}",
				meter_id=f"meter-{index % 1000}",
				display_name=f"Definition {index}",
				category=Category.ELECTRICITY,
				unit="kWh",
				value_schema=ValueSchema.DECIMAL_STRING,
				last_reading=None,
				multiplier="1.0",
				decimal_places=0,
				warning_threshold=None,
				rollover_value=None,
				calculation_method=CalculationMethod.DELTA,
				reading_cycle=ReadingCycle.DAILY_1X,
				purpose=Purpose.OPERATIONAL,
			)
			for index in range(MASTER_COUNT_WARN_THRESHOLD - 1000 + 1)
		],
	)
	service = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository),
		planned_reading_task_repository=repository,
		measurement_definition_repository=repository,
		meter_master_repository=repository,
		business_date_provider=lambda: date(2026, 2, 1),
	)

	with caplog.at_level(logging.WARNING, logger="application.work_package_download"):
		response = service.download_today()

	assert len(response.meters) == 1000
	assert len(response.measurement_definitions) == MASTER_COUNT_WARN_THRESHOLD - 1000 + 1
	assert "WorkPackage has high master count" in caplog.text
	assert str(MASTER_COUNT_WARN_THRESHOLD + 1) in caplog.text


def build_daily_3x_billing_repository(*, daily_3x_schedule: DailySchedule | None = None) -> FakeWorkPackageRepository:
	"""daily_3x + daily_1x + billing monthly を同時に検証できる fake repository を返す。"""

	return FakeWorkPackageRepository(
		latest_business_date=None,
		generation_definitions=[
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-electric-incoming-kwh",
				location_id="loc-electric-incoming",
				meter_id="meter-electric-incoming-001",
				reading_cycle=ReadingCycle.DAILY_3X,
				schedule=daily_3x_schedule,
			),
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-gas-hq-total",
				location_id="loc-gas-hq",
				meter_id="meter-gas-hq-001",
				reading_cycle=ReadingCycle.DAILY_1X,
			),
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-electric-incoming-kwh-billing",
				location_id="loc-electric-incoming",
				meter_id="meter-electric-incoming-001",
				reading_cycle=ReadingCycle.MONTHLY,
			),
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-gas-hq-total-billing",
				location_id="loc-gas-hq",
				meter_id="meter-gas-hq-001",
				reading_cycle=ReadingCycle.MONTHLY,
			),
		],
		meter_master_data=[],
		measurement_definition_data=[],
	)


def test_daily_3x_generates_three_sessions_per_day() -> None:
	"""daily_3x が 1 日あたり 3 session 分の planned reading を生成することを確認する。"""

	repository = build_daily_3x_billing_repository()
	generator = PlannedReadingGeneratorService(repository)

	generator.ensure_generated_through(date(2026, 2, 1))

	daily_3x_requests = [
		r for r in repository.created_requests
		if r.measurement_definition_id == "md-electric-incoming-kwh"
		and r.business_date == date(2026, 2, 1)
	]
	assert len(daily_3x_requests) == 3
	assert {r.reading_session for r in daily_3x_requests} == {
		ReadingSession.SESSION_0600,
		ReadingSession.SESSION_1400,
		ReadingSession.SESSION_2200,
	}


def test_daily_1x_generates_one_session_per_day() -> None:
	"""daily_1x が 1 日あたり 1 session の planned reading を生成することを確認する。"""

	repository = build_daily_3x_billing_repository()
	generator = PlannedReadingGeneratorService(repository)

	generator.ensure_generated_through(date(2026, 2, 1))

	daily_1x_requests = [
		r for r in repository.created_requests
		if r.measurement_definition_id == "md-gas-hq-total"
		and r.business_date == date(2026, 2, 1)
	]
	assert len(daily_1x_requests) == 1
	assert daily_1x_requests[0].reading_session == ReadingSession.SESSION_0800


def test_monthly_billing_generates_only_on_first_of_month() -> None:
	"""monthly (billing) task が月初のみ生成されることを確認する。"""

	repository = build_daily_3x_billing_repository()
	generator = PlannedReadingGeneratorService(repository)

	# 1/31 と 2/1 の 2 日間を生成
	generator.ensure_generated_through(date(2026, 2, 1))

	billing_requests_jan31 = [
		r for r in repository.created_requests
		if r.measurement_definition_id == "md-electric-incoming-kwh-billing"
		and r.business_date == date(2026, 1, 31)
	]
	billing_requests_feb1 = [
		r for r in repository.created_requests
		if r.measurement_definition_id == "md-electric-incoming-kwh-billing"
		and r.business_date == date(2026, 2, 1)
	]
	# 1/31 は月初でないので生成されない
	assert len(billing_requests_jan31) == 0
	# 2/1 は月初なので生成される
	assert len(billing_requests_feb1) == 1


def test_billing_monthly_tasks_grouped_into_monthly_tasks() -> None:
	"""billing md (monthly) のタスクが monthly_tasks グループに分類されることを確認する。"""

	repository = build_daily_3x_billing_repository()
	service = WorkPackageDownloadService(
		planned_reading_generator=PlannedReadingGeneratorService(repository),
		planned_reading_task_repository=repository,
		measurement_definition_repository=repository,
		meter_master_repository=repository,
		business_date_provider=lambda: date(2026, 2, 1),
	)

	response = service.download_today()

	# daily_3x → 3 task, daily_1x → 1 task = 4 daily
	assert len(response.work_package.daily_tasks) == 4
	# billing monthly × 2 definitions = 2 monthly
	assert len(response.work_package.monthly_tasks) == 2
	billing_md_ids = {task.measurement_definition_id for task in response.work_package.monthly_tasks}
	assert billing_md_ids == {"md-electric-incoming-kwh-billing", "md-gas-hq-total-billing"}


def test_definition_schedule_overrides_default_sessions() -> None:
	"""measurement_definition ごとの schedule が default を上書きすることを確認する。"""

	repository = build_daily_3x_billing_repository(
		daily_3x_schedule=DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0800), ScheduleItem(ReadingSession.SESSION_1400))),
	)
	generator = PlannedReadingGeneratorService(repository)

	generator.ensure_generated_through(date(2026, 2, 1))

	daily_3x_requests = [
		r for r in repository.created_requests
		if r.measurement_definition_id == "md-electric-incoming-kwh"
		and r.business_date == date(2026, 2, 1)
	]
	assert len(daily_3x_requests) == 2
	assert {r.reading_session for r in daily_3x_requests} == {
		ReadingSession.SESSION_0800,
		ReadingSession.SESSION_1400,
	}
	assert [
		r.scheduled_at for r in sorted(daily_3x_requests, key=lambda request: request.reading_session.value)
	] == [
		build_scheduled_at(date(2026, 2, 1), ReadingSession.SESSION_0800.value),
		build_scheduled_at(date(2026, 2, 1), ReadingSession.SESSION_1400.value),
	]


def test_weekly_definition_uses_its_own_weekday() -> None:
	"""weekly は measurement_definition ごとに指定した weekday のみ生成することを確認する。"""

	matching_date = date(2026, 2, 4)
	repository = build_single_definition_repository(
		measurement_definition_id="md-weekly-special",
		location_id="loc-weekly",
		meter_id="meter-weekly",
		reading_cycle=ReadingCycle.WEEKLY,
		schedule=WeeklySchedule(weekday=matching_date.weekday(), reading_session=ReadingSession.SESSION_2200),
		latest_business_date=date(2026, 2, 1),
	)
	generator = PlannedReadingGeneratorService(repository)

	generator.ensure_generated_through(date(2026, 2, 5))

	weekly_requests = [
		request
		for request in repository.created_requests
		if request.measurement_definition_id == "md-weekly-special"
	]

	assert [request.business_date for request in weekly_requests] == [matching_date]
	assert [request.reading_session for request in weekly_requests] == [ReadingSession.SESSION_2200]
	assert weekly_requests[0].scheduled_at == build_scheduled_at(matching_date, ReadingSession.SESSION_2200.value)


def test_monthly_definition_uses_its_own_day_of_month() -> None:
	"""monthly の day_of_month が measurement_definition ごとに反映されることを確認する。"""

	matching_date = date(2026, 2, 4)
	repository = build_single_definition_repository(
		measurement_definition_id="md-monthly-dom",
		location_id="loc-monthly-dom",
		meter_id="meter-monthly-dom",
		reading_cycle=ReadingCycle.MONTHLY,
		schedule=MonthlySchedule(
			selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
			day_of_month=4,
			reading_session=ReadingSession.SESSION_0800,
		),
		latest_business_date=date(2026, 2, 1),
	)
	generator = PlannedReadingGeneratorService(repository)

	generator.ensure_generated_through(date(2026, 2, 5))

	monthly_requests = [
		request
		for request in repository.created_requests
		if request.measurement_definition_id == "md-monthly-dom"
	]

	assert [request.business_date for request in monthly_requests] == [matching_date]
	assert [request.reading_session for request in monthly_requests] == [ReadingSession.SESSION_0800]
	assert monthly_requests[0].scheduled_at == build_scheduled_at(matching_date, ReadingSession.SESSION_0800.value)


def test_monthly_definition_uses_month_end_selector() -> None:
	"""monthly の month_edge selector が measurement_definition ごとに反映されることを確認する。"""

	matching_date = date(2026, 2, 28)
	repository = build_single_definition_repository(
		measurement_definition_id="md-monthly-edge",
		location_id="loc-monthly-edge",
		meter_id="meter-monthly-edge",
		reading_cycle=ReadingCycle.MONTHLY,
		schedule=MonthlySchedule(
			selector_type=MonthlyScheduleSelectorType.MONTH_EDGE,
			edge="last",
			reading_session=ReadingSession.SESSION_0800,
		),
		latest_business_date=date(2026, 2, 26),
	)
	generator = PlannedReadingGeneratorService(repository)

	generator.ensure_generated_through(date(2026, 2, 28))

	monthly_requests = [
		request
		for request in repository.created_requests
		if request.measurement_definition_id == "md-monthly-edge"
	]

	assert [request.business_date for request in monthly_requests] == [matching_date]
	assert [request.reading_session for request in monthly_requests] == [ReadingSession.SESSION_0800]
	assert monthly_requests[0].scheduled_at == build_scheduled_at(matching_date, ReadingSession.SESSION_0800.value)


def test_monthly_definition_rounds_missing_day_of_month_to_month_end() -> None:
	"""存在しない day_of_month は月末へ丸められることを確認する。"""

	matching_date = date(2026, 2, 28)
	repository = build_single_definition_repository(
		measurement_definition_id="md-monthly-round",
		location_id="loc-monthly-round",
		meter_id="meter-monthly-round",
		reading_cycle=ReadingCycle.MONTHLY,
		schedule=MonthlySchedule(
			selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
			day_of_month=31,
			reading_session=ReadingSession.SESSION_0800,
		),
		latest_business_date=date(2026, 2, 26),
	)
	generator = PlannedReadingGeneratorService(repository)

	generator.ensure_generated_through(date(2026, 2, 28))

	monthly_requests = [
		request
		for request in repository.created_requests
		if request.measurement_definition_id == "md-monthly-round"
	]

	assert [request.business_date for request in monthly_requests] == [matching_date]
	assert [request.reading_session for request in monthly_requests] == [ReadingSession.SESSION_0800]
	assert monthly_requests[0].scheduled_at == build_scheduled_at(matching_date, ReadingSession.SESSION_0800.value)


def test_every_two_months_definition_uses_month_parity_and_selector() -> None:
	"""every_two_months は month_parity と monthly selector の両方を満たした日にだけ生成する。"""

	matching_date = date(2026, 3, 4)
	repository = build_single_definition_repository(
		measurement_definition_id="md-bimonthly",
		location_id="loc-bimonthly",
		meter_id="meter-bimonthly",
		reading_cycle=ReadingCycle.EVERY_TWO_MONTHS,
		schedule=EveryTwoMonthsSchedule(
			selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
			month_parity=MonthParity.ODD,
			day_of_month=4,
			reading_session=ReadingSession.SESSION_0800,
		),
		latest_business_date=date(2026, 2, 27),
	)
	generator = PlannedReadingGeneratorService(repository)

	generator.ensure_generated_through(date(2026, 3, 5))

	bimonthly_requests = [
		request
		for request in repository.created_requests
		if request.measurement_definition_id == "md-bimonthly"
	]

	assert [request.business_date for request in bimonthly_requests] == [matching_date]
	assert [request.reading_session for request in bimonthly_requests] == [ReadingSession.SESSION_0800]
	assert bimonthly_requests[0].scheduled_at == build_scheduled_at(matching_date, ReadingSession.SESSION_0800.value)


def test_same_reading_cycle_definitions_can_use_distinct_schedules() -> None:
	"""同じ reading_cycle でも measurement_definition ごとに別 schedule を使えることを確認する。"""

	repository = FakeWorkPackageRepository(
		latest_business_date=None,
		generation_definitions=[
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-daily-a",
				location_id="loc-a",
				meter_id="meter-a",
				reading_cycle=ReadingCycle.DAILY_3X,
				schedule=DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0600),)),
			),
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id="md-daily-b",
				location_id="loc-b",
				meter_id="meter-b",
				reading_cycle=ReadingCycle.DAILY_3X,
				schedule=DailySchedule(reading_items=(ScheduleItem(ReadingSession.SESSION_0800), ScheduleItem(ReadingSession.SESSION_2200))),
			),
		],
	)
	generator = PlannedReadingGeneratorService(repository)

	generator.ensure_generated_through(date(2026, 2, 1))

	first_definition_sessions = [
		request.reading_session
		for request in repository.created_requests
		if request.measurement_definition_id == "md-daily-a"
		and request.business_date == date(2026, 2, 1)
	]
	second_definition_sessions = [
		request.reading_session
		for request in repository.created_requests
		if request.measurement_definition_id == "md-daily-b"
		and request.business_date == date(2026, 2, 1)
	]

	assert first_definition_sessions == [ReadingSession.SESSION_0600]
	assert second_definition_sessions == [ReadingSession.SESSION_0800, ReadingSession.SESSION_2200]
