"""Replacement repository implementations."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, or_, select

from api.models import CalculationMethod, ReadingSession, ValueSchema
from application.meter_reading_sync_repositories import HistoricalMeasuredReadingRecord, MeasurementDefinitionRecord, PlannedReadingTaskState, ReplacementBoundaryRecord, ReplacementUpsertRequest, UpsertDisposition, UpsertOutcome
from core.database import MeasurementDefinitionORM, MeterAssignmentORM, PlannedReadingORM, ReadingTransactionORM, ReplacementBoundaryORM, ReplacementEventORM, normalize_to_utc

from ._shared import SQLAlchemyRepositoryContext


class SQLAlchemyReplacementEventUpsertRepository(SQLAlchemyRepositoryContext):
	"""Persist replacement events with the same stale/tie-break rules as readings."""

	def upsert_replacement(self, request: ReplacementUpsertRequest) -> UpsertOutcome:
		incoming_client_updated_at = normalize_to_utc(request.client_updated_at)
		existing = self._session.get(ReplacementEventORM, request.replacement_event_id)
		if existing is None:
			self._session.add(
				ReplacementEventORM(
					replacement_event_id=request.replacement_event_id,
					location_id=request.location_id,
					old_meter_id=request.old_meter_id,
					new_meter_id=request.new_meter_id,
					replaced_at=request.replaced_at,
					old_final_reading=request.old_final_reading,
					new_initial_reading=request.new_initial_reading,
					client_updated_at=request.client_updated_at,
					updated_by=request.updated_by,
					note=request.note,
					arrival_sequence=1,
				)
			)
			self._session.flush()
			return UpsertOutcome(UpsertDisposition.INSERTED)

		existing_client_updated_at = normalize_to_utc(existing.client_updated_at)
		if incoming_client_updated_at < existing_client_updated_at:
			return UpsertOutcome(
				UpsertDisposition.IGNORED_STALE,
				detail="Incoming client_updated_at is older than the stored replacement event.",
			)

		disposition = UpsertDisposition.UPDATED
		if incoming_client_updated_at == existing_client_updated_at:
			disposition = UpsertDisposition.REPLACED_ON_TIE

		existing.location_id = request.location_id
		existing.old_meter_id = request.old_meter_id
		existing.new_meter_id = request.new_meter_id
		existing.replaced_at = request.replaced_at
		existing.old_final_reading = request.old_final_reading
		existing.new_initial_reading = request.new_initial_reading
		existing.client_updated_at = request.client_updated_at
		existing.updated_by = request.updated_by
		existing.note = request.note
		existing.arrival_sequence += 1
		self._session.flush()
		return UpsertOutcome(disposition)


class SQLAlchemyReplacementReflectionRepository(SQLAlchemyRepositoryContext):
	"""Apply accepted replacement side effects to assignments and planned readings."""

	def reflect_replacement(self, request: ReplacementUpsertRequest) -> None:
		replaced_at = normalize_to_utc(request.replaced_at)
		replacement_business_date = request.replaced_at.date()
		replacement_session_time = request.replaced_at.timetz().replace(tzinfo=None)
		measurement_definitions = self._find_measurement_definitions_for_replacement(
			request.location_id,
			request.old_meter_id,
			replacement_business_date,
		)
		for measurement_definition in measurement_definitions:
			self._close_old_meter_assignment(
				measurement_definition.measurement_definition_id,
				request.old_meter_id,
				replaced_at,
			)
			self._ensure_new_meter_assignment(
				measurement_definition.measurement_definition_id,
				request.new_meter_id,
				replaced_at,
			)
			measurement_definition.meter_id = request.new_meter_id
			self._create_or_update_boundary_fact(
				measurement_definition=measurement_definition,
				request=request,
				replaced_at=replaced_at,
				replacement_business_date=replacement_business_date,
			)
			self._supersede_and_regenerate_planned_readings(
				measurement_definition=measurement_definition,
				request=request,
				replaced_at=replaced_at,
				replacement_business_date=replacement_business_date,
				replacement_session_time=replacement_session_time,
			)
		self._session.flush()
		for measurement_definition in measurement_definitions:
			self._reevaluate_completed_readings_for_delayed_replacement(
				measurement_definition=measurement_definition,
				request=request,
				replacement_business_date=replacement_business_date,
			)

	def _create_or_update_boundary_fact(
		self,
		*,
		measurement_definition: MeasurementDefinitionORM,
		request: ReplacementUpsertRequest,
		replaced_at: datetime,
		replacement_business_date: date,
	) -> None:
		if measurement_definition.calculation_method != "delta":
			return
		if measurement_definition.value_schema != "decimal_string":
			return

		previous_measured = self._find_previous_measured_for_old_meter(
			measurement_definition.measurement_definition_id,
			request.old_meter_id,
			replaced_at,
		)

		old_meter_usage_delta: Decimal | None = None
		previous_raw: Decimal | None = None
		previous_planned_id: str | None = None
		if previous_measured is not None and request.old_final_reading is not None:
			previous_raw = previous_measured.raw_reading
			previous_planned_id = previous_measured.planned_reading_id
			old_meter_usage_delta = request.old_final_reading - previous_raw

		existing = self._session.get(
			ReplacementBoundaryORM,
			(request.replacement_event_id, measurement_definition.measurement_definition_id),
		)
		if existing is not None:
			existing.location_id = request.location_id
			existing.business_date = replacement_business_date
			existing.old_meter_id = request.old_meter_id
			existing.new_meter_id = request.new_meter_id
			existing.replaced_at = replaced_at
			existing.previous_measured_reading = previous_raw
			existing.previous_measured_planned_reading_id = previous_planned_id
			existing.old_final_reading = request.old_final_reading
			existing.old_meter_usage_delta = old_meter_usage_delta
			existing.new_initial_reading = request.new_initial_reading
		else:
			self._session.add(
				ReplacementBoundaryORM(
					replacement_event_id=request.replacement_event_id,
					measurement_definition_id=measurement_definition.measurement_definition_id,
					location_id=request.location_id,
					business_date=replacement_business_date,
					old_meter_id=request.old_meter_id,
					new_meter_id=request.new_meter_id,
					replaced_at=replaced_at,
					previous_measured_reading=previous_raw,
					previous_measured_planned_reading_id=previous_planned_id,
					old_final_reading=request.old_final_reading,
					old_meter_usage_delta=old_meter_usage_delta,
					new_initial_reading=request.new_initial_reading,
				)
			)

	def _find_previous_measured_for_old_meter(
		self,
		measurement_definition_id: str,
		old_meter_id: str,
		replaced_at: datetime,
	) -> HistoricalMeasuredReadingRecord | None:
		row = self._session.scalar(
			select(ReadingTransactionORM)
			.join(
				PlannedReadingORM,
				PlannedReadingORM.planned_reading_id == ReadingTransactionORM.planned_reading_id,
			)
			.where(
				PlannedReadingORM.measurement_definition_id == measurement_definition_id,
				PlannedReadingORM.meter_id == old_meter_id,
				ReadingTransactionORM.result_status == "measured",
				ReadingTransactionORM.raw_reading.is_not(None),
				PlannedReadingORM.scheduled_at < replaced_at,
			)
			.order_by(PlannedReadingORM.scheduled_at.desc(), ReadingTransactionORM.planned_reading_id.desc())
			.limit(1)
		)
		if row is None or row.raw_reading is None:
			return None
		return HistoricalMeasuredReadingRecord(
			planned_reading_id=row.planned_reading_id,
			raw_reading=row.raw_reading,
			recorded_at=row.recorded_at,
		)

	def _reevaluate_completed_readings_for_delayed_replacement(
		self,
		*,
		measurement_definition: MeasurementDefinitionORM,
		request: ReplacementUpsertRequest,
		replacement_business_date: date,
	) -> None:
		if measurement_definition.calculation_method != "delta":
			return
		if measurement_definition.value_schema != "decimal_string":
			return

		boundary = self._session.get(
			ReplacementBoundaryORM,
			(request.replacement_event_id, measurement_definition.measurement_definition_id),
		)
		if boundary is None:
			return

		completed_planned_readings = self._session.scalars(
			select(PlannedReadingORM)
			.where(
				PlannedReadingORM.measurement_definition_id == measurement_definition.measurement_definition_id,
				PlannedReadingORM.meter_id == request.new_meter_id,
				PlannedReadingORM.business_date >= replacement_business_date,
				PlannedReadingORM.task_state == PlannedReadingTaskState.COMPLETED.value,
			)
			.order_by(PlannedReadingORM.scheduled_at, PlannedReadingORM.planned_reading_id)
		).all()

		for planned_reading in completed_planned_readings:
			transaction = self._session.get(ReadingTransactionORM, planned_reading.planned_reading_id)
			if transaction is None:
				continue
			if transaction.result_status != "measured" or transaction.raw_reading is None:
				continue

			previous = self._get_latest_measured_reading(
				measurement_definition.measurement_definition_id,
				before_scheduled_at=planned_reading.scheduled_at,
				before_planned_reading_id=planned_reading.planned_reading_id,
				excluding_planned_reading_id=planned_reading.planned_reading_id,
			)
			if previous is None and boundary.new_initial_reading is not None:
				transaction.last_reading = boundary.new_initial_reading
				transaction.usage_delta = transaction.raw_reading - boundary.new_initial_reading
			elif previous is not None:
				transaction.last_reading = previous.raw_reading
				delta = transaction.raw_reading - previous.raw_reading
				md_record = self._get_measurement_definition_record(measurement_definition.measurement_definition_id)
				if delta < 0 and md_record is not None and md_record.rollover_value is not None:
					delta = (md_record.rollover_value - previous.raw_reading) + transaction.raw_reading
				multiplier = md_record.multiplier if md_record is not None else Decimal("1.0")
				transaction.usage_delta = delta * multiplier

	def _get_latest_measured_reading(
		self,
		measurement_definition_id: str,
		*,
		before_scheduled_at: datetime,
		before_planned_reading_id: str | None = None,
		excluding_planned_reading_id: str | None = None,
	) -> HistoricalMeasuredReadingRecord | None:
		query = (
			select(ReadingTransactionORM)
			.join(
				PlannedReadingORM,
				PlannedReadingORM.planned_reading_id == ReadingTransactionORM.planned_reading_id,
			)
			.where(
				PlannedReadingORM.measurement_definition_id == measurement_definition_id,
				ReadingTransactionORM.result_status == "measured",
				ReadingTransactionORM.raw_reading.is_not(None),
			)
		)
		if before_planned_reading_id is None:
			query = query.where(PlannedReadingORM.scheduled_at < before_scheduled_at)
		else:
			query = query.where(
				or_(
					PlannedReadingORM.scheduled_at < before_scheduled_at,
					and_(
						PlannedReadingORM.scheduled_at == before_scheduled_at,
						ReadingTransactionORM.planned_reading_id < before_planned_reading_id,
					),
				)
			)
		query = query.order_by(PlannedReadingORM.scheduled_at.desc(), ReadingTransactionORM.planned_reading_id.desc())
		if excluding_planned_reading_id is not None:
			query = query.where(ReadingTransactionORM.planned_reading_id != excluding_planned_reading_id)
		row = self._session.scalar(query.limit(1))
		if row is None or row.raw_reading is None:
			return None
		return HistoricalMeasuredReadingRecord(
			planned_reading_id=row.planned_reading_id,
			raw_reading=row.raw_reading,
			recorded_at=row.recorded_at,
		)

	def _get_measurement_definition_record(self, measurement_definition_id: str) -> MeasurementDefinitionRecord | None:
		row = self._session.get(MeasurementDefinitionORM, measurement_definition_id)
		if row is None:
			return None
		return MeasurementDefinitionRecord(
			measurement_definition_id=row.measurement_definition_id,
			value_schema=ValueSchema(row.value_schema),
			calculation_method=CalculationMethod(row.calculation_method),
			multiplier=row.multiplier,
			decimal_places=row.decimal_places,
			rollover_value=row.rollover_value,
		)

	def _find_measurement_definitions_for_replacement(
		self,
		location_id: str,
		old_meter_id: str,
		replacement_business_date: date,
	) -> list[MeasurementDefinitionORM]:
		candidates = self._session.scalars(
			select(MeasurementDefinitionORM)
			.where(MeasurementDefinitionORM.location_id == location_id)
			.order_by(MeasurementDefinitionORM.measurement_definition_id)
		).all()
		affected: list[MeasurementDefinitionORM] = []
		for candidate in candidates:
			if candidate.meter_id == old_meter_id:
				affected.append(candidate)
				continue
			active_assignment = self._session.scalar(
				select(MeterAssignmentORM)
				.where(
					MeterAssignmentORM.measurement_definition_id == candidate.measurement_definition_id,
					MeterAssignmentORM.meter_id == old_meter_id,
					MeterAssignmentORM.effective_to.is_(None),
				)
				.limit(1)
			)
			if active_assignment is not None:
				affected.append(candidate)
				continue
			affected_planned_reading = self._session.scalar(
				select(PlannedReadingORM.planned_reading_id)
				.where(
					PlannedReadingORM.measurement_definition_id == candidate.measurement_definition_id,
					PlannedReadingORM.meter_id == old_meter_id,
					PlannedReadingORM.business_date >= replacement_business_date,
				)
				.limit(1)
			)
			if affected_planned_reading is not None:
				affected.append(candidate)
		return affected

	def _close_old_meter_assignment(
		self,
		measurement_definition_id: str,
		old_meter_id: str,
		replaced_at: datetime,
	) -> None:
		assignment = self._session.scalar(
			select(MeterAssignmentORM)
			.where(
				MeterAssignmentORM.measurement_definition_id == measurement_definition_id,
				MeterAssignmentORM.meter_id == old_meter_id,
				MeterAssignmentORM.effective_to.is_(None),
			)
			.limit(1)
		)
		if assignment is None:
			return
		assignment.effective_to = replaced_at

	def _ensure_new_meter_assignment(
		self,
		measurement_definition_id: str,
		new_meter_id: str,
		replaced_at: datetime,
	) -> None:
		existing = self._session.scalar(
			select(MeterAssignmentORM)
			.where(
				MeterAssignmentORM.measurement_definition_id == measurement_definition_id,
				MeterAssignmentORM.meter_id == new_meter_id,
				MeterAssignmentORM.effective_to.is_(None),
			)
			.limit(1)
		)
		if existing is not None:
			return
		self._session.add(
			MeterAssignmentORM(
				measurement_definition_id=measurement_definition_id,
				meter_id=new_meter_id,
				effective_from=replaced_at,
				effective_to=None,
			)
		)

	def _supersede_and_regenerate_planned_readings(
		self,
		*,
		measurement_definition: MeasurementDefinitionORM,
		request: ReplacementUpsertRequest,
		replaced_at: datetime,
		replacement_business_date: date,
		replacement_session_time,
	) -> None:
		candidate_planned_readings = self._session.scalars(
			select(PlannedReadingORM)
			.where(
				PlannedReadingORM.measurement_definition_id == measurement_definition.measurement_definition_id,
				PlannedReadingORM.meter_id == request.old_meter_id,
				PlannedReadingORM.business_date >= replacement_business_date,
				PlannedReadingORM.task_state.in_(
					[
						PlannedReadingTaskState.OPEN.value,
						PlannedReadingTaskState.AUTO_CLOSED.value,
					]
				),
			)
			.order_by(PlannedReadingORM.business_date, PlannedReadingORM.reading_session)
		).all()
		for planned_reading in candidate_planned_readings:
			if not _planned_reading_is_affected_by_replacement(
				planned_reading,
				replacement_business_date=replacement_business_date,
				replacement_session_time=replacement_session_time,
			):
				continue
			planned_reading.task_state = PlannedReadingTaskState.SUPERSEDED.value
			planned_reading.closed_at = replaced_at
			planned_reading.close_reason = None
			replacement_planned_reading_id = _build_replacement_planned_reading_id(
				planned_reading.planned_reading_id,
				request.replacement_event_id,
			)
			if self._session.get(PlannedReadingORM, replacement_planned_reading_id) is not None:
				continue
			self._session.add(
				PlannedReadingORM(
					planned_reading_id=replacement_planned_reading_id,
					measurement_definition_id=planned_reading.measurement_definition_id,
					meter_id=request.new_meter_id,
					location_id=planned_reading.location_id,
					business_date=planned_reading.business_date,
					release_date=self._resolve_release_date(
						business_date=planned_reading.business_date,
						reading_session=ReadingSession(planned_reading.reading_session),
					),
					reading_session=planned_reading.reading_session,
					task_state=PlannedReadingTaskState.OPEN.value,
				)
			)


class SQLAlchemyReplacementBoundaryLookupRepository(SQLAlchemyRepositoryContext):
	"""Return the unconsumed replacement boundary for a new meter's first reading."""

	def get_replacement_boundary_for_new_meter_first_reading(
		self,
		measurement_definition_id: str,
		new_meter_id: str,
	) -> ReplacementBoundaryRecord | None:
		boundary = self._session.scalar(
			select(ReplacementBoundaryORM)
			.where(
				ReplacementBoundaryORM.measurement_definition_id == measurement_definition_id,
				ReplacementBoundaryORM.new_meter_id == new_meter_id,
			)
			.order_by(ReplacementBoundaryORM.replaced_at.desc())
			.limit(1)
		)
		if boundary is None or boundary.new_initial_reading is None:
			return None

		already_consumed = self._session.scalar(
			select(ReadingTransactionORM.planned_reading_id)
			.join(
				PlannedReadingORM,
				PlannedReadingORM.planned_reading_id == ReadingTransactionORM.planned_reading_id,
			)
			.where(
				PlannedReadingORM.measurement_definition_id == measurement_definition_id,
				PlannedReadingORM.meter_id == new_meter_id,
				ReadingTransactionORM.result_status == "measured",
				ReadingTransactionORM.raw_reading.is_not(None),
			)
			.limit(1)
		)
		if already_consumed is not None:
			return None

		return ReplacementBoundaryRecord(
			replacement_event_id=boundary.replacement_event_id,
			measurement_definition_id=boundary.measurement_definition_id,
			new_initial_reading=boundary.new_initial_reading,
		)


def _build_replacement_planned_reading_id(
	planned_reading_id: str,
	replacement_event_id: str,
) -> str:
	return f"{planned_reading_id}-replacement-{replacement_event_id}"


def _planned_reading_is_affected_by_replacement(
	planned_reading: PlannedReadingORM,
	*,
	replacement_business_date: date,
	replacement_session_time,
) -> bool:
	if planned_reading.business_date > replacement_business_date:
		return True
	if planned_reading.business_date < replacement_business_date:
		return False
	reading_session_time = datetime.strptime(planned_reading.reading_session, "%H:%M").time()
	return reading_session_time >= replacement_session_time
