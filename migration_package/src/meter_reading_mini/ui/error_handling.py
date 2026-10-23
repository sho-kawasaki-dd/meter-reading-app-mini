from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from meter_reading_mini.core.errors import DomainError

logger = logging.getLogger(__name__)


def show_domain_error(parent: QWidget | None, error: DomainError) -> None:
    QMessageBox.warning(parent, "入力内容を確認してください", error.message)


def _active_log_path() -> str | None:
    for handler in logging.getLogger().handlers:
        log_path = getattr(handler, "baseFilename", None)
        if log_path is not None:
            return str(log_path)
    return None


def _show_unexpected_error(
    parent: QWidget | None, error: BaseException, log_path: str | None
) -> None:
    logger.error(
        "予期しないエラーが発生しました",
        exc_info=(type(error), error, error.__traceback__),
    )
    message = "予期しないエラーが発生しました。詳細はログを確認してください。"
    if log_path is not None:
        message += f"\nログ: {log_path}"
    QMessageBox.critical(parent, "エラー", message)


def show_unexpected_error(parent: QWidget | None, error: BaseException) -> None:
    _show_unexpected_error(parent, error, _active_log_path())


def run_guarded[T](parent: QWidget | None, func: Callable[[], T]) -> tuple[bool, T | None]:
    try:
        return True, func()
    except DomainError as error:
        show_domain_error(parent, error)
    except Exception as error:
        show_unexpected_error(parent, error)
    return False, None


def install_excepthook(log_path: Path) -> None:
    def handle_uncaught_exception(
        exception_type: type[BaseException],
        error: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        logger.error(
            "未捕捉例外",
            exc_info=(exception_type, error, traceback),
        )
        application = QApplication.instance()
        if application is not None:
            message = "予期しないエラーが発生しました。詳細はログを確認してください。"
            QMessageBox.critical(None, "エラー", f"{message}\nログ: {log_path}")

    sys.excepthook = handle_uncaught_exception
