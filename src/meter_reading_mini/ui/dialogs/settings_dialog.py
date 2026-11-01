"""業務設定と表示設定の画面。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QTime, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.contract_models import ReadingCycle, ReadingSession
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.ui.display_settings import (
    ALLOWED_FONT_SCALES,
    DisplaySettings,
    apply_display_settings,
    load_display_settings,
    save_display_settings,
)
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.widgets.form_helpers import (
    day_offset,
    python_date,
    qdate,
    select_value,
    selected_value,
)
from meter_reading_mini.ui.widgets.master_common import scroll_area
from meter_reading_mini.ui.widgets.schedule_editor import CYCLE_LABELS
from meter_reading_mini.ui.widgets.schedule_panel import SchedulePanel, ScheduleSubject


class SettingsDialog(QDialog):
    saved = Signal()

    def __init__(
        self, services: AppContext, display_settings_path: Path, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._services = services
        self._display_settings_path = display_settings_path
        self._offset_loaded_session: ReadingSession | None = None
        self._offset_loaded_date: date | None = None
        self._offset_dirty = False
        self.setWindowTitle("設定")
        self.resize(850, 700)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("各項目の保存ボタンで設定を個別に保存します。"))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.tabs.addTab(scroll_area(self._build_cutoff_page()), "締め時刻")
        self.tabs.addTab(scroll_area(self._build_schedule_page()), "周期別スケジュール")
        self.tabs.addTab(scroll_area(self._build_offset_page()), "配信オフセット")
        self.tabs.addTab(scroll_area(self._build_display_page()), "表示設定")
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)

    def _today(self) -> date:
        return self._services.current_datetime().date()

    def _build_cutoff_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        cutoff = self._services.cutoff_settings.get_cutoff_time()
        self.cutoff_edit = QTimeEdit(QTime(cutoff.hour, cutoff.minute))
        self.cutoff_edit.setDisplayFormat("HH:mm")
        self.cutoff_edit.setObjectName("cutoffTime")
        self.timezone_label = QLabel(self._services.timezone_settings.get_timezone().key)
        form.addRow("締め時刻", self.cutoff_edit)
        form.addRow("業務タイムゾーン（表示のみ）", self.timezone_label)
        layout.addLayout(form)
        self.cutoff_save_button = QPushButton("締め時刻を保存")
        self.cutoff_save_button.clicked.connect(lambda: run_guarded(self, self._save_cutoff))
        layout.addWidget(self.cutoff_save_button)
        layout.addStretch()
        return page

    def _save_cutoff(self) -> None:
        self._services.cutoff_settings.save_cutoff_time(self.cutoff_edit.time().toString("HH:mm"))
        self.saved.emit()

    def _build_schedule_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        service = self._services.schedule_defaults
        self.schedule_panel = SchedulePanel(
            self._today,
            load_schedule=lambda subject, day: service.get_schedule(subject.cycle, day),
            load_history=lambda subject: [
                (record.effective_from_date, record.schedule_json)
                for record in service.list_schedule_history(subject.cycle)
            ],
            save_schedule=lambda subject, schedule, day: service.save_schedule(
                subject.cycle, schedule, effective_from_date=day
            ),
            subject_label="検針周期",
        )
        self.schedule_panel.set_subjects(
            [ScheduleSubject(cycle.value, CYCLE_LABELS[cycle], cycle) for cycle in ReadingCycle]
        )
        self.schedule_panel.saved.connect(self.saved.emit)
        layout.addWidget(self.schedule_panel)
        return page

    def _build_offset_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.offset_session_combo = QComboBox()
        for session in ReadingSession:
            self.offset_session_combo.addItem(session.value, session.value)
        self.offset_date_edit = QDateEdit()
        self.offset_date_edit.setCalendarPopup(True)
        self.offset_date_edit.setDisplayFormat("yyyy/MM/dd")
        self.offset_date_edit.setMinimumDate(qdate(self._today()))
        self.offset_date_edit.setDate(qdate(self._today()))
        self.offset_days_edit = QLineEdit()
        self.offset_days_edit.setObjectName("releaseOffsetDays")
        form.addRow("検針時間", self.offset_session_combo)
        form.addRow("適用開始日", self.offset_date_edit)
        form.addRow("配信オフセット（日）", self.offset_days_edit)
        layout.addLayout(form)
        layout.addWidget(QLabel("適用履歴"))
        self.offset_history = QPlainTextEdit()
        self.offset_history.setReadOnly(True)
        self.offset_history.setMaximumHeight(160)
        layout.addWidget(self.offset_history)
        self.offset_save_button = QPushButton("配信オフセットを保存")
        layout.addWidget(self.offset_save_button)
        layout.addStretch()
        self._load_offset(ReadingSession.SESSION_0000, self._today())
        self.offset_days_edit.textChanged.connect(self._mark_offset_dirty)
        self.offset_session_combo.currentIndexChanged.connect(self._change_offset_selection)
        self.offset_date_edit.dateChanged.connect(self._change_offset_selection)
        self.offset_save_button.clicked.connect(lambda: run_guarded(self, self._save_offset))
        return page

    def _mark_offset_dirty(self, *_args: object) -> None:
        self._offset_dirty = True

    def _load_offset(self, session: ReadingSession, day: date) -> None:
        service = self._services.release_offsets
        value = service.get_release_offset_days(session, day)
        history = service.list_release_offset_history(session)
        with QSignalBlocker(self.offset_days_edit):
            self.offset_days_edit.setText(str(value))
        self.offset_history.setPlainText(
            "\n".join(
                f"{row.effective_from_date.isoformat()}  {row.offset_days:+d} 日" for row in history
            )
        )
        self._offset_loaded_session = session
        self._offset_loaded_date = day
        self._offset_dirty = False

    def _restore_offset_selection(self) -> None:
        if self._offset_loaded_session is None or self._offset_loaded_date is None:
            return
        with QSignalBlocker(self.offset_session_combo), QSignalBlocker(self.offset_date_edit):
            select_value(self.offset_session_combo, self._offset_loaded_session.value, "検針時間")
            self.offset_date_edit.setDate(qdate(self._offset_loaded_date))

    def _change_offset_selection(self, *_args: object) -> None:
        if self._offset_dirty:
            answer = QMessageBox.question(
                self,
                "編集中の配信オフセット",
                "入力した変更を破棄して対象または適用開始日を切り替えますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._restore_offset_selection()
                return
        succeeded, _ = run_guarded(
            self,
            lambda: self._load_offset(
                ReadingSession(selected_value(self.offset_session_combo, "検針時間")),
                python_date(self.offset_date_edit.date()),
            ),
        )
        if not succeeded:
            self._restore_offset_selection()

    def _save_offset(self) -> None:
        session = ReadingSession(selected_value(self.offset_session_combo, "検針時間"))
        day = python_date(self.offset_date_edit.date())
        if day < self._today():
            raise ValidationError("適用開始日に過去の日付は指定できません。")
        if session != self._offset_loaded_session or day != self._offset_loaded_date:
            raise ValidationError("表示中の対象と適用開始日を再読み込みしてください。")
        offset = day_offset(self.offset_days_edit.text(), day, "配信オフセット")
        self._services.release_offsets.save_release_offset_days(
            session, offset, effective_from_date=day
        )
        self._offset_dirty = False
        self.saved.emit()
        self._load_offset(session, day)

    def _build_display_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        settings = load_display_settings(self._display_settings_path)
        self.font_combo = QComboBox()
        self.font_combo.addItem("OS 既定", "")
        families = sorted(QFontDatabase.families())
        for family in families:
            self.font_combo.addItem(family, family)
        if settings.font_family is not None and settings.font_family not in families:
            self.font_combo.addItem(
                f"{settings.font_family}（未インストール）", settings.font_family
            )
        select_value(self.font_combo, settings.font_family or "", "フォント")
        self.scale_combo = QComboBox()
        for scale in ALLOWED_FONT_SCALES:
            self.scale_combo.addItem(f"{scale}%", str(scale))
        select_value(self.scale_combo, str(settings.font_scale_percent), "文字サイズ")
        form.addRow("フォント", self.font_combo)
        form.addRow("文字サイズ", self.scale_combo)
        layout.addLayout(form)
        self.display_save_button = QPushButton("表示設定を保存して反映")
        self.display_save_button.clicked.connect(lambda: run_guarded(self, self._save_display))
        layout.addWidget(self.display_save_button)
        layout.addStretch()
        return page

    def _save_display(self) -> None:
        family = self.font_combo.currentData()
        if not isinstance(family, str):
            raise ValidationError("フォントを選択してください。")
        scale = int(selected_value(self.scale_combo, "文字サイズ"))
        settings = DisplaySettings(family or None, scale)
        save_display_settings(self._display_settings_path, settings)
        apply_display_settings(settings)
        self.saved.emit()
