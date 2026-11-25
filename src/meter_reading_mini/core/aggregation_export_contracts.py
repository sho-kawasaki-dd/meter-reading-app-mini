"""使用量集計の一括出力に用いる契約。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from meter_reading_mini.core.aggregation_contracts import (
    AggregationFormulaRecord,
    AggregationFormulaVersionRecord,
    AggregationPeriod,
    AggregationPeriodUnit,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionRecord,
    MeterLocationRecord,
)


@dataclass(frozen=True, slots=True)
class AggregationExportPeriodSpec:
    unit: AggregationPeriodUnit
    anchor: date


@dataclass(frozen=True, slots=True)
class AggregationExportFormula:
    formula: AggregationFormulaRecord
    versions: tuple[AggregationFormulaVersionRecord, ...]


@dataclass(frozen=True, slots=True)
class AggregationExportSnapshot:
    generated_at: datetime
    business_timezone: ZoneInfo
    as_of_business_date: date
    period: AggregationPeriod
    definitions: tuple[MeasurementDefinitionRecord, ...]
    formulas: tuple[AggregationExportFormula, ...]
    locations: tuple[MeterLocationRecord, ...]
