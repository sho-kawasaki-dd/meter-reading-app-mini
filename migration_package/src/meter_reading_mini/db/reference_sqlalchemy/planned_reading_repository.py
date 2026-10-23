"""Planned reading repository implementations."""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy import update as sqlalchemy_update

from api.models import PlannedReadingTask, ReadingCycle, ReadingSession
from application.meter_reading_sync_repositories import (
	PlannedReadingCloseReason,
	PlannedReadingCreateRequest,
	PlannedReadingGenerationDefinitionRecord,
	PlannedReadingRecompositionRecord,
	PlannedReadingRecord,
	PlannedReadingTaskState,
)
from application.schedule_settings import (
	DEFAULT_SCHEDULES,
	DailySchedule,
	EveryTwoMonthsSchedule,
	MonthlySchedule,
	MonthlyScheduleSelectorType,
	MonthParity,
	ScheduleSettings,
	WeeklySchedule,
	deserialize_schedule,
	validate_schedule_type_for_reading_cycle,
)
from core.database import MeasurementDefinitionORM, MeasurementDefinitionScheduleORM, MeterAssignmentORM, PlannedReadingORM, build_scheduled_at, normalize_to_utc

from ._shared import SQLAlchemyRepositoryContext


logger = logging.getLogger(__name__)


class SQLAlchemyPlannedReadingLookupRepository(SQLAlchemyRepositoryContext):
	"""Look up the minimal metadata for a planned reading."""

	def get_planned_reading(self, planned_reading_id: str) -> PlannedReadingRecord | None:
		row = self._session.get(PlannedReadingORM, planned_reading_id)
		if row is None:
			return None
		return PlannedReadingRecord(
			planned_reading_id=row.planned_reading_id,
			measurement_definition_id=row.measurement_definition_id,
			meter_id=row.meter_id,
			scheduled_at=row.scheduled_at,
		)


class SQLAlchemyPlannedReadingGenerationRepository(SQLAlchemyRepositoryContext):
	"""Backfill and create planned readings."""

	def get_latest_planned_business_date(self) -> date | None:
		return self._session.scalar(select(func.max(PlannedReadingORM.business_date)))

	def list_planned_reading_generation_definitions(
		self,
	) -> list[PlannedReadingGenerationDefinitionRecord]:
		rows = self._session.execute(
			select(MeasurementDefinitionORM, MeterAssignmentORM.meter_id, MeasurementDefinitionScheduleORM.schedule_json)
			.join(
				MeterAssignmentORM,
				MeterAssignmentORM.measurement_definition_id == MeasurementDefinitionORM.measurement_definition_id,
			)
			.outerjoin(
				MeasurementDefinitionScheduleORM,
				MeasurementDefinitionScheduleORM.measurement_definition_id == MeasurementDefinitionORM.measurement_definition_id,
			)
			.where(
				MeterAssignmentORM.effective_to.is_(None),
				MeasurementDefinitionORM.is_active == True,  # noqa: E712
			)
			.order_by(
				MeasurementDefinitionORM.location_id,
				MeasurementDefinitionORM.measurement_definition_id,
			)
		).all()
		return [
			PlannedReadingGenerationDefinitionRecord(
				measurement_definition_id=row.MeasurementDefinitionORM.measurement_definition_id,
				location_id=row.MeasurementDefinitionORM.location_id,
				meter_id=row.meter_id,
				reading_cycle=ReadingCycle(row.MeasurementDefinitionORM.reading_cycle),
				schedule=_resolve_generation_schedule(
					ReadingCycle(row.MeasurementDefinitionORM.reading_cycle),
					row.schedule_json,
				),
			)
			for row in rows
		]

	def list_measurement_definitions_without_active_assignment(self) -> list[MeasurementDefinitionORM]:
		"""Return active measurement definitions that have no current meter assignment."""

		rows = self._session.scalars(
			select(MeasurementDefinitionORM)
			.outerjoin(
				MeterAssignmentORM,
				(
					MeterAssignmentORM.measurement_definition_id == MeasurementDefinitionORM.measurement_definition_id
				) & (MeterAssignmentORM.effective_to.is_(None)),
			)
			.where(
				MeasurementDefinitionORM.is_active == True,  # noqa: E712
				MeterAssignmentORM.measurement_definition_id.is_(None),
			)
			.order_by(
				MeasurementDefinitionORM.location_id,
				MeasurementDefinitionORM.measurement_definition_id,
			)
		).all()
		return list(rows)

	def create_planned_reading_if_missing(self, request: PlannedReadingCreateRequest) -> bool:
		return self.create_planned_readings_if_missing([request]) > 0

	def create_planned_readings_if_missing(self, requests: list[PlannedReadingCreateRequest]) -> int:
		bind = self._session.get_bind()
		if bind is not None and bind.dialect.name == "sqlite":
			self._session.execute(text("BEGIN IMMEDIATE"))

		new_rows: list[PlannedReadingORM] = []
		seen_planned_reading_ids: set[str] = set()
		for request in requests:
			if request.planned_reading_id in seen_planned_reading_ids:
				continue
			seen_planned_reading_ids.add(request.planned_reading_id)
			existing = self._session.get(PlannedReadingORM, request.planned_reading_id)
			if existing is not None:
				continue
			new_rows.append(
				PlannedReadingORM(
					planned_reading_id=request.planned_reading_id,
					measurement_definition_id=request.measurement_definition_id,
					meter_id=request.meter_id,
					location_id=request.location_id,
					business_date=request.business_date,
					release_date=self._resolve_release_date(
						business_date=request.business_date + timedelta(days=request.day_offset),
						reading_session=request.reading_session,
					),
					reading_session=request.reading_session.value,
					scheduled_at=request.scheduled_at,
					task_state=PlannedReadingTaskState.OPEN.value,
				)
			)

		if not new_rows:
			return 0

		self._session.add_all(new_rows)
		self._session.flush()
		return len(new_rows)


