from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

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
    AggregationIssue,
    AggregationPeriodUnit,
    AggregationResult,
    AggregationTargetType,
)
from meter_reading_mini.core.aggregation_export_contracts import AggregationExportPeriodSpec
from meter_reading_mini.core.aggregation_formulas import AggregationFormulaService
from meter_reading_mini.core.aggregation_json import build_aggregation_json
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
from meter_reading_mini.core.errors import (
    FormulaValidationError,
    InvalidTimezoneError,
    ValidationError,
)
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
from meter_reading_mini.core.reading_history_contracts import (
    ReadingHistoryCode,
    ReadingHistoryQuery,
    ReadingHistoryRowKind,
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
        assign_meter: bool = True,
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
                initial_meter_id=meter if assign_meter else None,
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


def test_definition_initial_baseline_and_first_reading_gap(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.context.measurement_definitions.set_initial_reading("a", Decimal("10"))
    scenario.plan("gap-initial", "a", date(2026, 2, 1))
    scenario.plan("first-initial", "a", date(2026, 2, 2), value="15")
    result = scenario.result("a", date(2026, 2, 2), date(2026, 2, 3))
    assert result.value == Decimal("5")
    assert AggregationCode.MISSING_GAP_BETWEEN_BASELINE in result.missing_codes
    baseline = result.details[0].baseline
    assert baseline is not None
    assert (baseline.source, baseline.source_id, baseline.business_date) == (
        AggregationBaselineSource.DEFINITION_INITIAL,
        "a",
        date(2025, 1, 1),
    )
    scenario.conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed' WHERE planned_reading_id = ?",
        ("gap-initial",),
    )
    scenario.save("gap-initial", "12")
    result = scenario.result("a", date(2026, 2, 1), date(2026, 2, 2))
    assert result.value == Decimal("2")
    assert AggregationCode.MISSING_NO_BASELINE not in result.missing_codes


def test_definition_initial_aggregation_keeps_purposes_separate(scenario: Scenario) -> None:
    for purpose in Purpose:
        scenario.definition(purpose.value, purpose=purpose)
        scenario.context.measurement_definitions.set_initial_reading(purpose.value, Decimal("10"))
        scenario.plan(f"{purpose.value}-first", purpose.value, date(2026, 2, 10), value="15")

    for purpose in Purpose:
        report = scenario.context.aggregation_reports.get_report(
            date(2026, 2, 10), date(2026, 2, 11), purpose=purpose
        )
        assert [item.target_id for item in report.periods[0].results] == [purpose.value]
        result = report.periods[0].results[0]
        assert result.value == Decimal("5")
        assert not result.is_reference
        assert AggregationCode.MISSING_NO_BASELINE not in result.missing_codes
        assert result.details[0].baseline is not None
        assert result.details[0].baseline.source is AggregationBaselineSource.DEFINITION_INITIAL


def test_later_initial_assignment_uses_its_local_date_as_baseline(scenario: Scenario) -> None:
    scenario.definition("a", assign_meter=False)
    assigned_at = datetime(2026, 2, 1, 15, tzinfo=UTC)
    scenario.context.measurement_definitions.set_initial_meter_assignment(
        "a", scenario.meters["a"], assigned_at
    )
    scenario.context.measurement_definitions.set_initial_reading("a", Decimal("10"))
    scenario.plan("first", "a", date(2026, 2, 10), value="15")

    result = scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))
    baseline = result.details[0].baseline
    assert baseline is not None
    assert (baseline.source, baseline.source_id, baseline.at, baseline.business_date) == (
        AggregationBaselineSource.DEFINITION_INITIAL,
        "a",
        assigned_at,
        date(2026, 2, 2),
    )
    assert baseline.reading == Decimal("10")
    assert result.value == Decimal("5")
    assert AggregationCode.INFO_CARRYOVER in result.information_codes
    assert AggregationCode.MISSING_NO_BASELINE not in result.missing_codes


