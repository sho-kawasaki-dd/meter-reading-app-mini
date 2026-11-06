"""集計結果の表示・出力に共通する表記。"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import MAX_EMAX, MIN_EMIN, ROUND_HALF_UP, Decimal, localcontext

from meter_reading_mini.core.aggregation_contracts import (
    AggregationBaselineSource,
    AggregationCode,
    AggregationDetailSource,
    AggregationIssue,
    AggregationPeriodUnit,
    AggregationResult,
    AggregationTargetType,
)
from meter_reading_mini.core.errors import ValidationError

PERIOD_LABELS = {
    AggregationPeriodUnit.DAY: "日次",
    AggregationPeriodUnit.MONTH: "月次",
    AggregationPeriodUnit.CALENDAR_YEAR: "暦年",
    AggregationPeriodUnit.FISCAL_YEAR: "年度",
}
TARGET_LABELS = {
    AggregationTargetType.DEFINITION: "測定定義",
    AggregationTargetType.FORMULA: "集計式",
}
SOURCE_LABELS = {
    AggregationDetailSource.READING: "検針",
    AggregationDetailSource.REPLACEMENT: "交換境界",
}
BASELINE_LABELS = {
    AggregationBaselineSource.READING: "前回実測",
    AggregationBaselineSource.REPLACEMENT_INITIAL: "交換時初期値",
}


def display_value(value: Decimal | None, decimal_places: int | None) -> str:
    if value is None:
        return ""
    if not value.is_finite():
        raise ValidationError("集計値が有限の数値ではありません。")
    if decimal_places is None:
        return format(value, "f")
    if not 0 <= decimal_places <= 10:
        raise ValidationError("表示小数桁数は 0〜10 で指定してください。")
    with localcontext() as context:
        context.prec = max(
            len(value.as_tuple().digits),
            max(value.adjusted() + 1, 1) + decimal_places + 1,
        )
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        rounded = value.quantize(Decimal(f"1e-{decimal_places}"), rounding=ROUND_HALF_UP)
    return format(rounded, f".{decimal_places}f")


def issue_codes(issues: Sequence[AggregationIssue]) -> tuple[str, str, str]:
    present = {issue.code for issue in issues}

    def collect(kind: str) -> str:
        return ";".join(
            code.value for code in AggregationCode if code in present and code.startswith(kind)
        )

    return collect("MISSING_"), collect("WARN_"), collect("INFO_")


def result_state(result: AggregationResult | None, *, is_in_progress: bool = False) -> str:
    if result is None:
        state = "適用なし"
    elif result.exclusion_reason:
        state = "集計対象外"
    elif result.value is None:
        state = "値なし" if result.details else "予定なし"
    else:
        state = "参考値" if result.is_reference else "通常"
    if result is not None and result.is_partial_application:
        state += " / 一部期間のみ適用"
    if is_in_progress:
        state += " / 進行中"
    return state
