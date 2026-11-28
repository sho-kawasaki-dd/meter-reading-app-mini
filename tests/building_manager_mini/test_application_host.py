from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from building_manager_mini.ui.application_host import (
    ApplicationHostWindow,
    confirm_leave_application,
)


def test_host_opens_and_reuses_application_page_in_one_window(qapp) -> None:
    opened: list[str] = []
    selected: list[str] = []
    page = QWidget()
    host = ApplicationHostWindow(
        (("meter-reading", "検針"), ("work-log-test", "テスト")),
        opener=lambda application_id: opened.append(application_id) or page,
        on_selected=selected.append,
        on_error=lambda error: pytest.fail(str(error)),
        last_application="work-log-test",
    )

    assert host.selection.selected_application == "work-log-test"
    assert host.open_application("meter-reading")
    assert host.current_application == "meter-reading"
    assert host.current_page is page
    host.return_to_selection()

    assert host.current_application is None
    assert host.open_application("meter-reading")
    assert host.current_page is page
    assert opened == ["meter-reading", "meter-reading"]
    assert selected == ["meter-reading", "meter-reading"]
    host.close()


def test_host_preserves_current_page_when_switch_is_cancelled(qapp) -> None:
    pages = {"meter-reading": QWidget(), "work-log-test": QWidget()}
    decisions = iter((False, True, True))
    open_calls: list[str] = []
    host = ApplicationHostWindow(
        (("meter-reading", "検針"), ("work-log-test", "テスト")),
        opener=lambda application_id: open_calls.append(application_id) or pages[application_id],
        on_selected=lambda _application_id: None,
        on_error=lambda error: pytest.fail(str(error)),
        can_leave=lambda _application_id, _page: next(decisions),
    )
    assert host.open_application("meter-reading")

    assert not host.open_application("work-log-test")
    assert host.current_application == "meter-reading"
    assert host.current_page is pages["meter-reading"]

    assert host.open_application("work-log-test")
    assert host.current_application == "work-log-test"
    assert host.current_page is pages["work-log-test"]
    assert host.open_application("meter-reading")
    assert host.current_application == "meter-reading"
    assert host.current_page is pages["meter-reading"]
    assert open_calls == ["meter-reading", "work-log-test", "meter-reading"]
    host.close()


def test_host_reports_prepare_failure_and_keeps_selection_available(qapp) -> None:
    errors: list[Exception] = []
    second_page = QWidget()

    def open_application(application_id: str) -> QWidget:
        if application_id == "meter-reading":
            raise RuntimeError("準備失敗")
        return second_page

    host = ApplicationHostWindow(
        (("meter-reading", "検針"), ("work-log-test", "テスト")),
        opener=open_application,
        on_selected=lambda _application_id: None,
        on_error=errors.append,
    )

    assert not host.open_application("meter-reading")
    assert host.current_application is None
    assert host._stack.currentWidget() is host.selection
    assert len(errors) == 1
    assert str(errors[0]) == "準備失敗"
    assert host.open_application("work-log-test")
    assert host.current_page is second_page
    host.close()


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (QMessageBox.StandardButton.Save, True),
        (QMessageBox.StandardButton.Discard, True),
        (QMessageBox.StandardButton.Cancel, False),
    ],
)
def test_unsaved_application_changes_require_save_discard_or_cancel(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
    answer: QMessageBox.StandardButton,
    expected: bool,
) -> None:
    page = QWidget()
    save_calls: list[bool] = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: answer)

    result = confirm_leave_application(
        page,
        has_unsaved_changes=lambda: True,
        save_changes=lambda: save_calls.append(True),
    )

    assert result is expected
    assert save_calls == ([True] if answer == QMessageBox.StandardButton.Save else [])


def test_failed_save_keeps_application_open_and_reports_the_error(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = QWidget()
    errors: list[Exception] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: QMessageBox.StandardButton.Save,
    )

    def fail_save() -> None:
        raise OSError("保存に失敗")

    host = ApplicationHostWindow(
        (("meter-reading", "検針"), ("work-log-test", "テスト")),
        opener=lambda _application_id: page,
        on_selected=lambda _application_id: None,
        on_error=errors.append,
        can_leave=lambda _application_id, current_page: confirm_leave_application(
            current_page,
            has_unsaved_changes=lambda: True,
            save_changes=fail_save,
        ),
    )
    assert host.open_application("meter-reading")

    assert not host.open_application("work-log-test")
    assert host.current_application == "meter-reading"
    assert host.current_page is page
    assert [str(error) for error in errors] == ["保存に失敗"]
    host.close()


def test_modal_dialog_blocks_application_switch(qapp) -> None:
    page = QWidget()
    host = ApplicationHostWindow(
        (("meter-reading", "検針"),),
        opener=lambda _application_id: page,
        on_selected=lambda _application_id: None,
        on_error=lambda error: pytest.fail(str(error)),
    )
    assert host.open_application("meter-reading")
    dialog = QDialog(host)
    dialog.setModal(True)
    dialog.show()
    qapp.processEvents()

    assert QApplication.activeModalWidget() is dialog
    assert not host.return_to_selection()
    assert host.current_application == "meter-reading"

    dialog.close()
    assert host.return_to_selection()
    host.close()