@pytest.mark.parametrize("event", ["reading", "replacement"])
@pytest.mark.parametrize("matching_period", [False, True])
def test_initial_baseline_and_formula_period_survive_reactivation(
    scenario: Scenario, event: str, matching_period: bool
) -> None:
    for definition_id in ("a", "b"):
        scenario.definition(definition_id, cycle=ReadingCycle.MONTHLY)
        effective = (
            "2026-02-01 15:00:00"
            if definition_id == "a" or matching_period
            else "2026-02-02 15:00:00"
        )
        scenario.conn.execute(
            "UPDATE meter_assignments SET effective_from = ? WHERE measurement_definition_id = ?",
            (effective, definition_id),
        )
        scenario.context.measurement_definitions.set_initial_reading(
            definition_id, Decimal("10") if definition_id == "a" else Decimal("20")
        )

    if event == "reading":
        scenario.plan("a-first", "a", date(2026, 2, 10), value="15")
    else:
        scenario.meter("new", "location-a")
        scenario.context.meter_replacement.register_replacement(
            MeterReplacementInput(
                old_meter_id="meter-a",
                new_meter_id="new",
                replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
                old_final_reading=Decimal("15"),
                new_initial_reading=Decimal("0"),
                staff_id=scenario.staff_id,
            )
        )
    scenario.plan("b-first", "b", date(2026, 2, 10), value="25")
    scenario.formula(
        "monthly",
        (("a", "1"), ("b", "1")),
        cycle=AggregationCycleGroup.MONTHLY,
    )
    start, end = date(2026, 2, 10), date(2026, 2, 11)
    direct = scenario.result("a", start, end)
    formula = scenario.result("monthly", start, end)
    baseline = direct.details[0].baseline
    assert baseline is not None
    assert (baseline.source, baseline.business_date) == (
        AggregationBaselineSource.DEFINITION_INITIAL,
        date(2026, 2, 2),
    )
    assert direct.value == Decimal("5") and formula.value == Decimal("10")
    assert AggregationCode.INFO_CARRYOVER in direct.information_codes
    assert AggregationCode.INFO_CARRYOVER in formula.information_codes
    assert (AggregationCode.WARN_PERIOD_MISMATCH in formula.warning_codes) is not matching_period

    scenario.context.measurement_definitions.deactivate_measurement_definition("a")
    scenario.context.measurement_definitions.activate_measurement_definition("a")

    reactivated = scenario.context.measurement_definitions.get_measurement_definition("a")
    assert reactivated is not None and reactivated.generation_start_date == date(2026, 3, 15)
    assert scenario.result("a", start, end) == direct
    assert scenario.result("monthly", start, end) == formula


def test_initial_baseline_for_replacement_and_reactivation_is_stable(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.conn.execute(
        "UPDATE meter_assignments SET effective_from = '2026-02-01 15:00:00' "
        "WHERE measurement_definition_id = 'a'"
    )
    scenario.context.measurement_definitions.set_initial_reading("a", Decimal("10"))
    scenario.meter("new", "location-a")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("15"),
            new_initial_reading=Decimal("0"),
            staff_id=scenario.staff_id,
        )
    )
    result = scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))
    assert result.value == Decimal("5")
    baseline = result.details[0].baseline
    assert baseline is not None
    assert (baseline.source, baseline.business_date) == (
        AggregationBaselineSource.DEFINITION_INITIAL,
        date(2026, 2, 2),
    )
    scenario.context.measurement_definitions.deactivate_measurement_definition("a")
    scenario.context.measurement_definitions.activate_measurement_definition("a")
    again = scenario.result("a", date(2026, 2, 10), date(2026, 2, 11))
    assert again.details[0].baseline == baseline


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


def test_history_matches_direct_aggregation_with_consecutive_replacements(
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
        "after",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 6, tzinfo=UTC),
        meter_id="third",
        value="4",
    )
    day = date(2026, 2, 10)
    history = scenario.context.reading_history.get_history(ReadingHistoryQuery("a", day, day))
    assert history.total_usage == scenario.result("a", day, day + timedelta(days=1)).value == 17
    assert [row.row_kind for row in history.rows] == [
        ReadingHistoryRowKind.REPLACEMENT_OLD_FINAL,
        ReadingHistoryRowKind.REPLACEMENT_NEW_INITIAL,
        ReadingHistoryRowKind.REPLACEMENT_OLD_FINAL,
        ReadingHistoryRowKind.REPLACEMENT_NEW_INITIAL,
        ReadingHistoryRowKind.READING,
    ]
    assert history.rows[2].previous_reference_id == first.replacement_event_id
    assert ReadingHistoryCode.BASELINE_OUTSIDE_PERIOD in {
        issue.code for issue in history.rows[0].issues
    }
    assert history.warning_count == 0


def test_history_matches_direct_aggregation_with_rollover_and_replacement(
    scenario: Scenario,
) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.conn.execute(
        "UPDATE meter_assignments SET rollover_value = ? WHERE measurement_definition_id = ?",
        ("1000", "a"),
    )
    scenario.plan("before", "a", date(2026, 2, 9), value="990")
    scenario.plan("rollover", "a", date(2026, 2, 10), value="5")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 4, tzinfo=UTC),
            old_final_reading=Decimal("7"),
            new_initial_reading=Decimal("1"),
            staff_id=scenario.staff_id,
        )
    )
    scenario.plan(
        "after",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 5, tzinfo=UTC),
        meter_id="new",
        value="4",
    )
    day = date(2026, 2, 10)
    history = scenario.context.reading_history.get_history(ReadingHistoryQuery("a", day, day))
    assert history.total_usage == scenario.result("a", day, day + timedelta(days=1)).value == 20
    assert ReadingHistoryCode.ROLLOVER in {issue.code for issue in history.rows[0].issues}
    assert history.warning_count == 0


