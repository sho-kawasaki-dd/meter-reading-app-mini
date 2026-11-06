from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest

from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.core.aggregation_contracts import (
    AggregationBaselineSource,
    AggregationCode,
    AggregationCycleGroup,
    AggregationDetailSource,
    AggregationFormulaCreateInput,
    AggregationFormulaTerm,
    AggregationFormulaVersionInput,
    AggregationPeriodUnit,
    AggregationResult,
    AggregationTargetType,
)
from meter_reading_mini.core.aggregation_formulas import AggregationFormulaService
from meter_reading_mini.core.assignment_calculation_settings import (
    AssignmentCalculationCorrectionInput,
)
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ReadingSession,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import FormulaValidationError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    PlannedReadingCreateRequest,
    StaffCreateRequest,
)
from meter_reading_mini.core.meter_replacement_service import (
    MeterReplacementCorrectionInput,
    MeterReplacementInput,
)
from meter_reading_mini.core.reports import AggregationReportService
from meter_reading_mini.db.connection import connect, read_transaction, transaction
from meter_reading_mini.db.repositories import SQLiteRepository

NOW = datetime(2026, 3, 15, 12, tzinfo=UTC)


class Scenario:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repository = SQLiteRepository(conn, clock=lambda: NOW)
        self.context: AppContext = create_app_context(conn, clock=lambda: NOW)
        self.staff_id = self.repository.create_staff(StaffCreateRequest("試験担当")).staff_id
        self.locations: dict[str, str] = {}
        self.meters: dict[str, str] = {}
        self.times: dict[str, datetime] = {}

    def meter(self, meter_id: str, location_id: str) -> None:
        self.repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=meter_id,
                location_id=location_id,
                location_name=location_id,
                meter_name=meter_id,
                category=Category.ELECTRICITY,
            )
        )

    def definition(
        self,
        definition_id: str,
        *,
        cycle: ReadingCycle = ReadingCycle.DAILY_1X,
        purpose: Purpose = Purpose.OPERATIONAL,
        calculation: CalculationMethod = CalculationMethod.DELTA,
        schema: ValueSchema = ValueSchema.DECIMAL_STRING,
    ) -> None:
        location, meter = f"location-{definition_id}", f"meter-{definition_id}"
        self.meter(meter, location)
        self.repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id=definition_id,
                location_id=location,
                display_name=f"測定対象 {definition_id}",
                category=Category.ELECTRICITY,
                unit="kWh",
                value_schema=schema,
                multiplier=Decimal("1"),
                decimal_places=None,
                warning_threshold=None,
                rollover_value=None,
                calculation_method=calculation,
                reading_cycle=cycle,
                purpose=purpose,
                generation_start_date=date(2025, 1, 1),
                initial_schedule_json=None,
                initial_meter_id=meter,
            )
        )
        self.locations[definition_id] = location
        self.meters[definition_id] = meter

    def plan(
        self,
        plan_id: str,
        definition_id: str,
        business_date: date,
        *,
        at: datetime | None = None,
        meter_id: str | None = None,
        value: str | None = None,
        status: ResultStatus = ResultStatus.MEASURED,
    ) -> None:
        scheduled_at = at or datetime.combine(business_date, time(3, tzinfo=UTC))
        self.repository.create_planned_reading_if_missing(
            PlannedReadingCreateRequest(
                planned_reading_id=plan_id,
                measurement_definition_id=definition_id,
                meter_id=meter_id or self.meters[definition_id],
                location_id=self.locations[definition_id],
                business_date=business_date,
                release_date=business_date,
                reading_session=ReadingSession.SESSION_0800,
                scheduled_at=scheduled_at,
            )
        )
        self.times[plan_id] = scheduled_at
        if value is not None or status is not ResultStatus.MEASURED:
            self.save(plan_id, value, status)

    def save(
        self, plan_id: str, value: str | None, status: ResultStatus = ResultStatus.MEASURED
    ) -> None:
        self.context.reading_writer.save_manual_reading(
            ManualReadingInput(
                plan_id,
                status,
                self.times[plan_id],
                Decimal(value) if value is not None else None,
                None,
                "試験",
            ),
            staff_id=self.staff_id,
        )

    def formula_service(
        self, formula_id: str, *, registered: date = date(2026, 2, 10)
    ) -> AggregationFormulaService:
        return AggregationFormulaService(
            self.repository,
            self.repository,
            transaction_factory=partial(transaction, self.conn),
            current_business_date_provider=lambda: registered,
            clock=lambda: NOW,
            id_provider=lambda: formula_id,
        )

    def formula(
        self,
        formula_id: str,
        terms: tuple[tuple[str, str], ...],
        *,
        cycle: AggregationCycleGroup = AggregationCycleGroup.DAILY,
        purpose: Purpose = Purpose.OPERATIONAL,
        effective: date = date(2026, 2, 1),
        registered: date = date(2026, 2, 10),
    ) -> AggregationFormulaService:
        service = self.formula_service(formula_id, registered=registered)
        service.create_formula(
            AggregationFormulaCreateInput(
                display_name=formula_id,
                unit="kWh",
                category=Category.ELECTRICITY,
                cycle_group=cycle,
                purpose=purpose,
                decimal_places=None,
                initial_version=self.version(effective, terms),
            )
        )
        return service

    @staticmethod
    def version(
        effective: date, terms: tuple[tuple[str, str], ...], *, active: bool = True
    ) -> AggregationFormulaVersionInput:
        return AggregationFormulaVersionInput(
            effective,
            active,
            tuple(
                AggregationFormulaTerm(definition_id, Decimal(coefficient), index)
                for index, (definition_id, coefficient) in enumerate(terms)
            ),
        )

    def result(
        self,
        target_id: str,
        start: date,
        end: date,
        *,
        unit: AggregationPeriodUnit = AggregationPeriodUnit.DAY,
        purpose: Purpose = Purpose.OPERATIONAL,
        location_id: str | None = None,
    ) -> AggregationResult:
        report = self.context.aggregation_reports.get_report(
            start, end, unit=unit, purpose=purpose, location_id=location_id
        )
        assert len(report.periods) == 1
        return next(result for result in report.periods[0].results if result.target_id == target_id)


