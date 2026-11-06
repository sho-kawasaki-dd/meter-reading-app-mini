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
