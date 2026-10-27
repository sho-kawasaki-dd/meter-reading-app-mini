from __future__ import annotations

from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication, QDialog

from meter_reading_mini.ui.dialog_placement import center_dialog


@pytest.mark.parametrize(
    ("parent_frame", "expected"),
    [
        (None, QPoint(131, 14)),
        (QRect(0, 0, 100, 100), QPoint(0, 0)),
        (QRect(924, 620, 100, 100), QPoint(264, 29)),
    ],
)
def test_center_dialog_uses_frame_coordinates_with_window_decorations(
    monkeypatch: pytest.MonkeyPatch,
    parent_frame: QRect | None,
    expected: QPoint,
) -> None:
    screen = Mock()
    screen.availableGeometry.return_value = QRect(0, 0, 1024, 720)
    monkeypatch.setattr(QApplication, "primaryScreen", lambda: screen)
    dialog = Mock(spec=QDialog)
    parent = None
    if parent_frame is not None:
        parent = Mock(spec=QDialog)
        parent.screen.return_value = screen
        parent.frameGeometry.return_value = parent_frame
    dialog.parentWidget.return_value = parent
    dialog.frameGeometry.return_value = QRect(131, 14, 760, 691)
    dialog.geometry.return_value = QRect(139, 45, 744, 660)

    center_dialog(dialog)

    dialog.move.assert_called_once_with(expected)