@pytest.fixture
def scenario(memory_conn: sqlite3.Connection, sqlite_repository: SQLiteRepository) -> Scenario:
    return Scenario(memory_conn)


def test_exact_formula_purpose_location_and_direct_series(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.definition("b", cycle=ReadingCycle.DAILY_3X, purpose=Purpose.BILLING)
    for definition_id in ("a", "b"):
        scenario.plan(f"{definition_id}-base", definition_id, date(2026, 2, 9), value="0")
    large = "123456789012345678901234567890.000001"
    scenario.plan("a-day", "a", date(2026, 2, 10), value=large)
    scenario.plan("b-day", "b", date(2026, 2, 10), value="10")
    scenario.formula("formula", (("a", "1.25"), ("b", "-0.5")))
    before = scenario.conn.total_changes

    report = scenario.context.aggregation_reports.get_report(
        date(2026, 2, 10), date(2026, 2, 11), location_id="location-b"
    )

    assert scenario.conn.total_changes == before
    assert [result.target_id for result in report.periods[0].results] == ["formula"]
    result = report.periods[0].results[0]
    assert result.target_type is AggregationTargetType.FORMULA
    assert result.value == Decimal("154320986265432098626543209857.50000125")
    assert result.location_ids == ("location-a", "location-b")
    assert result.information_codes == (
        AggregationCode.INFO_CARRYOVER,
        AggregationCode.INFO_RETROACTIVE_FORMULA,
        AggregationCode.INFO_MULTI_LOCATION,
    )
    assert not result.is_reference
    assert [
        (detail.measurement_definition_id, detail.coefficient) for detail in result.details
    ] == [
        ("a", Decimal("1.25")),
        ("b", Decimal("-0.5")),
    ]
    assert {detail.baseline.source for detail in result.details if detail.baseline} == {
        AggregationBaselineSource.READING
    }

    operational = scenario.context.aggregation_reports.get_report(
        date(2026, 2, 10), date(2026, 2, 11)
    )
    assert [item.target_id for item in operational.periods[0].results] == ["a", "formula"]
    billing = scenario.context.aggregation_reports.get_report(
        date(2026, 2, 10), date(2026, 2, 11), purpose=Purpose.BILLING
    )
    assert [item.target_id for item in billing.periods[0].results] == ["b"]
    assert operational.periods[0].results[0].uses_current_attributes


def test_carryover_gap_auto_closed_and_late_recalculation(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 2, 1), value="100")
    scenario.plan("gap", "a", date(2026, 2, 2))
    scenario.conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed' WHERE planned_reading_id = 'gap'"
    )
    scenario.plan("last", "a", date(2026, 2, 3), value="130")
    start, end = date(2026, 2, 3), date(2026, 2, 4)

    result = scenario.result("a", start, end)
    assert result.value == 30
    assert result.missing_codes == (AggregationCode.MISSING_GAP_BETWEEN_BASELINE,)
    assert AggregationCode.INFO_CARRYOVER not in result.information_codes
    assert result.issues[0].related_ids == ("gap",)

    scenario.save("gap", "110")
    result = scenario.result("a", start, end)
    assert result.value == 20
    assert not result.is_reference
    assert result.information_codes == (AggregationCode.INFO_CARRYOVER,)
    assert result.details[0].baseline is not None
    assert result.details[0].baseline.source_id == "gap"
    assert scenario.conn.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'gap'"
    ).fetchone() == ("completed",)

    first = scenario.result("a", date(2026, 2, 1), date(2026, 2, 2))
    assert first.value == 0
    assert first.missing_codes == (AggregationCode.MISSING_NO_BASELINE,)


def test_historical_reading_corrections_change_following_aggregates(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 2, 1), value="100")
    scenario.plan("middle", "a", date(2026, 2, 2), value="110")
    scenario.plan("last", "a", date(2026, 2, 3), value="130")
    start, end = date(2026, 2, 2), date(2026, 2, 4)
    assert scenario.result("a", start, end, unit=AggregationPeriodUnit.MONTH).value == 30

    scenario.save("base", "105")
    assert scenario.result("a", start, end, unit=AggregationPeriodUnit.MONTH).value == 25
    scenario.save("middle", "115")
    result = scenario.result("a", start, end, unit=AggregationPeriodUnit.MONTH)
    assert result.value == 25
    assert [detail.source_usage for detail in result.details] == [Decimal("10"), Decimal("15")]