def test_history_raw_only_replacement_allows_previous_measured_value(scenario: Scenario) -> None:
    scenario.definition("a", calculation=CalculationMethod.RAW_ONLY)
    scenario.meter("new", "location-a")
    day = date(2026, 2, 10)
    spec = AggregationExportPeriodSpec(AggregationPeriodUnit.MONTH, day.replace(day=1))
    reports = scenario.context.aggregation_reports
    expected = json.loads(build_aggregation_json(reports.get_export_snapshot(spec)))["results"]
    scenario.plan("before", "a", date(2026, 2, 9), value="-100")
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("-110"),
            new_initial_reading=Decimal("-5"),
            staff_id=scenario.staff_id,
        )
    )
    history = scenario.context.reading_history.get_history(
        ReadingHistoryQuery("a", day.replace(day=9), day)
    )
    assert history.total_usage is None
    assert history.warning_count == 0
    assert [row.raw_reading for row in history.rows] == [
        Decimal("-100"),
        Decimal("-110"),
        Decimal("-5"),
    ]
    assert all(row.last_reading is None for row in history.rows)
    assert scenario.result("a", day, day.replace(day=11)).exclusion_reason
    assert (
        json.loads(build_aggregation_json(reports.get_export_snapshot(spec)))["results"] == expected
    )


@pytest.mark.parametrize("value", ["-0.00", "-2"])
def test_delta_history_and_reports_reject_signed_readings(scenario: Scenario, value: str) -> None:
    day = date(2026, 2, 10)
    scenario.definition("a")
    scenario.plan("a-day", "a", day, value="0")
    scenario.conn.execute("UPDATE reading_transactions SET raw_reading = ?", (value,))
    history = scenario.context.reading_history.get_history(ReadingHistoryQuery("a", day, day))
    assert ReadingHistoryCode.STATE_MISMATCH in {issue.code for issue in history.rows[0].issues}
    with pytest.raises(ValidationError, match="負数"):
        scenario.result("a", day, day.replace(day=11))


def test_history_reports_overlapping_assignments_without_aborting(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.meter("other", "location-a")
    scenario.plan("planned", "a", date(2026, 2, 10), value="100")
    scenario.conn.execute(
        "INSERT INTO meter_assignments "
        "(measurement_definition_id, meter_id, effective_from, multiplier) VALUES (?, ?, ?, ?)",
        ("a", "other", "2026-02-01 00:00:00", "1"),
    )
    day = date(2026, 2, 10)
    history = scenario.context.reading_history.get_history(ReadingHistoryQuery("a", day, day))
    assert history.warning_count == 1
    assert ReadingHistoryCode.ASSIGNMENT_UNRESOLVED in {
        issue.code for issue in history.rows[0].issues
    }
    assert history.rows[0].multiplier is None


def test_history_preserves_superseded_fault_result(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.meter("new", "location-a")
    scenario.plan("before", "a", date(2026, 2, 9), value="100")
    scenario.plan(
        "fault",
        "a",
        date(2026, 2, 10),
        at=datetime(2026, 2, 10, 4, tzinfo=UTC),
        status=ResultStatus.METER_FAULT,
    )
    scenario.context.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="meter-a",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 10, 3, tzinfo=UTC),
            old_final_reading=Decimal("110"),
            new_initial_reading=Decimal("1"),
            staff_id=scenario.staff_id,
        )
    )
    day = date(2026, 2, 10)
    history = scenario.context.reading_history.get_history(
        ReadingHistoryQuery("a", day, day, include_superseded=True)
    )
    fault = next(row for row in history.rows if row.planned_reading_id == "fault")
    assert fault.result_status is ResultStatus.METER_FAULT
    assert fault.note == "試験"
    assert {issue.code for issue in fault.issues} == {ReadingHistoryCode.SUPERSEDED}
    assert fault.multiplier is None
    assert history.warning_count == 0


def test_history_rejects_malformed_database_decimal(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.plan("bad", "a", date(2026, 2, 10), value="100")
    scenario.conn.execute(
        "UPDATE reading_transactions SET raw_reading = ? WHERE planned_reading_id = ?",
        ("not-decimal", "bad"),
    )
    day = date(2026, 2, 10)
    with pytest.raises(ValidationError):
        scenario.context.reading_history.get_history(ReadingHistoryQuery("a", day, day))


def test_history_preserves_original_precision_after_decimal_places_change(
    scenario: Scenario,
) -> None:
    scenario.definition("a")
    scenario.plan("reading", "a", date(2026, 2, 10), value="12.3400")
    scenario.conn.execute(
        "UPDATE measurement_definitions SET decimal_places = ? WHERE measurement_definition_id = ?",
        (0, "a"),
    )
    day = date(2026, 2, 10)
    history = scenario.context.reading_history.get_history(ReadingHistoryQuery("a", day, day))
    value = history.rows[0].raw_reading
    assert value is not None
    assert format(value, "f") == "12.3400"


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


@pytest.mark.parametrize(
    ("unit", "anchor", "start", "end"),
    [
        (AggregationPeriodUnit.DAY, date(2024, 2, 29), date(2024, 2, 29), date(2024, 3, 1)),
        (AggregationPeriodUnit.MONTH, date(2024, 2, 29), date(2024, 2, 1), date(2024, 3, 1)),
        (AggregationPeriodUnit.MONTH, date(2025, 12, 31), date(2025, 12, 1), date(2026, 1, 1)),
        (
            AggregationPeriodUnit.CALENDAR_YEAR,
            date(2025, 12, 31),
            date(2025, 1, 1),
            date(2026, 1, 1),
        ),
        (
            AggregationPeriodUnit.FISCAL_YEAR,
            date(2026, 3, 31),
            date(2025, 4, 1),
            date(2026, 4, 1),
        ),
    ],
)
def test_export_period_boundaries(
    scenario: Scenario, unit: AggregationPeriodUnit, anchor: date, start: date, end: date
) -> None:
    snapshot = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(unit, anchor)
    )
    period = snapshot.period
    assert (period.nominal_start, period.nominal_end) == (start, end)
    assert (period.start, period.end) == (start, min(end, date(2026, 3, 16)))
    assert period.is_in_progress == (unit is not AggregationPeriodUnit.DAY and end > NOW.date())


