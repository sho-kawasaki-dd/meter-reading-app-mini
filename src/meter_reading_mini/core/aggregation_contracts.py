"""集計式の契約と、Service・復元検査で共有する純粋な検証。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from meter_reading_mini.core.contract_models import (
    SIGNED_DECIMAL_STRING_PATTERN,
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ValueSchema,
)
from meter_reading_mini.core.errors import FormulaValidationError


class AggregationCycleGroup(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    EVERY_TWO_MONTHS = "every_two_months"


@dataclass(frozen=True, slots=True)
class AggregationFormulaTerm:
    measurement_definition_id: str
    coefficient: Decimal
    sort_order: int


@dataclass(frozen=True, slots=True)
class AggregationFormulaVersionInput:
    effective_from_date: date
    is_active: bool
    terms: tuple[AggregationFormulaTerm, ...]


@dataclass(frozen=True, slots=True)
class AggregationFormulaCreateInput:
    display_name: str
    unit: str
    category: Category
    cycle_group: AggregationCycleGroup
    purpose: Purpose
    decimal_places: int | None
    initial_version: AggregationFormulaVersionInput


@dataclass(frozen=True, slots=True)
class AggregationFormulaUpdateInput:
    display_name: str
    decimal_places: int | None


@dataclass(frozen=True, slots=True)
class AggregationFormulaRecord:
    formula_id: str
    display_name: str
    unit: str
    category: Category
    cycle_group: AggregationCycleGroup
    purpose: Purpose
    decimal_places: int | None


@dataclass(frozen=True, slots=True)
class AggregationFormulaVersionRecord:
    formula_id: str
    effective_from_date: date
    is_active: bool
    is_initial: bool
    created_at: datetime
    registered_business_date: date
    terms: tuple[AggregationFormulaTerm, ...]

    @property
    def is_retroactive(self) -> bool:
        return self.is_initial and self.effective_from_date < self.registered_business_date


class AggregationPeriodUnit(StrEnum):
    DAY = "day"
    MONTH = "month"
    CALENDAR_YEAR = "calendar_year"
    FISCAL_YEAR = "fiscal_year"


class AggregationTargetType(StrEnum):
    DEFINITION = "definition"
    FORMULA = "formula"


class AggregationDetailSource(StrEnum):
    READING = "reading"
    REPLACEMENT = "replacement"


class AggregationBaselineSource(StrEnum):
    READING = "reading"
    REPLACEMENT_INITIAL = "replacement_initial"


class AggregationCode(StrEnum):
    MISSING_UNMEASURED = "MISSING_UNMEASURED"
    MISSING_NON_MEASURED = "MISSING_NON_MEASURED"
    MISSING_PARTIAL_ITEMS = "MISSING_PARTIAL_ITEMS"
    MISSING_GAP_BETWEEN_BASELINE = "MISSING_GAP_BETWEEN_BASELINE"
    MISSING_NO_BASELINE = "MISSING_NO_BASELINE"
    MISSING_REPLACEMENT_READING = "MISSING_REPLACEMENT_READING"
    MISSING_ASSIGNMENT_PENDING = "MISSING_ASSIGNMENT_PENDING"
    WARN_NEGATIVE = "WARN_NEGATIVE"
    WARN_PERIOD_MISMATCH = "WARN_PERIOD_MISMATCH"
    INFO_CARRYOVER = "INFO_CARRYOVER"
    INFO_REPLACEMENT = "INFO_REPLACEMENT"
    INFO_DISABLED_DEFINITION = "INFO_DISABLED_DEFINITION"
    INFO_RETROACTIVE_FORMULA = "INFO_RETROACTIVE_FORMULA"
    INFO_VERSION_SWITCH = "INFO_VERSION_SWITCH"
    INFO_MULTI_LOCATION = "INFO_MULTI_LOCATION"


@dataclass(frozen=True, slots=True)
class AggregationIssue:
    code: AggregationCode
    message: str
    measurement_definition_id: str | None = None
    source_id: str | None = None
    related_ids: tuple[str, ...] = ()
    meter_id: str | None = None
    assignment_effective_from: datetime | None = None
    resolution: str | None = None


@dataclass(frozen=True, slots=True)
class AggregationBaseline:
    source: AggregationBaselineSource
    source_id: str
    business_date: date
    at: datetime
    reading: Decimal | None


@dataclass(frozen=True, slots=True)
class AggregationDetail:
    source: AggregationDetailSource
    source_id: str
    measurement_definition_id: str
    location_id: str
    business_date: date
    at: datetime
    status: str
    baseline: AggregationBaseline | None
    source_usage: Decimal | None
    coefficient: Decimal
    contribution: Decimal | None
    version_effective_from: date | None
    retroactive: bool
    issues: tuple[AggregationIssue, ...]


@dataclass(frozen=True, slots=True)
class AggregationApplication:
    start: date
    end: date
    version: AggregationFormulaVersionRecord
    contribution: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AggregationResult:
    target_type: AggregationTargetType
    target_id: str
    display_name: str
    purpose: Purpose
    category: Category
    unit: str
    decimal_places: int | None
    location_ids: tuple[str, ...]
    value: Decimal | None
    details: tuple[AggregationDetail, ...]
    issues: tuple[AggregationIssue, ...]
    applications: tuple[AggregationApplication, ...] = ()
    missing_definition_ids: tuple[str, ...] = ()
    is_partial_application: bool = False
    exclusion_reason: str | None = None
    uses_current_attributes: bool = False

    def _codes(self, prefix: str) -> tuple[AggregationCode, ...]:
        present = {issue.code for issue in self.issues}
        return tuple(
            code for code in AggregationCode if code in present and code.startswith(prefix)
        )

    @property
    def missing_codes(self) -> tuple[AggregationCode, ...]:
        return self._codes("MISSING_")

    @property
    def warning_codes(self) -> tuple[AggregationCode, ...]:
        return self._codes("WARN_")

    @property
    def information_codes(self) -> tuple[AggregationCode, ...]:
        return self._codes("INFO_")

    @property
    def is_reference(self) -> bool:
        return self.value is not None and bool(self.missing_codes)


@dataclass(frozen=True, slots=True)
class AggregationPeriod:
    unit: AggregationPeriodUnit
    start: date
    end: date
    nominal_start: date
    nominal_end: date
    is_in_progress: bool
    results: tuple[AggregationResult, ...]


@dataclass(frozen=True, slots=True)
class AggregationReport:
    requested_start: date
    requested_end: date
    as_of: date
    purpose: Purpose
    periods: tuple[AggregationPeriod, ...]


def resolve_aggregation_cycle_group(
    reading_cycle: ReadingCycle | str | None,
) -> AggregationCycleGroup:
    try:
        cycle = ReadingCycle(reading_cycle)
    except (TypeError, ValueError) as exc:
        raise FormulaValidationError("測定定義の検針周期が不正です。") from exc
    if cycle in {ReadingCycle.DAILY_1X, ReadingCycle.DAILY_2X, ReadingCycle.DAILY_3X}:
        return AggregationCycleGroup.DAILY
    if cycle == ReadingCycle.WEEKLY:
        return AggregationCycleGroup.WEEKLY
    if cycle == ReadingCycle.MONTHLY:
        return AggregationCycleGroup.MONTHLY
    return AggregationCycleGroup.EVERY_TWO_MONTHS


def serialize_aggregation_coefficient(value: object) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value.is_zero():
        raise FormulaValidationError("係数は 0 以外の有限 Decimal で指定してください。")
    return format(value, "f")


def parse_aggregation_coefficient(value: object) -> Decimal:
    if not isinstance(value, str) or not SIGNED_DECIMAL_STRING_PATTERN.fullmatch(value):
        raise FormulaValidationError("係数は有限の plain decimal 文字列でなければなりません。")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise FormulaValidationError("係数が不正です。") from exc
    if not number.is_finite() or number.is_zero() or format(number, "f") != value:
        raise FormulaValidationError(
            "係数は 0 以外の有限の plain decimal 文字列でなければなりません。"
        )
    return number


def validate_aggregation_term_attributes(
    formula: AggregationFormulaRecord,
    *,
    measurement_definition_id: str,
    calculation_method: CalculationMethod | str,
    value_schema: ValueSchema | str,
    unit: str | None,
    category: Category | str | None,
    reading_cycle: ReadingCycle | str | None,
) -> None:
    label = f"測定定義 {measurement_definition_id}"
    if calculation_method != CalculationMethod.DELTA or value_schema != ValueSchema.DECIMAL_STRING:
        raise FormulaValidationError(f"{label}は差分計算の Decimal 測定対象ではありません。")
    if unit != formula.unit or category != formula.category:
        raise FormulaValidationError(f"{label}の単位・カテゴリが集計式と一致しません。")
    if resolve_aggregation_cycle_group(reading_cycle) != formula.cycle_group:
        raise FormulaValidationError(f"{label}の周期グループが集計式と一致しません。")
