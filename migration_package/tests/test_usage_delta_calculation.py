# pyright: reportMissingImports=false

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from meter_reading_mini.core.contract_models import CalculationMethod, ResultStatus, ValueSchema
from meter_reading_mini.core.errors import NegativeUsageDeltaError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    HistoricalMeasuredReadingRecord,
    MeasurementDefinitionRecord,
    ReadingUpsertRequest,
    ReplacementBoundaryRecord,
    ResultSource,
)
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService


def build_request(
    *, raw_reading: str | None, result_status: ResultStatus = ResultStatus.MEASURED
) -> ReadingUpsertRequest:
    return ReadingUpsertRequest(
        planned_reading_id="plan-001",
        measurement_definition_id="md-001",
        result_status=result_status,
        recorded_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
        client_updated_at=datetime(2026, 4, 1, 8, 5, tzinfo=UTC),
        raw_reading=Decimal(raw_reading) if raw_reading is not None else None,
        structured_payload_json=None,
        updated_by="android-01",
        signature="sig-001",
        note=None,
        device_id="sheet-001",
        result_source=ResultSource.MOBILE_HTML,
    )


def test_calculator_returns_zero_for_first_delta_measurement() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        calculation_method=CalculationMethod.DELTA,
        multiplier=Decimal("1.0"),
    )

    result = service.calculate(definition, build_request(raw_reading="10.5"), None)

    assert result.last_reading is None
    assert result.usage_delta == Decimal("0")


def test_calculator_subtracts_previous_measured_reading() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        calculation_method=CalculationMethod.DELTA,
        multiplier=Decimal("1.0"),
    )
    previous = HistoricalMeasuredReadingRecord(
        planned_reading_id="plan-prev",
        raw_reading=Decimal("10.5"),
        recorded_at=datetime(2026, 4, 1, 7, 0, tzinfo=UTC),
    )

    result = service.calculate(definition, build_request(raw_reading="13.0"), previous)

    assert result.last_reading == Decimal("10.5")
    assert result.usage_delta == Decimal("2.5")


def test_calculator_applies_rollover_and_multiplier() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        calculation_method=CalculationMethod.DELTA,
        multiplier=Decimal("2.0"),
        rollover_value=Decimal("1000"),
    )
    previous = HistoricalMeasuredReadingRecord(
        planned_reading_id="plan-prev",
        raw_reading=Decimal("990"),
        recorded_at=datetime(2026, 4, 1, 7, 0, tzinfo=UTC),
    )

    result = service.calculate(definition, build_request(raw_reading="5"), previous)

    assert result.last_reading == Decimal("990")
    assert result.usage_delta == Decimal("30")


def test_calculator_returns_null_delta_for_raw_only() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
        calculation_method=CalculationMethod.RAW_ONLY,
    )
    previous = HistoricalMeasuredReadingRecord(
        planned_reading_id="plan-prev",
        raw_reading=Decimal("90"),
        recorded_at=datetime(2026, 4, 1, 7, 0, tzinfo=UTC),
    )

    result = service.calculate(definition, build_request(raw_reading=None), previous)

    assert result.last_reading is None
    assert result.usage_delta is None


def test_calculator_returns_null_delta_for_non_measured_status() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        calculation_method=CalculationMethod.DELTA,
    )
    previous = HistoricalMeasuredReadingRecord(
        planned_reading_id="plan-prev",
        raw_reading=Decimal("10.5"),
        recorded_at=datetime(2026, 4, 1, 7, 0, tzinfo=UTC),
    )

    result = service.calculate(
        definition,
        build_request(raw_reading=None, result_status=ResultStatus.SKIPPED),
        previous,
    )

    assert result.last_reading is None
    assert result.usage_delta is None


def test_calculator_uses_new_initial_reading_from_boundary_when_no_previous_measured() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        calculation_method=CalculationMethod.DELTA,
        multiplier=Decimal("1.0"),
    )
    boundary = ReplacementBoundaryRecord(
        replacement_event_id="replace-001",
        measurement_definition_id="md-001",
        new_initial_reading=Decimal("5.0"),
    )

    result = service.calculate(definition, build_request(raw_reading="8.0"), None, boundary)

    assert result.last_reading == Decimal("5.0")
    assert result.usage_delta == Decimal("3.0")


def test_calculator_ignores_boundary_when_previous_measured_exists() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        calculation_method=CalculationMethod.DELTA,
        multiplier=Decimal("1.0"),
    )
    previous = HistoricalMeasuredReadingRecord(
        planned_reading_id="plan-prev",
        raw_reading=Decimal("10.0"),
        recorded_at=datetime(2026, 4, 1, 7, 0, tzinfo=UTC),
    )
    boundary = ReplacementBoundaryRecord(
        replacement_event_id="replace-001",
        measurement_definition_id="md-001",
        new_initial_reading=Decimal("5.0"),
    )

    result = service.calculate(definition, build_request(raw_reading="12.0"), previous, boundary)

    assert result.last_reading == Decimal("10.0")
    assert result.usage_delta == Decimal("2.0")


def test_calculator_falls_back_to_zero_when_boundary_has_no_new_initial_reading() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        calculation_method=CalculationMethod.DELTA,
        multiplier=Decimal("1.0"),
    )
    boundary = ReplacementBoundaryRecord(
        replacement_event_id="replace-001",
        measurement_definition_id="md-001",
        new_initial_reading=None,
    )

    result = service.calculate(definition, build_request(raw_reading="8.0"), None, boundary)

    assert result.last_reading is None
    assert result.usage_delta == Decimal("0")


def test_calculator_applies_rollover_with_boundary_new_initial_reading() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        calculation_method=CalculationMethod.DELTA,
        multiplier=Decimal("2.0"),
        rollover_value=Decimal("1000"),
    )
    boundary = ReplacementBoundaryRecord(
        replacement_event_id="replace-001",
        measurement_definition_id="md-001",
        new_initial_reading=Decimal("990"),
    )

    result = service.calculate(definition, build_request(raw_reading="5"), None, boundary)

    assert result.last_reading == Decimal("990")
    assert result.usage_delta == Decimal("30")


def test_calculator_rejects_negative_delta_without_rollover() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
    )
    previous = HistoricalMeasuredReadingRecord(
        planned_reading_id="plan-prev",
        raw_reading=Decimal("10"),
        recorded_at=datetime(2026, 4, 1, 7, 0, tzinfo=UTC),
    )

    with pytest.raises(NegativeUsageDeltaError):
        service.calculate(definition, build_request(raw_reading="9"), previous)


def test_calculator_rejects_negative_delta_from_replacement_initial_without_rollover() -> None:
    service = UsageDeltaCalculationService()
    definition = MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
    )
    boundary = ReplacementBoundaryRecord(
        replacement_event_id="replace-001",
        measurement_definition_id="md-001",
        new_initial_reading=Decimal("10"),
    )

    with pytest.raises(NegativeUsageDeltaError):
        service.calculate(definition, build_request(raw_reading="9"), None, boundary)