def test_export_uses_one_read_transaction_and_one_clock_and_timezone(scenario: Scenario) -> None:
    calls: list[str] = []

    @contextmanager
    def tracked_transaction():
        calls.append("begin")
        with read_transaction(scenario.conn):
            yield
        calls.append("end")

    def clock() -> datetime:
        assert scenario.conn.in_transaction
        calls.append("clock")
        return datetime(2026, 3, 15, 15, 0, 0, 123, tzinfo=UTC)

    def timezone() -> ZoneInfo:
        assert scenario.conn.in_transaction
        calls.append("timezone")
        return ZoneInfo("Asia/Tokyo")

    scenario.definition("a")
    service = AggregationReportService(
        scenario.repository,
        read_transaction_factory=tracked_transaction,
        clock=clock,
        timezone_provider=timezone,
    )
    snapshot = service.get_export_snapshot(
        AggregationExportPeriodSpec(AggregationPeriodUnit.MONTH, date(2026, 3, 31))
    )
    document = json.loads(build_aggregation_json(snapshot))
    assert calls == ["begin", "clock", "timezone", "end"]
    assert document["generated_at"] == "2026-03-15 15:00:00"
    assert document["generated_at_local"] == "2026-03-16T00:00:00+09:00"
    assert document["business_timezone"] == "Asia/Tokyo"
    assert document["as_of_business_date"] == "2026-03-16"
    assert document["period"]["end"] == "2026-03-17"
    assert document["period"]["includes_current_business_date"] is True
    assert document["period"]["is_in_progress"] is True

    with pytest.raises(ValidationError, match="未来"):
        service.get_export_snapshot(
            AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 3, 17))
        )
    invalid = AggregationReportService(
        scenario.repository,
        read_transaction_factory=tracked_transaction,
        clock=clock,
        timezone_provider=lambda: UTC,
    )
    with pytest.raises(InvalidTimezoneError):
        invalid.get_export_snapshot(
            AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 3, 16))
        )


@pytest.mark.parametrize(
    ("unit", "anchor", "label", "nominal_start", "nominal_end", "in_progress"),
    [
        (
            AggregationPeriodUnit.DAY,
            date(2026, 3, 15),
            "2026-03-15",
            "2026-03-15",
            "2026-03-16",
            False,
        ),
        (
            AggregationPeriodUnit.MONTH,
            date(2026, 3, 31),
            "2026年3月",
            "2026-03-01",
            "2026-04-01",
            True,
        ),
        (
            AggregationPeriodUnit.CALENDAR_YEAR,
            date(2026, 12, 31),
            "2026年",
            "2026-01-01",
            "2027-01-01",
            True,
        ),
        (
            AggregationPeriodUnit.FISCAL_YEAR,
            date(2026, 3, 31),
            "2025年度",
            "2025-04-01",
            "2026-04-01",
            True,
        ),
    ],
)
def test_export_current_business_date_for_every_period(
    scenario: Scenario,
    unit: AggregationPeriodUnit,
    anchor: date,
    label: str,
    nominal_start: str,
    nominal_end: str,
    in_progress: bool,
) -> None:
    snapshot = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(unit, anchor)
    )
    document = json.loads(build_aggregation_json(snapshot))
    assert document["period"] == {
        "unit": unit.value,
        "label": label,
        "nominal_start": nominal_start,
        "nominal_end": nominal_end,
        "start": nominal_start,
        "end": "2026-03-16",
        "is_in_progress": in_progress,
        "includes_current_business_date": True,
    }


@pytest.mark.parametrize(
    ("unit", "anchor"),
    [
        (AggregationPeriodUnit.DAY, date(2026, 3, 16)),
        (AggregationPeriodUnit.MONTH, date(2026, 4, 1)),
        (AggregationPeriodUnit.CALENDAR_YEAR, date(2027, 1, 1)),
        (AggregationPeriodUnit.FISCAL_YEAR, date(2026, 4, 1)),
    ],
)
def test_export_rejects_future_periods(
    scenario: Scenario, unit: AggregationPeriodUnit, anchor: date
) -> None:
    with pytest.raises(ValidationError, match="未来"):
        scenario.context.aggregation_reports.get_export_snapshot(
            AggregationExportPeriodSpec(unit, anchor)
        )
    assert not scenario.conn.in_transaction