class SQLAlchemyPlannedReadingLifecycleRepository(SQLAlchemyRepositoryContext):
	"""Maintain lifecycle state for planned readings."""

	def auto_close_open_planned_readings(
		self,
		through_business_date: date,
		*,
		closed_at: datetime,
		close_reason: PlannedReadingCloseReason,
	) -> int:
		planned_reading_ids = self._session.scalars(
			select(PlannedReadingORM.planned_reading_id)
			.where(
				PlannedReadingORM.task_state == PlannedReadingTaskState.OPEN.value,
				PlannedReadingORM.business_date <= through_business_date,
			)
		).all()
		if not planned_reading_ids:
			return 0

		self._session.execute(
			sqlalchemy_update(PlannedReadingORM)
			.where(PlannedReadingORM.planned_reading_id.in_(planned_reading_ids))
			.values(
				task_state=PlannedReadingTaskState.AUTO_CLOSED.value,
				closed_at=closed_at,
				close_reason=close_reason.value,
			)
		)
		self._session.flush()
		return len(planned_reading_ids)

	def list_planned_reading_recomposition_candidates(
		self,
		measurement_definition_id: str,
		from_business_date: date,
	) -> list[PlannedReadingRecompositionRecord]:
		rows = self._session.scalars(
			select(PlannedReadingORM)
			.where(
				PlannedReadingORM.measurement_definition_id == measurement_definition_id,
				PlannedReadingORM.business_date >= from_business_date,
			)
			.order_by(
				PlannedReadingORM.business_date,
				PlannedReadingORM.reading_session,
				PlannedReadingORM.planned_reading_id,
			)
		).all()
		return [
			PlannedReadingRecompositionRecord(
				planned_reading_id=row.planned_reading_id,
				measurement_definition_id=row.measurement_definition_id,
				meter_id=row.meter_id,
				location_id=row.location_id,
				business_date=row.business_date,
				reading_session=ReadingSession(row.reading_session),
				scheduled_at=row.scheduled_at,
				task_state=PlannedReadingTaskState(row.task_state),
			)
			for row in rows
		]

	def supersede_planned_readings(self, planned_reading_ids: list[str], *, superseded_at: datetime) -> int:
		if not planned_reading_ids:
			return 0

		result = self._session.execute(
			sqlalchemy_update(PlannedReadingORM)
			.where(
				PlannedReadingORM.planned_reading_id.in_(planned_reading_ids),
				PlannedReadingORM.task_state != PlannedReadingTaskState.COMPLETED.value,
			)
			.values(
				task_state=PlannedReadingTaskState.SUPERSEDED.value,
				closed_at=normalize_to_utc(superseded_at),
				close_reason=None,
			)
		)
		self._session.flush()
		return result.rowcount or 0


