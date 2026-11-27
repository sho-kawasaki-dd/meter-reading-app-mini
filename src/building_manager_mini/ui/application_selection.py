from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

type ApplicationChoice = tuple[str, str]


class ApplicationSelectionDialog(QDialog):
    def __init__(
        self,
        applications: Sequence[ApplicationChoice],
        *,
        last_application: str | None = None,
    ) -> None:
        super().__init__()
        if not applications:
            raise ValueError("選択できるアプリがありません。")

        self.setWindowTitle("アプリを選択")
        self.application_combo = QComboBox(self)
        for application_id, label in applications:
            if not application_id or not label:
                raise ValueError("アプリ識別子と表示名は空にできません。")
            self.application_combo.addItem(label, application_id)
        if last_application is not None:
            index = self.application_combo.findData(last_application)
            if index >= 0:
                self.application_combo.setCurrentIndex(index)

        self.button_box = QDialogButtonBox(self)
        self.open_button = self.button_box.addButton(
            "開く", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.cancel_button = self.button_box.addButton(
            "終了", QDialogButtonBox.ButtonRole.RejectRole
        )
        self.open_button.setDefault(True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("起動するアプリを選択してください。", self))
        layout.addWidget(self.application_combo)
        layout.addWidget(self.button_box)

    @property
    def selected_application(self) -> str:
        selected = self.application_combo.currentData()
        if not isinstance(selected, str):
            raise RuntimeError("選択中のアプリを確認できません。")
        return selected


def select_application(
    applications: Sequence[ApplicationChoice],
    *,
    last_application: str | None = None,
) -> str | None:
    dialog = ApplicationSelectionDialog(
        applications,
        last_application=last_application,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.selected_application
