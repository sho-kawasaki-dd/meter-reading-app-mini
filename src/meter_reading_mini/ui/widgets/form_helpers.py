"""画面間で共有する入力変換。"""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QComboBox

from meter_reading_mini.core.errors import ValidationError


def qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def python_date(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


def selected_value(combo: QComboBox, label: str) -> str:
    value = combo.currentData()
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label}を選択してください。")
    return value


def select_value(combo: QComboBox, value: str, label: str) -> None:
    index = combo.findData(value)
    if index < 0:
        raise ValidationError(f"{label}を表示できません: {value}")
    combo.setCurrentIndex(index)


def day_offset(text: str, base_date: date, label: str) -> int:
    try:
        value = int(text.strip())
    except ValueError as error:
        raise ValidationError(f"{label}は整数で入力してください。") from error
    try:
        _ = base_date + timedelta(days=value)
    except OverflowError as error:
        raise ValidationError(f"{label}による日付が対応範囲外です。") from error
    return value
