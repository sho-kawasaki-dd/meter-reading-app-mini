from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import SheetBrowser
from playwright.sync_api import expect
from PySide6.QtCore import QModelIndex, QSignalBlocker, Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget
from pytestqt.qtbot import QtBot
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    SHEET_ID,
    STAFF_ID,
    build_services,
    confirmation_context,
    create_plan,
)
from test_mobile_reading_import import _replace_meter

from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync import MeterReadingSyncService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    ResultSource,
    StaffCreateRequest,
)
from meter_reading_mini.core.mobile_reading import (
    DRAFT_KIND,
    RESULT_KIND,
    encode_mr1_envelope,
    parse_mobile_reading_result,
)
from meter_reading_mini.core.reading_signature import verify_meter_reading_signature
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.db.connection import connect_readonly
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui.dialogs import mobile_reading_dialog
from meter_reading_mini.ui.dialogs.mobile_reading_dialog import MobileReadingDialog
from meter_reading_mini.ui.models.mobile_reading_preview_model import HEADERS
from meter_reading_mini.ui.sheet_save_directory_memory import SheetSaveDirectoryMemory
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory

NOW = datetime(2026, 2, 4, 3, tzinfo=UTC)


@dataclass
class ImportData:
    connection: sqlite3.Connection
    repository: SQLiteRepository
    writer: ReadingWriteOrchestrationService
    sync: MeterReadingSyncService
    services: AppContext
    staff_memory: StaffSelectionMemory = field(default_factory=StaffSelectionMemory)


@dataclass
class Messages:
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    information: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


def _entry(
    planned_id: str,
    raw: str | None,
    hour: int,
    *,
    updated_day: int = 6,
    status: str = "measured",
    note: str | None = None,
    structured: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "planned_reading_id": planned_id,
        "result_status": status,
        "raw_reading": raw,
        "structured_payload": structured,
        "recorded_at": _at(hour).isoformat(),
        "client_updated_at": datetime(2026, 2, updated_day, hour, tzinfo=UTC).isoformat(),
        "note": note,
    }


def _payload(*entries: dict[str, object], **header: object) -> dict[str, object]:
    return {"kind": RESULT_KIND, "v": 1, "sheet_id": SHEET_ID, "entries": list(entries), **header}


