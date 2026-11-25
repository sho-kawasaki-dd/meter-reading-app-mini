"""集計スナップショットを形式 v1 の JSON に変換する。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from meter_reading_mini.core.aggregation_contracts import (
    AggregationCode,
    AggregationPeriodUnit,
    AggregationResult,
    AggregationTargetType,
)
from meter_reading_mini.core.aggregation_export_contracts import (
    AggregationExportFormula,
    AggregationExportSnapshot,
)
from meter_reading_mini.core.contract_models import (
    SIGNED_DECIMAL_STRING_PATTERN,
    Category,
    Purpose,
)
from meter_reading_mini.core.decimal_arithmetic import exact_add
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import MeasurementDefinitionRecord
from meter_reading_mini.core.timeutil import utc_now_str


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value.is_finite():
        raise ValidationError("集計値が有限の数値ではありません。")
    text = format(value.copy_abs() if value.is_zero() else value, "f")
    if not SIGNED_DECIMAL_STRING_PATTERN.fullmatch(text):
        raise ValidationError("集計値が plain decimal 文字列ではありません。")
    return text


def _label(unit: AggregationPeriodUnit, start: date) -> str:
    if unit is AggregationPeriodUnit.DAY:
        return start.isoformat()
    if unit is AggregationPeriodUnit.MONTH:
        return f"{start.year}年{start.month}月"
    if unit is AggregationPeriodUnit.CALENDAR_YEAR:
        return f"{start.year}年"
    return f"{start.year}年度"


def _state(result: AggregationResult) -> tuple[str, str | None]:
    if result.exclusion_reason is not None:
        return "excluded", result.exclusion_reason
    if result.value is not None:
        return ("reference" if result.missing_codes else "calculated"), None
    if result.missing_codes:
        return "no_value", "欠測系の理由により使用量を算出できません。"
    return "no_value", "この有効期間に予定枠・交換境界はありません。"


def _applications(
    result: AggregationResult, definitions: dict[str, MeasurementDefinitionRecord]
) -> list[dict[str, object]]:
    applications: list[dict[str, object]] = []
    for application in result.applications:
        terms: list[dict[str, object]] = []
        for term in sorted(application.version.terms, key=lambda item: item.sort_order):
            definition = definitions[term.measurement_definition_id]
            source: Decimal | None = None
            contribution: Decimal | None = None
            for detail in result.details:
                if (
                    detail.version_effective_from == application.version.effective_from_date
                    and detail.measurement_definition_id == term.measurement_definition_id
                    and detail.contribution is not None
                ):
                    if detail.source_usage is None:
                        raise ValidationError("集計明細の元使用量がありません。")
                    source = (
                        detail.source_usage
                        if source is None
                        else exact_add(source, detail.source_usage)
                    )
                    contribution = (
                        detail.contribution
                        if contribution is None
                        else exact_add(contribution, detail.contribution)
                    )
            terms.append(
                {
                    "measurement_definition_id": term.measurement_definition_id,
                    "display_name": definition.display_name,
                    "location_id": definition.location_id,
                    "coefficient": _decimal(term.coefficient),
                    "source_usage": _decimal(source),
                    "contribution": _decimal(contribution),
                    "is_active": definition.is_active,
                }
            )
        applications.append(
            {
                "version_effective_from": application.version.effective_from_date.isoformat(),
                "start": application.start.isoformat(),
                "end": application.end.isoformat(),
                "is_retroactive": application.version.is_retroactive,
                "contribution": _decimal(application.contribution),
                "terms": terms,
            }
        )
    return applications


def _result(
    result: AggregationResult,
    definitions: dict[str, MeasurementDefinitionRecord],
    formulas: dict[str, AggregationExportFormula],
    locations: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    state, reason = _state(result)
    issue_order = {code: index for index, code in enumerate(AggregationCode)}
    issues = sorted(
        {(issue.code, issue.message) for issue in result.issues},
        key=lambda item: (issue_order[item[0]], item[1]),
    )
    location_ids = sorted(result.location_ids)
    output: dict[str, object] = {
        "target_type": result.target_type.value,
        "target_id": result.target_id,
        "display_name": result.display_name,
        "unit": result.unit,
        "category": result.category.value,
        "purpose": result.purpose.value,
        "decimal_places": result.decimal_places,
        "location_ids": location_ids,
        "locations": [
            {"location_id": location_id, "location_names": list(locations.get(location_id, ()))}
            for location_id in location_ids
        ],
        "value_state": state,
        "value": _decimal(result.value),
        "missing_codes": [code.value for code in result.missing_codes],
        "warning_codes": [code.value for code in result.warning_codes],
        "information_codes": [code.value for code in result.information_codes],
        "issues": [{"code": code.value, "message": message} for code, message in issues],
        "missing_definition_ids": list(result.missing_definition_ids),
        "reason": reason,
    }
    if result.target_type is AggregationTargetType.DEFINITION:
        definition = definitions[result.target_id]
        output.update(
            is_active=definition.is_active,
            calculation_method=definition.calculation_method.value,
            value_schema=definition.value_schema.value,
            reading_cycle=definition.reading_cycle.value if definition.reading_cycle else None,
        )
    else:
        output.update(
            cycle_group=formulas[result.target_id].formula.cycle_group.value,
            coverage="partial" if result.is_partial_application else "full",
            applications=_applications(result, definitions),
        )
    return output


def _unapplied(formula: AggregationExportFormula, end: date) -> dict[str, object]:
    record = formula.formula
    started = any(version.effective_from_date < end for version in formula.versions)
    return {
        "target_type": AggregationTargetType.FORMULA.value,
        "target_id": record.formula_id,
        "display_name": record.display_name,
        "unit": record.unit,
        "category": record.category.value,
        "purpose": record.purpose.value,
        "decimal_places": record.decimal_places,
        "location_ids": [],
        "locations": [],
        "value_state": "not_applied",
        "value": None,
        "missing_codes": [],
        "warning_codes": [],
        "information_codes": [],
        "issues": [],
        "missing_definition_ids": [],
        "reason": (
            "指定期間内に有効な式版がありません (無効化中)。"
            if started
            else "指定期間内に式の適用は開始されていません。"
        ),
        "cycle_group": record.cycle_group.value,
        "coverage": "none",
        "applications": [],
    }


def build_aggregation_json(snapshot: AggregationExportSnapshot) -> str:
    """ファイル操作を行わず、全対象の JSON テキストを返す。"""
    period = snapshot.period
    definitions = {item.measurement_definition_id: item for item in snapshot.definitions}
    formulas = {item.formula.formula_id: item for item in snapshot.formulas}
    locations = {item.location_id: item.location_names for item in snapshot.locations}
    results = [_result(result, definitions, formulas, locations) for result in period.results]
    applied_ids = {
        result.target_id
        for result in period.results
        if result.target_type is AggregationTargetType.FORMULA
    }
    results.extend(
        _unapplied(formula, period.end)
        for formula in snapshot.formulas
        if formula.formula.formula_id not in applied_ids
    )
    purpose_order = {value.value: index for index, value in enumerate(Purpose)}
    category_order = {value.value: index for index, value in enumerate(Category)}

    def sort_key(result: dict[str, object]) -> tuple[int, int, int, str, str]:
        target_type, purpose, category = (
            result["target_type"],
            result["purpose"],
            result["category"],
        )
        name, target_id = result["display_name"], result["target_id"]
        if (
            not isinstance(target_type, str)
            or not isinstance(purpose, str)
            or not isinstance(category, str)
            or not isinstance(name, str)
            or not isinstance(target_id, str)
        ):
            raise ValidationError("集計対象の属性が不正です。")
        return (
            0 if target_type == AggregationTargetType.DEFINITION.value else 1,
            purpose_order[purpose],
            category_order[category],
            name,
            target_id,
        )

    results.sort(key=sort_key)
    output = {
        "kind": "mrm-usage-aggregation",
        "v": 1,
        "generated_at": utc_now_str(snapshot.generated_at),
        "generated_at_local": snapshot.generated_at.astimezone(
            snapshot.business_timezone
        ).isoformat(timespec="seconds"),
        "business_timezone": snapshot.business_timezone.key,
        "as_of_business_date": snapshot.as_of_business_date.isoformat(),
        "period": {
            "unit": period.unit.value,
            "label": _label(period.unit, period.nominal_start),
            "nominal_start": period.nominal_start.isoformat(),
            "nominal_end": period.nominal_end.isoformat(),
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "is_in_progress": period.is_in_progress,
            "includes_current_business_date": (
                period.start <= snapshot.as_of_business_date < period.end
            ),
        },
        "results": results,
    }
    return json.dumps(output, ensure_ascii=False, indent=2) + "\n"