def test_export_matches_reports_for_all_purposes_and_versions(scenario: Scenario) -> None:
    for purpose in Purpose:
        definition_id = purpose.value
        scenario.definition(definition_id, purpose=purpose)
        scenario.plan(f"{definition_id}-base", definition_id, date(2026, 2, 9), value="0")
        scenario.plan(f"{definition_id}-first", definition_id, date(2026, 2, 10), value="0.1")
        scenario.plan(f"{definition_id}-second", definition_id, date(2026, 2, 11), value="0.3")
        service = scenario.formula(
            f"formula-{purpose.value}", ((definition_id, "1"),), purpose=purpose
        )
        service.save_version(
            f"formula-{purpose.value}",
            scenario.version(date(2026, 2, 11), ((definition_id, "-2"),)),
        )
    spec = AggregationExportPeriodSpec(AggregationPeriodUnit.MONTH, date(2026, 2, 28))
    snapshot = scenario.context.aggregation_reports.get_export_snapshot(spec)
    text = build_aggregation_json(snapshot)
    assert text == build_aggregation_json(snapshot)
    assert text == build_aggregation_json(
        scenario.context.aggregation_reports.get_export_snapshot(spec)
    )
    assert text.encode("utf-8").startswith(b"{\n") and text.endswith("\n")
    document = json.loads(text)
    assert document["kind"] == "mrm-usage-aggregation"
    assert document["v"] == 1
    assert list(document) == [
        "kind",
        "v",
        "generated_at",
        "generated_at_local",
        "business_timezone",
        "as_of_business_date",
        "period",
        "results",
    ]
    assert document["period"] == {
        "unit": "month",
        "label": "2026年2月",
        "nominal_start": "2026-02-01",
        "nominal_end": "2026-03-01",
        "start": "2026-02-01",
        "end": "2026-03-01",
        "is_in_progress": False,
        "includes_current_business_date": False,
    }
    results = document["results"]
    assert [(item["target_type"], item["purpose"]) for item in results] == [
        (kind, purpose.value) for kind in ("definition", "formula") for purpose in Purpose
    ]
    for purpose in Purpose:
        report = scenario.context.aggregation_reports.get_report(
            date(2026, 2, 1), date(2026, 3, 1), unit=AggregationPeriodUnit.MONTH, purpose=purpose
        )
        for result in report.periods[0].results:
            item = next(
                item
                for item in results
                if item["target_id"] == result.target_id
                and item["target_type"] == result.target_type
            )
            common_keys = [
                "target_type",
                "target_id",
                "display_name",
                "unit",
                "category",
                "purpose",
                "decimal_places",
                "location_ids",
                "locations",
                "value_state",
                "value",
                "missing_codes",
                "warning_codes",
                "information_codes",
                "issues",
                "missing_definition_ids",
                "reason",
            ]
            extra_keys = (
                ["is_active", "calculation_method", "value_schema", "reading_cycle"]
                if result.target_type is AggregationTargetType.DEFINITION
                else ["cycle_group", "coverage", "applications"]
            )
            assert list(item) == common_keys + extra_keys
            assert isinstance(item["value"], str)
            assert Decimal(item["value"]) == result.value
            assert item["missing_codes"] == list(result.missing_codes)
            assert item["warning_codes"] == list(result.warning_codes)
            assert item["information_codes"] == list(result.information_codes)
            assert item["missing_definition_ids"] == list(result.missing_definition_ids)
            if result.target_type is AggregationTargetType.FORMULA:
                applications = item["applications"]
                assert list(applications[0]) == [
                    "version_effective_from",
                    "start",
                    "end",
                    "is_retroactive",
                    "contribution",
                    "terms",
                ]
                assert list(applications[0]["terms"][0]) == [
                    "measurement_definition_id",
                    "display_name",
                    "location_id",
                    "coefficient",
                    "source_usage",
                    "contribution",
                    "is_active",
                ]
                assert [Decimal(app["contribution"]) for app in applications] == [
                    app.contribution for app in result.applications
                ]
                assert sum(Decimal(app["contribution"]) for app in applications) == result.value
                assert [app["terms"][0]["source_usage"] for app in applications] == ["0.1", "0.2"]
                assert item["warning_codes"] == ["WARN_NEGATIVE"]
                assert item["coverage"] == "full"