def _text(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _save_mobile(data: ImportData, *entries: dict[str, object]) -> None:
    result = parse_mobile_reading_result(_text(_payload(*entries)))
    response = data.sync.sync(result.to_sync_request(), confirmation_context())
    assert response.accepted_reading_items == len(entries)


def _build_import_data(connection: sqlite3.Connection, repository: SQLiteRepository) -> ImportData:
    writer, sync = build_services(connection, repository)
    for planned_id, hour in (("previous", 3), ("early", 8), ("late", 14), ("stale", 15)):
        create_plan(repository, planned_id, _at(hour))
    writer.save_manual_reading(
        ManualReadingInput("previous", ResultStatus.MEASURED, _at(3), Decimal("100"), None),
        staff_id=STAFF_ID,
    )
    return ImportData(
        connection,
        repository,
        writer,
        sync,
        create_app_context(connection, clock=lambda: NOW),
    )


@pytest.fixture
def import_data(memory_conn: sqlite3.Connection, sqlite_repository: SQLiteRepository) -> ImportData:
    return _build_import_data(memory_conn, sqlite_repository)


@pytest.fixture
def persistent_import_data(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> ImportData:
    return _build_import_data(*file_database_repository)


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> Messages:
    captured = Messages()
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, _title, text: captured.warnings.append(text)
    )
    monkeypatch.setattr(
        QMessageBox, "critical", lambda _parent, _title, text: captured.errors.append(text)
    )
    monkeypatch.setattr(
        QMessageBox, "information", lambda _parent, _title, text: captured.information.append(text)
    )

    def question(
        _parent: QWidget, _title: str, text: str, *_args: object
    ) -> QMessageBox.StandardButton:
        captured.questions.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    return captured


def _dialog(data: ImportData, qtbot: QtBot) -> MobileReadingDialog:
    dialog = MobileReadingDialog(data.services, SheetSaveDirectoryMemory(), data.staff_memory)
    qtbot.addWidget(dialog)
    return dialog


def _select_staff(dialog: MobileReadingDialog, staff_id: str = STAFF_ID) -> None:
    index = dialog.staff_combo.findData(staff_id)
    assert index > 0
    dialog.staff_combo.setCurrentIndex(index)


def _preview(dialog: MobileReadingDialog, payload: dict[str, object]) -> None:
    dialog.result_edit.setPlainText(_text(payload))
    dialog.preview_button.click()


def _cell(dialog: MobileReadingDialog, row: int, header: str) -> str:
    index = dialog.preview_model.index(row, HEADERS.index(header))
    result = dialog.preview_model.data(index)
    assert isinstance(result, str)
    return result


def test_manual_preview_waits_for_button_and_edits_invalidate_immediately(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = _dialog(import_data, qtbot)
    importer = import_data.services.mobile_reading_import
    original_preview = importer.preview
    calls: list[str] = []

    def record_preview(result):
        calls.append(result.sheet_id)
        return original_preview(result)

    monkeypatch.setattr(importer, "preview", record_preview)
    text = _text(_payload(_entry("early", "110", 8)))
    database_before = tuple(import_data.connection.iterdump())

    dialog.result_edit.setPlainText(text)
    assert calls == [] and dialog.preview_model.rowCount() == 0
    assert not dialog.commit_button.isEnabled()
    dialog.preview_button.click()
    assert calls == [SHEET_ID]
    assert dialog.preview_model.rowCount() == 1
    assert tuple(import_data.connection.iterdump()) == database_before
    assert not dialog.commit_button.isEnabled()

    _select_staff(dialog)
    assert dialog.commit_button.isEnabled()
    dialog.result_edit.setPlainText(f"{text}\n")
    assert dialog.preview_model.rowCount() == 0
    assert not dialog.commit_button.isEnabled()
    assert calls == [SHEET_ID]
    dialog.preview_button.click()
    assert calls == [SHEET_ID, SHEET_ID]
    assert dialog.commit_button.isEnabled()

    dialog.result_edit.clear()
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    dialog.preview_button.click()
    assert "入力してください" in messages.warnings[-1]
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    assert messages.errors == []


def test_clipboard_auto_previews_and_clears_old_preview_before_read_failure(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    code = encode_mr1_envelope(_payload(_entry("early", "110", 8)))
    QApplication.clipboard().setText(f"検針結果です。\n> {code}\n")
    dialog.paste_button.click()
    assert dialog.preview_model.rowCount() == 1 and dialog.commit_button.isEnabled()
    text_before = dialog.result_edit.toPlainText()

    class BrokenClipboard:
        def text(self) -> str:
            assert dialog.preview_model.rowCount() == 0
            assert not dialog.commit_button.isEnabled()
            raise RuntimeError("クリップボード取得失敗")

    monkeypatch.setattr(mobile_reading_dialog.QApplication, "clipboard", BrokenClipboard)
    dialog.paste_button.click()

    assert dialog.result_edit.toPlainText() == text_before
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    assert "予期しないエラー" in messages.errors[-1]
    assert "クリップボード取得失敗" in caplog.text


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_json_file_auto_previews_with_and_without_bom(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    encoding: str,
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    assert dialog.commit_button.isEnabled()
    target = tmp_path / "結果.json"
    text = _text(_payload(_entry("early", "115", 8, note="確認済み")))
    target.write_text(text, encoding=encoding)

    def choose(*_args: object) -> tuple[str, str]:
        assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
        return str(target), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    dialog.load_json_button.click()

    assert dialog.result_edit.toPlainText() == text
    assert dialog.preview_model.rowCount() == 1 and dialog.commit_button.isEnabled()
    assert "115" in _cell(dialog, 0, "入力結果")
    assert "確認済み" in _cell(dialog, 0, "入力結果")
    assert messages.warnings == messages.errors == []


def test_cancelling_json_selection_invalidates_preview_without_changing_text(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    text = dialog.result_edit.toPlainText()

    def cancel(*_args: object) -> tuple[str, str]:
        assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
        return "", ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", cancel)
    dialog.load_json_button.click()
    assert dialog.result_edit.toPlainText() == text
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    assert messages.warnings == messages.errors == []


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("mr1", "JSON 形式"),
        ("embedded", "JSON 形式"),
        ("array", "JSON オブジェクト"),
        ("quoted_mr1", "JSON オブジェクト"),
        ("nonfinite", "JSON 形式"),
        ("draft", "下書き"),
        ("broken", "JSON 形式"),
        ("encoding", "UTF-8"),
        ("missing", "UTF-8"),
    ],
)
def test_json_file_rejects_non_json_drafts_and_read_failures(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    payload = _payload(_entry("early", "110", 8))
    _preview(dialog, payload)
    before = dialog.result_edit.toPlainText()
    target = tmp_path / "invalid.json"
    if case == "mr1":
        target.write_text(encode_mr1_envelope(payload), encoding="utf-8")
    elif case == "embedded":
        target.write_text(f"結果:\n{_text(payload)}", encoding="utf-8")
    elif case == "array":
        target.write_text(json.dumps([payload]), encoding="utf-8")
    elif case == "quoted_mr1":
        target.write_text(json.dumps(encode_mr1_envelope(payload)), encoding="utf-8")
    elif case == "nonfinite":
        target.write_text('{"value": NaN}', encoding="utf-8")
    elif case == "draft":
        target.write_text(_text({"kind": DRAFT_KIND, "v": 1}), encoding="utf-8")
    elif case == "broken":
        target.write_text("{", encoding="utf-8")
    elif case == "encoding":
        target.write_bytes(b"\xff\xfe")

    def choose(*_args: object) -> tuple[str, str]:
        assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
        return str(target), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    dialog.load_json_button.click()

    assert message in messages.warnings[-1]
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    if case in {"encoding", "missing"}:
        assert dialog.result_edit.toPlainText() == before
    else:
        assert dialog.result_edit.toPlainText() == target.read_text(encoding="utf-8")
    assert messages.errors == []


@pytest.mark.parametrize("case", ["crc", "multiple", "draft", "broken", "duplicate"])
def test_invalid_result_text_never_reuses_previous_preview(
    import_data: ImportData, qtbot: QtBot, messages: Messages, case: str
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    entry = _entry("early", "110", 8)
    payload = _payload(entry)
    _preview(dialog, payload)
    assert dialog.commit_button.isEnabled()
    code = encode_mr1_envelope(payload)
    if case == "crc":
        prefix, checksum = code.rsplit(".", 1)
        altered = "0" if checksum[0] != "0" else "1"
        invalid = f"{prefix}.{altered}{checksum[1:]}"
        expected = "欠けているか改変"
    elif case == "multiple":
        invalid = code + "\n" + encode_mr1_envelope({**payload, "sheet_id": "b" * 32})
        expected = "複数の検針結果"
    elif case == "draft":
        invalid = _text({"kind": DRAFT_KIND, "v": 1})
        expected = "下書き"
    elif case == "duplicate":
        invalid = _text(_payload(entry, entry))
        expected = "重複"
    else:
        invalid = "{broken"
        expected = "JSON 形式"

    dialog.result_edit.setPlainText(invalid)
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    dialog.preview_button.click()
    assert expected in messages.warnings[-1]
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    assert messages.errors == []


@pytest.mark.parametrize("unexpected", [False, True])
def test_preview_failure_clears_old_state_before_error_notification(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    unexpected: bool,
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    assert dialog.commit_button.isEnabled()

    def fail(_result: object) -> None:
        assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
        if unexpected:
            raise RuntimeError("プレビュー処理失敗")
        raise ValidationError("プレビュー業務エラー")

    monkeypatch.setattr(import_data.services.mobile_reading_import, "preview", fail)
    dialog.preview_button.click()

    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    if unexpected:
        assert "予期しないエラー" in messages.errors[-1]
        assert "プレビュー処理失敗" in caplog.text
    else:
        assert "プレビュー業務エラー" in messages.warnings[-1]


def test_input_changed_during_preview_cannot_restore_an_old_snapshot(
    import_data: ImportData, qtbot: QtBot, messages: Messages
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    original = _text(_payload(_entry("early", "110", 8)))
    changed = _text(_payload(_entry("early", "115", 8)))
    dialog.result_edit.setPlainText(original)

    def edit_during_table_update() -> None:
        if dialog.preview_model.rowCount() == 1:
            dialog.result_edit.setPlainText(changed)

    dialog.preview_model.modelReset.connect(edit_during_table_update)
    dialog.preview_button.click()

    assert dialog.result_edit.toPlainText() == changed
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    assert "プレビュー中に入力内容が変更" in messages.warnings[-1]
    assert (
        import_data.repository.get_mobile_reading_import_targets(["early"])["early"].existing_result
        is None
    )


def test_staff_selection_is_shared_and_inactive_staff_cannot_be_selected(
    import_data: ImportData, qtbot: QtBot, messages: Messages
) -> None:
    payload = _payload(_entry("early", "110", 8))
    first = _dialog(import_data, qtbot)
    _preview(first, payload)
    assert not first.commit_button.isEnabled()
    _select_staff(first)
    assert first.commit_button.isEnabled() and import_data.staff_memory.staff_id == STAFF_ID

    second = _dialog(import_data, qtbot)
    assert second.staff_combo.currentData() == STAFF_ID
    _preview(second, payload)
    assert second.commit_button.isEnabled()

    fresh_memory = StaffSelectionMemory()
    fresh = MobileReadingDialog(import_data.services, SheetSaveDirectoryMemory(), fresh_memory)
    qtbot.addWidget(fresh)
    _preview(fresh, payload)
    assert fresh_memory.staff_id is None and fresh.staff_combo.currentData() == ""
    assert not fresh.commit_button.isEnabled()

    import_data.services.staff.deactivate_staff(STAFF_ID)
    second.show()
    assert not second.commit_button.isEnabled()

    third = _dialog(import_data, qtbot)
    assert third.staff_combo.findData(STAFF_ID) == -1
    assert third.staff_combo.currentData() == ""
    _preview(third, payload)
    assert third.preview_model.rowCount() == 1 and not third.commit_button.isEnabled()
    assert messages.warnings == messages.errors == []


@pytest.mark.parametrize("case", ["rejected", "duplicate", "stale"])
def test_no_accepted_rows_cannot_be_committed(
    import_data: ImportData, qtbot: QtBot, messages: Messages, case: str
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    late = _entry("late", "120", 14)
    if case != "rejected":
        _save_mobile(import_data, late)
    entry = (
        _entry("missing", "1", 8)
        if case == "rejected"
        else _entry("late", "119", 14, updated_day=5)
        if case == "stale"
        else late
    )
    _preview(dialog, _payload(entry))
    assert dialog.preview_model.rowCount() == 1
    assert not dialog.commit_button.isEnabled()
    assert case == "rejected" or "無視" in _cell(dialog, 0, "採否区分")
    assert messages.warnings == messages.errors == messages.questions == []


def test_preview_compares_excluded_input_and_recalculated_existing_result_and_commits(
    import_data: ImportData, qtbot: QtBot, messages: Messages
) -> None:
    late = _entry("late", "120", 14, note="既存")
    stale = _entry("stale", "130", 15, note="古い入力より新しい結果")
    _save_mobile(import_data, late, stale)
    old_targets = import_data.repository.get_mobile_reading_import_targets(["late", "stale"])
    before = old_targets["late"].existing_result
    before_stale = old_targets["stale"].existing_result
    assert before is not None
    assert before_stale is not None
    payload = _payload(
        late,
        _entry("early", "110", 8, note="今回"),
        _entry("missing", "1", 8),
        _entry("stale", "125", 15, updated_day=5),
        business_date="2026-02-03",
        filter={"reading_sessions": ["14:00"], "cycle_groups": ["weekly"]},
    )
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    database_before = tuple(import_data.connection.iterdump())
    _preview(dialog, payload)

    assert tuple(import_data.connection.iterdump()) == database_before
    assert dialog.preview_model.rowCount() == 4 and dialog.commit_button.isEnabled()
    assert _cell(dialog, 0, "場所") == "試験場所"
    assert _cell(dialog, 0, "測定項目") == "試験測定対象"
    assert _cell(dialog, 0, "業務日") == "2026-02-04"
    assert _cell(dialog, 0, "検針時間") == "08:00"
    assert _cell(dialog, 0, "現在の枠状態") == "完了"
    assert _cell(dialog, 0, "採否区分") == "無視: 重複"
    assert "使用量: 20" in _cell(dialog, 0, "既存結果")
    assert "結果元: スマホ検針" in _cell(dialog, 0, "既存結果")
    assert "使用量: 20" in _cell(dialog, 0, "入力結果")
    assert "使用量: 10" in _cell(dialog, 0, "最終見込結果")
    assert "既存結果を保持（間接再計算後）" in _cell(dialog, 0, "最終見込結果")
    assert "110" in _cell(dialog, 0, "最終見込の前回値・出所")
    assert "early" in _cell(dialog, 0, "最終見込の前回値・出所")
    warnings = _cell(dialog, 0, "警告・理由")
    assert "業務日" in warnings and "検針時間" in warnings and "周期区分" in warnings
    assert "シート: 週次 / 予定枠: 日次" in warnings
    assert _cell(dialog, 1, "採否区分") == "採用: 新規"
    assert _cell(dialog, 2, "採否区分") == "拒否"
    assert _cell(dialog, 2, "既存結果") == "不明"
    assert "未算出" in _cell(dialog, 2, "入力結果")
    assert _cell(dialog, 2, "最終見込結果") == "未算出"
    assert "見つかりません" in _cell(dialog, 2, "警告・理由")
    assert _cell(dialog, 3, "採否区分") == "無視: 古い入力"
    assert "値: 125" in _cell(dialog, 3, "入力結果")
    assert "値: 130" in _cell(dialog, 3, "既存結果")
    assert "値: 130" in _cell(dialog, 3, "最終見込結果")
    assert "既存結果を保持（間接再計算後）" in _cell(dialog, 3, "最終見込結果")
    assert "採用 1 件" in dialog.summary_label.text()
    assert "重複 1" in dialog.summary_label.text()
    assert (
        "古い入力 1" in dialog.summary_label.text() and "拒否 1 件" in dialog.summary_label.text()
    )
    assert "後続実測 2 件、交換境界 0 件" in dialog.summary_label.text()

    colors = [
        dialog.preview_model.data(
            dialog.preview_model.index(row, 0), Qt.ItemDataRole.BackgroundRole
        )
        for row in range(4)
    ]
    assert all(isinstance(color, QBrush) for color in colors)
    assert [color.color().name() for color in colors if isinstance(color, QBrush)] == [
        "#e4e4e4",
        "#fff1a8",
        "#ffe1e1",
        "#e4e4e4",
    ]
    notifications: list[int] = []
    dialog.reload_requested.connect(lambda: notifications.append(dialog.preview_model.rowCount()))
    dialog.commit_button.click()

    assert len(messages.questions) == 1
    assert "不採用" in messages.questions[0] and "重複 1" in messages.questions[0]
    assert "古い入力 1" in messages.questions[0]
    assert "拒否理由" in messages.questions[0] and "見つかりません" in messages.questions[0]
    assert len(messages.information) == 1
    assert "確定 1 件、重複 1 件、古い入力 1 件、拒否 1 件" in messages.information[0]
    assert notifications == [0]
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    targets = import_data.repository.get_mobile_reading_import_targets(["early", "late", "stale"])
    accepted = targets["early"].existing_result
    after = targets["late"].existing_result
    assert accepted is not None and accepted.raw_reading == Decimal("110")
    assert accepted.device_id == SHEET_ID and accepted.updated_by == "試験担当"
    assert accepted.result_source is ResultSource.MOBILE_HTML and accepted.signature
    assert after == replace(before, last_reading=Decimal("110"), usage_delta=Decimal("10"))
    assert targets["stale"].existing_result == before_stale
    assert messages.warnings == messages.errors == []


def test_auto_closed_revival_and_manual_result_overwrite_are_displayed_and_saved(
    import_data: ImportData, qtbot: QtBot, messages: Messages
) -> None:
    import_data.writer.save_manual_reading(
        ManualReadingInput("late", ResultStatus.MEASURED, _at(14), Decimal("120"), None),
        staff_id=STAFF_ID,
    )
    import_data.connection.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', close_reason = 'cutoff_elapsed', "
        "closed_at = ? WHERE planned_reading_id = ?",
        ("2026-02-04 07:00:00", "early"),
    )
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8), _entry("late", "125", 14)))

    assert "自動クローズ" in _cell(dialog, 0, "現在の枠状態")
    assert _cell(dialog, 0, "採否区分") == "採用: 自動クローズからの復活"
    assert "枠状態: 完了" in _cell(dialog, 0, "最終見込結果")
    assert _cell(dialog, 1, "採否区分") == "採用: 上書き"
    assert "結果元: PC 直接入力" in _cell(dialog, 1, "既存結果")
    assert "復活 1" in dialog.summary_label.text() and "上書き 1" in dialog.summary_label.text()
    dialog.commit_button.click()

    targets = import_data.repository.get_mobile_reading_import_targets(["early", "late"])
    assert targets["early"].task_state.value == "completed"
    final = targets["late"].existing_result
    assert final is not None and final.raw_reading == Decimal("125")
    assert final.last_reading == Decimal("110") and final.usage_delta == Decimal("15")
    assert final.result_source is ResultSource.MOBILE_HTML
    assert "確定 2 件" in messages.information[-1]
    assert messages.warnings == messages.errors == []


def test_warnings_do_not_exclude_accepted_rows(
    import_data: ImportData, qtbot: QtBot, messages: Messages
) -> None:
    import_data.connection.execute(
        "UPDATE measurement_definitions SET warning_threshold = ? "
        "WHERE measurement_definition_id = ?",
        ("5", DEFINITION_ID),
    )
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    assert dialog.commit_button.isEnabled()
    assert "警告閾値" in _cell(dialog, 0, "警告・理由")
    assert "警告あり 1 行" in dialog.summary_label.text()
    color = dialog.preview_model.data(
        dialog.preview_model.index(0, 0), Qt.ItemDataRole.BackgroundRole
    )
    assert isinstance(color, QBrush) and color.color().name() == "#fff1a8"
    assert messages.warnings == messages.errors == []


def test_preview_model_exposes_unwarned_rows_and_checks_invalid_indexes(
    import_data: ImportData, qtbot: QtBot, messages: Messages
) -> None:
    dialog = _dialog(import_data, qtbot)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    index = dialog.preview_model.index(0, HEADERS.index("入力結果"))
    color = dialog.preview_model.data(index, Qt.ItemDataRole.BackgroundRole)
    assert isinstance(color, QBrush) and color.color().name() == "#e2f2e5"
    assert dialog.preview_model.data(index, Qt.ItemDataRole.ToolTipRole) == _cell(
        dialog, 0, "入力結果"
    )
    assert dialog.preview_model.data(QModelIndex()) is None
    assert dialog.preview_model.headerData(-1, Qt.Orientation.Horizontal) is None
    assert dialog.preview_model.rowCount(index) == 0
    assert dialog.preview_model.columnCount(index) == 0
    assert messages.warnings == messages.errors == []


def test_rejected_existing_result_with_unavailable_previous_context_is_explicit(
    import_data: ImportData, qtbot: QtBot, messages: Messages
) -> None:
    late = _entry("late", "120", 14)
    _save_mobile(import_data, late)
    import_data.connection.execute(
        "INSERT INTO assignment_review_flags "
        "(measurement_definition_id, meter_id, assignment_effective_from, reason, created_at) "
        "SELECT measurement_definition_id, meter_id, effective_from, '要確認', ? "
        "FROM meter_assignments WHERE measurement_definition_id = ?",
        ("2026-02-04 00:00:00", DEFINITION_ID),
    )
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(late))

    assert not dialog.commit_button.isEnabled()
    assert _cell(dialog, 0, "採否区分") == "拒否"
    assert "既存結果を保持" in _cell(dialog, 0, "最終見込結果")
    assert _cell(dialog, 0, "最終見込の前回値・出所") == "未算出"
    detail = _cell(dialog, 0, "警告・理由")
    assert "最終見込の前回値を算出できません" in detail
    assert "割当条件の確認が必要" in detail
    assert messages.warnings == messages.errors == []


def test_confirmation_groups_excluded_rows_by_reason(
    import_data: ImportData, qtbot: QtBot, messages: Messages
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(
        dialog,
        _payload(
            _entry("early", "110", 8),
            _entry("missing-one", "1", 8),
            _entry("missing-two", "2", 8),
            _entry("late", "99", 14),
        ),
    )
    assert dialog.commit_button.isEnabled()
    dialog.commit_button.click()

    assert len(messages.questions) == 1
    assert "予定枠が見つかりません 2 件" in messages.questions[0]
    assert "使用量が負になります 1 件" in messages.questions[0]
    assert "確定 1 件" in messages.information[-1]
    assert "拒否 3 件" in messages.information[-1]
    assert messages.warnings == messages.errors == []


def test_rollover_and_threshold_warnings_are_both_visible(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    qtbot: QtBot,
    messages: Messages,
) -> None:
    writer, sync = build_services(
        memory_conn, sqlite_repository, multiplier=Decimal("2"), rollover_value=Decimal("100")
    )
    create_plan(sqlite_repository, "previous", _at(3))
    create_plan(sqlite_repository, "early", _at(8))
    writer.save_manual_reading(
        ManualReadingInput("previous", ResultStatus.MEASURED, _at(3), Decimal("98"), None),
        staff_id=STAFF_ID,
    )
    memory_conn.execute(
        "UPDATE measurement_definitions SET warning_threshold = ? "
        "WHERE measurement_definition_id = ?",
        ("10", DEFINITION_ID),
    )
    data = ImportData(
        memory_conn,
        sqlite_repository,
        writer,
        sync,
        create_app_context(memory_conn, clock=lambda: NOW),
    )
    dialog = _dialog(data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "5", 8)))

    assert dialog.commit_button.isEnabled()
    assert "使用量: 14" in _cell(dialog, 0, "最終見込結果")
    warning = _cell(dialog, 0, "警告・理由")
    assert "ロールオーバー" in warning and "警告閾値" in warning
    dialog.commit_button.click()
    assert "確定 1 件" in messages.information[-1]
    assert messages.warnings == messages.errors == []


def test_structured_values_and_uncalculated_rejected_inputs_are_distinguished(
    import_data: ImportData, qtbot: QtBot, messages: Messages
) -> None:
    import_data.repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="power-factor",
            location_id=LOCATION_ID,
            display_name="力率",
            category=Category.WATER,
            unit="%",
            value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            multiplier=Decimal("1"),
            decimal_places=None,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY,
            reading_cycle=ReadingCycle.MONTHLY,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=date(2026, 2, 1),
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )
    create_plan(import_data.repository, "power", _at(8), measurement_definition_id="power-factor")
    dialog = _dialog(import_data, qtbot)
    _preview(
        dialog,
        _payload(
            _entry("power", None, 8, structured={"percent": "95.50", "phase": "lag"}),
            _entry("early", None, 8, structured={"unexpected": "入力"}),
        ),
    )
    assert "95.50%（遅れ）" in _cell(dialog, 0, "入力結果")
    assert "使用量: なし" in _cell(dialog, 0, "入力結果")
    assert "95.50%（遅れ）" in _cell(dialog, 0, "最終見込結果")
    assert _cell(dialog, 1, "採否区分") == "拒否"
    assert "構造化入力" in _cell(dialog, 1, "入力結果")
    assert "入力" in _cell(dialog, 1, "入力結果")
    assert "未算出" in _cell(dialog, 1, "入力結果")
    assert messages.warnings == messages.errors == []


def test_repeated_boundary_recalculation_count_is_shown_once(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    qtbot: QtBot,
    messages: Messages,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    for planned_id, hour in (("old", 3), ("first", 4), ("second", 5)):
        create_plan(sqlite_repository, planned_id, _at(hour))
    writer.save_manual_reading(
        ManualReadingInput("old", ResultStatus.MEASURED, _at(3), Decimal("100"), None),
        staff_id=STAFF_ID,
    )
    _replace_meter(memory_conn, sqlite_repository, writer, initial="5")
    create_plan(sqlite_repository, "new", _at(8), meter_id="new-meter")
    data = ImportData(
        memory_conn,
        sqlite_repository,
        writer,
        sync,
        create_app_context(memory_conn, clock=lambda: NOW),
    )
    _save_mobile(data, _entry("new", "8", 8))
    dialog = _dialog(data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("first", "105", 4), _entry("second", "110", 5)))

    assert dialog.commit_button.isEnabled()
    assert "後続実測 1 件、交換境界 1 件" in dialog.summary_label.text()
    dialog.commit_button.click()
    assert len(messages.information) == 1
    assert messages.warnings == messages.errors == []


@pytest.mark.parametrize(
    ("initial", "expected"), [("5", "交換時の新メーター初期値"), (None, "交換時の初期値なし")]
)
def test_replacement_previous_source_is_displayed(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    qtbot: QtBot,
    messages: Messages,
    initial: str | None,
    expected: str,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "old", _at(3))
    writer.save_manual_reading(
        ManualReadingInput("old", ResultStatus.MEASURED, _at(3), Decimal("100"), None),
        staff_id=STAFF_ID,
    )
    _replace_meter(memory_conn, sqlite_repository, writer, initial=initial)
    create_plan(sqlite_repository, "new", _at(8), meter_id="new-meter")
    data = ImportData(
        memory_conn,
        sqlite_repository,
        writer,
        sync,
        create_app_context(memory_conn, clock=lambda: NOW),
    )
    dialog = _dialog(data, qtbot)
    _preview(dialog, _payload(_entry("new", "8", 8)))
    assert expected in _cell(dialog, 0, "入力計算時の前回値・出所")
    assert expected in _cell(dialog, 0, "最終見込の前回値・出所")
    assert messages.warnings == messages.errors == []


@pytest.mark.parametrize("source", ["mr1", "json"])
def test_generated_html_round_trip_through_dialog_and_database(
    persistent_import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    sheet_browser: SheetBrowser,
    tmp_path: Path,
    source: str,
) -> None:
    data = persistent_import_data
    dialog = _dialog(data, qtbot)
    notifications: list[bool] = []
    dialog.reload_requested.connect(lambda: notifications.append(True))
    sheet_path = tmp_path / "export.html"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(sheet_path), ""))
    dialog.export_button.click()
    assert sheet_path.exists() and notifications == [True]

    context = sheet_browser.new_context(viewport=(390, 844), mobile=True)
    page = sheet_browser.open_file(sheet_path, context=context)
    assert page.url == sheet_path.as_uri()
    assert page.evaluate("navigator.maxTouchPoints > 0 && matchMedia('(pointer: coarse)').matches")
    page.locator('.tab[data-tab="all"]').tap()
    target = page.locator('.card[data-slot-id="early"]')
    target.locator(".raw").fill("110.00")
    target.locator(".note").fill("現場で確認")
    target.locator(".confirm").tap()
    expect(page.locator("#result-count")).to_contain_text("出力対象: 1 件")
    page.evaluate(
        """() => Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: { writeText: () => Promise.reject(new Error("clipboard unavailable")) }
        })"""
    )
    page.locator("#result-copy").tap()
    expect(page.locator("#result-feedback")).to_contain_text("手動でコピー")
    mr1 = page.locator("#result-export-text").input_value()
    parsed = parse_mobile_reading_result(mr1)
    assert len(parsed.entries) == 1 and parsed.entries[0].planned_reading_id == "early"
    with page.expect_download() as download_info:
        page.locator("#result-download").tap()
    json_path = tmp_path / "result.json"
    download_info.value.save_as(json_path)
    assert parse_mobile_reading_result(json_path.read_text(encoding="utf-8")) == parsed

    _select_staff(dialog)
    before = tuple(data.connection.iterdump())
    if source == "mr1":
        QApplication.clipboard().setText(mr1)
        dialog.paste_button.click()
        assert dialog.result_edit.toPlainText() == mr1
    else:
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(json_path), ""))
        dialog.load_json_button.click()
        assert dialog.result_edit.toPlainText() == json_path.read_text(encoding="utf-8")
    assert dialog.preview_model.rowCount() == 1 and dialog.commit_button.isEnabled()
    assert "110.00" in _cell(dialog, 0, "入力結果")
    assert "採用 1 件" in dialog.summary_label.text()
    assert tuple(data.connection.iterdump()) == before

    dialog.commit_button.click()
    target_record = data.repository.get_mobile_reading_import_targets(["early"])["early"]
    stored = target_record.existing_result
    assert stored is not None and stored.raw_reading == Decimal("110.00")
    assert stored.last_reading == Decimal("100") and stored.usage_delta == Decimal("10")
    assert stored.note == "現場で確認"
    assert stored.device_id == parsed.sheet_id and stored.updated_by == "試験担当"
    assert stored.result_source is ResultSource.MOBILE_HTML and stored.signature
    assert stored.recorded_at == parsed.entries[0].recorded_at
    assert stored.client_updated_at == parsed.entries[0].client_updated_at
    verify_meter_reading_signature(
        planned_reading_id="early",
        result_status=ResultStatus.MEASURED,
        raw_reading=Decimal("110.00"),
        structured_payload_json=None,
        recorded_at=parsed.entries[0].recorded_at,
        device_id=parsed.sheet_id,
        signature=stored.signature,
    )
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    with closing(connect_readonly(tmp_path / "meter-reading.db")) as readonly:
        row = readonly.execute(
            "SELECT p.task_state, r.result_status, r.raw_reading, r.last_reading, r.usage_delta, "
            "r.device_id, r.updated_by, r.result_source, r.signature, r.note "
            "FROM planned_readings AS p JOIN reading_transactions AS r USING (planned_reading_id) "
            "WHERE p.planned_reading_id = ?",
            ("early",),
        ).fetchone()
        assert row == (
            "completed",
            "measured",
            "110.00",
            "100",
            "10.00",
            parsed.sheet_id,
            "試験担当",
            "mobile_html",
            stored.signature,
            "現場で確認",
        )
        assert readonly.execute(
            "SELECT task_state FROM planned_readings WHERE planned_reading_id = ?", ("late",)
        ).fetchone() == ("open",)
        assert readonly.execute(
            "SELECT COUNT(*) FROM reading_transactions WHERE planned_reading_id = ?", ("late",)
        ).fetchone() == (0,)
    assert notifications == [True, True]
    assert "確定 1 件" in messages.information[-1]
    assert messages.warnings == messages.errors == []


