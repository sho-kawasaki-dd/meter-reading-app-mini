from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from meter_reading_mini.db.connection import connect_memory
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.ui import display_settings

WORK_AREA_WIDTH = 1366
WORK_AREA_HEIGHT = 768 - 40


@pytest.fixture(scope="session")
def qapp_args(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        return []
    config_path = tmp_path_factory.mktemp("qt-offscreen") / "config.json"
    config_path.write_text(
        json.dumps({"screens": [{"width": WORK_AREA_WIDTH, "height": WORK_AREA_HEIGHT}]}),
        encoding="utf-8",
    )
    return ["pytest", "-platform", f"offscreen:configfile={config_path}"]


@pytest.fixture
def restore_app_font(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[QFont]:
    original = QFont(qapp.font())
    monkeypatch.setattr(display_settings, "_base_font", None)
    yield original
    QApplication.setFont(original)


@contextmanager
def unchecked_constraints(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        yield
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")


@pytest.fixture
def memory_conn() -> Iterator[sqlite3.Connection]:
    conn = connect_memory()
    create_schema(conn)
    try:
        yield conn
    finally:
        conn.close()
