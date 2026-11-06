"""集計画面に表示した系列を CSV / PNG に出力する。"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from PySide6.QtCharts import QChartView
from PySide6.QtCore import QIODevice, QSaveFile

from meter_reading_mini.core.aggregation_contracts import (
    AggregationDetail,
    AggregationPeriod,
    AggregationReport,
    AggregationResult,
    AggregationTargetType,
)
from meter_reading_mini.core.errors import FileOutputError, ValidationError
from meter_reading_mini.ui.aggregation_display import (
    BASELINE_LABELS,
    PERIOD_LABELS,
    SOURCE_LABELS,
    TARGET_LABELS,
    display_value,
    issue_codes,
    result_state,
)
from meter_reading_mini.ui.atomic_write import atomic_write_text
from meter_reading_mini.ui.models.reading_overview_model import CATEGORY_LABELS, PURPOSE_LABELS

CSV_HEADERS = (
    "行種別",
    "集計単位",
    "期間開始業務日",
    "期間終了業務日_含まない",
    "対象種別",
    "対象ID",
    "対象名",
    "目的",
    "カテゴリ",
    "単位",
    "場所ID",
    "式版開始業務日",
    "後付け設定",
    "測定定義ID",
    "係数",
    "明細元",
    "明細ID",
    "基準業務日",
    "実測業務日",
    "元使用量_無丸め",
    "表示値",
    "無丸め値",
    "欠測系",
    "警告",
    "情報",
    "式適用区間",
    "基準元",
    "基準ID",
    "基準読値_無丸め",
    "集計状態",
)


def _text(value: str) -> str:
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def _common(period: AggregationPeriod, result: AggregationResult) -> list[str]:
    return [
        PERIOD_LABELS[period.unit],
        period.start.isoformat(),
        period.end.isoformat(),
        TARGET_LABELS[result.target_type],
        _text(result.target_id),
        _text(result.display_name),
        PURPOSE_LABELS[result.purpose],
        CATEGORY_LABELS[result.category],
        _text(result.unit),
    ]


def _summary_row(period: AggregationPeriod, result: AggregationResult) -> list[str]:
    application = result.applications[0] if len(result.applications) == 1 else None
    application_ranges = ";".join(
        f"{item.start.isoformat()}/{item.end.isoformat()}"
        f"（版:{item.version.effective_from_date.isoformat()}）"
        for item in result.applications
    )
    return [
        "集計",
        *_common(period, result),
        _text(";".join(sorted(result.location_ids))),
        application.version.effective_from_date.isoformat() if application else "",
        ("はい" if application.version.is_retroactive else "いいえ") if application else "",
        *("" for _ in range(7)),
        display_value(result.value, result.decimal_places),
        "" if result.value is None else format(result.value, "f"),
        *issue_codes(result.issues),
        application_ranges,
        "",
        "",
        "",
        result_state(result, is_in_progress=period.is_in_progress),
    ]


def _unapplied_row(period: AggregationPeriod, result: AggregationResult) -> list[str]:
    return [
        "集計",
        *_common(period, result),
        *("" for _ in range(19)),
        result_state(None, is_in_progress=period.is_in_progress),
    ]


def _detail_row(
    period: AggregationPeriod, result: AggregationResult, detail: AggregationDetail
) -> list[str]:
    application = next(
        (
            item
            for item in result.applications
            if item.version.effective_from_date == detail.version_effective_from
            and item.start <= detail.business_date < item.end
        ),
        None,
    )
    return [
        "明細",
        *_common(period, result),
        _text(detail.location_id),
        detail.version_effective_from.isoformat() if detail.version_effective_from else "",
        ("はい" if detail.retroactive else "いいえ") if detail.version_effective_from else "",
        _text(detail.measurement_definition_id),
        format(detail.coefficient, "f"),
        SOURCE_LABELS[detail.source],
        _text(detail.source_id),
        detail.baseline.business_date.isoformat() if detail.baseline else "",
        detail.business_date.isoformat(),
        "" if detail.source_usage is None else format(detail.source_usage, "f"),
        display_value(detail.contribution, result.decimal_places),
        "" if detail.contribution is None else format(detail.contribution, "f"),
        *issue_codes(detail.issues),
        (
            f"{application.start.isoformat()}/{application.end.isoformat()}"
            f"（版:{application.version.effective_from_date.isoformat()}）"
            if application
            else ""
        ),
        BASELINE_LABELS[detail.baseline.source] if detail.baseline else "",
        _text(detail.baseline.source_id) if detail.baseline else "",
        (
            format(detail.baseline.reading, "f")
            if detail.baseline and detail.baseline.reading is not None
            else ""
        ),
        "",
    ]


def aggregation_csv(
    report: AggregationReport, target_type: AggregationTargetType, target_id: str
) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)
    template = next(
        (
            result
            for period in report.periods
            for result in period.results
            if result.target_type == target_type and result.target_id == target_id
        ),
        None,
    )
    if template is None:
        raise ValidationError("出力対象の集計結果がありません。集計を再取得してください。")
    for period in report.periods:
        result = next(
            (
                item
                for item in period.results
                if item.target_type == target_type and item.target_id == target_id
            ),
            None,
        )
        if result is None:
            writer.writerow(_unapplied_row(period, template))
        else:
            writer.writerow(_summary_row(period, result))
            writer.writerows(_detail_row(period, result, detail) for detail in result.details)
    return "\ufeff" + output.getvalue()


def save_aggregation_csv(
    path: Path, report: AggregationReport, target_type: AggregationTargetType, target_id: str
) -> None:
    try:
        atomic_write_text(path, aggregation_csv(report, target_type, target_id))
    except OSError as error:
        raise FileOutputError(
            f"集計 CSV を保存できませんでした。\n保存先: {path}\n"
            "保存先やアクセス権を確認してください。"
        ) from error


def save_chart_png(path: Path, view: QChartView) -> None:
    image = view.grab()
    if image.isNull():
        raise FileOutputError("グラフの画像を取得できませんでした。")
    output = QSaveFile(str(path))
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise FileOutputError(
            f"PNG を保存できませんでした。\n保存先: {path}\n{output.errorString()}"
        )
    try:
        if not image.save(output, "PNG"):
            raise FileOutputError(f"PNG の画像書き込みに失敗しました。\n保存先: {path}")
        if not output.commit():
            raise FileOutputError(
                f"PNG の保存を確定できませんでした。\n保存先: {path}\n{output.errorString()}"
            )
    finally:
        if output.isOpen():
            output.cancelWriting()
            # 中止後の commit で一時ファイルを破棄する。
            output.commit()
        del output
