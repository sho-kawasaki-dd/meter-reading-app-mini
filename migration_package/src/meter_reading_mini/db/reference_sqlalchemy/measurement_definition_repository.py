"""Measurement definition repository implementations."""

from __future__ import annotations

from sqlalchemy import select

from api.models import CalculationMethod, Category, MeasurementDefinitionData, Purpose, ReadingCycle, ValueSchema
from application.meter_reading_sync_repositories import MeasurementDefinitionRecord
from core.database import MeasurementDefinitionORM, MeasurementDefinitionScheduleORM, PlannedReadingORM, ReadingTransactionORM

from ._shared import SQLAlchemyRepositoryContext


class SQLAlchemyMeasurementDefinitionLookupRepository(SQLAlchemyRepositoryContext):
	"""Resolve validation metadata for a measurement definition."""

	def get_measurement_definition(
		self,
		measurement_definition_id: str,
	) -> MeasurementDefinitionRecord | None:
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


class SQLAlchemyMeasurementDefinitionScheduleRepository(SQLAlchemyRepositoryContext):
	"""Read and write measurement-definition schedules."""

	def get_measurement_definition_schedule(self, measurement_definition_id: str) -> str | None:
		row = self._session.get(MeasurementDefinitionScheduleORM, measurement_definition_id)
		if row is None:
			return None
		return row.schedule_json

	def upsert_measurement_definition_schedule(self, measurement_definition_id: str, schedule_json: str) -> None:
		row = self._session.get(MeasurementDefinitionScheduleORM, measurement_definition_id)
		if row is None:
			self._session.add(
				MeasurementDefinitionScheduleORM(
					measurement_definition_id=measurement_definition_id,
					schedule_json=schedule_json,
				)
			)
		else:
			row.schedule_json = schedule_json
		self._session.flush()


class SQLAlchemyMeasurementDefinitionQueryRepository(SQLAlchemyRepositoryContext):
	"""List measurement definitions for work-package downloads."""

	def list_measurement_definition_data(self) -> list[MeasurementDefinitionData]:
		latest_raw_reading = (
			select(ReadingTransactionORM.raw_reading)
			.join(
				PlannedReadingORM,
				PlannedReadingORM.planned_reading_id == ReadingTransactionORM.planned_reading_id,
			)
			.where(
				PlannedReadingORM.measurement_definition_id == MeasurementDefinitionORM.measurement_definition_id,
				PlannedReadingORM.meter_id == MeasurementDefinitionORM.meter_id,
				ReadingTransactionORM.result_status == "measured",
				ReadingTransactionORM.raw_reading.is_not(None),
			)
			.order_by(PlannedReadingORM.scheduled_at.desc(), ReadingTransactionORM.planned_reading_id.desc())
			.limit(1)
			.scalar_subquery()
		)
		rows = self._session.execute(
			select(MeasurementDefinitionORM, latest_raw_reading.label("last_reading"))
			.where(MeasurementDefinitionORM.is_active == True)  # noqa: E712
			.order_by(
				MeasurementDefinitionORM.location_id,
				MeasurementDefinitionORM.measurement_definition_id,
			)
		).all()
		return [
			MeasurementDefinitionData(
				measurement_definition_id=row.MeasurementDefinitionORM.measurement_definition_id,
				location_id=row.MeasurementDefinitionORM.location_id,
				meter_id=row.MeasurementDefinitionORM.meter_id,
				display_name=row.MeasurementDefinitionORM.display_name,
				category=Category(row.MeasurementDefinitionORM.category),
				unit=row.MeasurementDefinitionORM.unit,
				value_schema=ValueSchema(row.MeasurementDefinitionORM.value_schema),
				last_reading=row.last_reading,
				multiplier=row.MeasurementDefinitionORM.multiplier,
				decimal_places=row.MeasurementDefinitionORM.decimal_places,
				warning_threshold=row.MeasurementDefinitionORM.warning_threshold,
				rollover_value=row.MeasurementDefinitionORM.rollover_value,
				calculation_method=CalculationMethod(row.MeasurementDefinitionORM.calculation_method),
				reading_cycle=ReadingCycle(row.MeasurementDefinitionORM.reading_cycle),
				purpose=Purpose(row.MeasurementDefinitionORM.purpose),
			)
			for row in rows
		]