def test_partial_is_recalculated_for_whole_month_not_inherited_from_days(
    scenario: Scenario,
) -> None:
    scenario.definition("a", cycle=ReadingCycle.MONTHLY)
    scenario.definition("b", cycle=ReadingCycle.MONTHLY)
    scenario.plan("a-base", "a", date(2026, 1, 10), value="100")
    scenario.plan("b-base", "b", date(2026, 1, 15), value="200")
    scenario.plan("a-month", "a", date(2026, 2, 10), value="110")
    scenario.plan("b-month", "b", date(2026, 2, 15), value="220")
    scenario.formula("monthly", (("a", "1"), ("b", "1")), cycle=AggregationCycleGroup.MONTHLY)

    day = scenario.result("monthly", date(2026, 2, 10), date(2026, 2, 11))
    assert day.value == 10
    assert day.missing_definition_ids == ("b",)
    assert day.missing_codes == (AggregationCode.MISSING_PARTIAL_ITEMS,)
    assert not day.warning_codes

    month = scenario.result(
        "monthly", date(2026, 2, 1), date(2026, 3, 1), unit=AggregationPeriodUnit.MONTH
    )
    assert month.value == 30
    assert not month.missing_codes
    assert month.warning_codes == (AggregationCode.WARN_PERIOD_MISMATCH,)
    assert not month.is_reference
    assert [
        (detail.baseline.business_date, detail.business_date)
        for detail in month.details
        if detail.baseline
    ] == [
        (date(2026, 1, 10), date(2026, 2, 10)),
        (date(2026, 1, 15), date(2026, 2, 15)),
    ]


def test_no_schedule_unmeasured_and_nonmeasured_are_distinct(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.definition("b")
    scenario.formula("formula", (("a", "1"), ("b", "1")))
    start, end = date(2026, 2, 10), date(2026, 2, 11)
    empty = scenario.result("formula", start, end)
    assert empty.value is None
    assert not empty.missing_codes

    scenario.plan("a-open", "a", start)
    scenario.plan("b-skip", "b", start, status=ResultStatus.SKIPPED)
    missing = scenario.result("formula", start, end)
    assert missing.value is None
    assert not missing.is_reference
    assert missing.missing_codes == (
        AggregationCode.MISSING_UNMEASURED,
        AggregationCode.MISSING_NON_MEASURED,
    )
    assert AggregationCode.MISSING_PARTIAL_ITEMS not in missing.missing_codes

    scenario.save("a-open", "5")
    partial_result = scenario.result("formula", start, end)
    assert partial_result.value == 0
    assert partial_result.missing_definition_ids == ("b",)
    assert partial_result.missing_codes == (
        AggregationCode.MISSING_NON_MEASURED,
        AggregationCode.MISSING_PARTIAL_ITEMS,
        AggregationCode.MISSING_NO_BASELINE,
    )


def test_formula_versions_inactive_ranges_and_negative_contributions(scenario: Scenario) -> None:
    scenario.definition("a")
    for day, value in (
        (9, "100"),
        (10, "110"),
        (11, "120"),
        (12, "130"),
        (13, "140"),
        (14, "150"),
        (15, "160"),
    ):
        scenario.plan(f"plan-{day}", "a", date(2026, 2, day), value=value)
    service = scenario.formula("formula", (("a", "1"),))
    service.save_version("formula", scenario.version(date(2026, 2, 11), (("a", "-2"),)))
    service.save_version("formula", scenario.version(date(2026, 2, 13), (), active=False))
    service.save_version("formula", scenario.version(date(2026, 2, 15), (("a", "0.5"),)))

    result = scenario.result(
        "formula", date(2026, 2, 10), date(2026, 2, 16), unit=AggregationPeriodUnit.MONTH
    )
    assert result.value == Decimal("-25.0")
    assert result.is_partial_application
    assert result.warning_codes == (AggregationCode.WARN_NEGATIVE,)
    assert AggregationCode.INFO_VERSION_SWITCH in result.information_codes
    assert AggregationCode.INFO_RETROACTIVE_FORMULA in result.information_codes
    assert [(span.start, span.end) for span in result.applications] == [
        (date(2026, 2, 10), date(2026, 2, 11)),
        (date(2026, 2, 11), date(2026, 2, 13)),
        (date(2026, 2, 15), date(2026, 2, 16)),
    ]
    assert [span.contribution for span in result.applications] == [
        Decimal("10"),
        Decimal("-40"),
        Decimal("5"),
    ]
    assert [
        (detail.source_id, detail.contribution, detail.version_effective_from)
        for detail in result.details
    ] == [
        ("plan-10", Decimal("10"), date(2026, 2, 1)),
        ("plan-11", Decimal("-20"), date(2026, 2, 11)),
        ("plan-12", Decimal("-20"), date(2026, 2, 11)),
        ("plan-15", Decimal("5.0"), date(2026, 2, 15)),
    ]


def test_business_date_midnight_year_fiscal_leap_and_future(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 2, 10), value="100")
    midnight = datetime(2026, 2, 11, 15, tzinfo=UTC)
    scenario.plan("midnight", "a", date(2026, 2, 11), at=midnight, value="123")
    result = scenario.result("a", date(2026, 2, 11), date(2026, 2, 12))
    assert result.value == 23
    assert result.details[0].business_date == date(2026, 2, 11)
    assert scenario.result("a", date(2026, 2, 12), date(2026, 2, 13)).value is None

    service = scenario.context.aggregation_reports
    fiscal = service.get_report(
        date(2024, 2, 29), date(2026, 4, 2), unit=AggregationPeriodUnit.FISCAL_YEAR
    )
    assert [(period.nominal_start, period.nominal_end) for period in fiscal.periods] == [
        (date(2023, 4, 1), date(2024, 4, 1)),
        (date(2024, 4, 1), date(2025, 4, 1)),
        (date(2025, 4, 1), date(2026, 4, 1)),
    ]
    assert fiscal.periods[0].start == date(2024, 2, 29)
    assert fiscal.periods[-1].end == date(2026, 3, 16)
    assert fiscal.periods[-1].is_in_progress
    calendar = service.get_report(
        date(2025, 12, 31), date(2026, 4, 2), unit=AggregationPeriodUnit.CALENDAR_YEAR
    )
    assert [(period.nominal_start, period.nominal_end) for period in calendar.periods] == [
        (date(2025, 1, 1), date(2026, 1, 1)),
        (date(2026, 1, 1), date(2027, 1, 1)),
    ]
    assert service.get_report(date(2026, 3, 16), date(2026, 3, 20)).periods == ()


