from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QWidget


def center_dialog(dialog: QWidget) -> None:
    parent = dialog.parentWidget()
    screen = parent.screen() if parent is not None else QApplication.primaryScreen()
    if screen is None:
        screen = dialog.screen()
    if screen is None:
        return

    available = screen.availableGeometry()
    frame = dialog.frameGeometry()
    target = parent.frameGeometry().center() if parent is not None else available.center()
    frame_x = min(
        max(target.x() - frame.width() // 2, available.left()),
        available.right() - frame.width() + 1,
    )
    frame_y = min(
        max(target.y() - frame.height() // 2, available.top()),
        available.bottom() - frame.height() + 1,
    )
    dialog.move(QPoint(frame_x, frame_y))
