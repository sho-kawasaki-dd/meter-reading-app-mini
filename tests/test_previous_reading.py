from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    HistoricalMeasuredReadingRecord,
    PlannedReadingRecord,
    ReplacementBoundaryRecord,
)
from meter_reading_mini.core.previous_reading import (
    CalculationContext,
    PreviousReadingSource,
    describe_initial_previous_reading,
    resolve_calculation_context,
)

SCHEDULED_AT = datetime(2026, 2, 4, 6, 0, tzinfo=UTC)
PREVIOUS_AT = datetime(2026, 2, 4, 5, 0, tzinfo=UTC)


class FakeHistory:
    def __init__(self, previous: HistoricalMeasuredReadingRecord | None) -> None:
        self.previous = previous
        self.calls: list[dict[str, Any]] = []

    def get_latest_measured_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> HistoricalMeasuredReadingRecord | None:
        self.calls.append(
            {
                "definition": measurement_definition_id,
                "meter": meter_id,
                "before": before_scheduled_at,
                "before_id": before_planned_reading_id,
                "excluding": excluding_planned_reading_id,
            }
        )
        return self.previous

    def list_following_measured_readings(self, *args: object, **kwargs: object) -> list[Any]:
        raise NotImplementedError

    def list_measured_readings_on_or_after(self, *args: object, **kwargs: object) -> list[Any]:
        raise NotImplementedError


class FakeBoundaries:
    def __init__(self, boundary: ReplacementBoundaryRecord | None) -> None:
        self.boundary = boundary
        self.calls: list[dict[str, Any]] = []

    def get_replacement_boundary_for_new_meter_first_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> ReplacementBoundaryRecord | None:
        self.calls.append(
            {
                "definition": measurement_definition_id,
                "meter": meter_id,
                "before": before_scheduled_at,
                "before_id": before_planned_reading_id,
                "excluding": excluding_planned_reading_id,
            }
        )
        return self.boundary


def _planned(scheduled_at: datetime | None = SCHEDULED_AT) -> PlannedReadingRecord:
    return PlannedReadingRecord(
        planned_reading_id="plan-target",
        measurement_definition_id="definition-001",
        meter_id="meter-001",
        scheduled_at=scheduled_at,
    )


def _previous(scheduled_at: datetime | None = PREVIOUS_AT) -> HistoricalMeasuredReadingRecord:
    return HistoricalMeasuredReadingRecord(
        planned_reading_id="plan-previous",
        raw_reading=Decimal("12.50"),
        recorded_at=PREVIOUS_AT,
        meter_id="meter-001",
        scheduled_at=scheduled_at,
    )


def _boundary(initial: str | None) -> ReplacementBoundaryRecord:
    return ReplacementBoundaryRecord(
        replacement_event_id="replacement-001",
        measurement_definition_id="definition-001",
        new_initial_reading=Decimal(initial) if initial is not None else None,
        new_meter_id="meter-001",
    )


def _resolve(
    previous: HistoricalMeasuredReadingRecord | None,
    boundary: ReplacementBoundaryRecord | None,
) -> CalculationContext:
    return resolve_calculation_context(
        FakeHistory(previous),
        FakeBoundaries(boundary),
        _planned(),
        excluding_planned_reading_id="plan-target",
    )


def test_measured_previous_keeps_the_reading_and_its_source_slot() -> None:
    initial = describe_initial_previous_reading(_resolve(_previous(), None))

    assert initial.source is PreviousReadingSource.MEASURED
    assert initial.reading == Decimal("12.50")
    assert initial.scheduled_at == PREVIOUS_AT
    assert initial.planned_reading_id == "plan-previous"


def test_replacement_boundary_hides_previous_measured_reading() -> None:
    context = _resolve(_previous(), _boundary("95.0"))
    initial = describe_initial_previous_reading(context)

    assert context.previous is None
    assert initial.source is PreviousReadingSource.REPLACEMENT_INITIAL
    assert initial.reading == Decimal("95.0")
    assert initial.scheduled_at is None
    assert initial.planned_reading_id is None


def test_replacement_without_initial_reading_is_marked_explicitly() -> None:
    initial = describe_initial_previous_reading(_resolve(None, _boundary(None)))

    assert initial.source is PreviousReadingSource.REPLACEMENT_NO_INITIAL
    assert initial.reading is None
    assert initial.scheduled_at is None
    assert initial.planned_reading_id is None


def test_no_previous_reading_and_no_boundary_means_none() -> None:
    initial = describe_initial_previous_reading(_resolve(None, None))

    assert initial.source is PreviousReadingSource.NONE
    assert (initial.reading, initial.scheduled_at, initial.planned_reading_id) == (None, None, None)


def test_context_lookup_uses_slot_order_and_excludes_the_target_itself() -> None:
    history = FakeHistory(None)
    boundaries = FakeBoundaries(None)

    resolve_calculation_context(
        history, boundaries, _planned(), excluding_planned_reading_id="plan-target"
    )

    expected = {
        "definition": "definition-001",
        "meter": "meter-001",
        "before": SCHEDULED_AT,
        "before_id": "plan-target",
        "excluding": "plan-target",
    }
    assert history.calls == [expected]
    assert boundaries.calls == [expected]


def test_missing_schedule_information_is_rejected() -> None:
    with pytest.raises(ValidationError, match="予定日時"):
        resolve_calculation_context(FakeHistory(None), FakeBoundaries(None), _planned(None))
    with pytest.raises(ValidationError, match="前回実測"):
        describe_initial_previous_reading(_resolve(_previous(None), None))