def test_fiscal_boundary_and_monthly_progress_are_clipped_to_business_date(
    scenario: Scenario,
) -> None:
    scenario.definition("a")
    service = AggregationReportService(
        scenario.repository,
        read_transaction_factory=partial(read_transaction, scenario.conn),
        current_business_date_provider=lambda: date(2026, 4, 15),
    )
    fiscal = service.get_report(
        date(2026, 3, 31), date(2026, 4, 2), unit=AggregationPeriodUnit.FISCAL_YEAR
    )
    assert [(period.start, period.end) for period in fiscal.periods] == [
        (date(2026, 3, 31), date(2026, 4, 1)),
        (date(2026, 4, 1), date(2026, 4, 2)),
    ]
    assert not fiscal.periods[0].is_in_progress
    assert not fiscal.periods[1].is_in_progress
    monthly = service.get_report(
        date(2026, 4, 1), date(2026, 6, 1), unit=AggregationPeriodUnit.MONTH
    )
    assert len(monthly.periods) == 1
    assert monthly.periods[0].end == date(2026, 4, 16)
    assert monthly.periods[0].nominal_end == date(2026, 5, 1)
    assert monthly.periods[0].is_in_progress


def test_timezone_and_daylight_saving_use_stored_business_dates(scenario: Scenario) -> None:
    scenario.context.timezone_settings.save_timezone("America/New_York")
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 3, 7), value="100")
    scenario.plan(
        "before-dst",
        "a",
        date(2026, 3, 8),
        at=datetime(2026, 3, 8, 6, tzinfo=UTC),
        value="110",
    )
    scenario.plan(
        "after-dst",
        "a",
        date(2026, 3, 8),
        at=datetime(2026, 3, 8, 7, tzinfo=UTC),
        value="120",
    )
    result = scenario.result("a", date(2026, 3, 8), date(2026, 3, 9))
    assert result.value == 20
    assert [detail.business_date for detail in result.details] == [date(2026, 3, 8)] * 2
    assert not result.is_reference

    before_midnight = create_app_context(
        scenario.conn, clock=lambda: datetime(2026, 3, 16, 3, 30, tzinfo=UTC)
    )
    after_midnight = create_app_context(
        scenario.conn, clock=lambda: datetime(2026, 3, 16, 4, 30, tzinfo=UTC)
    )
    start, end = date(2026, 3, 15), date(2026, 3, 17)
    assert before_midnight.aggregation_reports.get_report(start, end).as_of == date(2026, 3, 15)
    assert after_midnight.aggregation_reports.get_report(start, end).as_of == date(2026, 3, 16)


def test_same_scheduled_time_uses_planned_id_and_detects_gap(scenario: Scenario) -> None:
    scenario.definition("a", cycle=ReadingCycle.DAILY_3X)
    at = datetime(2026, 2, 10, 3, tzinfo=UTC)
    scenario.plan("z-last", "a", date(2026, 2, 10), at=at, value="130")
    scenario.plan("a-base", "a", date(2026, 2, 9), at=at, value="100")
    scenario.plan("m-gap", "a", date(2026, 2, 9), at=at)
    start, end = date(2026, 2, 10), date(2026, 2, 11)
    result = scenario.result("a", start, end)
    assert result.value == 30
    assert result.missing_codes == (AggregationCode.MISSING_GAP_BETWEEN_BASELINE,)
    assert result.issues[0].related_ids == ("m-gap",)
    scenario.save("m-gap", "110")
    result = scenario.result("a", start, end)
    assert result.value == 20
    assert result.details[0].baseline is not None
    assert result.details[0].baseline.source_id == "m-gap"
    assert result.information_codes == (AggregationCode.INFO_CARRYOVER,)


def test_nonmeasured_between_outside_period_marks_gap(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 2, 8), value="100")
    scenario.plan("skipped", "a", date(2026, 2, 9), status=ResultStatus.UNREACHABLE)
    scenario.plan("last", "a", date(2026, 2, 10), value="110")
    result = scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))
    assert result.value == 10
    assert result.missing_codes == (AggregationCode.MISSING_GAP_BETWEEN_BASELINE,)
    assert result.issues[0].related_ids == ("skipped",)