class SQLAlchemyPlannedReadingRecompositionRepository(SQLAlchemyPlannedReadingLifecycleRepository):
	"""Recompose future planned readings after schedule changes."""

	def recompose_future_planned_readings(
		self,
		measurement_definition_id: str,
		reading_cycle: ReadingCycle,
		schedule: ScheduleSettings,
		*,
		from_business_date: date | None = None,
	) -> tuple[int, int]:
		validate_schedule_type_for_reading_cycle(reading_cycle, schedule)
		start_business_date = from_business_date or date.today()
		latest_business_date = self._session.scalar(
			select(func.max(PlannedReadingORM.business_date)).where(
				PlannedReadingORM.measurement_definition_id == measurement_definition_id,
			)
		)
		if latest_business_date is None or start_business_date > latest_business_date:
			logger.info(
				"Recomposed future planned readings for %s (%s) from %s: superseded=0 generated=0",
				measurement_definition_id,
				reading_cycle.value,
				start_business_date.isoformat(),
			)
			return (0, 0)

		measurement_definition = self._session.get(MeasurementDefinitionORM, measurement_definition_id)
		if measurement_definition is None:
			raise ValueError(f"Unknown measurement_definition_id: {measurement_definition_id}")

		active_meter_id = self._session.scalar(
			select(MeterAssignmentORM.meter_id)
			.where(
				MeterAssignmentORM.measurement_definition_id == measurement_definition_id,
				MeterAssignmentORM.effective_to.is_(None),
			)
			.limit(1)
		)
		meter_id = active_meter_id or measurement_definition.meter_id
		if meter_id is None:
			raise ValueError(f"No active meter assignment for measurement_definition_id={measurement_definition_id}")

		planned_readings = self._session.scalars(
			select(PlannedReadingORM)
			.where(
				PlannedReadingORM.measurement_definition_id == measurement_definition_id,
				PlannedReadingORM.business_date >= start_business_date,
				PlannedReadingORM.business_date <= latest_business_date,
			)
			.order_by(
				PlannedReadingORM.business_date,
				PlannedReadingORM.reading_session,
				PlannedReadingORM.planned_reading_id,
			)
		).all()
		planned_reading_by_id = {row.planned_reading_id: row for row in planned_readings}
		desired_slot_keys: set[tuple[date, str]] = set()
		generated_count = 0
		scheduled_tzinfo = self._scheduled_timezone_provider()

		current_business_date = start_business_date
		while current_business_date <= latest_business_date:
			for reading_session, day_offset in _resolve_recomposition_schedule_items(reading_cycle, current_business_date, schedule):
				desired_slot_keys.add((current_business_date, reading_session.value))
				planned_reading_id = _build_recomposition_planned_reading_id(
					measurement_definition_id,
					current_business_date,
					reading_session,
				)
				release_date = self._resolve_release_date(
					business_date=current_business_date + timedelta(days=day_offset),
					reading_session=reading_session,
				)
				scheduled_at = build_scheduled_at(
					current_business_date,
					reading_session.value,
					day_offset=day_offset,
					tzinfo=scheduled_tzinfo,
				)
				planned_reading = planned_reading_by_id.get(planned_reading_id)
				if planned_reading is None:
					self._session.add(
						PlannedReadingORM(
							planned_reading_id=planned_reading_id,
							measurement_definition_id=measurement_definition_id,
							meter_id=meter_id,
							location_id=measurement_definition.location_id,
							business_date=current_business_date,
							release_date=release_date,
							reading_session=reading_session.value,
							scheduled_at=scheduled_at,
							task_state=PlannedReadingTaskState.OPEN.value,
						)
					)
					generated_count += 1
					continue

				if planned_reading.task_state == PlannedReadingTaskState.COMPLETED.value:
					continue

				planned_reading_changed = (
					planned_reading.meter_id != meter_id
					or planned_reading.location_id != measurement_definition.location_id
					or planned_reading.business_date != current_business_date
					or planned_reading.release_date != release_date
					or planned_reading.reading_session != reading_session.value
					or planned_reading.scheduled_at != scheduled_at
					or planned_reading.task_state != PlannedReadingTaskState.OPEN.value
					or planned_reading.closed_at is not None
					or planned_reading.close_reason is not None
				)
				if planned_reading_changed:
					planned_reading.meter_id = meter_id
					planned_reading.location_id = measurement_definition.location_id
					planned_reading.business_date = current_business_date
					planned_reading.release_date = release_date
					planned_reading.reading_session = reading_session.value
					planned_reading.scheduled_at = scheduled_at
					planned_reading.task_state = PlannedReadingTaskState.OPEN.value
					planned_reading.closed_at = None
					planned_reading.close_reason = None
					generated_count += 1

			current_business_date += timedelta(days=1)

		self._session.flush()
		superseded_ids = [
			row.planned_reading_id
			for row in planned_readings
			if row.task_state in {
				PlannedReadingTaskState.OPEN.value,
				PlannedReadingTaskState.AUTO_CLOSED.value,
			}
			and (row.business_date, row.reading_session) not in desired_slot_keys
		]
		superseded_count = 0
		if superseded_ids:
			superseded_count = self.supersede_planned_readings(superseded_ids, superseded_at=datetime.now(tz=scheduled_tzinfo))

		logger.info(
			"Recomposed future planned readings for %s (%s) from %s through %s: superseded=%d generated=%d",
			measurement_definition_id,
			reading_cycle.value,
			start_business_date.isoformat(),
			latest_business_date.isoformat(),
			superseded_count,
			generated_count,
		)
		return superseded_count, generated_count


