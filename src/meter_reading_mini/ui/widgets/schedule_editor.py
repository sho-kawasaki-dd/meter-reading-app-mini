"""周期別・測定定義別で共用するスケジュール編集部品。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.core.contract_models import ReadingCycle, ReadingSession
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    DailySchedule,
    EveryTwoMonthsSchedule,
    MonthlySchedule,
    MonthlyScheduleSelectorType,
    MonthParity,
    ScheduleItem,
    ScheduleSettings,
    WeeklySchedule,
)
from meter_reading_mini.core.schedule_settings import (
    validate_expected_daily_count,
    validate_schedule_type_for_reading_cycle,
)
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.widgets.form_helpers import (
    day_offset,
    python_date,
    qdate,
    select_value,
    selected_value,
)

WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")
CYCLE_LABELS: dict[ReadingCycle, str] = {
    ReadingCycle.DAILY_1X: "日次 1 回",
    ReadingCycle.DAILY_2X: "日次 2 回",
    ReadingCycle.DAILY_3X: "日次 3 回",
    ReadingCycle.WEEKLY: "週次",
    ReadingCycle.MONTHLY: "月次",
    ReadingCycle.EVERY_TWO_MONTHS: "隔月",
}


def _session_combo() -> QComboBox:
    combo = QComboBox()
    for session in ReadingSession:
        combo.addItem(session.value, session.value)
    return combo


def _weekday_combo() -> QComboBox:
    combo = QComboBox()
    for index, name in enumerate(WEEKDAYS):
        combo.addItem(f"{name}曜日", str(index))
    return combo


def schedule_description(schedule: ScheduleSettings) -> str:
    _validate_displayable_schedule(schedule)

    def session_text(session: ReadingSession, offset: int) -> str:
        return f"{session.value}（日付オフセット {offset:+d}）"

    if isinstance(schedule, DailySchedule):
        return "、".join(
            session_text(item.reading_session, item.day_offset) for item in schedule.reading_items
        )
    if isinstance(schedule, WeeklySchedule):
        if not 0 <= schedule.weekday < len(WEEKDAYS):
            raise ValidationError("スケジュールの曜日が正しくありません。")
        return f"毎週{WEEKDAYS[schedule.weekday]}曜日 " + session_text(
            schedule.reading_session, schedule.day_offset
        )
    if schedule.selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
        selector = f"{schedule.day_of_month}日"
    elif schedule.selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
        if schedule.nth_weekday is None or not 0 <= schedule.nth_weekday < len(WEEKDAYS):
            raise ValidationError("スケジュールの曜日が正しくありません。")
        selector = f"第{schedule.nth}{WEEKDAYS[schedule.nth_weekday]}曜日"
    elif schedule.edge == "first":
        selector = "月初"
    elif schedule.edge == "last":
        selector = "月末"
    else:
        raise ValidationError("スケジュールの月端指定が正しくありません。")
    prefix = ""
    if isinstance(schedule, EveryTwoMonthsSchedule):
        prefix = "奇数月 " if schedule.month_parity is MonthParity.ODD else "偶数月 "
    return prefix + selector + " " + session_text(schedule.reading_session, schedule.day_offset)


def _validate_displayable_schedule(schedule: ScheduleSettings) -> None:
    if isinstance(schedule, WeeklySchedule) and not 0 <= schedule.weekday < len(WEEKDAYS):
        raise ValidationError("スケジュールの曜日が正しくありません。")
    if isinstance(schedule, (MonthlySchedule, EveryTwoMonthsSchedule)):
        if schedule.selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
            if schedule.day_of_month is None or not 1 <= schedule.day_of_month <= 31:
                raise ValidationError("スケジュールの指定日が正しくありません。")
        elif schedule.selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
            if schedule.nth is None or not 1 <= schedule.nth <= 5:
                raise ValidationError("スケジュールの第 N 曜日が正しくありません。")
            if schedule.nth_weekday is None or not 0 <= schedule.nth_weekday < len(WEEKDAYS):
                raise ValidationError("スケジュールの曜日が正しくありません。")
        elif schedule.edge not in ("first", "last"):
            raise ValidationError("スケジュールの月端指定が正しくありません。")


class ScheduleEditor(QWidget):
    changed = Signal()

    def __init__(
        self, current_business_date: Callable[[], date], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._current_business_date = current_business_date
        self._cycle: ReadingCycle | None = None
        self._loading = False
        layout = QVBoxLayout(self)

        self.cycle_label = QLabel()
        layout.addWidget(self.cycle_label)
        self.effective_date_edit = QDateEdit()
        self.effective_date_edit.setObjectName("scheduleEffectiveDate")
        self.effective_date_edit.setCalendarPopup(True)
        self.effective_date_edit.setDisplayFormat("yyyy/MM/dd")
        today = qdate(current_business_date())
        self.effective_date_edit.setMinimumDate(today)
        self.effective_date_edit.setDate(today)
        dates = QFormLayout()
        dates.addRow("適用開始日", self.effective_date_edit)
        layout.addLayout(dates)

        self.daily_widget = QWidget()
        daily_layout = QVBoxLayout(self.daily_widget)
        self.daily_table = QTableWidget(0, 2)
        self.daily_table.setObjectName("scheduleDailyItems")
        self.daily_table.setHorizontalHeaderLabels(["検針時間", "日付オフセット"])
        self.daily_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.daily_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.daily_table.setMinimumHeight(140)
        daily_layout.addWidget(self.daily_table)
        buttons = QHBoxLayout()
        self.add_item_button = QPushButton("検針時間を追加")
        self.remove_item_button = QPushButton("選択した時間を削除")
        buttons.addWidget(self.add_item_button)
        buttons.addWidget(self.remove_item_button)
        buttons.addStretch()
        daily_layout.addLayout(buttons)
        layout.addWidget(self.daily_widget)

        self.single_widget = QWidget()
        single_layout = QFormLayout(self.single_widget)
        self.single_session = _session_combo()
        self.single_offset = QLineEdit("0")
        single_layout.addRow("検針時間", self.single_session)
        single_layout.addRow("日付オフセット", self.single_offset)
        layout.addWidget(self.single_widget)

        self.weekly_widget = QWidget()
        weekly_layout = QFormLayout(self.weekly_widget)
        self.weekday = _weekday_combo()
        weekly_layout.addRow("曜日", self.weekday)
        layout.addWidget(self.weekly_widget)

        self.monthly_widget = QWidget()
        monthly_layout = QFormLayout(self.monthly_widget)
        self.selector = QComboBox()
        for label, value in (
            ("指定日", MonthlyScheduleSelectorType.DAY_OF_MONTH),
            ("第 N 曜日", MonthlyScheduleSelectorType.NTH_WEEKDAY),
            ("月端", MonthlyScheduleSelectorType.MONTH_EDGE),
        ):
            self.selector.addItem(label, value.value)
        self.day_of_month = QSpinBox()
        self.day_of_month.setRange(1, 31)
        self.nth = QSpinBox()
        self.nth.setRange(1, 5)
        self.nth_weekday = _weekday_combo()
        self.edge = QComboBox()
        self.edge.addItem("月初", "first")
        self.edge.addItem("月末", "last")
        self.parity = QComboBox()
        self.parity.addItem("奇数月", MonthParity.ODD.value)
        self.parity.addItem("偶数月", MonthParity.EVEN.value)
        monthly_layout.addRow("日付指定", self.selector)
        monthly_layout.addRow("指定日", self.day_of_month)
        monthly_layout.addRow("第 N", self.nth)
        monthly_layout.addRow("曜日", self.nth_weekday)
        monthly_layout.addRow("月端", self.edge)
        monthly_layout.addRow("奇偶月", self.parity)
        layout.addWidget(self.monthly_widget)

        layout.addWidget(QLabel("適用履歴"))
        self.history_text = QPlainTextEdit()
        self.history_text.setObjectName("scheduleHistory")
        self.history_text.setReadOnly(True)
        self.history_text.setMaximumHeight(110)
        layout.addWidget(self.history_text)

        self.add_item_button.clicked.connect(lambda: self.add_daily_item())
        self.remove_item_button.clicked.connect(
            lambda: run_guarded(self, self._remove_selected_item)
        )
        for combo in (
            self.single_session,
            self.weekday,
            self.selector,
            self.nth_weekday,
            self.edge,
            self.parity,
        ):
            combo.currentIndexChanged.connect(self._on_changed)
        for spin in (self.day_of_month, self.nth):
            spin.valueChanged.connect(self._on_changed)
        self.single_offset.textChanged.connect(self._on_changed)
        self.selector.currentIndexChanged.connect(self._update_selector_fields)

    def _on_changed(self, *_args: object) -> None:
        if not self._loading:
            self.changed.emit()

    def _update_selector_fields(self, *_args: object) -> None:
        selected = self.selector.currentData()
        self.day_of_month.setEnabled(selected == MonthlyScheduleSelectorType.DAY_OF_MONTH.value)
        is_nth = selected == MonthlyScheduleSelectorType.NTH_WEEKDAY.value
        self.nth.setEnabled(is_nth)
        self.nth_weekday.setEnabled(is_nth)
        self.edge.setEnabled(selected == MonthlyScheduleSelectorType.MONTH_EDGE.value)

    def add_daily_item(
        self, session: ReadingSession = ReadingSession.SESSION_0800, day_offset: int = 0
    ) -> None:
        row = self.daily_table.rowCount()
        self.daily_table.insertRow(row)
        combo = _session_combo()
        select_value(combo, session.value, "検針時間")
        offset = QLineEdit(str(day_offset))
        combo.currentIndexChanged.connect(self._on_changed)
        offset.textChanged.connect(self._on_changed)
        self.daily_table.setCellWidget(row, 0, combo)
        self.daily_table.setCellWidget(row, 1, offset)
        self._on_changed()

    def _remove_selected_item(self) -> None:
        row = self.daily_table.currentRow()
        if row < 0:
            raise ValidationError("削除する検針時間を選択してください。")
        self.daily_table.removeRow(row)
        self._on_changed()

    def set_schedule(self, cycle: ReadingCycle, schedule: ScheduleSettings) -> None:
        validate_schedule_type_for_reading_cycle(cycle, schedule)
        _validate_displayable_schedule(schedule)
        self._loading = True
        try:
            self._cycle = cycle
            self.cycle_label.setText(f"検針周期: {CYCLE_LABELS[cycle]}")
            self.daily_widget.setVisible(isinstance(schedule, DailySchedule))
            self.single_widget.setVisible(not isinstance(schedule, DailySchedule))
            self.weekly_widget.setVisible(isinstance(schedule, WeeklySchedule))
            self.monthly_widget.setVisible(
                isinstance(schedule, (MonthlySchedule, EveryTwoMonthsSchedule))
            )
            self.parity.setEnabled(isinstance(schedule, EveryTwoMonthsSchedule))
            if isinstance(schedule, DailySchedule):
                self.daily_table.setRowCount(0)
                for item in schedule.reading_items:
                    self.add_daily_item(item.reading_session, item.day_offset)
            else:
                select_value(self.single_session, schedule.reading_session.value, "検針時間")
                self.single_offset.setText(str(schedule.day_offset))
                if isinstance(schedule, WeeklySchedule):
                    select_value(self.weekday, str(schedule.weekday), "曜日")
                else:
                    select_value(self.selector, schedule.selector_type.value, "日付指定")
                    if schedule.selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
                        if schedule.day_of_month is None or not 1 <= schedule.day_of_month <= 31:
                            raise ValidationError("スケジュールの指定日が正しくありません。")
                        self.day_of_month.setValue(schedule.day_of_month)
                    elif schedule.selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
                        if schedule.nth is None or not 1 <= schedule.nth <= 5:
                            raise ValidationError("スケジュールの第 N 曜日が正しくありません。")
                        self.nth.setValue(schedule.nth)
                        select_value(self.nth_weekday, str(schedule.nth_weekday), "曜日")
                    else:
                        select_value(self.edge, schedule.edge or "", "月端")
                    if isinstance(schedule, EveryTwoMonthsSchedule):
                        select_value(self.parity, schedule.month_parity.value, "奇偶月")
            self._update_selector_fields()
        finally:
            self._loading = False

    def schedule(self) -> ScheduleSettings:
        cycle = self._cycle
        if cycle is None:
            raise ValidationError("検針周期を選択してください。")
        effective = self.effective_date()
        if cycle in (ReadingCycle.DAILY_1X, ReadingCycle.DAILY_2X, ReadingCycle.DAILY_3X):
            items = []
            for row in range(self.daily_table.rowCount()):
                combo = self.daily_table.cellWidget(row, 0)
                offset = self.daily_table.cellWidget(row, 1)
                if not isinstance(combo, QComboBox) or not isinstance(offset, QLineEdit):
                    raise ValidationError("検針時間の入力欄を読み取れません。")
                items.append(
                    ScheduleItem(
                        ReadingSession(selected_value(combo, "検針時間")),
                        day_offset(offset.text(), effective, "日付オフセット"),
                    )
                )
            result: ScheduleSettings = DailySchedule(tuple(items))
        else:
            session = ReadingSession(selected_value(self.single_session, "検針時間"))
            offset = day_offset(self.single_offset.text(), effective, "日付オフセット")
            if cycle is ReadingCycle.WEEKLY:
                result = WeeklySchedule(int(selected_value(self.weekday, "曜日")), session, offset)
            else:
                selector = MonthlyScheduleSelectorType(selected_value(self.selector, "日付指定"))
                fields = {
                    "day_of_month": (
                        self.day_of_month.value()
                        if selector is MonthlyScheduleSelectorType.DAY_OF_MONTH
                        else None
                    ),
                    "nth": (
                        self.nth.value()
                        if selector is MonthlyScheduleSelectorType.NTH_WEEKDAY
                        else None
                    ),
                    "nth_weekday": (
                        int(selected_value(self.nth_weekday, "曜日"))
                        if selector is MonthlyScheduleSelectorType.NTH_WEEKDAY
                        else None
                    ),
                    "edge": (
                        selected_value(self.edge, "月端")
                        if selector is MonthlyScheduleSelectorType.MONTH_EDGE
                        else None
                    ),
                }
                if cycle is ReadingCycle.MONTHLY:
                    result = MonthlySchedule(
                        selector, **fields, reading_session=session, day_offset=offset
                    )
                else:
                    result = EveryTwoMonthsSchedule(
                        selector,
                        MonthParity(selected_value(self.parity, "奇偶月")),
                        **fields,
                        reading_session=session,
                        day_offset=offset,
                    )
        validate_schedule_type_for_reading_cycle(cycle, result)
        validate_expected_daily_count(cycle, result)
        return result

    def effective_date(self) -> date:
        result = python_date(self.effective_date_edit.date())
        if result < self._current_business_date():
            raise ValidationError("適用開始日に過去の日付は指定できません。")
        return result

    def set_history(self, lines: Sequence[str]) -> None:
        self.history_text.setPlainText("\n".join(lines))
