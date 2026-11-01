"""業務タイムゾーンの壁時計日時を入力する。"""

from __future__ import annotations

from datetime import datetime, tzinfo

from PySide6.QtCore import QDate, QSignalBlocker, QTime, Signal
from PySide6.QtWidgets import QDateEdit, QHBoxLayout, QTimeEdit, QWidget

from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.timezone_settings import normalize_to_utc, wall_datetime_to_utc


class WallDateTimeEdit(QWidget):
    value_changed = Signal()

    def __init__(self, value: datetime, timezone: tzinfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._timezone = timezone
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy/MM/dd")
        self.date_edit.setDateRange(QDate(1, 1, 1), QDate(9999, 12, 31))
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm:ss")
        layout.addWidget(self.date_edit)
        layout.addWidget(self.time_edit)
        self.set_datetime(value)
        self.date_edit.dateChanged.connect(lambda *_: self.value_changed.emit())
        self.time_edit.timeChanged.connect(lambda *_: self.value_changed.emit())

    def set_datetime(self, value: datetime) -> None:
        local = normalize_to_utc(value).astimezone(self._timezone)
        day = QDate(local.year, local.month, local.day)
        clock = QTime(local.hour, local.minute, local.second)
        with QSignalBlocker(self.date_edit), QSignalBlocker(self.time_edit):
            self.date_edit.setDate(day)
            self.time_edit.setTime(clock)
        if self.date_edit.date() != day or self.time_edit.time() != clock:
            raise ValidationError("日時を入力欄に表示できません。")
        self.value_changed.emit()

    def to_utc(self, *, now: datetime) -> datetime:
        day = self.date_edit.date()
        clock = self.time_edit.time()
        wall = datetime(
            day.year(),
            day.month(),
            day.day(),
            clock.hour(),
            clock.minute(),
            clock.second(),
        )
        return wall_datetime_to_utc(wall, self._timezone, now=now)
