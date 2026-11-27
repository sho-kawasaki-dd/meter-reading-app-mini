from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox
from pytestqt.qtbot import QtBot
from reading_service_helpers import DEFINITION_ID, STAFF_ID, build_services, create_plan
from test_reading_history import REPLACED, START, ZONE, _assignments, _boundary, _build, _reading

from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.core.contract_models import CalculationMethod, Category
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.reading_history_contracts import (
    ReadingHistoryCode,
    ReadingHistoryIssue,
)
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui import reading_history_export
from meter_reading_mini.ui.dialogs.direct_reading_dialog import DirectReadingDialog
from meter_reading_mini.ui.dialogs.reading_history_dialog import ReadingHistoryDialog
from meter_reading_mini.ui.models.reading_history_model import HEADERS, ReadingHistoryTableModel
from meter_reading_mini.ui.reading_history_export import (
    CSV_HEADERS,
    history_filename_prefix,
    reading_history_csv,
    save_reading_history_csv,
)
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory


def _mismatched_history():
    prior = _reading("a", START + timedelta(days=1), "old", raw="120", last="100", usage="40")
    current = _reading("c", START + timedelta(days=2), "old", raw="130", last="100", usage="20")
    boundary = replace(
        _boundary(),
        previous_measured_reading=Decimal("100"),
        previous_measured_planned_reading_id="saved",
    )
    return _build([prior, current], [boundary], _assignments())


def test_model_keeps_saved_baseline_and_explains_mismatch(qtbot: QtBot) -> None:
    history = _mismatched_history()
    model = ReadingHistoryTableModel()
    model.set_rows(history.rows, ZONE)
    last_column = HEADERS.index("前回値")
    source_column = HEADERS.index("前回値の出所")
    assert model.data(model.index(1, last_column)) == "100"
    assert model.data(model.index(1, source_column)) == "出所未確定"
    tip = model.data(model.index(1, source_column), Qt.ItemDataRole.ToolTipRole)
    assert isinstance(tip, str) and "120" in tip and "HIST_BASELINE_MISMATCH" in tip
    assert model.data(model.index(1, 0), Qt.ItemDataRole.BackgroundRole) is not None
    assert model.data(model.index(2, 0)) == "交換（旧最終）"


def test_csv_columns_injection_replacements_and_atomic_bom(tmp_path: Path) -> None:
    history = _mismatched_history()
    definition = replace(
        history.definition,
        measurement_definition_id="=id",
        display_name="+名前",
        location_id="-location",
        location_name="@場所",
        unit="\tunit",
    )
    reading = replace(
        history.rows[1],
        raw_reading=Decimal("-1.200"),
        note="\r=危険",
        power_factor="=数式",
        updated_by="+担当",
    )
    history = replace(history, definition=definition, rows=(reading, *history.rows[2:]))
    path = tmp_path / "履歴.csv"
    save_reading_history_csv(path, history, ZONE)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))
    assert (
        list(rows[0])
        == list(CSV_HEADERS)
        == [
            "測定定義ID",
            "測定定義名",
            "場所ID",
            "場所名",
            "単位",
            "行種別",
            "業務日",
            "検針時間",
            "予定日時",
            "枠状態",
            "結果状態",
            "メーターID",
            "メーター名",
            "製造番号",
            "読値",
            "前回値",
            "前回値の出所",
            "前回値の参照元",
            "倍率",
            "ロールオーバー値",
            "使用量",
            "力率",
            "記録日時",
            "担当者",
            "取込元",
            "備考",
            "交換ID",
            "予定枠ID",
            "警告・情報コード",
        ]
    )
    assert [rows[0][key] for key in ("測定定義ID", "測定定義名", "場所ID", "場所名", "単位")] == [
        "'=id",
        "'+名前",
        "'-location",
        "'@場所",
        "'\tunit",
    ]
    assert rows[0]["読値"] == "-1.200"
    assert rows[0]["備考"] == "'\r=危険"
    assert rows[0]["力率"] == "'=数式"
    assert rows[0]["担当者"] == "'+担当"
    assert rows[0]["前回値"] == "100"
    assert rows[0]["前回値の出所"] == rows[0]["前回値の参照元"] == ""
    assert "HIST_BASELINE_MISMATCH" in rows[0]["警告・情報コード"]
    old, new = rows[1:]
    assert old["行種別"] == "replacement_old_final"
    assert old["前回値の出所"] == "" and old["前回値の参照元"] == "saved"
    assert new["行種別"] == "replacement_new_initial"
    for row in (old, new):
        assert row["記録日時"] == row["取込元"] == ""
        assert row["担当者"] == "担当" and row["備考"] == "交換"
        assert row["予定日時"] == "2026-02-04 09:00:00+09:00"
    assert list(tmp_path.iterdir()) == [path]
    assert reading_history_csv(history, ZONE).startswith("\ufeff")