def test_superseded_slots_neither_contribute_nor_create_gaps(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 2, 9), value="100")
    scenario.plan(
        "superseded",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 2, tzinfo=UTC),
        status=ResultStatus.SKIPPED,
    )
    scenario.conn.execute(
        "UPDATE planned_readings SET task_state = 'superseded', "
        "close_reason = 'schedule_recomposed' WHERE planned_reading_id = 'superseded'"
    )
    scenario.plan(
        "last",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 4, tzinfo=UTC),
        value="110",
    )
    result = scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))
    assert result.value == 10
    assert [detail.source_id for detail in result.details] == ["last"]
    assert not result.missing_codes
    assert result.information_codes == (AggregationCode.INFO_CARRYOVER,)


def test_formula_location_selection_uses_all_versions_in_requested_range(
    scenario: Scenario,
) -> None:
    scenario.definition("a")
    scenario.definition("b")
    scenario.plan("a-base", "a", date(2026, 2, 9), value="10")
    scenario.plan("b-base", "b", date(2026, 2, 9), value="20")
    scenario.plan("a-current", "a", date(2026, 2, 10), value="15")
    scenario.plan("b-current", "b", date(2026, 2, 11), value="27")
    service = scenario.formula("formula", (("a", "1"),))
    service.save_version("formula", scenario.version(date(2026, 2, 11), (("b", "1"),)))
    report = scenario.context.aggregation_reports.get_report(
        date(2026, 2, 10), date(2026, 2, 12), location_id="location-b"
    )
    assert [
        next(result.value for result in period.results if result.target_id == "formula")
        for period in report.periods
    ] == [Decimal("5"), Decimal("7")]


