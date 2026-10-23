"""reading 保存前の履歴取得・計算・永続化を束ねる application service。"""

from __future__ import annotations

import logging
from dataclasses import replace

logger = logging.getLogger(__name__)

from api.models import ResultStatus
from application.meter_reading_sync_repositories import (
	HistoricalMeasuredReadingLookupRepository,
	MeasurementDefinitionLookupRepository,
	PlannedReadingLookupRepository,
	ReadingPersistenceRepository,
	ReadingUpsertRepository,
	ReadingUpsertRequest,
	ReplacementBoundaryLookupRepository,
	UpsertOutcome,
)
from application.usage_delta_calculation import UsageDeltaCalculationService


class ReadingWriteOrchestrationError(ValueError):
	"""writing 前の計算準備に失敗したことを表す。"""


class ReadingWriteOrchestrationService(ReadingUpsertRepository):
	"""履歴 lookup、pure 計算、永続化を 1 つにまとめる service。"""

	def __init__(
		self,
		measurement_definition_repository: MeasurementDefinitionLookupRepository,
		historical_reading_repository: HistoricalMeasuredReadingLookupRepository,
		persistence_repository: ReadingPersistenceRepository,
		*,
		planned_reading_repository: PlannedReadingLookupRepository | None = None,
		boundary_repository: ReplacementBoundaryLookupRepository | None = None,
		calculation_service: UsageDeltaCalculationService | None = None,
	) -> None:
		self._measurement_definition_repository = measurement_definition_repository
		self._historical_reading_repository = historical_reading_repository
		self._persistence_repository = persistence_repository
		self._planned_reading_repository = planned_reading_repository
		self._boundary_repository = boundary_repository
		self._calculation_service = calculation_service or UsageDeltaCalculationService()

	def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
		"""保存前に usage_delta を計算し、計算済み request を永続化する。"""

		measurement_definition = self._measurement_definition_repository.get_measurement_definition(
			request.measurement_definition_id
		)
		if measurement_definition is None:
			raise ReadingWriteOrchestrationError(
				f"Unknown measurement_definition_id for write orchestration: {request.measurement_definition_id}"
			)

		planned_reading = None
		if self._planned_reading_repository is not None:
			planned_reading = self._planned_reading_repository.get_planned_reading(request.planned_reading_id)
			if planned_reading is None:
				raise ReadingWriteOrchestrationError(
					f"Unknown planned_reading_id for write orchestration: {request.planned_reading_id}"
				)

		replacement_boundary = None
		if self._boundary_repository is not None and planned_reading is not None:
			replacement_boundary = self._boundary_repository.get_replacement_boundary_for_new_meter_first_reading(
				request.measurement_definition_id,
				planned_reading.meter_id,
			)

		planned_scheduled_at = planned_reading.scheduled_at if planned_reading is not None and planned_reading.scheduled_at is not None else request.recorded_at

		previous_measured_reading = None
		if replacement_boundary is None:
			lookup_kwargs: dict[str, object] = {"before_scheduled_at": planned_scheduled_at}
			if planned_reading is not None and planned_reading.scheduled_at is not None:
				lookup_kwargs["before_planned_reading_id"] = request.planned_reading_id
			previous_measured_reading = self._historical_reading_repository.get_latest_measured_reading(
				request.measurement_definition_id,
				**lookup_kwargs,
				excluding_planned_reading_id=request.planned_reading_id,
			)

		calculation_result = self._calculation_service.calculate(
			measurement_definition,
			request,
			previous_measured_reading,
			replacement_boundary,
		)
		logger.debug(
			"usage_delta for %s: %s (last_reading=%s)",
			request.planned_reading_id,
			calculation_result.usage_delta,
			calculation_result.last_reading,
		)
		calculated_request = replace(
			request,
			last_reading=calculation_result.last_reading,
			usage_delta=calculation_result.usage_delta,
		)
		outcome = self._persistence_repository.upsert_reading(calculated_request)
		if (
			outcome.accepted
			and request.result_status is ResultStatus.MEASURED
			and request.raw_reading is not None
			and planned_reading is not None
		):
			self._persistence_repository.recalculate_following_measured_reading(
				request.measurement_definition_id,
				after_scheduled_at=planned_scheduled_at,
				after_planned_reading_id=request.planned_reading_id,
			)
		return outcome


__all__ = [
	"ReadingWriteOrchestrationError",
	"ReadingWriteOrchestrationService",
]
