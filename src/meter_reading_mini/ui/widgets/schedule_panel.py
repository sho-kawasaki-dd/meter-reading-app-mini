"""スケジュールの対象選択・履歴・保存を共通化する。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.core.contract_models import ReadingCycle
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import ScheduleSettings
from meter_reading_mini.core.schedule_settings import deserialize_schedule
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.widgets.form_helpers import python_date, qdate
from meter_reading_mini.ui.widgets.schedule_editor import ScheduleEditor, schedule_description


@dataclass(frozen=True, slots=True)
class ScheduleSubject:
    key: str
    label: str
    cycle: ReadingCycle


type ScheduleLoader = Callable[[ScheduleSubject, date], ScheduleSettings]
type ScheduleHistoryLoader = Callable[[ScheduleSubject], Sequence[tuple[date, str | None]]]
type ScheduleSaver = Callable[[ScheduleSubject, ScheduleSettings, date], None]
type ScheduleResetter = Callable[[ScheduleSubject, date], None]


class SchedulePanel(QWidget):
    saved = Signal()

    def __init__(
        self,
        current_business_date: Callable[[], date],
        *,
        load_schedule: ScheduleLoader,
        load_history: ScheduleHistoryLoader,
        save_schedule: ScheduleSaver,
        reset_schedule: ScheduleResetter | None = None,
        subject_label: str = "対象",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._load_schedule = load_schedule
        self._load_history = load_history
        self._save_schedule = save_schedule
        self._reset_schedule = reset_schedule
        self._subjects: dict[str, ScheduleSubject] = {}
        self._loaded_subject: ScheduleSubject | None = None
        self._loaded_date: date | None = None
        self._dirty = False

        layout = QVBoxLayout(self)
        selection = QHBoxLayout()
        selection.addWidget(QLabel(subject_label))
        self.subject_combo = QComboBox()
        self.subject_combo.setObjectName("scheduleSubject")
        selection.addWidget(self.subject_combo, 1)
        layout.addLayout(selection)
        self.editor = ScheduleEditor(current_business_date)
        layout.addWidget(self.editor)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("スケジュールを保存")
        self.reset_button = QPushButton("周期既定に戻す")
        self.reset_button.setVisible(reset_schedule is not None)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.editor.changed.connect(self._mark_dirty)
        self.subject_combo.currentIndexChanged.connect(self._change_selection)
        self.editor.effective_date_edit.dateChanged.connect(self._change_selection)
        self.save_button.clicked.connect(lambda: run_guarded(self, self._save))
        self.reset_button.clicked.connect(lambda: run_guarded(self, self._reset))
        self._set_available(False)

    def _set_available(self, available: bool) -> None:
        self.editor.setEnabled(available)
        self.save_button.setEnabled(available)
        self.reset_button.setEnabled(available)

    def set_subjects(self, subjects: Sequence[ScheduleSubject]) -> None:
        old = self._loaded_subject
        new_subjects = {subject.key: subject for subject in subjects}
        if old is not None and old.key not in new_subjects and self._dirty:
            raise ValidationError("編集中のスケジュールを保存または破棄してから更新してください。")
        self._subjects = new_subjects
        with QSignalBlocker(self.subject_combo):
            self.subject_combo.clear()
            for subject in subjects:
                self.subject_combo.addItem(subject.label, subject.key)
            index = self.subject_combo.findData(old.key) if old is not None else -1
            self.subject_combo.setCurrentIndex(index if index >= 0 else (0 if subjects else -1))
        if not subjects:
            self._loaded_subject = None
            self._loaded_date = None
            self._dirty = False
            self.editor.set_history([])
            self._set_available(False)
            return
        self._set_available(True)
        selected = self._selected_subject()
        if old is not None and selected.key == old.key and selected.cycle == old.cycle:
            self._loaded_subject = selected
            return
        self._load(selected, python_date(self.editor.effective_date_edit.date()))

    def select_subject(self, key: str) -> None:
        index = self.subject_combo.findData(key)
        if index < 0:
            raise ValidationError(f"スケジュールの対象が見つかりません: {key}")
        self.subject_combo.setCurrentIndex(index)

    def _selected_subject(self) -> ScheduleSubject:
        key = self.subject_combo.currentData()
        if not isinstance(key, str) or key not in self._subjects:
            raise ValidationError("スケジュールの対象を選択してください。")
        return self._subjects[key]

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _restore_selection(self) -> None:
        if self._loaded_subject is None or self._loaded_date is None:
            return
        with (
            QSignalBlocker(self.subject_combo),
            QSignalBlocker(self.editor.effective_date_edit),
        ):
            self.subject_combo.setCurrentIndex(
                self.subject_combo.findData(self._loaded_subject.key)
            )
            self.editor.effective_date_edit.setDate(qdate(self._loaded_date))

    def _change_selection(self, *_args: object) -> None:
        if self._loaded_subject is None:
            return
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "編集中のスケジュール",
                "入力した変更を破棄して対象または適用開始日を切り替えますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._restore_selection()
                return
        succeeded, _ = run_guarded(
            self,
            lambda: self._load(
                self._selected_subject(), python_date(self.editor.effective_date_edit.date())
            ),
        )
        if not succeeded:
            self._restore_selection()

    def _load(self, subject: ScheduleSubject, effective_date: date) -> None:
        schedule = self._load_schedule(subject, effective_date)
        lines: list[str] = []
        for start, raw in self._load_history(subject):
            if raw is None:
                description = "周期既定"
            else:
                try:
                    historic = deserialize_schedule(subject.cycle, raw)
                except (TypeError, ValueError) as error:
                    raise ValidationError(
                        f"{start.isoformat()} のスケジュール履歴が不正です: {error}"
                    ) from error
                description = schedule_description(historic)
            lines.append(f"{start.isoformat()}  {description}")
        self.editor.set_schedule(subject.cycle, schedule)
        self.editor.set_history(lines)
        self._loaded_subject = subject
        self._loaded_date = effective_date
        self._dirty = False

    def _save(self) -> None:
        subject = self._selected_subject()
        effective = self.editor.effective_date()
        if subject != self._loaded_subject or effective != self._loaded_date:
            raise ValidationError("表示中の対象と適用開始日を再読み込みしてください。")
        schedule = self.editor.schedule()
        self._save_schedule(subject, schedule, effective)
        self._dirty = False
        self.saved.emit()
        self._load(subject, effective)

    def _reset(self) -> None:
        if self._reset_schedule is None:
            raise ValidationError("周期既定に戻す操作は利用できません。")
        subject = self._selected_subject()
        effective = self.editor.effective_date()
        if subject != self._loaded_subject or effective != self._loaded_date:
            raise ValidationError("表示中の対象と適用開始日を再読み込みしてください。")
        answer = QMessageBox.question(
            self,
            "周期既定に戻す",
            "この日以降のスケジュールを周期既定に戻します。よろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._reset_schedule(subject, effective)
        self._dirty = False
        self.saved.emit()
        self._load(subject, effective)
