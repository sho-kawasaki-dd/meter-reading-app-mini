"""Reading repository implementations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, or_, select

from api.models import CalculationMethod, ValueSchema
from application.meter_reading_sync_repositories import (
	HistoricalMeasuredReadingRecord,
	MeasurementDefinitionRecord,
	ReadingUpsertRequest,
	ReplacementBoundaryRecord,
	UpsertDisposition,
	UpsertOutcome,
)
from core.database import (
	MeasurementDefinitionORM,
	PlannedReadingORM,
	ReadingTransactionORM,
	ReplacementBoundaryORM,
	normalize_to_utc,
)

from ._shared import SQLAlchemyRepositoryContext


class SQLAlchemyHistoricalReadingLookupRepository(SQLAlchemyRepositoryContext):
	"""Return the latest prior measured decimal reading."""

	def get_latest_measured_reading(
		self,
		measurement_definition_id: str,
		*,
		before_scheduled_at: datetime,
		before_planned_reading_id: str | None = None,
		excluding_planned_reading_id: str | None = None,
	) -> HistoricalMeasuredReadingRecord | None:
		query = self._build_measured_reading_query(
			measurement_definition_id,
			boundary_scheduled_at=before_scheduled_at,
			boundary_planned_reading_id=before_planned_reading_id,
			direction="previous",
		)
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

	def _build_measured_reading_query(
		self,
		measurement_definition_id: str,
		*,
		boundary_scheduled_at: datetime,
		boundary_planned_reading_id: str | None,
		direction: str,
	):
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
		if direction == "previous":
			if boundary_planned_reading_id is None:
				query = query.where(PlannedReadingORM.scheduled_at < boundary_scheduled_at)
			else:
				query = query.where(
					or_(
						PlannedReadingORM.scheduled_at < boundary_scheduled_at,
						and_(
							PlannedReadingORM.scheduled_at == boundary_scheduled_at,
							ReadingTransactionORM.planned_reading_id < boundary_planned_reading_id,
						),
					)
				)
			query = query.order_by(PlannedReadingORM.scheduled_at.desc(), ReadingTransactionORM.planned_reading_id.desc())
		elif direction == "next":
			if boundary_planned_reading_id is None:
				query = query.where(PlannedReadingORM.scheduled_at > boundary_scheduled_at)
			else:
				query = query.where(
					or_(
						PlannedReadingORM.scheduled_at > boundary_scheduled_at,
						and_(
							PlannedReadingORM.scheduled_at == boundary_scheduled_at,
							ReadingTransactionORM.planned_reading_id > boundary_planned_reading_id,
						),
					)
				)
			query = query.order_by(PlannedReadingORM.scheduled_at.asc(), ReadingTransactionORM.planned_reading_id.asc())
		else:
			raise ValueError(f"Unknown direction: {direction}")
		return query


class SQLAlchemyReadingPersistenceRepository(SQLAlchemyRepositoryContext):
	"""Persist resolved readings with stale/tie-break handling."""

	def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
		incoming_client_updated_at = normalize_to_utc(request.client_updated_at)
		existing = self._session.get(ReadingTransactionORM, request.planned_reading_id)
		if existing is None:
			self._session.add(
				ReadingTransactionORM(
					planned_reading_id=request.planned_reading_id,
					result_status=request.result_status.value,
					recorded_at=request.recorded_at,
					client_updated_at=request.client_updated_at,
					raw_reading=request.raw_reading,
					last_reading=request.last_reading,
					usage_delta=request.usage_delta,
					structured_payload_json=request.structured_payload_json,
					device_id=request.device_id,
					updated_by=request.updated_by,
					signature=request.signature,
					note=request.note,
					result_source=request.result_source.value,
					arrival_sequence=1,
				)
			)
			self._mark_planned_reading_completed(
				request.planned_reading_id,
				closed_at=request.recorded_at,
			)
			self._session.flush()
			return UpsertOutcome(UpsertDisposition.INSERTED)

		existing_client_updated_at = normalize_to_utc(existing.client_updated_at)
		if incoming_client_updated_at < existing_client_updated_at:
			return UpsertOutcome(
				UpsertDisposition.IGNORED_STALE,
				detail="Incoming client_updated_at is older than the stored reading.",
			)

		disposition = UpsertDisposition.UPDATED
		if incoming_client_updated_at == existing_client_updated_at:
			disposition = UpsertDisposition.REPLACED_ON_TIE

		existing.result_status = request.result_status.value
		existing.recorded_at = request.recorded_at
		existing.client_updated_at = request.client_updated_at
		existing.raw_reading = request.raw_reading
		existing.last_reading = request.last_reading
		existing.usage_delta = request.usage_delta
		existing.structured_payload_json = request.structured_payload_json
		existing.device_id = request.device_id
		existing.updated_by = request.updated_by
		existing.signature = request.signature
		existing.note = request.note
		existing.result_source = request.result_source.value
		existing.arrival_sequence += 1
		self._mark_planned_reading_completed(
			request.planned_reading_id,
			closed_at=request.recorded_at,
		)
		self._session.flush()
		return UpsertOutcome(disposition)

	def recalculate_following_measured_reading(
		self,
		measurement_definition_id: str,
		*,
		after_scheduled_at: datetime,
		after_planned_reading_id: str | None = None,
	) -> int:
		next_transaction = self._session.scalar(
			self._build_measured_reading_query(
				measurement_definition_id,
				boundary_scheduled_at=after_scheduled_at,
				boundary_planned_reading_id=after_planned_reading_id,
				direction="next",
			).limit(1)
		)
		if next_transaction is None:
			return 0

		self._recalculate_measured_transaction(next_transaction)
		self._session.flush()
		return 1

	def _build_measured_reading_query(
		self,
		measurement_definition_id: str,
		*,
		boundary_scheduled_at: datetime,
		boundary_planned_reading_id: str | None,
		direction: str,
	):
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
		if direction == "previous":
			if boundary_planned_reading_id is None:
				query = query.where(PlannedReadingORM.scheduled_at < boundary_scheduled_at)
			else:
				query = query.where(
					or_(
						PlannedReadingORM.scheduled_at < boundary_scheduled_at,
						and_(
							PlannedReadingORM.scheduled_at == boundary_scheduled_at,
							ReadingTransactionORM.planned_reading_id < boundary_planned_reading_id,
						),
					)
				)
			query = query.order_by(PlannedReadingORM.scheduled_at.desc(), ReadingTransactionORM.planned_reading_id.desc())
		elif direction == "next":
			if boundary_planned_reading_id is None:
				query = query.where(PlannedReadingORM.scheduled_at > boundary_scheduled_at)
			else:
				query = query.where(
					or_(
						PlannedReadingORM.scheduled_at > boundary_scheduled_at,
						and_(
							PlannedReadingORM.scheduled_at == boundary_scheduled_at,
							ReadingTransactionORM.planned_reading_id > boundary_planned_reading_id,
						),
					)
				)
			query = query.order_by(PlannedReadingORM.scheduled_at.asc(), ReadingTransactionORM.planned_reading_id.asc())
		else:
			raise ValueError(f"Unknown direction: {direction}")
		return query

	def _recalculate_measured_transaction(self, transaction: ReadingTransactionORM) -> None:
		planned_reading = transaction.planned_reading
		measurement_definition = self._get_measurement_definition_record(planned_reading.measurement_definition_id)
		if measurement_definition is None:
			return

		previous_measured_reading = self._get_latest_measured_reading(
			planned_reading.measurement_definition_id,
			before_scheduled_at=planned_reading.scheduled_at,
			before_planned_reading_id=planned_reading.planned_reading_id,
			excluding_planned_reading_id=planned_reading.planned_reading_id,
		)
		replacement_boundary = None
		if previous_measured_reading is None:
			replacement_boundary = self._get_replacement_boundary_for_new_meter_first_reading(
				planned_reading.measurement_definition_id,
				planned_reading.meter_id,
			)

		last_reading, usage_delta = self._calculate_usage_snapshot(
			measurement_definition=measurement_definition,
			raw_reading=transaction.raw_reading,
			previous_measured_reading=previous_measured_reading,
			replacement_boundary=replacement_boundary,
		)
		transaction.last_reading = last_reading
		transaction.usage_delta = usage_delta

	def _calculate_usage_snapshot(
		self,
		*,
		measurement_definition: MeasurementDefinitionRecord,
		raw_reading: Decimal | None,
		previous_measured_reading: HistoricalMeasuredReadingRecord | None,
		replacement_boundary: ReplacementBoundaryRecord | None = None,
	) -> tuple[Decimal | None, Decimal | None]:
		if raw_reading is None:
			return None, None

		if measurement_definition.calculation_method is CalculationMethod.RAW_ONLY:
			return None, None

		if previous_measured_reading is None and replacement_boundary is not None and replacement_boundary.new_initial_reading is not None:
			last_reading = replacement_boundary.new_initial_reading
			delta = raw_reading - last_reading
			if delta < 0 and measurement_definition.rollover_value is not None:
				delta = (measurement_definition.rollover_value - last_reading) + raw_reading
			return last_reading, delta * measurement_definition.multiplier

		if previous_measured_reading is None:
			return None, Decimal("0")

		last_reading = previous_measured_reading.raw_reading
		delta = raw_reading - last_reading
		if delta < 0 and measurement_definition.rollover_value is not None:
			delta = (measurement_definition.rollover_value - last_reading) + raw_reading
		return last_reading, delta * measurement_definition.multiplier

	def _get_latest_measured_reading(
		self,
		measurement_definition_id: str,
		*,
		before_scheduled_at: datetime,
		before_planned_reading_id: str | None = None,
		excluding_planned_reading_id: str | None = None,
	) -> HistoricalMeasuredReadingRecord | None:
		query = self._build_measured_reading_query(
			measurement_definition_id,
			boundary_scheduled_at=before_scheduled_at,
			boundary_planned_reading_id=before_planned_reading_id,
			direction="previous",
		)
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

	def _get_replacement_boundary_for_new_meter_first_reading(
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

	def _mark_planned_reading_completed(self, planned_reading_id: str, *, closed_at: datetime) -> None:
		planned_reading = self._session.get(PlannedReadingORM, planned_reading_id)
		if planned_reading is None:
			return

		planned_reading.task_state = "completed"
		planned_reading.closed_at = closed_at
		planned_reading.close_reason = None