@pytest.mark.parametrize("change", ["text", "text_reverted", "staff", "staff_reverted", "inactive"])
def test_confirmation_revalidates_input_and_staff_after_nested_event_loop(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    second_staff = import_data.services.staff.create_staff(StaffCreateRequest("別の担当者"))
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    old_text = dialog.result_edit.toPlainText()
    notifications: list[bool] = []
    dialog.reload_requested.connect(lambda: notifications.append(True))

    def change_during_confirmation(*_args: object) -> QMessageBox.StandardButton:
        assert dialog.commit_button.isEnabled()
        if change.startswith("text"):
            dialog.result_edit.setPlainText(_text(_payload(_entry("early", "115", 8))))
            if change == "text_reverted":
                dialog.result_edit.setPlainText(old_text)
        elif change.startswith("staff"):
            _select_staff(dialog, second_staff.staff_id)
            if change == "staff_reverted":
                _select_staff(dialog)
        else:
            import_data.services.staff.deactivate_staff(STAFF_ID)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", change_during_confirmation)
    dialog.commit_button.click()

    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    assert "再度プレビュー" in dialog.summary_label.text()
    assert "再度プレビュー" in messages.warnings[-1]
    assert messages.information == messages.errors == []
    assert notifications == []
    target = import_data.repository.get_mobile_reading_import_targets(["early"])["early"]
    assert target.existing_result is None


def test_confirmation_checks_text_even_if_edit_signal_was_blocked(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    with QSignalBlocker(dialog.result_edit):
        dialog.result_edit.setPlainText(_text(_payload(_entry("early", "115", 8))))
    assert dialog.commit_button.isEnabled()

    dialog.commit_button.click()

    assert messages.questions == []
    assert "入力内容または担当者が変更" in messages.warnings[-1]
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    assert (
        import_data.repository.get_mobile_reading_import_targets(["early"])["early"].existing_result
        is None
    )


def test_cancel_confirmation_preserves_an_unchanged_preview(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    dialog.commit_button.click()

    assert dialog.preview_model.rowCount() == 1 and dialog.commit_button.isEnabled()
    assert (
        import_data.repository.get_mobile_reading_import_targets(["early"])["early"].existing_result
        is None
    )
    assert messages.information == messages.warnings == messages.errors == []


def test_changed_database_rejects_old_preview_and_preserves_concurrent_result(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    notifications: list[bool] = []
    dialog.reload_requested.connect(lambda: notifications.append(True))

    def save_other_result(*_args: object) -> QMessageBox.StandardButton:
        import_data.writer.save_manual_reading(
            ManualReadingInput("early", ResultStatus.MEASURED, _at(8), Decimal("109"), None),
            staff_id=STAFF_ID,
        )
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", save_other_result)
    dialog.commit_button.click()

    existing = import_data.repository.get_mobile_reading_import_targets(["early"])[
        "early"
    ].existing_result
    assert existing is not None and existing.raw_reading == Decimal("109")
    assert existing.result_source is ResultSource.MANUAL
    assert "再度プレビュー" in messages.warnings[-1]
    assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
    assert notifications == [] and messages.information == messages.errors == []


def test_unexpected_commit_failure_clears_preview_before_reporting(
    import_data: ImportData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dialog = _dialog(import_data, qtbot)
    _select_staff(dialog)
    _preview(dialog, _payload(_entry("early", "110", 8)))
    notifications: list[bool] = []
    dialog.reload_requested.connect(lambda: notifications.append(True))

    def fail(*_args: object, **_kwargs: object) -> None:
        assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
        raise RuntimeError("一括確定失敗")

    monkeypatch.setattr(import_data.services.mobile_reading_import, "commit", fail)
    dialog.commit_button.click()

    assert "再度プレビュー" in dialog.summary_label.text()
    assert "予期しないエラー" in messages.errors[-1]
    assert "一括確定失敗" in caplog.text
    assert notifications == [] and messages.information == []
    assert (
        import_data.repository.get_mobile_reading_import_targets(["early"])["early"].existing_result
        is None
    )