def test_export_states_unapplied_versions_and_disabled_definitions(scenario: Scenario) -> None:
    for definition_id in ("zero", "reference", "missing", "empty"):
        scenario.definition(definition_id)
    scenario.definition("raw", calculation=CalculationMethod.RAW_ONLY)
    scenario.definition(
        "power",
        calculation=CalculationMethod.RAW_ONLY,
        schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
    )
    scenario.context.measurement_definitions.set_initial_reading("zero", Decimal("0"))
    scenario.plan("zero-day", "zero", date(2026, 2, 10), value="0")
    scenario.plan("reference-first", "reference", date(2026, 2, 10), value="4")
    scenario.plan("missing-day", "missing", date(2026, 2, 10))
    scenario.formula("partial-items", (("zero", "-1"), ("missing", "1")))
    scenario.formula("all-missing", (("missing", "1"),))
    scenario.formula("future", (("zero", "1"),), effective=date(2026, 3, 16))
    disabled = scenario.formula("disabled", (("zero", "1"),), effective=date(2026, 6, 1))
    disabled.save_version("disabled", scenario.version(date(2026, 3, 1), (), active=False))
    scenario.conn.execute(
        "UPDATE measurement_definitions SET is_active = 0 "
        "WHERE measurement_definition_id IN ('raw', 'zero')"
    )

    day = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 2, 10))
    )
    results = {
        item["target_id"]: item for item in json.loads(build_aggregation_json(day))["results"]
    }
    assert (results["zero"]["value_state"], results["zero"]["value"]) == ("calculated", "0")
    assert "INFO_DISABLED_DEFINITION" in results["zero"]["information_codes"]
    assert results["reference"]["value_state"] == "reference"
    assert results["missing"]["value_state"] == "no_value"
    assert results["missing"]["reason"] == "欠測系の理由により使用量を算出できません。"
    assert results["empty"]["reason"] == "この有効期間に予定枠・交換境界はありません。"
    for target in ("raw", "power"):
        assert results[target]["value_state"] == "excluded"
        assert results[target]["reason"]
    assert "INFO_DISABLED_DEFINITION" in results["raw"]["information_codes"]
    assert results["partial-items"]["missing_definition_ids"] == ["missing"]
    assert results["partial-items"]["applications"][0]["terms"][0]["contribution"] == "0"
    assert results["zero"]["is_active"] is False
    assert results["raw"]["is_active"] is False
    assert results["partial-items"]["applications"][0]["terms"][0]["is_active"] is False
    assert results["all-missing"]["missing_definition_ids"] == []
    assert "INFO_DISABLED_DEFINITION" in results["partial-items"]["information_codes"]
    for item in results.values():
        assert (item["reason"] is None) == (item["value"] is not None)

    month = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(AggregationPeriodUnit.MONTH, date(2026, 3, 1))
    )
    month_items = {
        item["target_id"]: item for item in json.loads(build_aggregation_json(month))["results"]
    }
    for target in ("future", "disabled"):
        item = month_items[target]
        assert item["value_state"] == "not_applied" and item["coverage"] == "none"
        assert all(
            item[key] == []
            for key in (
                "location_ids",
                "locations",
                "applications",
                "issues",
                "missing_codes",
                "warning_codes",
                "information_codes",
                "missing_definition_ids",
            )
        )
    assert month_items["future"]["reason"] == "指定期間内に式の適用は開始されていません。"
    assert month_items["disabled"]["reason"] == "指定期間内に有効な式版がありません (無効化中)。"


def test_export_sums_multiple_terms_and_readings_per_formula_version(scenario: Scenario) -> None:
    for definition_id in ("a", "b", "missing"):
        scenario.definition(definition_id)
    for definition_id, readings in (
        ("a", ("0.1", "0.3", "0.6")),
        ("b", ("0.4", "0.5", "0.7")),
    ):
        scenario.plan(f"{definition_id}-base", definition_id, date(2026, 2, 9), value="0")
        for day, reading in enumerate(readings, start=10):
            scenario.plan(
                f"{definition_id}-{day}", definition_id, date(2026, 2, day), value=reading
            )
    service = scenario.formula("formula", (("b", "-2"), ("a", "1.5"), ("missing", "1")))
    service.save_version(
        "formula",
        scenario.version(date(2026, 2, 12), (("a", "0.5"), ("missing", "1"), ("b", "-2"))),
    )
    snapshot = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(AggregationPeriodUnit.MONTH, date(2026, 2, 1))
    )
    item = next(
        item
        for item in json.loads(build_aggregation_json(snapshot))["results"]
        if item["target_id"] == "formula"
    )
    assert item["value"] == "-0.80"
    assert item["value_state"] == "reference"
    assert item["missing_definition_ids"] == ["missing"]
    assert "MISSING_PARTIAL_ITEMS" in item["missing_codes"]
    assert [app["is_retroactive"] for app in item["applications"]] == [True, False]
    assert [
        (
            application["version_effective_from"],
            application["start"],
            application["end"],
            application["contribution"],
            [
                (term["measurement_definition_id"], term["source_usage"], term["contribution"])
                for term in application["terms"]
            ],
        )
        for application in item["applications"]
    ] == [
        (
            "2026-02-01",
            "2026-02-01",
            "2026-02-12",
            "-0.55",
            [("b", "0.5", "-1.0"), ("a", "0.3", "0.45"), ("missing", None, None)],
        ),
        (
            "2026-02-12",
            "2026-02-12",
            "2026-03-01",
            "-0.25",
            [("a", "0.3", "0.15"), ("missing", None, None), ("b", "0.2", "-0.4")],
        ),
    ]
    report = scenario.context.aggregation_reports.get_report(
        date(2026, 2, 1), date(2026, 3, 1), unit=AggregationPeriodUnit.MONTH
    )
    result = next(result for result in report.periods[0].results if result.target_id == "formula")
    assert Decimal(item["value"]) == result.value
    assert [Decimal(app["contribution"]) for app in item["applications"]] == [
        app.contribution for app in result.applications
    ]


