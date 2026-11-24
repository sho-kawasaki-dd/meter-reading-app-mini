"""保存済み検針履歴の表示モデル。"""

from __future__ import annotations

from datetime import datetime, tzinfo
from decimal import Decimal

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from meter_reading_mini.core.reading_history_contracts import (
    ReadingHistoryCode,
    ReadingHistoryRow,
    ReadingHistoryRowKind,
)
from meter_reading_mini.ui.models.reading_overview_model import (
    PREVIOUS_SOURCE_LABELS,
    RESULT_STATUS_LABELS,
    TASK_STATE_LABELS,
)

HEADERS = (
    "行種別",
    "業務日",
    "検針時間",
    "予定日時",
    "枠状態",
    "結果状態",
    "メーター",
    "製造番号",
    "読値",
    "前回値",
    "前回値の出所",
    "前回値の参照元",
    "倍率",
    "ロールオーバー値",
    "使用量",
    "力率",
    "記録日時",
    "担当者",
    "取込元",
    "備考",
    "交換ID",
    "予定枠ID",
    "警告・情報",
)
ROW_LABELS = {
    ReadingHistoryRowKind.READING: "検針",
    ReadingHistoryRowKind.REPLACEMENT_OLD_FINAL: "交換（旧最終）",
    ReadingHistoryRowKind.REPLACEMENT_NEW_INITIAL: "交換（新開始）",
}
WARNING_CODES = frozenset(
    {
        ReadingHistoryCode.STATE_MISMATCH,
        ReadingHistoryCode.BASELINE_MISMATCH,
        ReadingHistoryCode.ASSIGNMENT_UNRESOLVED,
        ReadingHistoryCode.ASSIGNMENT_PENDING,
        ReadingHistoryCode.REPLACEMENT_FINAL_MISSING,
        ReadingHistoryCode.REPLACEMENT_INITIAL_MISSING,
    }
)


def decimal_text(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def local_datetime(value: datetime | None, timezone: tzinfo) -> str:
    return "" if value is None else value.astimezone(timezone).isoformat(sep=" ")


def _values(row: ReadingHistoryRow, timezone: tzinfo) -> tuple[str, ...]:
    source = (
        PREVIOUS_SOURCE_LABELS[row.previous_source]
        if row.previous_source is not None
        else "出所未確定"
        if any(issue.code is ReadingHistoryCode.BASELINE_MISMATCH for issue in row.issues)
        else ""
    )
    return (
        ROW_LABELS[row.row_kind],
        row.business_date.isoformat(),
        row.reading_session.value if row.reading_session else "",
        local_datetime(row.at, timezone),
        TASK_STATE_LABELS[row.task_state] if row.task_state else "",
        RESULT_STATUS_LABELS[row.result_status] if row.result_status else "",
        f"{row.meter_name} [{row.meter_id}]",
        row.meter_serial or "",
        decimal_text(row.raw_reading),
        decimal_text(row.last_reading),
        source,
        row.previous_reference_id or "",
        decimal_text(row.multiplier),
        decimal_text(row.rollover_value),
        decimal_text(row.usage_delta),
        row.power_factor or "",
        local_datetime(row.recorded_at, timezone),
        row.updated_by or "",
        row.result_source or "",
        row.note or "",
        row.replacement_event_id or "",
        row.planned_reading_id or "",
        " / ".join(issue.message for issue in row.issues),
    )


class ReadingHistoryTableModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: tuple[ReadingHistoryRow, ...] = ()
        self._values: tuple[tuple[str, ...], ...] = ()

    def set_rows(self, rows: tuple[ReadingHistoryRow, ...], timezone: tzinfo) -> None:
        values = tuple(_values(row, timezone) for row in rows)
        self.beginResetModel()
        self._rows = rows
        self._values = values
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return HEADERS[section] if 0 <= section < len(HEADERS) else None
        return None

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        if not 0 <= index.column() < len(HEADERS):
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._values[index.row()][index.column()]
        if role == Qt.ItemDataRole.ToolTipRole and row.issues:
            return "\n".join(f"{issue.code.value}: {issue.message}" for issue in row.issues)
        if role == Qt.ItemDataRole.BackgroundRole:
            if any(issue.code in WARNING_CODES for issue in row.issues):
                return QBrush(QColor("#fff1a8"))
            if row.row_kind is not ReadingHistoryRowKind.READING:
                return QBrush(QColor("#dceeff"))
        return None
