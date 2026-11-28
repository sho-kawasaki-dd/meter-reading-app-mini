from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QToolBar,
    QWidget,
)

from building_manager_mini.ui.application_selection import (
    ApplicationChoice,
    ApplicationSelectionWidget,
)

type ApplicationOpener = Callable[[str], QWidget]
type ApplicationSelectionHandler = Callable[[str], None]
type ApplicationErrorHandler = Callable[[Exception], None]
type ApplicationLeaveHandler = Callable[[str, QWidget], bool]


def _allow_leave(_application_id: str, _page: QWidget) -> bool:
    return True


def confirm_leave_application(
    parent: QWidget,
    *,
    has_unsaved_changes: Callable[[], bool],
    save_changes: Callable[[], None],
) -> bool:
    if not has_unsaved_changes():
        return True
    answer = QMessageBox.question(
        parent,
        "未保存の変更",
        "変更を保存しますか？\n「破棄」を選ぶと編集内容は失われます。",
        QMessageBox.StandardButton.Save
        | QMessageBox.StandardButton.Discard
        | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    )
    if answer == QMessageBox.StandardButton.Save:
        save_changes()
        return True
    return answer == QMessageBox.StandardButton.Discard


class ApplicationHostWindow(QMainWindow):
    def __init__(
        self,
        applications: Sequence[ApplicationChoice],
        *,
        opener: ApplicationOpener,
        on_selected: ApplicationSelectionHandler,
        on_error: ApplicationErrorHandler,
        can_leave: ApplicationLeaveHandler | None = None,
        last_application: str | None = None,
    ) -> None:
        super().__init__()
        self._opener = opener
        self._on_selected = on_selected
        self._on_error = on_error
        self._can_leave: ApplicationLeaveHandler = can_leave or _allow_leave
        self._pages: dict[str, QWidget] = {}
        self._current_application: str | None = None
        self._transition_in_progress = False

        self.setWindowTitle("建物管理 mini")
        self._stack = QStackedWidget(self)
        self.selection = ApplicationSelectionWidget(
            applications,
            last_application=last_application,
        )
        self._stack.addWidget(self.selection)
        self.setCentralWidget(self._stack)
        self.selection.open_requested.connect(self._open_requested_application)

        self.switch_action = QAction("アプリ選択に戻る", self)
        self.switch_action.setVisible(False)
        self.switch_action.triggered.connect(self.return_to_selection)
        toolbar = QToolBar("アプリ", self)
        toolbar.setMovable(False)
        toolbar.addAction(self.switch_action)
        self.addToolBar(toolbar)
        toolbar.setVisible(False)
        self._switch_toolbar = toolbar
        self.switch_action.setShortcut("Ctrl+Shift+A")

    @property
    def current_application(self) -> str | None:
        return self._current_application

    @property
    def current_page(self) -> QWidget | None:
        if self._current_application is None:
            return None
        return self._pages[self._current_application]

    def open_application(self, application_id: str) -> bool:
        if (
            self._current_application is not None
            and self._current_application != application_id
            and not self.return_to_selection()
        ):
            return False
        self.selection.set_selected_application(application_id)
        self._open_requested_application(application_id)
        return self._current_application == application_id

    def return_to_selection(self) -> bool:
        application_id = self._current_application
        if application_id is None or self._transition_in_progress:
            return False
        if QApplication.activeModalWidget() is not None:
            return False
        page = self._pages[application_id]
        self._transition_in_progress = True
        try:
            can_leave = self._can_leave(application_id, page)
        except Exception as error:
            self._on_error(error)
            return False
        finally:
            self._transition_in_progress = False
        if not can_leave:
            return False
        self._current_application = None
        self._stack.setCurrentWidget(self.selection)
        self.switch_action.setVisible(False)
        self._switch_toolbar.setVisible(False)
        return True

    def _open_requested_application(self, application_id: str) -> None:
        if self._transition_in_progress:
            return
        self._transition_in_progress = True
        try:
            self._on_selected(application_id)
            page = self._opener(application_id)
            existing_page = self._pages.get(application_id)
            if existing_page is not None and existing_page is not page:
                raise RuntimeError(f"アプリ画面が再利用されませんでした: {application_id}")
            if existing_page is None:
                self._pages[application_id] = page
                self._stack.addWidget(page)
            self._current_application = application_id
            self._stack.setCurrentWidget(page)
            page.show()
            self.switch_action.setVisible(True)
            self._switch_toolbar.setVisible(True)
        except Exception as error:
            self._on_error(error)
        finally:
            self._transition_in_progress = False
