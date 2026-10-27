"""usage_delta の純粋計算ロジック。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from meter_reading_mini.core.contract_models import CalculationMethod, ResultStatus
from meter_reading_mini.core.errors import NegativeUsageDeltaError
from meter_reading_mini.core.meter_reading_sync_repositories import (
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

        if (
            previous_measured_reading is None
            and replacement_boundary is not None
            and replacement_boundary.new_initial_reading is not None
        ):
            last_reading = replacement_boundary.new_initial_reading
            delta = _calculate_delta(
                reading_request.raw_reading,
                last_reading,
                measurement_definition.rollover_value,
            )
            return UsageDeltaCalculationResult(
                last_reading=last_reading,
                usage_delta=delta * measurement_definition.multiplier,
            )

        if previous_measured_reading is None:
            return UsageDeltaCalculationResult(last_reading=None, usage_delta=Decimal("0"))

        last_reading = previous_measured_reading.raw_reading
        delta = _calculate_delta(
            reading_request.raw_reading,
            last_reading,
            measurement_definition.rollover_value,
        )

        return UsageDeltaCalculationResult(
            last_reading=last_reading,
            usage_delta=delta * measurement_definition.multiplier,
        )


def _calculate_delta(
    raw_reading: Decimal,
    last_reading: Decimal,
    rollover_value: Decimal | None,
) -> Decimal:
    delta = raw_reading - last_reading
    if delta >= 0:
        return delta
    if rollover_value is None:
        raise NegativeUsageDeltaError(
            f"前回値 {last_reading} より小さい今回値 {raw_reading} は、"
            "ロールオーバー値未設定のため使用量を計算できません。"
        )

    corrected_delta = (rollover_value - last_reading) + raw_reading
    if corrected_delta < 0:
        raise NegativeUsageDeltaError(
            f"今回値 {raw_reading} と前回値 {last_reading} から使用量を計算できません。"
        )
    return corrected_delta


__all__ = [
    "UsageDeltaCalculationResult",
    "UsageDeltaCalculationService",
]