def test_replacement_is_counted_once_and_correction_recalculates(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.plan("before", "a", date(2026, 2, 9), value="100")
    replacement = scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("110"),
            new_initial_reading=Decimal("5"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan(
        "after",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 4, tzinfo=UTC),
        meter_id="new",
        value="8",
    )
    start, end = date(2026, 2, 10), date(2026, 2, 11)
    result = scenario.result("a", start, end)
    assert result.value == 13
    assert not result.is_reference
    assert [detail.source for detail in result.details] == [
        AggregationDetailSource.REPLACEMENT,
        AggregationDetailSource.READING,
    ]
    assert [detail.source_usage for detail in result.details] == [Decimal("10"), Decimal("3")]
    assert result.details[1].baseline is not None
    assert result.details[1].baseline.source is AggregationBaselineSource.REPLACEMENT_INITIAL
    assert AggregationCode.INFO_REPLACEMENT in result.information_codes

    scenario.context.meter_replacement.correct_replacement(
        MeterReplacementCorrectionInput(
            replacement.replacement_event_id,
            Decimal("112"),
            Decimal("6"),
            scenario.staff_id,
        )
    )
    assert scenario.result("a", start, end).value == 14


def test_consecutive_replacements_use_initial_baselines_and_recalculate(
    scenario: Scenario,
) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.meter("third", "location-a")
    scenario.plan("before", "a", date(2026, 2, 9), value="100")
    first = scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("110"),
            new_initial_reading=Decimal("5"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="new",
            new_meter_id="third",
            replaced_at=datetime(2026, 2, 10, 5, tzinfo=UTC),
            old_final_reading=Decimal("9"),
            new_initial_reading=Decimal("1"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan(
        "third-reading",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 6, tzinfo=UTC),
        meter_id="third",
        value="4",
    )
    start, end = date(2026, 2, 10), date(2026, 2, 11)
    result = scenario.result("a", start, end)
    assert result.value == 17
    assert [detail.source_usage for detail in result.details] == [
        Decimal("10"),
        Decimal("4"),
        Decimal("3"),
    ]
    assert result.details[1].baseline is not None
    assert result.details[1].baseline.source_id == first.replacement_event_id
    assert not result.is_reference

    scenario.context.meter_replacement.correct_replacement(
        MeterReplacementCorrectionInput(
            first.replacement_event_id, Decimal("110"), Decimal("6"), scenario.staff_id
        )
    )
    assert scenario.result("a", start, end).value == 16


def test_replacement_and_nonmeasured_gap_coexist(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.plan("before", "a", date(2026, 2, 9), value="100")
    scenario.plan(
        "missing",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 2, tzinfo=UTC),
        status=ResultStatus.METER_FAULT,
    )
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("110"),
            new_initial_reading=Decimal("5"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan(
        "after",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 4, tzinfo=UTC),
        meter_id="new",
        value="8",
    )
    start, end = date(2026, 2, 10), date(2026, 2, 11)
    result = scenario.result("a", start, end)
    assert result.value == 13
    assert result.missing_codes == (
        AggregationCode.MISSING_NON_MEASURED,
        AggregationCode.MISSING_GAP_BETWEEN_BASELINE,
    )
    assert AggregationCode.INFO_REPLACEMENT in result.information_codes
    scenario.save("missing", "105")
    result = scenario.result("a", start, end)
    assert result.value == 13
    assert not result.is_reference


def test_monthly_periods_remain_equal_when_one_item_has_replacement(
    scenario: Scenario,
) -> None:
    scenario.definition("a", cycle=ReadingCycle.MONTHLY)
    scenario.definition("b", cycle=ReadingCycle.MONTHLY)
    scenario.meter("new", "location-a")
    scenario.plan("a-base", "a", date(2026, 1, 10), value="100")
    scenario.plan("b-base", "b", date(2026, 1, 10), value="200")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 5, 3, tzinfo=UTC),
            old_final_reading=Decimal("110"),
            new_initial_reading=Decimal("5"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan("a-current", "a", date(2026, 2, 10), meter_id="new", value="8")
    scenario.plan("b-current", "b", date(2026, 2, 10), value="213")
    scenario.formula("monthly", (("a", "1"), ("b", "-1")), cycle=AggregationCycleGroup.MONTHLY)
    result = scenario.result(
        "monthly", date(2026, 2, 1), date(2026, 3, 1), unit=AggregationPeriodUnit.MONTH
    )
    assert result.value == 0
    assert not result.warning_codes
    assert not result.missing_codes
    assert AggregationCode.INFO_REPLACEMENT in result.information_codes
    assert len(result.details) == 3


def test_every_two_months_period_mismatch_is_a_warning(scenario: Scenario) -> None:
    scenario.definition("a", cycle=ReadingCycle.EVERY_TWO_MONTHS)
    scenario.definition("b", cycle=ReadingCycle.EVERY_TWO_MONTHS)
    scenario.plan("a-base", "a", date(2025, 12, 10), value="100")
    scenario.plan("b-base", "b", date(2025, 12, 15), value="200")
    scenario.plan("a-current", "a", date(2026, 2, 10), value="110")
    scenario.plan("b-current", "b", date(2026, 2, 15), value="220")
    scenario.formula(
        "bimonthly",
        (("a", "1"), ("b", "1")),
        cycle=AggregationCycleGroup.EVERY_TWO_MONTHS,
    )
    result = scenario.result(
        "bimonthly", date(2026, 2, 1), date(2026, 3, 1), unit=AggregationPeriodUnit.MONTH
    )
    assert result.value == 30
    assert result.warning_codes == (AggregationCode.WARN_PERIOD_MISMATCH,)
    assert not result.is_reference


def test_period_mismatch_detects_gap_between_equal_outer_boundaries(scenario: Scenario) -> None:
    scenario.definition("a", cycle=ReadingCycle.MONTHLY)
    scenario.definition("b", cycle=ReadingCycle.MONTHLY)
    scenario.meter("new", "location-a")
    scenario.plan("a-base", "a", date(2026, 1, 10), value="100")
    scenario.plan("b-base", "b", date(2026, 1, 10), value="200")
    scenario.plan("a-january", "a", date(2026, 1, 20), value="101")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 5, 3, tzinfo=UTC),
            old_final_reading=None,
            new_initial_reading=Decimal("5"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan("a-current", "a", date(2026, 2, 10), meter_id="new", value="8")
    scenario.plan("b-current", "b", date(2026, 2, 10), value="210")
    scenario.formula(
        "monthly",
        (("a", "1"), ("b", "1")),
        cycle=AggregationCycleGroup.MONTHLY,
        effective=date(2026, 1, 1),
    )

    result = scenario.result(
        "monthly",
        date(2026, 1, 11),
        date(2026, 3, 1),
        unit=AggregationPeriodUnit.CALENDAR_YEAR,
    )
    assert result.value == 14
    assert result.missing_codes == (AggregationCode.MISSING_REPLACEMENT_READING,)
    assert result.warning_codes == (AggregationCode.WARN_PERIOD_MISMATCH,)


def test_replacement_without_old_baseline_discloses_zero(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("110"),
            new_initial_reading=Decimal("5"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan(
        "after",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 4, tzinfo=UTC),
        meter_id="new",
        value="8",
    )
    result = scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))
    assert result.value == 3
    assert result.details[0].source_usage == 0
    assert result.missing_codes == (AggregationCode.MISSING_NO_BASELINE,)


@pytest.mark.parametrize(
    ("old_final", "new_initial", "expected"),
    [(None, "5", Decimal("3")), ("110", None, Decimal("10"))],
)
def test_missing_replacement_readings_preserve_calculable_part(
    scenario: Scenario, old_final: str | None, new_initial: str | None, expected: Decimal
) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.plan("before", "a", date(2026, 2, 9), value="100")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal(old_final) if old_final else None,
            new_initial_reading=Decimal(new_initial) if new_initial else None,
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan(
        "after",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 4, tzinfo=UTC),
        meter_id="new",
        value="8",
    )
    result = scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))
    assert result.value == expected
    assert result.missing_codes == (AggregationCode.MISSING_REPLACEMENT_READING,)
    assert result.is_reference


