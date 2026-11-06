"""予定枠と結果を一覧表示するテーブルモデル。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import tzinfo
from decimal import Decimal

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from meter_reading_mini.core.contract_models import (
    Category,
    PowerFactorPhase,
    PowerFactorReading,
    Purpose,
    ReadingCycle,
    ResultStatus,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    PlannedReadingCloseReason,
    PlannedReadingTaskState,
    ReadingOverviewRecord,
)
from meter_reading_mini.core.previous_reading import PreviousReadingSource
from meter_reading_mini.core.reading_overview import is_reading_warning

CATEGORY_LABELS = {
    Category.ELECTRICITY: "電気",
    Category.GAS: "ガス",
    Category.WATER: "水道",
    Category.BOILER: "ボイラー",
    Category.OTHERS: "その他",
}
PURPOSE_LABELS = {
    Purpose.OPERATIONAL: "運用",
    Purpose.BILLING: "請求",
    Purpose.AUDIT: "監査",
}
CYCLE_LABELS = {
    ReadingCycle.DAILY_1X: "日次1回",
    ReadingCycle.DAILY_2X: "日次2回",
    ReadingCycle.DAILY_3X: "日次3回",
    ReadingCycle.WEEKLY: "週次",
    ReadingCycle.MONTHLY: "月次",
    ReadingCycle.EVERY_TWO_MONTHS: "隔月",
}
CYCLE_GROUP_LABELS = {
    "daily": "日次",
    ReadingCycle.WEEKLY.value: "週次",
    ReadingCycle.MONTHLY.value: "月次",
    ReadingCycle.EVERY_TWO_MONTHS.value: "隔月",
}
PREVIOUS_SOURCE_LABELS = {
    PreviousReadingSource.MEASURED: "前回実測",
    PreviousReadingSource.REPLACEMENT_INITIAL: "交換時の新メーター初期値",
    PreviousReadingSource.REPLACEMENT_NO_INITIAL: "交換時の初期値なし",
    PreviousReadingSource.NONE: "前回値なし",
}
TASK_STATE_LABELS = {
    PlannedReadingTaskState.OPEN: "未消化",
    PlannedReadingTaskState.COMPLETED: "完了",
    PlannedReadingTaskState.AUTO_CLOSED: "自動クローズ",
    PlannedReadingTaskState.SUPERSEDED: "差し替え済み",
}
RESULT_STATUS_LABELS = {
    ResultStatus.MEASURED: "実測",
    ResultStatus.SKIPPED: "未実施",
    ResultStatus.UNREACHABLE: "到達不可",
    ResultStatus.METER_FAULT: "計器不良",
    ResultStatus.REPLACED: "メーター交換",
}
CLOSE_REASON_LABELS = {
    PlannedReadingCloseReason.CUTOFF_ELAPSED: "締め時刻経過",
    PlannedReadingCloseReason.METER_REPLACED: "メーター交換",
    PlannedReadingCloseReason.SCHEDULE_RECOMPOSED: "スケジュール変更",
    PlannedReadingCloseReason.DEFINITION_DEACTIVATED: "測定定義の無効化",
}
PHASE_LABELS = {
    PowerFactorPhase.LEAD: "進み",
    PowerFactorPhase.LAG: "遅れ",
    PowerFactorPhase.UNITY: "力率1",
}
HEADERS = (
    "予定日時",
    "場所",
    "メーター",
    "測定対象",
    "周期",
    "枠状態",
    "結果状態",
    "読値",
    "前回値",
    "使用量",
    "単位",
    "廃止・終了理由",
)


def _decimal_text(value: Decimal | None) -> str:
    return "—" if value is None else format(value, "f")


def _reading_text(reading: ReadingOverviewRecord) -> str:
    if reading.structured_payload_json is not None:
        if reading.raw_reading is not None:
            raise ValueError(f"読値の形式が重複しています: {reading.planned_reading_id}")
        payload = PowerFactorReading.model_validate_json(reading.structured_payload_json)
        return f"{format(payload.percent, 'f')}%（{PHASE_LABELS[payload.phase]}）"
    return _decimal_text(reading.raw_reading)


def _row_values(reading: ReadingOverviewRecord, timezone: tzinfo) -> tuple[str, ...]:
    result = "—" if reading.result_status is None else RESULT_STATUS_LABELS[reading.result_status]
    if reading.task_state is PlannedReadingTaskState.SUPERSEDED and reading.result_status:
        result = f"旧結果: {result}"
    return (
        reading.scheduled_at.astimezone(timezone).strftime("%Y/%m/%d %H:%M"),
        reading.location_name,
        reading.meter_name,
        reading.display_name,
        CYCLE_LABELS[reading.reading_cycle],
        TASK_STATE_LABELS[reading.task_state],
        result,
        _reading_text(reading),
        _decimal_text(reading.last_reading),
        _decimal_text(reading.usage_delta),
        reading.unit,
        "—" if reading.close_reason is None else CLOSE_REASON_LABELS[reading.close_reason],
    )


class ReadingOverviewTableModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._readings: tuple[ReadingOverviewRecord, ...] = ()
        self._rows: tuple[tuple[str, ...], ...] = ()

    def set_readings(self, readings: Sequence[ReadingOverviewRecord], timezone: tzinfo) -> None:
        values = tuple(readings)
        rows = tuple(_row_values(reading, timezone) for reading in values)
        self.beginResetModel()
        self._readings = values
        self._rows = rows
        self.endResetModel()

    def reading_at(self, row: int) -> ReadingOverviewRecord | None:
        if 0 <= row < len(self._readings):
            return self._readings[row]
        return None

    def row_for_id(self, planned_reading_id: str) -> int | None:
        return next(
            (
                row
                for row, reading in enumerate(self._readings)
                if reading.planned_reading_id == planned_reading_id
            ),
            None,
        )

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
        reading = self._readings[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.BackgroundRole:
            if is_reading_warning(reading):
                return QBrush(QColor("#fff1a8"))
            if reading.task_state is PlannedReadingTaskState.AUTO_CLOSED:
                return QBrush(QColor("#e4e4e4"))
            if reading.task_state is PlannedReadingTaskState.OPEN:
                return QBrush(QColor("#dceeff"))
        if role == Qt.ItemDataRole.ToolTipRole:
            details = [f"予定枠 ID: {reading.planned_reading_id}"]
            if reading.superseded_by_planned_reading_id:
                details.append(f"後継枠 ID: {reading.superseded_by_planned_reading_id}")
            if reading.note:
                details.append(f"備考: {reading.note}")
            return "\n".join(details)
        return None