def test_export_excludes_pending_replacement_detail_from_term_sum(scenario: Scenario) -> None:
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
        "SELECT measurement_definition_id, meter_id, effective_from, '確認待ち', ? "
        "FROM meter_assignments WHERE measurement_definition_id = ? AND meter_id = ?",
        ("2026-02-10 03:00:00", "a", "meter-a"),
    )
    scenario.formula("formula", (("a", "2"),))
    snapshot = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 2, 10))
    )
    results = {
        item["target_id"]: item for item in json.loads(build_aggregation_json(snapshot))["results"]
    }
    term = results["formula"]["applications"][0]["terms"][0]
    assert (term["source_usage"], term["contribution"]) == ("3", "6")
    assert results["formula"]["value"] == "6"
    assert results["formula"]["value_state"] == "reference"
    assert "MISSING_ASSIGNMENT_PENDING" in results["formula"]["missing_codes"]
    assert "INFO_REPLACEMENT" in results["formula"]["information_codes"]
    report = scenario.context.aggregation_reports.get_report(date(2026, 2, 10), date(2026, 2, 11))
    for result in report.periods[0].results:
        item = results[result.target_id]
        assert Decimal(item["value"]) == result.value
        assert item["missing_codes"] == list(result.missing_codes)
        assert item["warning_codes"] == list(result.warning_codes)
        assert item["information_codes"] == list(result.information_codes)


def test_export_rejects_invalid_data_and_validates_only_applied_references(
    scenario: Scenario,
) -> None:
    scenario.definition("a")
    scenario.plan("base", "a", date(2026, 2, 9), value="100")
    scenario.plan("last", "a", date(2026, 2, 10), value="110")
    scenario.formula("future", (("a", "1"),), effective=date(2026, 6, 1))
    scenario.conn.execute("UPDATE measurement_definitions SET unit = 'm3'")
    spec = AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 2, 10))
    build_aggregation_json(scenario.context.aggregation_reports.get_export_snapshot(spec))
    scenario.conn.execute(
        "UPDATE reading_transactions SET usage_delta = NULL WHERE planned_reading_id = 'last'"
    )
    with pytest.raises(ValidationError):
        scenario.context.aggregation_reports.get_export_snapshot(spec)
    assert not scenario.conn.in_transaction


def test_export_partial_coverage_uses_actual_period(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.formula("part", (("a", "1"),), effective=date(2026, 3, 10))
    full = scenario.formula("full", (("a", "1"),), effective=date(2026, 3, 1))
    full.save_version("full", scenario.version(date(2026, 3, 16), (), active=False))
    snapshot = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(AggregationPeriodUnit.MONTH, date(2026, 3, 1))
    )
    results = {
        item["target_id"]: item for item in json.loads(build_aggregation_json(snapshot))["results"]
    }
    assert results["part"]["coverage"] == "partial"
    assert results["full"]["coverage"] == "full"
    assert results["full"]["applications"][0]["end"] == "2026-03-16"


def test_export_sorts_by_purpose_then_category_name_and_id(scenario: Scenario) -> None:
    for definition_id in ("z", "a", "m", "audit", "billing", "gas", "water"):
        scenario.definition(definition_id)
    scenario.conn.execute(
        "UPDATE measurement_definitions SET display_name = '同名' "
        "WHERE measurement_definition_id IN ('a', 'z')"
    )
    scenario.conn.execute(
        "UPDATE measurement_definitions SET category = 'gas' "
        "WHERE measurement_definition_id = 'gas'"
    )
    scenario.conn.execute(
        "UPDATE measurement_definitions SET category = 'water' "
        "WHERE measurement_definition_id = 'water'"
    )
    scenario.conn.execute(
        "UPDATE measurement_definitions SET purpose = 'billing' "
        "WHERE measurement_definition_id = 'billing'"
    )
    scenario.conn.execute(
        "UPDATE measurement_definitions SET purpose = 'audit' "
        "WHERE measurement_definition_id = 'audit'"
    )
    snapshot = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 2, 10))
    )
    document = json.loads(build_aggregation_json(snapshot))
    assert [item["target_id"] for item in document["results"]] == [
        "a",
        "z",
        "m",
        "gas",
        "water",
        "billing",
        "audit",
    ]


def test_export_rejects_bad_applied_formula_and_all_invalid_version_histories(
    scenario: Scenario,
) -> None:
    scenario.definition("a")
    scenario.formula("active", (("a", "1"),))
    scenario.formula("future", (("a", "1"),), effective=date(2026, 6, 1))
    spec = AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 2, 10))
    scenario.conn.execute("UPDATE measurement_definitions SET unit = 'm3'")
    with pytest.raises(FormulaValidationError):
        scenario.context.aggregation_reports.get_export_snapshot(spec)
    scenario.conn.execute("UPDATE measurement_definitions SET unit = 'kWh'")
    scenario.conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        scenario.conn.execute(
            "UPDATE aggregation_formula_terms SET coefficient = '0' WHERE formula_id = 'future'"
        )
    finally:
        scenario.conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(FormulaValidationError, match="係数"):
        scenario.context.aggregation_reports.get_export_snapshot(spec)
    assert not scenario.conn.in_transaction


