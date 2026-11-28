from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtCharts import QCategoryAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDate, QSaveFile, Qt
from PySide6.QtGui import QFont, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox, QTableWidget
from pytestqt.qtbot import QtBot

from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.core.aggregation_contracts import AggregationPeriodUnit
from meter_reading_mini.core.aggregation_export_contracts import (
    AggregationExportPeriodSpec,
    AggregationExportSnapshot,
)
from meter_reading_mini.core.assignment_calculation_settings import (
    AssignmentCalculationCorrectionInput,
)
from meter_reading_mini.core.contract_models import Purpose
from meter_reading_mini.core.errors import FileOutputError, ValidationError
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui import aggregation_export, atomic_write
from meter_reading_mini.ui.dialogs.aggregation_json_export_dialog import (
    AggregationJsonExportDialog,
)
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.display_settings import DisplaySettings, apply_display_settings
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.dashboard import DashboardWindow

from .test_reports import Scenario


@dataclass
class DashboardData:
    scenario: Scenario
    formula_id: str
    window: DashboardWindow
    messages: list[tuple[str, str]]


@pytest.fixture
def dashboard_data(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> DashboardData:
    scenario = Scenario(memory_conn)
    scenario.definition("a")
    scenario.definition("b", purpose=Purpose.BILLING)
    scenario.plan("a-prev", "a", date(2026, 2, 28), value="10")
    scenario.plan("b-prev", "b", date(2026, 2, 28), value="5")
    scenario.plan("a-1", "a", date(2026, 3, 1), value="20")
    scenario.plan("b-1", "b", date(2026, 3, 1), value="8")
    scenario.plan("a-2", "a", date(2026, 3, 2), value="25")
    scenario.formula(
        "formula",
        (("a", "1"), ("b", "-1")),
        effective=date(2026, 3, 1),
        registered=date(2026, 3, 10),
    )
    scenario.context.measurement_definitions.deactivate_measurement_definition("b")
    messages: list[tuple[str, str]] = []
    for kind in ("warning", "critical", "information"):
        monkeypatch.setattr(
            QMessageBox,
            kind,
            lambda _parent, _title, text, *args, name=kind: (
                messages.append((name, text)) or QMessageBox.StandardButton.Ok
            ),
        )
    window = DashboardWindow(scenario.context, StaffSelectionMemory())
    qtbot.addWidget(window)
    window.show()
    window.start_date.setDate(QDate(2026, 3, 1))
    window.end_date.setDate(QDate(2026, 3, 2))
    window.refresh()
    return DashboardData(scenario, "formula", window, messages)


def _select_formula(data: DashboardData) -> None:
    index = data.window.target_combo.findData(f"formula:{data.formula_id}")
    assert index >= 0
    data.window.target_combo.setCurrentIndex(index)


def _cell(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    assert item is not None
    return item.text()


def _json_dialog(data: DashboardData, qtbot: QtBot) -> AggregationJsonExportDialog:
    dialog = AggregationJsonExportDialog(data.scenario.context, data.window)
    qtbot.addWidget(dialog)
    return dialog


def _json_unit(dialog: AggregationJsonExportDialog, unit: AggregationPeriodUnit) -> None:
    dialog.unit_combo.setCurrentIndex(dialog.unit_combo.findData(unit.value))


def test_json_button_opens_independent_dialog_without_a_report(
    dashboard_data: DashboardData, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = dashboard_data.window
    window.purpose_combo.setCurrentIndex(window.purpose_combo.findData(Purpose.BILLING.value))
    window.invalidate()
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(
        AggregationJsonExportDialog,
        "exec",
        lambda dialog: (
            opened.append((dialog.unit_combo.currentData(), dialog.period_label.text()))
            or QDialog.DialogCode.Rejected
        ),
    )
    assert window.json_button.isEnabled()
    window.json_button.click()
    assert opened == [("day", "2026-03-15 〜 2026-03-15 (2026-03-15)")]


def test_json_period_controls_and_future_limits(
    dashboard_data: DashboardData, qtbot: QtBot
) -> None:
    dialog = _json_dialog(dashboard_data, qtbot)
    assert dialog.day_edit.date() == QDate(2026, 3, 15)
    assert dialog.day_edit.maximumDate() == QDate(2026, 3, 15)
    assert "締め前" in dialog.notice_label.text()
    dialog.day_edit.setDate(QDate(2026, 3, 16))
    assert dialog.day_edit.date() == QDate(2026, 3, 15)
    dialog.day_edit.setDate(QDate(2026, 3, 14))
    assert not dialog.notice_label.text()

    _json_unit(dialog, AggregationPeriodUnit.MONTH)
    assert dialog.month_edit.date() == QDate(2026, 3, 1)
    assert dialog.month_edit.maximumDate() == QDate(2026, 3, 1)
    assert dialog.period_label.text() == "2026-03-01 〜 2026-03-31 (2026年3月)"
    assert "途中値" in dialog.notice_label.text()
    dialog.month_edit.setDate(QDate(2026, 4, 1))
    assert dialog.month_edit.date() == QDate(2026, 3, 1)
    dialog.month_edit.setDate(QDate(2026, 2, 1))
    assert dialog.period_label.text() == "2026-02-01 〜 2026-02-28 (2026年2月)"
    assert not dialog.notice_label.text()

    _json_unit(dialog, AggregationPeriodUnit.CALENDAR_YEAR)
    assert dialog.year_edit.maximum() == dialog.year_edit.value() == 2026
    assert dialog.period_label.text() == "2026-01-01 〜 2026-12-31 (2026年)"
    assert "途中値" in dialog.notice_label.text()
    dialog.year_edit.setValue(2027)
    assert dialog.year_edit.value() == 2026

    _json_unit(dialog, AggregationPeriodUnit.FISCAL_YEAR)
    assert dialog.fiscal_year_edit.maximum() == dialog.fiscal_year_edit.value() == 2025
    assert dialog.period_label.text() == "2025-04-01 〜 2026-03-31 (2025年度)"
    assert "途中値" in dialog.notice_label.text()
    dialog.fiscal_year_edit.setValue(2026)
    assert dialog.fiscal_year_edit.value() == 2025


def test_json_fiscal_year_defaults_to_current_year_from_april(
    dashboard_data: DashboardData, qtbot: QtBot
) -> None:
    dashboard_data.scenario.context = create_app_context(
        dashboard_data.scenario.conn, clock=lambda: datetime(2026, 4, 1, tzinfo=UTC)
    )
    dialog = _json_dialog(dashboard_data, qtbot)
    _json_unit(dialog, AggregationPeriodUnit.FISCAL_YEAR)
    assert dialog.fiscal_year_edit.value() == 2026
    assert dialog.period_label.text() == "2026-04-01 〜 2027-03-31 (2026年度)"
    assert "途中値" in dialog.notice_label.text()


def test_json_cancel_and_extension_overwrite_refusal_do_not_read_or_write(
    dashboard_data: DashboardData, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dialog = _json_dialog(dashboard_data, qtbot)
    calls: list[object] = []
    monkeypatch.setattr(
        dashboard_data.scenario.context.aggregation_reports,
        "get_export_snapshot",
        lambda spec: calls.append(spec),
    )
    names: list[str] = []
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda _parent, _title, default, _filter: (names.append(default) or "", ""),
    )
    dialog.save_button.click()
    assert names == ["meter-usage-day-2026-03-15-20260315_210000.json"]
    assert calls == [] and list(tmp_path.iterdir()) == [] and dashboard_data.messages == []

    target = tmp_path / "data.json"
    target.write_text("元データ", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(tmp_path / "data"), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    dialog.save_button.click()
    assert calls == [] and target.read_text(encoding="utf-8") == "元データ"
    assert dashboard_data.messages == []


def test_json_extension_completion_confirms_before_replacing_existing_file(
    dashboard_data: DashboardData, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dialog = _json_dialog(dashboard_data, qtbot)
    target = tmp_path / "existing.json"
    target.write_text("元データ", encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *_args: (str(target.with_suffix("")), "")
    )
    questions: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, text, *_args: (
            questions.append(text) or QMessageBox.StandardButton.Yes
        ),
    )
    dialog.save_button.click()
    assert len(questions) == 1 and target.name in questions[0]
    assert json.loads(target.read_text(encoding="utf-8"))["kind"] == "mrm-usage-aggregation"
    assert dashboard_data.messages[0][0] == "information"


@pytest.mark.parametrize(
    ("unit", "start", "nominal_end"),
    [
        (AggregationPeriodUnit.DAY, "2026-03-15", "2026-03-16"),
        (AggregationPeriodUnit.MONTH, "2026-03-01", "2026-04-01"),
        (AggregationPeriodUnit.CALENDAR_YEAR, "2026-01-01", "2027-01-01"),
        (AggregationPeriodUnit.FISCAL_YEAR, "2025-04-01", "2026-04-01"),
    ],
)
def test_json_output_for_each_period_unit(
    unit: AggregationPeriodUnit,
    start: str,
    nominal_end: str,
    dashboard_data: DashboardData,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dialog = _json_dialog(dashboard_data, qtbot)
    _json_unit(dialog, unit)
    target = tmp_path / "output.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    dialog.save_button.click()
    document = json.loads(target.read_text(encoding="utf-8"))
    period = document["period"]
    assert period["unit"] == unit.value
    assert period["nominal_start"] == period["start"] == start
    assert period["nominal_end"] == nominal_end
    assert period["end"] == "2026-03-16"
    assert period["is_in_progress"] is (unit is not AggregationPeriodUnit.DAY)
    assert period["includes_current_business_date"] is True
    assert dashboard_data.messages[0][0] == "information"


def test_json_save_rechecks_snapshot_after_business_day_changes(
    dashboard_data: DashboardData, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = [datetime(2026, 3, 15, 14, 59, tzinfo=UTC)]
    dashboard_data.scenario.context = create_app_context(
        dashboard_data.scenario.conn, clock=lambda: now[0]
    )
    dialog = _json_dialog(dashboard_data, qtbot)
    _json_unit(dialog, AggregationPeriodUnit.MONTH)
    target = tmp_path / "all.json"
    defaults: list[str] = []
    snapshots: list[AggregationExportSnapshot] = []
    get_snapshot = dashboard_data.scenario.context.aggregation_reports.get_export_snapshot

    def snapshot_once(spec: AggregationExportPeriodSpec) -> AggregationExportSnapshot:
        snapshot = get_snapshot(spec)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(
        dashboard_data.scenario.context.aggregation_reports, "get_export_snapshot", snapshot_once
    )

    def choose(_parent: object, _title: str, default: str, _filter: str) -> tuple[str, str]:
        defaults.append(default)
        now[0] = datetime(2026, 3, 15, 15, 1, tzinfo=UTC)
        return str(target.with_suffix("")), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    dialog.save_button.click()
    assert defaults == ["meter-usage-month-2026-03-01-20260315_235900.json"]
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    document = json.loads(raw)
    assert document["generated_at"] == "2026-03-15 15:01:00"
    assert document["generated_at_local"] == "2026-03-16T00:01:00+09:00"
    assert document["as_of_business_date"] == "2026-03-16"
    assert document["period"]["start"] == "2026-03-01"
    assert document["period"]["end"] == "2026-03-17"
    assert len(snapshots) == 1
    assert {item["purpose"] for item in document["results"]} == {"operational", "billing"}
    assert len(document["results"]) == 3
    assert dashboard_data.messages == [
        (
            "information",
            f"JSON を保存しました。\n保存先: {target}\n"
            "集計期間: 2026-03-01 〜 2026-03-16\n出力件数: 3",
        )
    ]


def test_json_rejects_period_that_becomes_future_after_save_selection(
    dashboard_data: DashboardData, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = [datetime(2026, 3, 15, 12, tzinfo=UTC)]
    dashboard_data.scenario.context = create_app_context(
        dashboard_data.scenario.conn, clock=lambda: now[0]
    )
    dialog = _json_dialog(dashboard_data, qtbot)
    target = tmp_path / "old.json"
    target.write_text("元データ", encoding="utf-8")

    def choose(*_args: object) -> tuple[str, str]:
        now[0] = datetime(2026, 3, 14, 12, tzinfo=UTC)
        return str(target), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    dialog.save_button.click()
    assert target.read_text(encoding="utf-8") == "元データ"
    assert len(dashboard_data.messages) == 1
    assert dashboard_data.messages[0][0] == "warning"
    assert "未来" in dashboard_data.messages[0][1]


@pytest.mark.parametrize("failure", ["domain", "write", "unexpected"])
def test_json_failures_preserve_existing_file_and_do_not_report_success(
    failure: str,
    dashboard_data: DashboardData,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dialog = _json_dialog(dashboard_data, qtbot)
    target = tmp_path / "existing.json"
    target.write_text("元データ", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    if failure == "domain":

        def fail_snapshot(_spec: object) -> None:
            raise ValidationError("集計データが不正です。")

        monkeypatch.setattr(
            dashboard_data.scenario.context.aggregation_reports,
            "get_export_snapshot",
            fail_snapshot,
        )
    elif failure == "write":

        def fail_replace(_source: object, _target: object) -> None:
            raise OSError("書込失敗")

        monkeypatch.setattr(atomic_write.os, "replace", fail_replace)
    else:

        def fail_unexpected(_spec: object) -> None:
            raise RuntimeError("予期しない障害")

        monkeypatch.setattr(
            dashboard_data.scenario.context.aggregation_reports,
            "get_export_snapshot",
            fail_unexpected,
        )
    dialog.save_button.click()
    assert target.read_text(encoding="utf-8") == "元データ"
    assert list(tmp_path.iterdir()) == [target]
    assert len(dashboard_data.messages) == 1
    kind, message = dashboard_data.messages[0]
    assert kind == ("critical" if failure == "unexpected" else "warning")
    expected = {"domain": "不正", "write": "アクセス権", "unexpected": "エラー"}
    assert expected[failure] in message


def test_dashboard_purpose_location_partial_and_disabled_definition(
    dashboard_data: DashboardData,
) -> None:
    window = dashboard_data.window
    assert window.target_combo.findData("definition:a") >= 0
    assert window.target_combo.findData("definition:b") == -1
    _select_formula(dashboard_data)
    assert _cell(window.period_table, 0, 3) == "7"
    assert _cell(window.period_table, 0, 5) == "通常"
    assert _cell(window.period_table, 0, 8) == "あり"
    assert "後から設定" in window.detail_text.toPlainText()
    assert "現在は無効な測定定義" in window.detail_text.toPlainText()
    assert "複数の場所" in window.detail_text.toPlainText()
    assert window.details_table.rowCount() == 2
    assert _cell(window.details_table, 0, 5) == "前回実測"
    assert _cell(window.details_table, 0, 6) == "a-prev"
    assert _cell(window.details_table, 0, 13) == "実測"
    assert window.png_button.isEnabled()
    assert window.chart.series()
    assert "運用" in window.chart.title()
    assert "参考値あり" in window.chart.title()
    axes = window.chart.axes(Qt.Orientation.Horizontal)
    assert len(axes) == 1 and isinstance(axes[0], QCategoryAxis)
    assert axes[0].categoriesLabels() == ["1\n2026-03-01", "2\n2026-03-02"]

    window.period_table.selectRow(1)
    assert _cell(window.period_table, 1, 3) == "5"
    assert "参考値" in _cell(window.period_table, 1, 5)
    assert _cell(window.period_table, 1, 6) == "あり"
    assert "使用量のない式項目: b" in window.detail_text.toPlainText()

    window.location_combo.setCurrentIndex(window.location_combo.findData("location-a"))
    assert not window.csv_button.isEnabled()
    assert not window.png_button.isEnabled()
    assert window.period_table.rowCount() == 0
    window.refresh()
    _select_formula(dashboard_data)
    assert _cell(window.period_table, 0, 3) == "7"
    assert window.details_table.rowCount() == 2
    assert {_cell(window.details_table, row, 1) for row in range(2)} == {
        "location-a",
        "location-b",
    }

    window.location_combo.setCurrentIndex(0)
    window.purpose_combo.setCurrentIndex(window.purpose_combo.findData(Purpose.BILLING.value))
    window.refresh()
    assert window.target_combo.findData("definition:b") >= 0
    assert window.target_combo.findData("formula:formula") == -1
    assert window.target_combo.findData("definition:a") == -1


def test_dashboard_monthly_progress_and_year_units(dashboard_data: DashboardData) -> None:
    window = dashboard_data.window
    window.end_date.setDate(QDate(2026, 3, 15))
    window.unit_combo.setCurrentIndex(window.unit_combo.findData(AggregationPeriodUnit.MONTH.value))
    window.refresh()
    _select_formula(dashboard_data)
    assert window.period_table.rowCount() == 1
    assert _cell(window.period_table, 0, 3) == "12"
    assert "進行中" in _cell(window.period_table, 0, 5)
    assert "進行中の期間あり" in window.chart.title()
    assert "現在業務日まで" in window.detail_text.toPlainText()
    assert window.unit_combo.findData(AggregationPeriodUnit.CALENDAR_YEAR.value) >= 0
    assert window.unit_combo.findData(AggregationPeriodUnit.FISCAL_YEAR.value) >= 0


def test_dashboard_supports_early_dates_and_rejects_unrepresentable_end(
    dashboard_data: DashboardData,
) -> None:
    window = dashboard_data.window
    window.start_date.setDate(QDate(1, 1, 1))
    window.end_date.setDate(QDate(1, 1, 1))
    window.refresh()
    assert window._report is not None
    assert window._report.requested_start == date.min
    assert window._report.requested_end == date(1, 1, 2)

    window.end_date.setDate(QDate(9999, 12, 31))
    window.refresh()
    assert window._report is None
    assert not window.csv_button.isEnabled() and not window.png_button.isEnabled()
    assert "翌日を表現できません" in dashboard_data.messages[-1][1]


def test_missing_periods_are_distinct_and_break_the_graph(dashboard_data: DashboardData) -> None:
    dashboard_data.scenario.plan("a-4", "a", date(2026, 3, 4), value="30")
    window = dashboard_data.window
    window.end_date.setDate(QDate(2026, 3, 4))
    window.refresh()
    _select_formula(dashboard_data)
    assert _cell(window.period_table, 2, 5) == "予定なし"
    window.period_table.selectRow(2)
    assert "予定枠・交換境界はありません" in window.detail_text.toPlainText()
    series = window.chart.series()
    assert len(series) == 2
    first, second = series
    assert isinstance(first, QLineSeries) and isinstance(second, QLineSeries)
    assert first.count() == 2 and second.count() == 1


def test_redrawing_hides_removed_axes_before_they_are_deleted(
    dashboard_data: DashboardData,
) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    old_axes = window.chart.axes()
    assert len(old_axes) == 2 and all(axis.isVisible() for axis in old_axes)

    window._draw_chart()

    assert all(not axis.isVisible() for axis in old_axes)
    assert len(window.chart.axes()) == 2


def test_unmeasured_period_is_not_shown_as_no_schedule(dashboard_data: DashboardData) -> None:
    dashboard_data.scenario.plan("a-3", "a", date(2026, 3, 3))
    window = dashboard_data.window
    window.end_date.setDate(QDate(2026, 3, 3))
    window.refresh()
    _select_formula(dashboard_data)
    window.period_table.selectRow(2)
    assert _cell(window.period_table, 2, 5) == "値なし"
    assert "予定枠が未実測" in window.detail_text.toPlainText()


def test_disabled_version_and_partial_application_remain_visible_in_csv(
    dashboard_data: DashboardData, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scenario = dashboard_data.scenario
    service = scenario.formula_service("unused", registered=date(2026, 3, 1))
    service.save_version(
        dashboard_data.formula_id, scenario.version(date(2026, 3, 2), (), active=False)
    )
    window = dashboard_data.window
    window.refresh()
    _select_formula(dashboard_data)
    assert _cell(window.period_table, 1, 5) == "適用なし"
    csv_path = tmp_path / "disabled.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(csv_path), ""))
    window.csv_button.click()
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8-sig"), newline="")))
    assert rows[-1]["集計状態"] == "適用なし"
    assert rows[-1]["表示値"] == rows[-1]["場所ID"] == ""

    window.unit_combo.setCurrentIndex(window.unit_combo.findData(AggregationPeriodUnit.MONTH.value))
    window.refresh()
    _select_formula(dashboard_data)
    assert "一部期間のみ適用" in _cell(window.period_table, 0, 5)
    assert "一部期間のみ適用" in window.chart.title()
    assert "全期間の完全な値ではありません" in window.detail_text.toPlainText()
    assert "2026-03-01 以上 2026-03-02 未満" in window.detail_text.toPlainText()


def test_dashboard_and_csv_display_definition_initial(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario = Scenario(memory_conn)
    scenario.definition("initial")
    scenario.context.measurement_definitions.set_initial_reading("initial", Decimal("10"))
    scenario.plan("first", "initial", date(2026, 3, 1), value="15")
    errors: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "critical", lambda _parent, _title, text, *args: errors.append(text)
    )
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, _title, text, *args: errors.append(text)
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: QMessageBox.StandardButton.Ok)
    window = DashboardWindow(scenario.context, StaffSelectionMemory())
    qtbot.addWidget(window)
    window.show()
    window.start_date.setDate(QDate(2026, 3, 1))
    window.end_date.setDate(QDate(2026, 3, 1))
    window.refresh()
    index = window.target_combo.findData("definition:initial")
    assert index >= 0
    window.target_combo.setCurrentIndex(index)

    assert _cell(window.period_table, 0, 3) == "5"
    assert _cell(window.details_table, 0, 5) == "測定定義の初期値"
    assert _cell(window.details_table, 0, 6) == "initial"
    assert _cell(window.details_table, 0, 7) == "10"

    csv_path = tmp_path / "initial.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(csv_path), ""))
    window.csv_button.click()
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8-sig"), newline="")))
    detail = next(row for row in rows if row["行種別"] == "明細")
    assert (detail["基準元"], detail["基準ID"], detail["基準読値_無丸め"]) == (
        "測定定義の初期値",
        "initial",
        "10",
    )
    assert not errors


def test_dashboard_csv_and_png_outputs_and_cancel(
    dashboard_data: DashboardData, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    csv_path = tmp_path / "集計.csv"
    png_path = tmp_path / "グラフ.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: ("", ""))
    window.csv_button.click()
    assert not dashboard_data.messages

    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(csv_path), ""))
    window.csv_button.click()
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8-sig"), newline="")))
    assert rows[0]["行種別"] == "集計"
    assert rows[0]["場所ID"] == "location-a;location-b"
    assert rows[0]["無丸め値"] == "7"
    assert rows[0]["後付け設定"] == "はい"
    assert {row["場所ID"] for row in rows if row["行種別"] == "明細"} == {
        "location-a",
        "location-b",
    }
    assert dashboard_data.messages[-1][0] == "information"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(png_path), ""))
    window.png_button.click()
    image = QImage(str(png_path))
    assert not image.isNull()
    assert image.width() > 0 and image.height() > 0
    assert dashboard_data.messages[-1][0] == "information"


def test_png_encoding_and_open_failures_are_reported(
    dashboard_data: DashboardData, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    existing = tmp_path / "existing.png"
    existing.write_bytes("元データ".encode())
    with monkeypatch.context() as patch:
        patch.setattr(QPixmap, "save", lambda *_args, **_kwargs: False)
        with pytest.raises(FileOutputError, match="画像書き込み"):
            aggregation_export.save_chart_png(existing, window.chart_view)
        patch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(existing), ""))
        window.png_button.click()
        assert dashboard_data.messages[-1][0] == "warning"
        assert "画像書き込み" in dashboard_data.messages[-1][1]
        assert list(tmp_path.iterdir()) == [existing]
    assert existing.read_bytes() == "元データ".encode()
    assert list(tmp_path.iterdir()) == [existing]
    with pytest.raises(FileOutputError, match="保存できません"):
        aggregation_export.save_chart_png(tmp_path / "missing" / "graph.png", window.chart_view)


def test_png_commit_failure_preserves_existing_file_and_is_reported(
    dashboard_data: DashboardData, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    existing = tmp_path / "existing.png"
    existing.write_bytes("元データ".encode())
    monkeypatch.setattr(QSaveFile, "commit", lambda _self: False)
    with pytest.raises(FileOutputError, match="確定できません"):
        aggregation_export.save_chart_png(existing, window.chart_view)
    assert existing.read_bytes() == "元データ".encode()
    assert list(tmp_path.iterdir()) == [existing]

    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(existing), ""))
    window.png_button.click()
    assert dashboard_data.messages[-1][0] == "warning"
    assert "確定できません" in dashboard_data.messages[-1][1]
    assert existing.read_bytes() == "元データ".encode()
    assert list(tmp_path.iterdir()) == [existing]


@pytest.mark.parametrize("kind", ["csv", "png"])
def test_data_change_during_export_dialog_cannot_write_old_results(
    dashboard_data: DashboardData,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    path = tmp_path / f"stale.{kind}"

    def choose(*_args: object) -> tuple[str, str]:
        window.invalidate()
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    (window.csv_button if kind == "csv" else window.png_button).click()
    assert not path.exists()
    assert dashboard_data.messages[-1][0] == "warning"
    assert "集計を再取得" in dashboard_data.messages[-1][1]


def test_refresh_failure_discards_table_chart_and_exports(
    dashboard_data: DashboardData, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    assert window.period_table.rowCount() and window.csv_button.isEnabled()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ValidationError("データ不整合")

    monkeypatch.setattr(dashboard_data.scenario.context.aggregation_reports, "get_report", fail)
    window.refresh()
    assert not window.chart.series()
    assert window.period_table.rowCount() == 0
    assert not window.csv_button.isEnabled()
    assert not window.png_button.isEnabled()
    assert "取得に失敗" in window.status_label.text()
    assert dashboard_data.messages[-1] == ("warning", "データ不整合")


@pytest.mark.parametrize("raw", ["1e400", "1e-400"])
def test_unrenderable_value_preserves_exact_table_and_csv(
    dashboard_data: DashboardData, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    report = window._report
    assert report is not None
    first = report.periods[0]
    changed = tuple(
        replace(item, value=Decimal(raw)) if item.target_id == dashboard_data.formula_id else item
        for item in first.results
    )
    synthetic = replace(report, periods=(replace(first, results=changed), *report.periods[1:]))
    monkeypatch.setattr(
        dashboard_data.scenario.context.aggregation_reports, "get_report", lambda *a, **k: synthetic
    )
    window.refresh()
    _select_formula(dashboard_data)
    assert "描画できない値" in window.chart_note.text()
    assert not window.png_button.isEnabled()
    assert window.csv_button.isEnabled()
    assert _cell(window.period_table, 0, 4) == format(Decimal(raw), "f")


def test_distinct_values_collapsed_by_float_do_not_produce_a_misleading_chart(
    dashboard_data: DashboardData, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard_data.scenario.plan("a-3", "a", date(2026, 3, 3), value="30")
    window = dashboard_data.window
    window.end_date.setDate(QDate(2026, 3, 3))
    window.refresh()
    _select_formula(dashboard_data)
    report = window._report
    assert report is not None
    raw_values = ("9007199254740992", "9007199254740993", "9007199254740992")
    periods = tuple(
        replace(
            period,
            results=tuple(
                replace(item, value=Decimal(raw))
                if item.target_id == dashboard_data.formula_id
                else item
                for item in period.results
            ),
        )
        for period, raw in zip(report.periods, raw_values, strict=True)
    )
    synthetic = replace(report, periods=periods)
    monkeypatch.setattr(
        dashboard_data.scenario.context.aggregation_reports, "get_report", lambda *a, **k: synthetic
    )
    window.refresh()
    _select_formula(dashboard_data)

    assert "異なる値を区別できません（表の行: 1, 2, 3）" in window.chart_note.text()
    assert not window.png_button.isEnabled()
    assert window.csv_button.isEnabled()
    assert [_cell(window.period_table, row, 4) for row in range(3)] == list(raw_values)


def test_unrenderable_tick_spacing_disables_png(dashboard_data: DashboardData) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    first, second = window._rows
    assert first[1] is not None and second[1] is not None
    window._rows = [
        (first[0], replace(first[1], value=Decimal("10000000000000000"))),
        (second[0], replace(second[1], value=Decimal("10000000000000002"))),
    ]

    window._draw_chart()

    assert "目盛りを区別できません" in window.chart_note.text()
    assert not window.png_button.isEnabled()
    assert window.csv_button.isEnabled()


def test_chart_uses_enough_precision_for_distinct_tick_labels(
    dashboard_data: DashboardData,
) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    first, second = window._rows
    assert first[1] is not None and second[1] is not None
    window._rows = [
        (first[0], replace(first[1], value=Decimal("100000000"))),
        (second[0], replace(second[1], value=Decimal("100000001"))),
    ]

    window._draw_chart()

    assert window.png_button.isEnabled()
    axis = window.chart.axes(Qt.Orientation.Vertical)[0]
    assert isinstance(axis, QValueAxis)
    assert axis.labelFormat() == "%.10g"
    ticks = [axis.min() + (axis.max() - axis.min()) * (index / 4) for index in range(5)]
    assert len({format(tick, ".10g") for tick in ticks}) == 5


def test_padded_range_overflow_disables_png(dashboard_data: DashboardData) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    first, second = window._rows
    assert first[1] is not None and second[1] is not None
    window._rows = [
        (first[0], replace(first[1], value=Decimal("-8.3e307"))),
        (second[0], replace(second[1], value=Decimal("8.3e307"))),
    ]

    window._draw_chart()

    assert "範囲が描画可能範囲外" in window.chart_note.text()
    assert not window.png_button.isEnabled()
    assert window.csv_button.isEnabled()


def test_chart_escapes_rich_text_in_series_name_and_unit(dashboard_data: DashboardData) -> None:
    window = dashboard_data.window
    _select_formula(dashboard_data)
    window._rows = [
        (
            period,
            replace(result, display_name="<b>設備</b>", unit="<kWh>&") if result else None,
        )
        for period, result in window._rows
    ]

    window._draw_chart()

    assert "&lt;b&gt;設備&lt;/b&gt;" in window.chart.title()
    assert "&lt;kWh&gt;&amp;" in window.chart.title()
    axis = window.chart.axes(Qt.Orientation.Vertical)[0]
    assert axis.titleText() == "&lt;kWh&gt;&amp;"


def test_pending_assignment_guides_correction_and_refreshes(
    dashboard_data: DashboardData, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = dashboard_data.scenario
    assignment = scenario.repository.list_meter_assignments("a")[0]
    scenario.conn.execute(
        "INSERT INTO assignment_review_flags "
        "(measurement_definition_id, meter_id, assignment_effective_from, reason, created_at) "
        "SELECT measurement_definition_id, meter_id, effective_from, '倍率の確認待ち', ? "
        "FROM meter_assignments WHERE measurement_definition_id = ? AND meter_id = ?",
        ("2026-03-01 03:00:00", "a", "meter-a"),
    )
    window = dashboard_data.window
    window.refresh()
    _select_formula(dashboard_data)
    assert _cell(window.period_table, 0, 3) == "-3"
    assert "参考値" in _cell(window.period_table, 0, 5)
    assert "倍率の確認待ち" in window.detail_text.toPlainText()
    assert "割当条件の訂正画面" in window.detail_text.toPlainText()
    assert "+0900" in window.pending_combo.currentText()
    assert window.correction_button.isEnabled()
    opened: list[bool] = []

    def correct(dialog: MasterDialog) -> int:
        opened.append(dialog.tabs.currentWidget() is dialog.correction_tab)
        scenario.context.assignment_calculation_settings.correct_assignment(
            AssignmentCalculationCorrectionInput(
                "a",
                "meter-a",
                assignment.effective_from,
                Decimal("2"),
                None,
                scenario.staff_id,
                "倍率を確認",
            )
        )
        dialog.saved.emit()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(MasterDialog, "exec", correct)
    window.correction_button.click()
    assert opened == [True]
    _select_formula(dashboard_data)
    assert _cell(window.period_table, 0, 3) == "17"
    assert not window.correction_button.isEnabled()


def test_dashboard_controls_reachable_at_150_percent(
    dashboard_data: DashboardData, restore_app_font: QFont
) -> None:
    apply_display_settings(DisplaySettings(None, 150))
    window = dashboard_data.window
    window.resize(1280, 680)
    window.show()
    QApplication.processEvents()
    assert window.width() <= 1280 and window.height() <= 680
    content = window.scroll_area.widget()
    assert content is not None
    for target in (
        window.refresh_button,
        window.target_combo,
        window.chart_view,
        window.period_table,
        window.detail_text,
        window.correction_button,
        window.details_table,
        window.csv_button,
        window.png_button,
    ):
        position = target.mapTo(content, target.rect().center())
        window.scroll_area.ensureVisible(position.x(), position.y(), 10, 10)
        QApplication.processEvents()
        center = target.mapToGlobal(target.rect().center())
        assert (
            window.scroll_area.viewport()
            .rect()
            .contains(window.scroll_area.viewport().mapFromGlobal(center))
        ), f"{target.objectName() or type(target).__name__}: {target.size()}"
        assert window.rect().contains(window.mapFromGlobal(center))
