from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QMessageBox, QTreeWidgetItem
from pytestqt.qtbot import QtBot

from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ValueSchema,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
)
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.reading_route_page import ReadingRouteTree


def _definition_request(
    definition_id: str,
    location_id: str,
    category: Category,
    display_name: str,
) -> MeasurementDefinitionCreateRequest:
    return MeasurementDefinitionCreateRequest(
        measurement_definition_id=definition_id,
        location_id=location_id,
        display_name=display_name,
        category=category,
        unit="kWh",
        value_schema=ValueSchema.DECIMAL_STRING,
        multiplier=Decimal("1"),
        decimal_places=2,
        warning_threshold=None,
        rollover_value=None,
        calculation_method=CalculationMethod.DELTA,
        reading_cycle=ReadingCycle.DAILY_1X,
        purpose=Purpose.OPERATIONAL,
        generation_start_date=date(2026, 2, 1),
        initial_schedule_json=None,
        initial_meter_id=None,
    )


@pytest.fixture
def route_dialog(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    fixed_clock: datetime,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> MasterDialog:
    connection, _ = file_database_repository
    services = create_app_context(connection, clock=lambda: fixed_clock)
    for request in (
        _definition_request("electric-a", "location-b", Category.ELECTRICITY, "電力A"),
        _definition_request("electric-z", "location-b", Category.ELECTRICITY, "電力Z"),
        _definition_request("water-a", "location-b", Category.WATER, "水量A"),
        _definition_request("gas-a", "location-a", Category.GAS, "ガスA"),
    ):
        services.measurement_definitions.create_measurement_definition(request)
    dialog = MasterDialog(services, StaffSelectionMemory())
    qtbot.addWidget(dialog)
    dialog.show()
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    return dialog


def _item_by_id(parent: QTreeWidgetItem, item_id: str) -> QTreeWidgetItem | None:
    for index in range(parent.childCount()):
        child = parent.child(index)
        if child is not None and child.text(2) == item_id:
            return child
    return None


def _top_item(tree: ReadingRouteTree, item_id: str) -> QTreeWidgetItem:
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if item is not None and item.text(2) == item_id:
            return item
    pytest.fail(f"場所 {item_id} が見つかりません")


def _child_item(parent: QTreeWidgetItem, item_id: str, message: str = "子項目") -> QTreeWidgetItem:
    item = _item_by_id(parent, item_id)
    if item is None:
        pytest.fail(f"{message} {item_id} が見つかりません")
    return item


def _location_ids(tree: ReadingRouteTree) -> list[str]:
    result: list[str] = []
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if item is not None:
            result.append(item.text(2))
    return result


def _child_ids(parent: QTreeWidgetItem) -> list[str]:
    result: list[str] = []
    for index in range(parent.childCount()):
        item = parent.child(index)
        if item is not None:
            result.append(item.text(2))
    return result


def _definition_id(dialog: MasterDialog, display_name: str) -> str:
    definitions = dialog.services.measurement_definitions.list_measurement_definitions(
        show_inactive=True
    )
    for definition in definitions:
        if definition.display_name == display_name:
            return definition.measurement_definition_id
    pytest.fail(f"測定定義 {display_name} が見つかりません")


def test_route_tab_shows_defaults_as_unconfigured(route_dialog: MasterDialog) -> None:
    page = route_dialog.reading_route_page
    assert page is route_dialog.tabs.widget(5)
    assert _location_ids(page.tree) == ["location-a", "location-b"]

    location_b = _top_item(page.tree, "location-b")
    assert _child_ids(location_b) == [
        "location-b:electricity",
        "location-b:water",
    ]
    electricity = _child_item(location_b, "location-b:electricity", "カテゴリ")
    electric_a = _definition_id(route_dialog, "電力A")
    electric_z = _definition_id(route_dialog, "電力Z")
    assert _child_ids(electricity) == [electric_a, electric_z]
    assert location_b.text(1) == "未設定"
    assert electricity.text(1) == "未設定"
    assert _child_item(electricity, electric_a, "測定定義").text(1) == "未設定"
    assert not page.save_button.isEnabled()
    assert not page.up_button.isEnabled()
    assert not page.reset_button.isEnabled()


def test_button_and_keyboard_moves_only_within_siblings(
    route_dialog: MasterDialog, qtbot: QtBot
) -> None:
    page = route_dialog.reading_route_page
    location_b = _top_item(page.tree, "location-b")

    page.tree.setCurrentItem(location_b)
    page.up_button.click()
    assert _location_ids(page.tree) == ["location-b", "location-a"]
    qtbot.keyClick(
        page.tree,
        Qt.Key.Key_Down,
        modifier=Qt.KeyboardModifier.ControlModifier,
    )
    assert _location_ids(page.tree) == ["location-a", "location-b"]

    location_b = _top_item(page.tree, "location-b")
    electricity = _child_item(location_b, "location-b:electricity", "カテゴリ")
    electric_a = _definition_id(route_dialog, "電力A")
    electric_z_id = _definition_id(route_dialog, "電力Z")
    electric_z = _child_item(electricity, electric_z_id, "測定定義")
    page.tree.setCurrentItem(electric_z)
    qtbot.keyClick(
        page.tree,
        Qt.Key.Key_Home,
        modifier=Qt.KeyboardModifier.ControlModifier,
    )
    assert _child_ids(electricity) == [electric_z_id, electric_a]
    assert electric_z.parent() is electricity
    assert page.save_button.isEnabled()

    water = _child_item(location_b, "location-b:water", "カテゴリ")
    assert (
        ReadingRouteTree.can_drop_item(
            electric_z,
            _child_item(
                water,
                _definition_id(route_dialog, "水量A"),
                "測定定義",
            ),
            QAbstractItemView.DropIndicatorPosition.AboveItem,
        )
        is False
    )
    assert ReadingRouteTree.can_drop_item(
        electric_z,
        _child_item(electricity, electric_a, "測定定義"),
        QAbstractItemView.DropIndicatorPosition.AboveItem,
    )


def test_save_reload_and_reset_persist_all_three_levels(
    route_dialog: MasterDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = route_dialog.reading_route_page
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)

    location_b = _top_item(page.tree, "location-b")
    page.tree.setCurrentItem(location_b)
    page.first_button.click()
    electricity = _child_item(location_b, "location-b:electricity", "カテゴリ")
    page.tree.setCurrentItem(electricity)
    page.last_button.click()
    electric_z_id = _definition_id(route_dialog, "電力Z")
    electric_a = _definition_id(route_dialog, "電力A")
    electric_z = _child_item(electricity, electric_z_id, "測定定義")
    page.tree.setCurrentItem(electric_z)
    page.first_button.click()
    page.save_button.click()

    assert not page.has_unsaved_changes()
    assert not page.save_button.isEnabled()
    assert page.reset_button.isEnabled()
    page.reload()
    location_b = _top_item(page.tree, "location-b")
    assert _location_ids(page.tree) == ["location-b", "location-a"]
    assert _child_ids(location_b) == [
        "location-b:water",
        "location-b:electricity",
    ]
    electricity = _child_item(location_b, "location-b:electricity", "カテゴリ")
    assert _child_ids(electricity) == [electric_z_id, electric_a]
    assert location_b.text(1) == "設定済み"
    assert all(
        child is not None and child.text(1) == "設定済み"
        for index in range(electricity.childCount())
        if (child := electricity.child(index)) is not None
    )

    page.reset_button.click()
    page.reload()
    assert not page.reset_button.isEnabled()
    assert _location_ids(page.tree) == ["location-a", "location-b"]
    assert _top_item(page.tree, "location-b").text(1) == "未設定"


def test_discard_cancel_preserves_edit_and_tab_switch_is_reverted(
    route_dialog: MasterDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = route_dialog.reading_route_page
    route_dialog.tabs.setCurrentWidget(page)
    page.tree.setCurrentItem(_top_item(page.tree, "location-b"))
    page.up_button.click()
    expected = _location_ids(page.tree)

    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
    assert not page.confirm_discard_changes()
    assert _location_ids(page.tree) == expected
    route_dialog.tabs.setCurrentIndex(0)
    assert route_dialog.tabs.currentWidget() is page
    assert _location_ids(page.tree) == expected


def test_returning_to_route_tab_reloads_after_other_changes(
    route_dialog: MasterDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = route_dialog.reading_route_page
    route_dialog.tabs.setCurrentWidget(page)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    route_dialog.tabs.setCurrentIndex(0)
    route_dialog.services.measurement_definitions.create_measurement_definition(
        _definition_request("new-water", "location-c", Category.WATER, "追加水量")
    )
    route_dialog.tabs.setCurrentWidget(page)
    assert not page.has_unsaved_changes()
    assert _location_ids(page.tree) == ["location-a", "location-b", "location-c"]
    assert _top_item(page.tree, "location-b").text(1) == "未設定"


def test_stale_editor_is_reported_and_prompted_to_reload(
    route_dialog: MasterDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = route_dialog.reading_route_page
    page.tree.setCurrentItem(_top_item(page.tree, "location-b"))
    page.up_button.click()
    route_dialog.services.measurement_definitions.create_measurement_definition(
        _definition_request("new-water", "location-c", Category.WATER, "追加水量")
    )
    messages: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: messages.append(text))

    page.save_button.click()

    assert page.has_unsaved_changes()
    assert "再読込" in messages[-1]


def test_unexpected_errors_use_existing_guarded_error_dialog(
    route_dialog: MasterDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = route_dialog.reading_route_page
    page.tree.setCurrentItem(_top_item(page.tree, "location-b"))
    page.up_button.click()
    messages: list[str] = []
    monkeypatch.setattr(
        page._service,
        "save_route",
        lambda _request: (_ for _ in ()).throw(RuntimeError("試験用失敗")),
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, _title, text: messages.append(text),
    )

    page.save_button.click()

    assert page.has_unsaved_changes()
    assert "予期しないエラー" in messages[-1]


def test_empty_route_disables_editing(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    fixed_clock: datetime,
    qtbot: QtBot,
) -> None:
    connection, _ = file_database_repository
    services = create_app_context(connection, clock=lambda: fixed_clock)
    dialog = MasterDialog(services, StaffSelectionMemory())
    qtbot.addWidget(dialog)
    page = dialog.reading_route_page

    assert page.tree.isHidden()
    assert not page.empty_label.isHidden()
    assert not page.save_button.isEnabled()
    assert not page.reset_button.isEnabled()
    assert not page.up_button.isEnabled()