class SQLAlchemyPlannedReadingTaskQueryRepository(SQLAlchemyRepositoryContext):
	"""List open planned-reading tasks for download endpoints."""

	def list_open_planned_reading_tasks_for_release_date(self, release_date: date) -> list[PlannedReadingTask]:
		rows = self._session.execute(
			select(PlannedReadingORM, MeasurementDefinitionORM)
			.join(
				MeasurementDefinitionORM,
				MeasurementDefinitionORM.measurement_definition_id == PlannedReadingORM.measurement_definition_id,
			)
			.where(
				PlannedReadingORM.release_date <= release_date,
				PlannedReadingORM.task_state == PlannedReadingTaskState.OPEN.value,
			)
			.order_by(
				PlannedReadingORM.release_date,
				PlannedReadingORM.business_date,
				PlannedReadingORM.reading_session,
				PlannedReadingORM.location_id,
				PlannedReadingORM.measurement_definition_id,
			)
		).all()
		return [_to_planned_reading_task(planned_reading, measurement_definition) for planned_reading, measurement_definition in rows]

	def list_open_planned_reading_tasks_for_business_date(self, business_date: date) -> list[PlannedReadingTask]:
		rows = self._session.execute(
			select(PlannedReadingORM, MeasurementDefinitionORM)
			.join(
				MeasurementDefinitionORM,
				MeasurementDefinitionORM.measurement_definition_id == PlannedReadingORM.measurement_definition_id,
			)
			.where(
				PlannedReadingORM.business_date == business_date,
				PlannedReadingORM.task_state == PlannedReadingTaskState.OPEN.value,
			)
			.order_by(
				PlannedReadingORM.reading_session,
				PlannedReadingORM.location_id,
				PlannedReadingORM.measurement_definition_id,
			)
		).all()
		return [_to_planned_reading_task(planned_reading, measurement_definition) for planned_reading, measurement_definition in rows]

	def list_open_planned_reading_tasks(self, business_date: date) -> list[PlannedReadingTask]:
		return self.list_open_planned_reading_tasks_for_business_date(business_date)


