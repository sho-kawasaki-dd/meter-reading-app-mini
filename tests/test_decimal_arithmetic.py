from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest

from meter_reading_mini.core.contract_models import CalculationMethod, ValueSchema
from meter_reading_mini.core.decimal_arithmetic import (
    exact_add,
    exact_multiply,
    exact_subtract,
)
from meter_reading_mini.core.errors import NegativeUsageDeltaError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    HistoricalMeasuredReadingRecord,
    MeasurementDefinitionRecord,
)
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService

BIG = "9" * 40


def _definition(
    *, multiplier: str = "1", rollover_value: str | None = None
) -> MeasurementDefinitionRecord:
    return MeasurementDefinitionRecord(
        measurement_definition_id="md-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        calculation_method=CalculationMethod.DELTA,
        multiplier=Decimal(multiplier),
        rollover_value=Decimal(rollover_value) if rollover_value is not None else None,
    )


def _previous(value: str) -> HistoricalMeasuredReadingRecord:
    return HistoricalMeasuredReadingRecord(
        planned_reading_id="plan-prev",
        raw_reading=Decimal(value),
        recorded_at=datetime(2026, 4, 1, 7, 0, tzinfo=UTC),
    )


def test_exact_operations_keep_scale_rules_and_trailing_zeros() -> None:
    assert format(exact_add(Decimal("1.50"), Decimal("2.255")), "f") == "3.755"
    assert format(exact_subtract(Decimal("5.100"), Decimal("1.1")), "f") == "4.000"
    assert format(exact_multiply(Decimal("1.50"), Decimal("2.0")), "f") == "3.000"
    assert format(exact_subtract(Decimal("1.0"), Decimal("1.00")), "f") == "0.00"


def test_exact_operations_do_not_round_beyond_28_digits() -> None:
    assert format(exact_add(Decimal(BIG), Decimal("1")), "f") == "1" + "0" * 40
    assert format(exact_subtract(Decimal("1" + "0" * 40), Decimal("1")), "f") == BIG
    assert format(exact_multiply(Decimal(BIG), Decimal(BIG)), "f") == str(int(BIG) ** 2)
    precise = exact_multiply(Decimal("1." + "1" * 30), Decimal("1." + "1" * 30))
    assert precise.as_tuple().exponent == -60
    assert exact_add(Decimal("0." + "1" * 35), Decimal("1")) == Decimal("1." + "1" * 35)


def test_exact_operations_normalize_positive_exponent_inputs() -> None:
    assert format(exact_add(Decimal("1E+3"), Decimal("1")), "f") == "1001"
    assert format(exact_multiply(Decimal("1E+30"), Decimal("2")), "f") == "2" + "0" * 30


def test_exact_operations_reject_non_finite_values() -> None:
    for value in (Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            exact_add(value, Decimal("1"))


def test_exact_operations_ignore_caller_decimal_precision() -> None:
    expected = exact_multiply(Decimal("1." + "1" * 30), Decimal("3"))
    with localcontext() as context:
        context.prec = 5
        assert exact_multiply(Decimal("1." + "1" * 30), Decimal("3")) == expected
        assert format(exact_subtract(Decimal(BIG), Decimal("1")), "f") == "9" * 39 + "8"
        assert context.prec == 5


def test_usage_calculation_is_exact_at_and_beyond_28_digits() -> None:
    service = UsageDeltaCalculationService()
    boundary = "9" * 28
    beyond = "9" * 30 + ".123456789"

    at_boundary = service.calculate_measured_reading(
        _definition(multiplier="1.5"), Decimal(boundary), _previous("1")
    )
    assert format(at_boundary.usage_delta or Decimal(0), "f") == "14999999999999999999999999997.0"

    large = service.calculate_measured_reading(
        _definition(multiplier="1234567890.123456789"), Decimal(beyond), _previous("0.000000001")
    )
    with localcontext() as context:
        context.prec = 200
        expected = (Decimal(beyond) - Decimal("0.000000001")) * Decimal("1234567890.123456789")
    assert large.usage_delta == expected
    assert large.usage_delta is not None
    assert large.usage_delta.as_tuple().exponent == -18


def test_usage_calculation_scales_for_mixed_decimal_places_and_rollover() -> None:
    service = UsageDeltaCalculationService()

    mixed = service.calculate_measured_reading(
        _definition(multiplier="2.50"), Decimal("12.3"), _previous("10.125")
    )
    assert mixed.usage_delta is not None
    assert format(mixed.usage_delta, "f") == "5.43750"

    rolled = service.calculate_measured_reading(
        _definition(multiplier="10", rollover_value="1" + "0" * 30),
        Decimal("5.5"),
        _previous("9" * 30),
    )
    assert rolled.usage_delta is not None
    assert format(rolled.usage_delta, "f") == "65.0"
    assert rolled.last_reading == Decimal("9" * 30)


def test_usage_calculation_still_rejects_negative_corrected_delta() -> None:
    service = UsageDeltaCalculationService()

    with pytest.raises(NegativeUsageDeltaError):
        service.calculate_measured_reading(
            _definition(rollover_value="100"), Decimal("5"), _previous("200")
        )
