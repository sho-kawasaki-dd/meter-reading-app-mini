from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.ui import error_handling
from meter_reading_mini.ui.error_handling import (
    install_excepthook,
    run_guarded,
    show_unexpected_error,
)


def test_run_guarded_returns_successful_value(qtbot: object) -> None:
    assert run_guarded(None, lambda: 42) == (True, 42)


def test_run_guarded_distinguishes_successful_none(qtbot: object) -> None:
    assert run_guarded(None, lambda: None) == (True, None)


def test_run_guarded_shows_domain_error_and_returns_failure(
    monkeypatch: pytest.MonkeyPatch, qtbot: object
) -> None:
    messages: list[tuple[object, ...]] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args))

    result = run_guarded(None, lambda: (_ for _ in ()).throw(ValidationError("入力エラー")))

    assert result == (False, None)
    assert messages == [(None, "入力内容を確認してください", "入力エラー")]


def test_run_guarded_shows_unexpected_error_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    qtbot: object,
) -> None:
    messages: list[tuple[object, ...]] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    error = RuntimeError("故障")

    result = run_guarded(None, lambda: (_ for _ in ()).throw(error))

    assert result == (False, None)
    assert "故障" in caplog.text
    assert str(messages[0][2]).startswith(
        "予期しないエラーが発生しました。詳細はログを確認してください。"
    )


def test_show_unexpected_error_includes_active_log_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: object
) -> None:
    log_path = tmp_path / "app.log"
    monkeypatch.setattr(
        error_handling,
        "_active_log_path",
        lambda: str(log_path),
    )
    messages: list[tuple[object, ...]] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))

    show_unexpected_error(None, RuntimeError("故障"))

    assert str(log_path) in str(messages[0][2])


def test_install_excepthook_logs_and_shows_log_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    qtbot: object,
) -> None:
    messages: list[tuple[object, ...]] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    log_path = tmp_path / "app.log"
    install_excepthook(log_path)
    error = RuntimeError("未捕捉")

    sys.excepthook(type(error), error, error.__traceback__)

    assert "未捕捉" in caplog.text
    assert str(log_path) in str(messages[0][2])