def test_pending_assignment_excludes_boundary_then_correction_updates_report(
    scenario: Scenario,
) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.plan("before", "a", date(2026, 2, 9), value="100")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("110"),
            new_initial_reading=Decimal("5"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan(
        "after",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 4, tzinfo=UTC),
        meter_id="new",
        value="8",
    )
    assignment = scenario.repository.list_meter_assignments("a")[0]
    scenario.conn.execute(
        "INSERT INTO assignment_review_flags "
        "(measurement_definition_id, meter_id, assignment_effective_from, reason, created_at) "
        "SELECT measurement_definition_id, meter_id, effective_from, '移行時の条件を確認', ? "
        "FROM meter_assignments WHERE measurement_definition_id = ? AND meter_id = ?",
        ("2026-02-10 03:00:00", "a", "meter-a"),
    )
    start, end = date(2026, 2, 10), date(2026, 2, 11)
    result = scenario.result("a", start, end)
    assert result.value == 3
    assert result.missing_codes == (AggregationCode.MISSING_ASSIGNMENT_PENDING,)
    issue = next(
        issue for issue in result.issues if issue.code is AggregationCode.MISSING_ASSIGNMENT_PENDING
    )
    assert issue.meter_id == "meter-a"
    assert issue.assignment_effective_from == assignment.effective_from
    assert "移行時の条件を確認" in issue.message
    assert issue.resolution is not None
    assert result.details[0].contribution is None

    scenario.context.assignment_calculation_settings.correct_assignment(
        AssignmentCalculationCorrectionInput(
            "a",
            "meter-a",
            assignment.effective_from,
            Decimal("2"),
            None,
            scenario.staff_id,
            "倍率を確認",
        )
    )
    corrected = scenario.result("a", start, end)
    assert corrected.value == 23
    assert not corrected.is_reference


def test_pending_new_assignment_excludes_affected_results_and_boundary(
    scenario: Scenario,
) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.plan("before", "a", date(2026, 2, 9), value="100")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("110"),
            new_initial_reading=Decimal("5"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan(
        "after",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 4, tzinfo=UTC),
        meter_id="new",
        value="8",
    )
    scenario.conn.execute(
        "INSERT INTO assignment_review_flags "
        "(measurement_definition_id, meter_id, assignment_effective_from, reason, created_at) "
        "SELECT measurement_definition_id, meter_id, effective_from, '新条件の確認待ち', ? "
        "FROM meter_assignments WHERE measurement_definition_id = ? AND meter_id = ?",
        ("2026-02-10 03:00:00", "a", "new"),
    )
    result = scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))
    assert result.value is None
    assert not result.is_reference
    assert result.missing_codes == (AggregationCode.MISSING_ASSIGNMENT_PENDING,)
    assert [detail.contribution for detail in result.details] == [None, None]
    assert all(
        issue.meter_id == "new"
        for issue in result.issues
        if issue.code is AggregationCode.MISSING_ASSIGNMENT_PENDING
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE replacement_boundaries SET old_meter_usage_delta = NULL",
        "UPDATE replacement_boundaries SET old_final_reading = NULL",
        "UPDATE replacement_boundaries SET previous_measured_reading = NULL",
        "UPDATE replacement_boundaries SET old_meter_usage_delta = '-1'",
    ],
)
def test_inconsistent_replacement_is_a_business_error(scenario: Scenario, mutation: str) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.plan("before", "a", date(2026, 2, 9), value="100")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("110"),
            new_initial_reading=Decimal("5"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.conn.execute(mutation)
    with pytest.raises(ValidationError, match="交換境界"):
        scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))


def test_overlapping_assignment_is_not_silently_aggregated(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.meter("overlap", "location-a")
    scenario.plan("before", "a", date(2026, 2, 9), value="100")
    scenario.plan("last", "a", date(2026, 2, 10), value="110")
    scenario.conn.execute(
        "INSERT INTO meter_assignments "
        "(measurement_definition_id, meter_id, effective_from, effective_to, multiplier) "
        "VALUES (?, ?, ?, ?, ?)",
        ("a", "overlap", "2026-02-10 00:00:00", "2026-02-11 00:00:00", "1"),
    )
    with pytest.raises(ValidationError, match="割当"):
        scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("unit", "m3"),
        ("category", "water"),
        ("reading_cycle", "monthly"),
        ("calculation_method", "raw_only"),
        ("value_schema", "power_factor_percent_phase"),
    ],
)
def test_formula_attributes_are_revalidated_on_report(
    scenario: Scenario, column: str, value: str
) -> None:
    scenario.definition("a")
    scenario.formula("formula", (("a", "1"),))
    scenario.conn.execute(
        f"UPDATE measurement_definitions SET {column} = ? WHERE measurement_definition_id = 'a'",
        (value,),
    )
    with pytest.raises(FormulaValidationError):
        scenario.result("formula", date(2026, 2, 10), date(2026, 2, 11))
    assert not scenario.conn.in_transaction


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE reading_transactions SET usage_delta = NULL WHERE planned_reading_id = 'last'",
        "UPDATE reading_transactions SET raw_reading = NULL WHERE planned_reading_id = 'last'",
        "UPDATE reading_transactions SET last_reading = '999' WHERE planned_reading_id = 'last'",
        "UPDATE reading_transactions SET usage_delta = '-1' WHERE planned_reading_id = 'last'",
        "UPDATE planned_readings SET task_state = 'open' WHERE planned_reading_id = 'last'",
        "UPDATE reading_transactions SET result_status = 'skipped' "
        "WHERE planned_reading_id = 'last'",
    ],
)
def test_inconsistent_reading_is_a_business_error(scenario: Scenario, mutation: str) -> None:
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 2, 9), value="100")
    scenario.plan("last", "a", date(2026, 2, 10), value="110")
    scenario.conn.execute(mutation)
    with pytest.raises(ValidationError):
        scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))
    assert not scenario.conn.in_transaction