def test_export_precision_locations_sort_order_and_issues(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.definition("b")
    scenario.definition("missing-location", assign_meter=False)
    scenario.conn.execute("DELETE FROM physical_meters WHERE meter_id = 'meter-missing-location'")
    scenario.meter("other", "location-a")
    scenario.conn.execute(
        "UPDATE physical_meters SET location_name = '別名' WHERE meter_id = 'other'"
    )
    for definition_id, reading in (("a", "123456789012345678901234567890.000001"), ("b", "0.2")):
        scenario.context.measurement_definitions.set_initial_reading(definition_id, Decimal("0"))
        scenario.plan(f"{definition_id}-day", definition_id, date(2026, 2, 10), value=reading)
    scenario.formula("formula", (("a", "0.0000001"), ("b", "-1")))
    snapshot = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 2, 10))
    )
    results = {
        item["target_id"]: item for item in json.loads(build_aggregation_json(snapshot))["results"]
    }
    assert results["missing-location"]["locations"] == [
        {"location_id": "location-missing-location", "location_names": []}
    ]
    assert results["a"]["locations"][0]["location_names"] == ["location-a", "別名"]
    assert results["a"]["value"] == "123456789012345678901234567890.000001"
    formula = results["formula"]
    assert formula["location_ids"] == ["location-a", "location-b"]
    terms = formula["applications"][0]["terms"]
    assert terms[0]["coefficient"] == "0.0000001"
    assert terms[0]["contribution"] == "12345678901234567890123.4567890000001"
    assert terms[1]["contribution"] == "-0.2"
    assert formula["value"] == "12345678901234567890123.2567890000001"
    assert "INFO_MULTI_LOCATION" in formula["information_codes"]
    issue_order = {code.value: index for index, code in enumerate(AggregationCode)}
    for result in results.values():
        issues = [(issue["code"], issue["message"]) for issue in result["issues"]]
        assert issues == sorted(set(issues), key=lambda issue: (issue_order[issue[0]], issue[1]))


def test_export_preserves_strings_and_sorts_deduplicated_issues(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.conn.execute(
        "UPDATE measurement_definitions SET display_name = ?, unit = ?",
        ("=対象\n日本語", "+kWh"),
    )
    snapshot = scenario.context.aggregation_reports.get_export_snapshot(
        AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 2, 10))
    )
    result = snapshot.period.results[0]
    issues = (
        AggregationIssue(AggregationCode.WARN_NEGATIVE, "警告"),
        AggregationIssue(AggregationCode.MISSING_UNMEASURED, "欠測B"),
        AggregationIssue(AggregationCode.MISSING_UNMEASURED, "欠測A"),
        AggregationIssue(AggregationCode.MISSING_UNMEASURED, "欠測B"),
    )
    snapshot = replace(
        snapshot, period=replace(snapshot.period, results=(replace(result, issues=issues),))
    )
    text = build_aggregation_json(snapshot)
    assert "\r" not in text
    item = json.loads(text)["results"][0]
    assert item["display_name"] == "=対象\n日本語"
    assert item["unit"] == "+kWh"
    assert item["issues"] == [
        {"code": "MISSING_UNMEASURED", "message": "欠測A"},
        {"code": "MISSING_UNMEASURED", "message": "欠測B"},
        {"code": "WARN_NEGATIVE", "message": "警告"},
    ]


def test_export_every_query_and_history_uses_one_read_transaction(scenario: Scenario) -> None:
    scenario.definition("a")
    scenario.formula("one", (("a", "1"),))
    scenario.formula("two", (("a", "2"),))
    queries: list[tuple[str, bool]] = []

    class TrackedRepository(SQLiteRepository):
        assignments = 0

        def list_meter_assignments(self, measurement_definition_id: str):
            self.assignments += 1
            return super().list_meter_assignments(measurement_definition_id)

    repository = TrackedRepository(scenario.conn, clock=lambda: NOW)
    service = AggregationReportService(
        repository,
        read_transaction_factory=partial(read_transaction, scenario.conn),
        clock=lambda: NOW,
    )
    scenario.conn.set_trace_callback(
        lambda sql: queries.append((sql, scenario.conn.in_transaction))
    )
    try:
        service.get_export_snapshot(
            AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 2, 10))
        )
    finally:
        scenario.conn.set_trace_callback(None)
    assert sum(sql == "BEGIN" for sql, _ in queries) == 1
    reads = [(sql, inside) for sql, inside in queries if sql.lstrip().upper().startswith("SELECT")]
    assert reads and all(inside for _, inside in reads)
    assert any("physical_meters" in sql for sql, _ in reads)
    assert repository.assignments == 1


def test_export_consistent_snapshot_during_concurrent_updates(
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
                with transaction(other):
                    other.execute("UPDATE aggregation_formula_terms SET coefficient = '2'")
                    other.execute("UPDATE physical_meters SET location_name = '新名称'")
                self.changed = True
            return formulas

    try:
        service = AggregationReportService(
            ConcurrentRepository(conn, clock=lambda: NOW),
            read_transaction_factory=partial(read_transaction, conn),
            clock=lambda: NOW,
        )
        spec = AggregationExportPeriodSpec(AggregationPeriodUnit.DAY, date(2026, 2, 10))
        documents = [
            json.loads(build_aggregation_json(service.get_export_snapshot(spec))) for _ in range(2)
        ]
        values = []
        for document in documents:
            results = {item["target_id"]: item for item in document["results"]}
            values.append(
                (results["formula"]["value"], results["a"]["locations"][0]["location_names"])
            )
        assert values == [("10", ["location-a"]), ("20", ["新名称"])]
    finally:
        other.close()