def _resolve_generation_schedule(reading_cycle: ReadingCycle, schedule_json: str | None):
	if schedule_json is None:
		return DEFAULT_SCHEDULES[reading_cycle]
	return deserialize_schedule(reading_cycle, schedule_json)


def _resolve_recomposition_schedule_items(
	reading_cycle: ReadingCycle,
	business_date: date,
	schedule: ScheduleSettings,
) -> tuple[tuple[ReadingSession, int], ...]:
	if isinstance(schedule, DailySchedule):
		return tuple((item.reading_session, item.day_offset) for item in schedule.reading_items)

	if isinstance(schedule, WeeklySchedule):
		if business_date.weekday() != schedule.weekday:
			return ()
		return ((schedule.reading_session, schedule.day_offset),)

	if isinstance(schedule, MonthlySchedule):
		if not _matches_recomposition_monthly_selector(business_date, schedule):
			return ()
		return ((schedule.reading_session, schedule.day_offset),)

	if isinstance(schedule, EveryTwoMonthsSchedule):
		if not _matches_recomposition_month_parity(business_date, schedule.month_parity):
			return ()
		monthly_proxy = MonthlySchedule(
			selector_type=schedule.selector_type,
			day_of_month=schedule.day_of_month,
			nth=schedule.nth,
			nth_weekday=schedule.nth_weekday,
			edge=schedule.edge,
			reading_session=schedule.reading_session,
			day_offset=schedule.day_offset,
		)
		if not _matches_recomposition_monthly_selector(business_date, monthly_proxy):
			return ()
		return ((schedule.reading_session, schedule.day_offset),)

	return ()


def _matches_recomposition_monthly_selector(business_date: date, schedule: MonthlySchedule) -> bool:
	if schedule.selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
		assert schedule.day_of_month is not None
		_, last_day = calendar.monthrange(business_date.year, business_date.month)
		target_day = min(schedule.day_of_month, last_day)
		return business_date.day == target_day

	if schedule.selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
		assert schedule.nth is not None and schedule.nth_weekday is not None
		if business_date.weekday() != schedule.nth_weekday:
			return False
		week_number = (business_date.day - 1) // 7 + 1
		return week_number == schedule.nth

	if schedule.selector_type is MonthlyScheduleSelectorType.MONTH_EDGE:
		assert schedule.edge is not None
		if schedule.edge == "first":
			return business_date.day == 1
		if schedule.edge == "last":
			_, last_day = calendar.monthrange(business_date.year, business_date.month)
			return business_date.day == last_day

	return False


def _matches_recomposition_month_parity(business_date: date, month_parity: MonthParity) -> bool:
	if month_parity is MonthParity.ODD:
		return business_date.month % 2 == 1
	return business_date.month % 2 == 0


def _build_recomposition_planned_reading_id(
	measurement_definition_id: str,
	business_date: date,
	reading_session: ReadingSession,
) -> str:
	return f"plan-{measurement_definition_id}-{business_date.isoformat()}-{reading_session.value.replace(':', '')}"


def _to_planned_reading_task(planned_reading: PlannedReadingORM, measurement_definition: MeasurementDefinitionORM) -> PlannedReadingTask:
	return PlannedReadingTask(
		planned_reading_id=planned_reading.planned_reading_id,
		measurement_definition_id=planned_reading.measurement_definition_id,
		meter_id=planned_reading.meter_id,
		location_id=planned_reading.location_id,
		business_date=planned_reading.business_date,
		release_date=planned_reading.release_date,
		reading_session=ReadingSession(planned_reading.reading_session),
		scheduled_at=planned_reading.scheduled_at,
		reading_cycle=ReadingCycle(measurement_definition.reading_cycle),
		required=True,
	)