def test_current_attributes_inactive_and_ineligible_definitions(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.definition("raw", calculation=CalculationMethod.RAW_ONLY)
    scenario.definition(
        "power",
        calculation=CalculationMethod.RAW_ONLY,
        schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
    )
    scenario.plan("base", "a", date(2026, 2, 9), value="100")
    scenario.plan("last", "a", date(2026, 2, 10), value="110")
    scenario.conn.execute(
        "UPDATE measurement_definitions SET is_active = 0, purpose = 'billing', "
        "category = 'water', unit = 'm3' WHERE measurement_definition_id = 'a'"
    )
    start, end = date(2026, 2, 10), date(2026, 2, 11)
    result = scenario.result("a", start, end, purpose=Purpose.BILLING)
    assert (result.value, result.category, result.unit) == (Decimal("10"), Category.WATER, "m3")
    assert result.information_codes == (
        AggregationCode.INFO_CARRYOVER,
        AggregationCode.INFO_DISABLED_DEFINITION,
    )
    assert result.uses_current_attributes
    for definition_id in ("raw", "power"):
        excluded = scenario.result(definition_id, start, end)
        assert excluded.value is None
        assert excluded.exclusion_reason


def test_formula_can_use_inactive_definition_history_with_information(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 2, 9), value="100")
    scenario.plan("last", "a", date(2026, 2, 10), value="110")
    scenario.formula("formula", (("a", "1"),))
    scenario.conn.execute(
        "UPDATE measurement_definitions SET is_active = 0 WHERE measurement_definition_id = 'a'"
    )
    result = scenario.result("formula", date(2026, 2, 10), date(2026, 2, 11))
    assert result.value == 10
    assert AggregationCode.INFO_DISABLED_DEFINITION in result.information_codes
    assert not result.is_reference


def test_invalid_stored_coefficient_is_not_aggregated(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.formula("formula", (("a", "1"),))
    scenario.conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        scenario.conn.execute("UPDATE aggregation_formula_terms SET coefficient = '0'")
    finally:
        scenario.conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(FormulaValidationError, match="係数"):
        scenario.result("formula", date(2026, 2, 10), date(2026, 2, 11))


def test_many_periods_preserve_exact_results_and_previous_order(scenario: Scenario) -> None:
    scenario.definition("a")
    start = date(2025, 1, 1)
    days = 2000
    plans: list[tuple[object, ...]] = []
    readings: list[tuple[object, ...]] = []
    for index in range(days):
        business_date = start + timedelta(days=index)
        timestamp = f"{business_date.isoformat()} 03:00:00"
        plan_id = f"plan-{index:04}"
        plans.append(
            (
                plan_id,
                "a",
                "meter-a",
                "location-a",
                business_date.isoformat(),
                business_date.isoformat(),
                "08:00",
                timestamp,
                "completed",
            )
        )
        readings.append(
            (
                plan_id,
                "measured",
                timestamp,
                timestamp,
                str(index),
                str(index - 1) if index else None,
                "1" if index else "0",
                "試験担当",
                "署名",
                "manual",
            )
        )
    scenario.conn.executemany(
        "INSERT INTO planned_readings "
        "(planned_reading_id, measurement_definition_id, meter_id, location_id, business_date, "
        "release_date, reading_session, scheduled_at, task_state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        plans,
    )
    scenario.conn.executemany(
        "INSERT INTO reading_transactions "
        "(planned_reading_id, result_status, recorded_at, client_updated_at, raw_reading, "
        "last_reading, usage_delta, updated_by, signature, result_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        readings,
    )
    service = AggregationReportService(
        scenario.repository,
        read_transaction_factory=partial(read_transaction, scenario.conn),
        current_business_date_provider=lambda: date(2032, 1, 1),
    )
    report = service.get_report(start, start + timedelta(days=days))
    assert len(report.periods) == days
    assert sum(period.results[0].value or Decimal(0) for period in report.periods) == days - 1
    assert report.periods[0].results[0].missing_codes == (AggregationCode.MISSING_NO_BASELINE,)
    assert report.periods[-1].results[0].information_codes == (AggregationCode.INFO_CARRYOVER,)


def test_consistent_read_snapshot_during_concurrent_formula_update(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, _ = file_database_repository
    scenario = Scenario(conn)
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 2, 9), value="100")
    scenario.plan("last", "a", date(2026, 2, 10), value="110")
    scenario.formula("formula", (("a", "1"),))
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    other = connect(path)

    class ConcurrentRepository(SQLiteRepository):
        changed = False

        def list_aggregation_formulas(self):
            formulas = super().list_aggregation_formulas()
            if not self.changed:
                other.execute("UPDATE aggregation_formula_terms SET coefficient = '2'")
                self.changed = True
            return formulas

    try:
        repository = ConcurrentRepository(conn, clock=lambda: NOW)
        service = AggregationReportService(
            repository,
            read_transaction_factory=partial(read_transaction, conn),
            current_business_date_provider=lambda: date(2026, 3, 15),
        )
        start, end = date(2026, 2, 10), date(2026, 2, 11)
        first = service.get_report(start, end)
        second = service.get_report(start, end)
        assert (
            next(item.value for item in first.periods[0].results if item.target_id == "formula")
            == 10
        )
        assert (
            next(item.value for item in second.periods[0].results if item.target_id == "formula")
            == 20
        )
    finally:
        other.close()