@pytest.mark.parametrize(
    ("value", "safe"),
    [
        ("  水道 / 主__計器:*?<>|  ", "水道_主_計器_"),
        ("CON.txt", "definition-id"),
        (" lpt9 .csv ", "definition-id"),
        ("... ", "definition-id"),
        (" . . 名\t称. . ", "名_称"),
        ("電" * 41, "電" * 40),
        ("x" * 39 + ".y", "x" * 39),
    ],
)
def test_filename_prefix_is_safe(value: str, safe: str) -> None:
    history = _mismatched_history()
    history = replace(
        history,
        definition=replace(
            history.definition, display_name=value, measurement_definition_id="definition-id"
        ),
    )
    start = history.query.start_date.strftime("%Y%m%d")
    end = history.query.end_date.strftime("%Y%m%d")
    assert history_filename_prefix(history) == f"meter_reading_history_{safe}_{start}-{end}"


@pytest.mark.parametrize("definition_id", ["..", "CON.txt"])
def test_filename_prefix_rejects_unusable_fallback(definition_id: str) -> None:
    history = _mismatched_history()
    history = replace(
        history,
        definition=replace(
            history.definition, display_name="NUL", measurement_definition_id=definition_id
        ),
    )
    with pytest.raises(ValidationError, match="ファイル名"):
        history_filename_prefix(history)


@pytest.fixture
def history_dialog(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    fixed_clock: datetime,
    qtbot: QtBot,
    request: pytest.FixtureRequest,
) -> ReadingHistoryDialog:
    conn, repository = file_database_repository
    build_services(
        conn, repository, calculation_method=getattr(request, "param", CalculationMethod.DELTA)
    )
    create_plan(repository, "plan", datetime(2026, 2, 2, 3, tzinfo=UTC))
    dialog = ReadingHistoryDialog(
        create_app_context(conn, clock=lambda: fixed_clock),
        StaffSelectionMemory(),
        definition_id=DEFINITION_ID,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog


def test_filters_clear_results_preserve_selection_and_prevent_future(
    history_dialog: ReadingHistoryDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = history_dialog
    dialog.refresh()
    assert dialog.table_model.rowCount() == 1 and dialog.csv_button.isEnabled()
    dialog.category_combo.setCurrentIndex(dialog.category_combo.findData(Category.WATER.value))
    assert dialog.definition_combo.currentData() == DEFINITION_ID
    assert dialog.table_model.rowCount() == 0 and not dialog.csv_button.isEnabled()
    dialog.refresh()
    dialog.category_combo.setCurrentIndex(dialog.category_combo.findData(Category.GAS.value))
    assert dialog.definition_combo.currentData() == ""
    assert not dialog.csv_button.isEnabled()
    dialog.category_combo.setCurrentIndex(0)
    dialog.definition_combo.setCurrentIndex(dialog.definition_combo.findData(DEFINITION_ID))
    dialog.end_date.setDate(QDate(2026, 2, 3))
    dialog.refresh()
    dialog.end_date.setDate(QDate(2100, 1, 1))
    assert dialog.end_date.date() == QDate(2026, 2, 4)
    assert not dialog.csv_button.isEnabled()
    dialog.refresh()
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: QMessageBox.StandardButton.Ok)

    def fail(_query: object) -> None:
        raise ValidationError("取得失敗")

    monkeypatch.setattr(dialog._services.reading_history, "get_history", fail)
    dialog.refresh()
    assert dialog.table_model.rowCount() == 0 and not dialog.csv_button.isEnabled()
    assert "失敗" in dialog.status_label.text()


def test_save_failure_never_reports_success(
    history_dialog: ReadingHistoryDialog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = history_dialog
    dialog.refresh()
    messages: list[str] = []
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *_args: (str(tmp_path / "a.csv"), "")
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: messages.append("success"))
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: messages.append("failure"))

    def fail(*_args: object) -> None:
        raise OSError("書込失敗")

    monkeypatch.setattr(reading_history_export, "atomic_write_text", fail)
    dialog.csv_button.click()
    assert messages == ["failure"]
    assert not (tmp_path / "a.csv").exists()


