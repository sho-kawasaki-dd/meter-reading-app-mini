"""usage_delta の純粋計算ロジック。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from api.models import CalculationMethod, ResultStatus
from application.meter_reading_sync_repositories import (
	HistoricalMeasuredReadingRecord,
	MeasurementDefinitionRecord,
	ReadingUpsertRequest,
	ReplacementBoundaryRecord,
)


@dataclass(frozen=True, slots=True)
class UsageDeltaCalculationResult:
	"""計算済み usage_delta と監査用 last_reading。"""

	last_reading: Decimal | None
	usage_delta: Decimal | None


class UsageDeltaCalculationService:
	"""measurement definition の計算規則に従って差分を確定する。"""

	def calculate(
		self,
		measurement_definition: MeasurementDefinitionRecord,
		reading_request: ReadingUpsertRequest,
		previous_measured_reading: HistoricalMeasuredReadingRecord | None,
		replacement_boundary: ReplacementBoundaryRecord | None = None,
	) -> UsageDeltaCalculationResult:
		"""resolved 済み reading request から確定差分を返す。"""

		if reading_request.result_status is not ResultStatus.MEASURED:
			return UsageDeltaCalculationResult(last_reading=None, usage_delta=None)

		if reading_request.raw_reading is None:
			return UsageDeltaCalculationResult(last_reading=None, usage_delta=None)

		if measurement_definition.calculation_method is CalculationMethod.RAW_ONLY:
			return UsageDeltaCalculationResult(last_reading=None, usage_delta=None)

		if previous_measured_reading is None and replacement_boundary is not None and replacement_boundary.new_initial_reading is not None:
			last_reading = replacement_boundary.new_initial_reading
			delta = reading_request.raw_reading - last_reading
			if delta < 0 and measurement_definition.rollover_value is not None:
				delta = (measurement_definition.rollover_value - last_reading) + reading_request.raw_reading
			return UsageDeltaCalculationResult(
				last_reading=last_reading,
				usage_delta=delta * measurement_definition.multiplier,
			)

		if previous_measured_reading is None:
			return UsageDeltaCalculationResult(last_reading=None, usage_delta=Decimal("0"))

		last_reading = previous_measured_reading.raw_reading
		delta = reading_request.raw_reading - last_reading
		if delta < 0 and measurement_definition.rollover_value is not None:
			delta = (measurement_definition.rollover_value - last_reading) + reading_request.raw_reading

		return UsageDeltaCalculationResult(
			last_reading=last_reading,
			usage_delta=delta * measurement_definition.multiplier,
		)


__all__ = [
	"UsageDeltaCalculationResult",
	"UsageDeltaCalculationService",
]
