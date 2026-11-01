"""業務日の予定枠と検針結果を表示するメイン画面。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, time
from pathlib import Path

from PySide6.QtCore import QDate, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QTabBar,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.contract_models import Category, ReadingCycle
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentReviewFlagRecord,
    MeterAssignmentRecord,
    PlannedReadingTaskState,
    ReadingOverviewRecord,
)
from meter_reading_mini.core.reading_overview import (
    ReadingOverviewFilter,
    filter_reading_overview,
    summarize_reading_overview,
)
from meter_reading_mini.ui.dialogs.direct_reading_dialog import DirectReadingDialog
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.dialogs.meter_replacement_dialog import MeterReplacementDialog
from meter_reading_mini.ui.dialogs.settings_dialog import SettingsDialog
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.models.reading_overview_model import (
    CATEGORY_LABELS,
    TASK_STATE_LABELS,
    ReadingOverviewTableModel,
)
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory

_CYCLE_TABS: tuple[tuple[str, frozenset[ReadingCycle]], ...] = (
    ("すべて", frozenset()),
    (
        "日次",
        frozenset({ReadingCycle.DAILY_1X, ReadingCycle.DAILY_2X, ReadingCycle.DAILY_3X}),
    ),
    ("週次", frozenset({ReadingCycle.WEEKLY})),
    ("月次", frozenset({ReadingCycle.MONTHLY})),
    ("隔月", frozenset({ReadingCycle.EVERY_TWO_MONTHS})),
)


@dataclass(frozen=True, slots=True)
class _ClockBoundary:
    business_date: date
    cutoff: time
    after_cutoff: bool


@dataclass(frozen=True, slots=True)
class _ReviewAssignment:
    flag: AssignmentReviewFlagRecord
    assignment: MeterAssignmentRecord

    def contains(self, reading: ReadingOverviewRecord) -> bool:
        return (
            reading.measurement_definition_id == self.assignment.measurement_definition_id
            and reading.meter_id == self.assignment.meter_id
            and reading.scheduled_at >= self.assignment.effective_from
            and (
                self.assignment.effective_to is None
                or reading.scheduled_at < self.assignment.effective_to
            )
        )


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _python_date(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


class MainWindow(QMainWindow):
    def __init__(self, services: AppContext, *, display_settings_path: Path | None = None) -> None:
        super().__init__()
        self._services = services
        self._display_settings_path = display_settings_path
        self._staff_memory = StaffSelectionMemory()
        self._all_readings: tuple[ReadingOverviewRecord, ...] = ()
        self._review_assignments: tuple[_ReviewAssignment, ...] = ()
        self._view_loaded = False
        self._last_boundary = self._clock_boundary()
        self.setWindowTitle("検針管理 mini")
        self.resize(1200, 720)

        self._build_toolbar()
        self._build_contents()
        self._connect_signals()
        self._update_date_buttons()
        self._refresh_and_reload(self._last_boundary)

        self.boundary_timer = QTimer(self)
        self.boundary_timer.setInterval(60_000)
        self.boundary_timer.timeout.connect(self._on_timer)
        self.boundary_timer.start()

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("操作", self)
        toolbar.setObjectName("mainToolbar")
        self.addToolBar(toolbar)
        self.refresh_action = QAction("予定枠を更新", self)
        self.manual_reading_action = QAction("PC 直接入力・訂正", self)
        self.manual_reading_action.setEnabled(False)
        self.master_action = QAction("マスタ管理", self)
        self.replacement_action = QAction("メーター交換", self)
        self.settings_action = QAction("設定", self)
        for action in (
            self.refresh_action,
            self.manual_reading_action,
            self.master_action,
            self.replacement_action,
            self.settings_action,
        ):
            toolbar.addAction(action)

    def _build_contents(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("対象業務日"))
        self.previous_day_button = QPushButton("前日")
        self.date_edit = QDateEdit()
        self.date_edit.setObjectName("businessDate")
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy/MM/dd")
        self.date_edit.setMaximumDate(_qdate(self._last_boundary.business_date))
        self.date_edit.setDate(_qdate(self._last_boundary.business_date))
        self.next_day_button = QPushButton("翌日")
        self.today_button = QPushButton("今日")
        for widget in (
            self.previous_day_button,
            self.date_edit,
            self.next_day_button,
            self.today_button,
        ):
            date_row.addWidget(widget)
        date_row.addStretch()
        layout.addLayout(date_row)

        self.cycle_tabs = QTabBar()
        self.cycle_tabs.setObjectName("readingCycleTabs")
        for label, _ in _CYCLE_TABS:
            self.cycle_tabs.addTab(label)
        layout.addWidget(self.cycle_tabs)

        filters = QHBoxLayout()
        self.state_filter = QComboBox()
        self.state_filter.setObjectName("stateFilter")
        self.state_filter.addItem("すべて", None)
        for state, label in TASK_STATE_LABELS.items():
            self.state_filter.addItem(label, state.value)
        self.category_filter = QComboBox()
        self.category_filter.setObjectName("categoryFilter")
        self.category_filter.addItem("すべて", None)
        for category, label in CATEGORY_LABELS.items():
            self.category_filter.addItem(label, category.value)
        self.location_filter = QComboBox()
        self.location_filter.setObjectName("locationFilter")
        self.location_filter.addItem("すべて", None)
        self.keyword_filter = QLineEdit()
        self.keyword_filter.setObjectName("keywordFilter")
        self.keyword_filter.setPlaceholderText("場所・メーター・測定対象を検索")
        for label, widget in (
            ("状態", self.state_filter),
            ("カテゴリ", self.category_filter),
            ("場所", self.location_filter),
            ("検索", self.keyword_filter),
        ):
            filters.addWidget(QLabel(label))
            filters.addWidget(widget)
        layout.addLayout(filters)

        self.review_group = QGroupBox("計算条件の確認待ち")
        review_layout = QVBoxLayout(self.review_group)
        self.review_text = QPlainTextEdit()
        self.review_text.setObjectName("assignmentReviewReasons")
        self.review_text.setReadOnly(True)
        self.review_text.setMaximumHeight(90)
        self.review_correction_button = QPushButton("計算条件の訂正")
        review_layout.addWidget(self.review_text)
        review_layout.addWidget(self.review_correction_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.review_group.hide()
        layout.addWidget(self.review_group)

        self.table_model = ReadingOverviewTableModel()
        self.table = QTableView()
        self.table.setObjectName("readingOverview")
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, stretch=1)

        detail_row = QHBoxLayout()
        self.selection_details = QLabel("予定枠を選択してください。")
        self.selection_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.successor_button = QPushButton("後継枠を表示")
        self.successor_button.setEnabled(False)
        detail_row.addWidget(self.selection_details, stretch=1)
        detail_row.addWidget(self.successor_button)
        layout.addLayout(detail_row)
        self.statusBar().showMessage("一覧を読み込んでいます。")

    def _connect_signals(self) -> None:
        self.refresh_action.triggered.connect(self.refresh_planned_readings)
        self.manual_reading_action.triggered.connect(self._on_manual_reading)
        self.master_action.triggered.connect(lambda: self._guard(self._open_master))
        self.replacement_action.triggered.connect(lambda: self._guard(self._open_replacement))
        self.settings_action.triggered.connect(lambda: self._guard(self._open_settings))
        self.review_correction_button.clicked.connect(
            lambda: self._guard(self._show_review_guidance)
        )
        self.previous_day_button.clicked.connect(
            lambda: self.date_edit.setDate(self.date_edit.date().addDays(-1))
        )
        self.next_day_button.clicked.connect(
            lambda: self.date_edit.setDate(self.date_edit.date().addDays(1))
        )
        self.today_button.clicked.connect(self._select_today)
        self.date_edit.dateChanged.connect(self._on_date_changed)
        self.cycle_tabs.currentChanged.connect(self._on_filter_changed)
        self.state_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.category_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.location_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.keyword_filter.textChanged.connect(self._on_filter_changed)
        selection = self.table.selectionModel()
        if selection is not None:
            selection.currentChanged.connect(self._on_selection_changed)
        self.successor_button.clicked.connect(self._on_successor)

    def _clock_boundary(self) -> _ClockBoundary:
        current = self._services.current_datetime()
        cutoff = self._services.cutoff_settings.get_cutoff_time()
        return _ClockBoundary(current.date(), cutoff, current.time() >= cutoff)

    def _update_date_buttons(self) -> None:
        selected = self.date_edit.date()
        self.previous_day_button.setEnabled(selected > self.date_edit.minimumDate())
        self.next_day_button.setEnabled(selected < self.date_edit.maximumDate())

    def _select_today(self) -> None:
        self._guard(self._select_current_business_date, clear_on_error=True)

    def _select_current_business_date(self) -> None:
        boundary = self._clock_boundary()
        if boundary != self._last_boundary:
            self._refresh_and_reload(boundary, follow_today=True)
        self.date_edit.setDate(_qdate(boundary.business_date))

    def _on_date_changed(self, *_args: object) -> None:
        self._update_date_buttons()
        self.refresh_planned_readings()

    def _on_filter_changed(self, *_args: object) -> None:
        if self._view_loaded:
            self._guard(self._apply_filters, clear_on_error=True)

    def _on_selection_changed(self, *_args: object) -> None:
        self._update_selection()

    def _on_timer(self) -> None:
        self._guard(self._check_clock_boundary, clear_on_error=True)

    def _check_clock_boundary(self) -> None:
        boundary = self._clock_boundary()
        if boundary != self._last_boundary:
            self._refresh_and_reload(boundary, follow_today=True)

    def _guard(self, operation: Callable[[], object], *, clear_on_error: bool = False) -> None:
        succeeded, _ = run_guarded(self, operation)
        if not succeeded and clear_on_error:
            self._clear_view()

    def _clear_view(self) -> None:
        self._view_loaded = False
        self._all_readings = ()
        self._review_assignments = ()
        self.table_model.set_readings((), UTC)
        self.review_group.hide()
        self._update_selection()
        self.statusBar().showMessage("一覧の更新に失敗しました。予定枠を再更新してください。")

    def refresh_planned_readings(self) -> None:
        self._guard(lambda: self._refresh_and_reload(self._clock_boundary()), clear_on_error=True)

    def notify_dialog_saved(self) -> None:
        self.refresh_planned_readings()

    def _refresh_and_reload(self, boundary: _ClockBoundary, *, follow_today: bool = False) -> None:
        self._services.planned_reading_maintenance.refresh()
        previous_date = _python_date(self.date_edit.date())
        with QSignalBlocker(self.date_edit):
            self.date_edit.setMaximumDate(_qdate(boundary.business_date))
            if follow_today and previous_date == self._last_boundary.business_date:
                self.date_edit.setDate(_qdate(boundary.business_date))
        self._update_date_buttons()
        self._load_view()
        self._last_boundary = boundary

    def _load_review_assignments(self) -> tuple[_ReviewAssignment, ...]:
        service = self._services.assignment_calculation_settings
        flags = service.list_review_flags()
        assignments: dict[str, list[MeterAssignmentRecord]] = {}
        result: list[_ReviewAssignment] = []
        for flag in flags:
            if flag.measurement_definition_id not in assignments:
                assignments[flag.measurement_definition_id] = service.list_assignments(
                    flag.measurement_definition_id
                )
            matching = [
                assignment
                for assignment in assignments[flag.measurement_definition_id]
                if assignment.meter_id == flag.meter_id
                and assignment.effective_from == flag.assignment_effective_from
                and assignment.needs_review
            ]
            if len(matching) != 1:
                raise ValidationError(
                    f"確認待ちの割当を特定できません: {flag.measurement_definition_id} / "
                    f"{flag.meter_id}"
                )
            result.append(_ReviewAssignment(flag, matching[0]))
        return tuple(result)

    def _load_view(self) -> None:
        overview = self._services.reading_overview.get_overview(_python_date(self.date_edit.date()))
        reviews = self._load_review_assignments()
        self._all_readings = overview.readings
        self._review_assignments = reviews
        self._populate_locations()
        self._show_review_assignments()
        self._view_loaded = True
        self._apply_filters()

    def _populate_locations(self) -> None:
        selected = self.location_filter.currentData()
        locations: dict[str, set[str]] = {}
        for reading in self._all_readings:
            locations.setdefault(reading.location_id, set()).add(reading.location_name)
        with QSignalBlocker(self.location_filter):
            self.location_filter.clear()
            self.location_filter.addItem("すべて", None)
            for location_id, names in sorted(
                locations.items(), key=lambda item: (sorted(item[1])[0], item[0])
            ):
                label = " / ".join(sorted(names))
                if len(names) > 1:
                    label += "（場所名が不一致）"
                self.location_filter.addItem(label, location_id)
            index = self.location_filter.findData(selected)
            self.location_filter.setCurrentIndex(max(index, 0))

    def _show_review_assignments(self) -> None:
        if not self._review_assignments:
            self.review_group.hide()
            self.review_text.clear()
            return
        timezone = self._services.timezone_settings.get_timezone()
        lines = []
        for review in self._review_assignments:
            flag = review.flag
            start = flag.assignment_effective_from.astimezone(timezone).strftime("%Y/%m/%d %H:%M")
            lines.append(
                f"測定定義 {flag.measurement_definition_id} / メーター {flag.meter_id}"
                f" / 適用開始 {start}: {flag.reason}"
            )
        self.review_text.setPlainText("\n".join(lines))
        self.review_group.show()

    def _criteria(self) -> ReadingOverviewFilter:
        state = self.state_filter.currentData()
        category = self.category_filter.currentData()
        location = self.location_filter.currentData()
        return ReadingOverviewFilter(
            reading_cycles=_CYCLE_TABS[self.cycle_tabs.currentIndex()][1],
            task_states=(
                frozenset({PlannedReadingTaskState(state)})
                if isinstance(state, str)
                else frozenset()
            ),
            categories=(
                frozenset({Category(category)}) if isinstance(category, str) else frozenset()
            ),
            location_ids=frozenset({location}) if isinstance(location, str) else frozenset(),
            keyword=self.keyword_filter.text(),
        )

    def _selected_reading(self) -> ReadingOverviewRecord | None:
        return self.table_model.reading_at(self.table.currentIndex().row())

    def _apply_filters(self) -> None:
        selected = self._selected_reading()
        readings = filter_reading_overview(self._all_readings, self._criteria())
        counts = summarize_reading_overview(readings)
        self.table_model.set_readings(readings, self._services.timezone_settings.get_timezone())
        if selected is not None:
            row = self.table_model.row_for_id(selected.planned_reading_id)
            if row is not None:
                self.table.selectRow(row)
        self._update_selection()
        self.statusBar().showMessage(
            f"全 {counts.total} 件 / 未消化 {counts.open} / 完了 {counts.completed} / "
            f"自動クローズ {counts.auto_closed} / 差し替え済み {counts.superseded} / "
            f"警告 {counts.warnings}"
        )

    def _update_selection(self) -> None:
        reading = self._selected_reading()
        blocked = reading is not None and any(
            review.contains(reading) for review in self._review_assignments
        )
        self.manual_reading_action.setEnabled(
            reading is not None
            and reading.task_state is not PlannedReadingTaskState.SUPERSEDED
            and not blocked
        )
        self.successor_button.setEnabled(
            reading is not None and reading.superseded_by_planned_reading_id is not None
        )
        if reading is None:
            self.selection_details.setText("予定枠を選択してください。")
            return
        details = f"予定枠 ID: {reading.planned_reading_id}"
        if reading.superseded_by_planned_reading_id:
            details += f" / 後継枠 ID: {reading.superseded_by_planned_reading_id}"
        if blocked:
            details += " / 計算条件の確認待ち"
        self.selection_details.setText(details)

    def _on_successor(self) -> None:
        self._guard(self._select_successor)

    def _select_successor(self) -> None:
        reading = self._selected_reading()
        if reading is None or reading.superseded_by_planned_reading_id is None:
            raise ValidationError("後継枠のある予定枠を選択してください。")
        successor_id = reading.superseded_by_planned_reading_id
        if not any(item.planned_reading_id == successor_id for item in self._all_readings):
            raise ValidationError(f"この業務日に後継枠が見つかりません: {successor_id}")
        with (
            QSignalBlocker(self.cycle_tabs),
            QSignalBlocker(self.state_filter),
            QSignalBlocker(self.category_filter),
            QSignalBlocker(self.location_filter),
            QSignalBlocker(self.keyword_filter),
        ):
            self.cycle_tabs.setCurrentIndex(0)
            self.state_filter.setCurrentIndex(0)
            self.category_filter.setCurrentIndex(0)
            self.location_filter.setCurrentIndex(0)
            self.keyword_filter.clear()
        self._apply_filters()
        row = self.table_model.row_for_id(successor_id)
        if row is None:
            raise ValidationError(f"後継枠を表示できません: {successor_id}")
        self.table.selectRow(row)
        self.table.scrollTo(self.table_model.index(row, 0))

    def _on_manual_reading(self) -> None:
        succeeded, _ = run_guarded(self, self._open_manual_reading)
        if not succeeded:
            self.manual_reading_action.setEnabled(False)

    def _open_manual_reading(self) -> None:
        reading = self._selected_reading()
        if reading is None or reading.task_state is PlannedReadingTaskState.SUPERSEDED:
            raise ValidationError("入力可能な予定枠を選択してください。")
        self._review_assignments = self._load_review_assignments()
        self._show_review_assignments()
        self._update_selection()
        if any(review.contains(reading) for review in self._review_assignments):
            raise ValidationError("計算条件の確認待ちです。訂正が完了するまで入力できません。")
        dialog = DirectReadingDialog(self._services, reading, self._staff_memory, self)
        dialog.saved.connect(self.notify_dialog_saved)
        dialog.exec()

    def _show_review_guidance(self) -> None:
        reviews = self._load_review_assignments()
        self._review_assignments = reviews
        self._show_review_assignments()
        self._update_selection()
        if not reviews:
            raise ValidationError("計算条件の確認待ちはありません。")
        reading = self._selected_reading()
        selected = next(
            (review for review in reviews if reading is not None and review.contains(reading)),
            reviews[0],
        )
        self._open_master(selected.flag)

    def _open_master(self, flag: AssignmentReviewFlagRecord | None = None) -> None:
        dialog = MasterDialog(self._services, self._staff_memory, self)
        dialog.saved.connect(self.notify_dialog_saved)
        if flag is not None:
            dialog.show_assignment_correction(flag)
        dialog.exec()

    def _open_settings(self) -> None:
        if self._display_settings_path is None:
            raise ValidationError("表示設定の保存先が設定されていません。")
        dialog = SettingsDialog(self._services, self._display_settings_path, self)
        dialog.saved.connect(self.notify_dialog_saved)
        dialog.exec()

    def _open_replacement(self) -> None:
        reading = self._selected_reading()
        dialog = MeterReplacementDialog(
            self._services,
            self._staff_memory,
            self,
            location_id=reading.location_id if reading is not None else None,
            old_meter_id=reading.meter_id if reading is not None else None,
        )
        dialog.saved.connect(self.notify_dialog_saved)
        dialog.exec()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.boundary_timer.stop()
        super().closeEvent(event)
