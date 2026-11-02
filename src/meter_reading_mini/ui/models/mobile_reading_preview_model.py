"""スマホ検針の入力・既存結果・最終見込を対比する表。"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from pydantic import ValidationError as PydanticValidationError
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from meter_reading_mini.core.contract_models import PowerFactorReading, ResultStatus
from meter_reading_mini.core.meter_reading_sync_repositories import ResultSource
from meter_reading_mini.core.mobile_reading_import import (
    MobileReadingDisposition,
    MobileReadingImportPreview,
    MobileReadingPreviewRow,
    MobileReadingWarning,
    MobileReadingWarningCode,
    describe_mobile_reading_rejection,
)
from meter_reading_mini.core.previous_reading import InitialPreviousReading
from meter_reading_mini.ui.models.reading_overview_model import (
    CYCLE_GROUP_LABELS,
    PHASE_LABELS,
    PREVIOUS_SOURCE_LABELS,
    RESULT_STATUS_LABELS,
    TASK_STATE_LABELS,
)

HEADERS = (
    "予定枠 ID",
    "場所",
    "測定項目",
    "業務日",
    "検針時間",
    "現在の枠状態",
    "採否区分",
    "既存結果",
    "入力結果",
    "入力計算時の前回値・出所",
    "最終見込結果",
    "最終見込の前回値・出所",
    "警告・理由",
)

_DISPOSITION_LABELS = {
    MobileReadingDisposition.NEW: "採用: 新規",
    MobileReadingDisposition.REVIVED: "採用: 自動クローズからの復活",
    MobileReadingDisposition.OVERWRITTEN: "採用: 上書き",
    MobileReadingDisposition.DUPLICATE: "無視: 重複",
    MobileReadingDisposition.STALE: "無視: 古い入力",
    MobileReadingDisposition.REJECTED: "拒否",
}
_RESULT_SOURCES = {
    ResultSource.MANUAL: "PC 直接入力",
    ResultSource.MOBILE_HTML: "スマホ検針",
}
_WARNING_LABELS = {
    MobileReadingWarningCode.BUSINESS_DATE_MISMATCH: "業務日がシートと異なります",
    MobileReadingWarningCode.READING_SESSION_MISMATCH: "検針時間がシートの条件と異なります",
    MobileReadingWarningCode.CYCLE_GROUP_MISMATCH: "周期区分がシートの条件と異なります",
    MobileReadingWarningCode.THRESHOLD_EXCEEDED: "警告閾値を超えています",
    MobileReadingWarningCode.ROLLOVER_APPLIED: "ロールオーバーを適用します",
}


def _decimal_text(value: Decimal | str | None) -> str:
    if value is None:
        return "なし"
    return format(value, "f") if isinstance(value, Decimal) else value


def _value_text(raw: Decimal | str | None, structured: str | None, unit: str) -> str:
    values: list[str] = []
    if raw is not None:
        values.append(f"{_decimal_text(raw)} {unit}".rstrip())
    if structured is not None:
        try:
            reading = PowerFactorReading.model_validate_json(structured)
        except PydanticValidationError:
            values.append(f"構造化入力: {structured}")
        else:
            values.append(f"{format(reading.percent, 'f')}%（{PHASE_LABELS[reading.phase]}）")
    return " / ".join(values) if values else "なし"


def _usage_text(value: Decimal | None, unit: str) -> str:
    return f"{_decimal_text(value)} {unit}".rstrip() if value is not None else "なし"


def _result_text(
    status: ResultStatus,
    raw: Decimal | str | None,
    structured: str | None,
    usage: str,
    unit: str,
    *,
    last_reading: Decimal | None = None,
    source: ResultSource | None = None,
    note: str | None = None,
    show_last_reading: bool = False,
) -> str:
    lines = [
        f"状態: {RESULT_STATUS_LABELS[status]}",
        f"値: {_value_text(raw, structured, unit)}",
        f"使用量: {usage}",
    ]
    if show_last_reading:
        lines.append(f"前回値: {_decimal_text(last_reading)}")
    if source is not None:
        lines.append(f"結果元: {_RESULT_SOURCES[source]}")
    if note:
        lines.append(f"備考: {note}")
    return "\n".join(lines)


def _previous_text(previous: InitialPreviousReading | None) -> str:
    if previous is None:
        return "未算出"
    source = PREVIOUS_SOURCE_LABELS[previous.source]
    if previous.planned_reading_id is not None:
        source += f": {previous.planned_reading_id}"
    return f"{_decimal_text(previous.reading)}（{source}）"


def _warning_text(warning: MobileReadingWarning) -> str:
    text = _WARNING_LABELS[warning.code]
    if warning.expected is not None and warning.actual is not None:
        expected = warning.expected
        actual = warning.actual
        if warning.code is MobileReadingWarningCode.CYCLE_GROUP_MISMATCH:
            expected = tuple(CYCLE_GROUP_LABELS[value] for value in expected)
            actual = CYCLE_GROUP_LABELS[actual]
        text += f"（シート: {', '.join(expected)} / 予定枠: {actual}）"
    return text


def _row_values(
    row: MobileReadingPreviewRow, recalculated_existing_ids: frozenset[str]
) -> tuple[str, ...]:
    target = row.target
    unit = target.unit if target is not None else ""
    existing = target.existing_result if target is not None else None
    if target is None:
        existing_text = "不明"
    elif existing is None:
        existing_text = "結果なし"
    else:
        existing_text = _result_text(
            existing.result_status,
            existing.raw_reading,
            existing.structured_payload_json,
            _usage_text(existing.usage_delta, unit),
            unit,
            last_reading=existing.last_reading,
            source=existing.result_source,
            note=existing.note,
            show_last_reading=True,
        )
    calculation = row.input_calculation
    input_text = _result_text(
        row.input.result_status,
        row.input.raw_reading,
        row.input.structured_payload_json,
        _usage_text(calculation.usage_delta, unit) if calculation is not None else "未算出",
        unit,
        note=row.input.note,
    )
    final = row.final
    final_result = final.result if final is not None else None
    if final is None:
        final_text = "未算出"
    elif final_result is None:
        final_text = f"枠状態: {TASK_STATE_LABELS[final.task_state]}\n結果なし"
    else:
        if row.disposition in {
            MobileReadingDisposition.DUPLICATE,
            MobileReadingDisposition.STALE,
            MobileReadingDisposition.REJECTED,
        }:
            description = "既存結果を保持"
            if row.input.planned_reading_id in recalculated_existing_ids:
                description += "（間接再計算後）"
        else:
            description = "採用後の見込"
        final_text = (
            f"枠状態: {TASK_STATE_LABELS[final.task_state]}\n{description}\n"
            + _result_text(
                final_result.result_status,
                final_result.raw_reading,
                final_result.structured_payload_json,
                _usage_text(final_result.usage_delta, unit),
                unit,
                last_reading=final_result.last_reading,
                source=final_result.result_source,
                note=final_result.note,
                show_last_reading=True,
            )
        )
    details: list[str] = []
    if row.reason is not None:
        details.append(describe_mobile_reading_rejection(row.reason))
    elif row.disposition is MobileReadingDisposition.DUPLICATE:
        details.append("保存済み結果と同一のため入力を適用しません")
    elif row.disposition is MobileReadingDisposition.STALE:
        details.append("保存済み結果より古いため入力を適用しません")
    details.extend(_warning_text(warning) for warning in row.warnings)
    if final is not None and final.calculation_error is not None:
        details.append(
            "最終見込の前回値を算出できません: "
            + describe_mobile_reading_rejection(final.calculation_error)
        )
    return (
        row.input.planned_reading_id,
        target.location_name if target is not None else "不明",
        target.display_name if target is not None else "不明",
        target.business_date.isoformat() if target is not None else "不明",
        target.reading_session.value if target is not None else "不明",
        TASK_STATE_LABELS[target.task_state] if target is not None else "不明",
        _DISPOSITION_LABELS[row.disposition],
        existing_text,
        input_text,
        _previous_text(calculation.previous if calculation is not None else None),
        final_text,
        _previous_text(final.previous if final is not None else None),
        "\n".join(details) if details else "なし",
    )


def preview_summary_text(preview: MobileReadingImportPreview) -> str:
    summary = preview.summary
    warning_count = sum(bool(row.warnings) for row in preview.rows)
    return (
        f"採用 {summary.accepted} 件（新規 {summary.new}、復活 {summary.revived}、"
        f"上書き {summary.overwritten}） / "
        f"重複 {summary.duplicate}、古い入力 {summary.stale}、拒否 {summary.rejected} 件\n"
        f"警告あり {warning_count} 行 / 間接再計算: 後続実測 {summary.recalculated_existing} 件、"
        f"交換境界 {summary.recalculated_boundaries} 件"
    )


def confirmation_text(preview: MobileReadingImportPreview) -> str:
    summary = preview.summary
    lines = [preview_summary_text(preview)]
    if summary.duplicate or summary.stale or summary.rejected:
        lines.append("不採用の入力は保存しません。")
        reasons = Counter(
            describe_mobile_reading_rejection(row.reason)
            for row in preview.rows
            if row.reason is not None
        )
        lines.extend(f"拒否理由: {reason} {count} 件" for reason, count in reasons.items())
    lines.append("採用対象を一括確定しますか？")
    return "\n".join(lines)


class MobileReadingPreviewTableModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._readings: tuple[MobileReadingPreviewRow, ...] = ()
        self._rows: tuple[tuple[str, ...], ...] = ()

    def set_preview(self, preview: MobileReadingImportPreview | None) -> None:
        readings = preview.rows if preview is not None else ()
        recalculated = (
            preview.effects.recalculated_existing_ids if preview is not None else frozenset()
        )
        rows = tuple(_row_values(row, recalculated) for row in readings)
        self.beginResetModel()
        self._readings = readings
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._readings)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(HEADERS)
        ):
            return HEADERS[section]
        return None

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if not index.isValid() or not (0 <= index.row() < len(self._readings)):
            return None
        if not 0 <= index.column() < len(HEADERS):
            return None
        row = self._readings[index.row()]
        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole}:
            return self._rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.BackgroundRole:
            if row.disposition is MobileReadingDisposition.REJECTED:
                return QBrush(QColor("#ffe1e1"))
            if row.disposition in {
                MobileReadingDisposition.DUPLICATE,
                MobileReadingDisposition.STALE,
            }:
                return QBrush(QColor("#e4e4e4"))
            if row.warnings:
                return QBrush(QColor("#fff1a8"))
            return QBrush(QColor("#e2f2e5"))
        return None