@pytest.mark.parametrize(
    ("history_dialog", "value"),
    [(CalculationMethod.DELTA, "123.45"), (CalculationMethod.RAW_ONLY, "-12.50")],
    indirect=["history_dialog"],
)
def test_saved_reading_to_display_and_csv(
    history_dialog: ReadingHistoryDialog,
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    dialog = history_dialog
    (reading,) = dialog._services.reading_overview.get_overview(date(2026, 2, 2)).readings
    direct = DirectReadingDialog(dialog._services, reading, StaffSelectionMemory())
    qtbot.addWidget(direct)
    direct.staff_combo.setCurrentIndex(direct.staff_combo.findData(STAFF_ID))
    invalid, message = ("-0.00", "負のゼロ") if value.startswith("-") else ("-12.50", "10進数")
    direct.value_edit.setText(invalid)
    assert not direct.save_button.isEnabled()
    assert message in direct.preview_text.toPlainText()
    direct.value_edit.setText(value)
    assert direct.save_button.isEnabled(), direct.preview_text.toPlainText()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    direct.save_button.click()
    dialog.refresh()
    assert dialog.table_model.data(dialog.table_model.index(0, HEADERS.index("読値"))) == value
    if value == "123.45":
        assert "保存済み使用量の単純合計: 0" in dialog.status_label.text()
    path = tmp_path / "history.csv"
    messages: list[str] = []
    suggested: list[str] = []

    def choose(_parent: object, _title: str, default: str, _filter: str) -> tuple[str, str]:
        suggested.append(default)
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    monkeypatch.setattr(
        "meter_reading_mini.ui.csv_export.local_timestamp_for_filename",
        lambda: "20260204_123456",
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: messages.append("success"))
    dialog.csv_button.click()
    assert messages == ["success"]
    assert dialog._history is not None
    assert suggested == [
        f"meter_reading_history_{dialog._history.definition.display_name}_"
        "20260105-20260204_20260204_123456.csv"
    ]
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["読値"] == value
    assert rows[0]["予定枠ID"] == "plan"
    assert rows[0]["取込元"] == "manual"


def test_export_rejects_stale_result_during_save_dialog(
    history_dialog: ReadingHistoryDialog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = history_dialog
    dialog.refresh()
    messages: list[str] = []

    def choose(*_args: object) -> tuple[str, str]:
        dialog.superseded_check.setChecked(True)
        return str(tmp_path / "stale.csv"), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: messages.append("success"))
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: messages.append("failure"))
    dialog.csv_button.click()
    assert messages == ["failure"]
    assert not (tmp_path / "stale.csv").exists()


def test_multiple_pending_assignments_show_selected_reason(
    history_dialog: ReadingHistoryDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _mismatched_history()
    first = ReadingHistoryIssue(ReadingHistoryCode.ASSIGNMENT_PENDING, "旧の確認", "old", START)
    second = ReadingHistoryIssue(ReadingHistoryCode.ASSIGNMENT_PENDING, "新の確認", "new", REPLACED)
    rows = (
        replace(history.rows[0], issues=(first,)),
        replace(history.rows[1], issues=(first, second)),
    )
    monkeypatch.setattr(
        history_dialog._services.reading_history,
        "get_history",
        lambda _query: replace(history, rows=rows),
    )
    history_dialog.refresh()
    assert history_dialog.pending_combo.count() == 2
    assert history_dialog.pending_reason.text() == "旧の確認"
    history_dialog.pending_combo.setCurrentIndex(1)
    assert history_dialog.pending_reason.text() == "新の確認"
    assert history_dialog.correction_button.isEnabled()


def test_inactive_selection_and_reload(
    history_dialog: ReadingHistoryDialog,
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    qtbot: QtBot,
) -> None:
    conn, _repository = file_database_repository
    dialog = history_dialog
    dialog.inactive_check.setChecked(True)
    assert dialog.definition_combo.currentData() == DEFINITION_ID
    dialog.refresh()
    conn.execute(
        "UPDATE measurement_definitions SET is_active = 0 WHERE measurement_definition_id = ?",
        (DEFINITION_ID,),
    )
    dialog.invalidate_for_reload()
    assert dialog.definition_combo.currentData() == DEFINITION_ID
    assert not dialog.csv_button.isEnabled()
    dialog.inactive_check.setChecked(False)
    assert dialog.definition_combo.currentData() == ""
    other = ReadingHistoryDialog(
        dialog._services, StaffSelectionMemory(), definition_id=DEFINITION_ID
    )
    qtbot.addWidget(other)
    assert other.inactive_check.isChecked()
    assert other.definition_combo.currentData() == DEFINITION_ID
