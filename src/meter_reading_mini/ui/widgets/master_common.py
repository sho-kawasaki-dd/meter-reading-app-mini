"""マスタ画面の一覧と確認操作を共有する。"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QMessageBox,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from meter_reading_mini.core.contract_models import Category
from meter_reading_mini.ui.models.reading_overview_model import CATEGORY_LABELS


def category_combo() -> QComboBox:
    combo = QComboBox()
    for category in Category:
        combo.addItem(CATEGORY_LABELS[category], category.value)
    return combo


def make_table(headers: Sequence[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setMinimumHeight(150)
    return table


def selected_id(table: QTableWidget) -> str | None:
    item = table.item(table.currentRow(), 0)
    value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
    return value if isinstance(value, str) else None


def populate_table(
    table: QTableWidget, rows: Sequence[tuple[str, Sequence[str]]], selected: str | None
) -> bool:
    selected_row = -1
    with QSignalBlocker(table):
        table.setRowCount(len(rows))
        for row, (key, values) in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, key)
                table.setItem(row, column, item)
            if key == selected:
                selected_row = row
        if selected_row >= 0:
            table.selectRow(selected_row)
        else:
            table.setCurrentCell(-1, -1)
            table.clearSelection()
    return selected_row >= 0


def confirm(parent: QWidget, title: str, message: str) -> bool:
    return (
        QMessageBox.question(
            parent,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        == QMessageBox.StandardButton.Yes
    )


def scroll_area(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area
