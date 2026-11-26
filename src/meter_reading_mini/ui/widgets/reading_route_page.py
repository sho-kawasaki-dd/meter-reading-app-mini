"""検針順路の 3 階層を編集する画面。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragMoveEvent, QDropEvent, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.contract_models import Category
from meter_reading_mini.core.reading_route_service import (
    ReadingRouteEditor,
    ReadingRouteSaveCategory,
    ReadingRouteSaveLocation,
    ReadingRouteSaveRequest,
)
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.models.reading_overview_model import CATEGORY_LABELS
from meter_reading_mini.ui.widgets.master_common import confirm

_ITEM_KIND_ROLE = int(Qt.ItemDataRole.UserRole)
_ITEM_ID_ROLE = _ITEM_KIND_ROLE + 1
_CATEGORY_ROLE = _ITEM_KIND_ROLE + 2


class ReadingRouteTree(QTreeWidget):
    order_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("readingRouteTree")
        self.setHeaderLabels(("項目", "設定", "ID"))
        self.setColumnCount(3)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        modifiers = event.modifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            if event.key() == Qt.Key.Key_Up:
                self._page().move_selected_up()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Down:
                self._page().move_selected_down()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Home:
                self._page().move_selected_first()
                event.accept()
                return
            if event.key() == Qt.Key.Key_End:
                self._page().move_selected_last()
                event.accept()
                return
        super().keyPressEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._is_valid_drop(event):
            super().dragMoveEvent(event)
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if not self._is_valid_drop(event):
            event.ignore()
            return
        before = self._child_order(self.currentItem())
        super().dropEvent(event)
        if self._child_order(self.currentItem()) != before:
            self.order_changed.emit()

    def _page(self) -> ReadingRoutePage:
        parent = self.parentWidget()
        if not isinstance(parent, ReadingRoutePage):
            raise RuntimeError("検針順路ツリーの親画面を取得できません。")
        return parent

    def _dragged_item(self) -> QTreeWidgetItem | None:
        selected = self.selectedItems()
        return selected[0] if len(selected) == 1 else self.currentItem()

    def _is_valid_drop(self, event: QDropEvent | QDragMoveEvent) -> bool:
        source = self._dragged_item()
        if source is None:
            return False
        indicator = self.dropIndicatorPosition()
        target = self.itemAt(event.position().toPoint())
        return self.can_drop_item(source, target, indicator)

    @staticmethod
    def can_drop_item(
        source: QTreeWidgetItem,
        target: QTreeWidgetItem | None,
        indicator: QAbstractItemView.DropIndicatorPosition,
    ) -> bool:
        if indicator is QAbstractItemView.DropIndicatorPosition.OnViewport:
            return source.parent() is None and target is None
        if target is None or indicator is QAbstractItemView.DropIndicatorPosition.OnItem:
            return False
        return source.parent() is target.parent()

    @staticmethod
    def _child_order(item: QTreeWidgetItem | None) -> tuple[str, ...]:
        if item is None:
            return ()
        parent = item.parent()
        result: list[str] = []
        if parent is not None:
            for index in range(parent.childCount()):
                child = parent.child(index)
                if child is None:
                    raise RuntimeError("検針順路の子項目を取得できません。")
                result.append(str(child.data(0, _ITEM_ID_ROLE)))
        else:
            tree = item.treeWidget()
            if tree is None:
                return ()
            for index in range(tree.topLevelItemCount()):
                child = tree.topLevelItem(index)
                if child is None:
                    raise RuntimeError("検針順路の場所項目を取得できません。")
                result.append(str(child.data(0, _ITEM_ID_ROLE)))
        return tuple(result)


class ReadingRoutePage(QWidget):
    def __init__(self, services: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = services.reading_route
        self._baseline: tuple[tuple[object, ...], ...] = ()
        self._has_saved_order = False

        layout = QVBoxLayout(self)
        notice = QLabel(
            "場所、カテゴリ、測定定義を同じ階層内で並べ替えます。\n"
            "保存すると表示中のすべての項目が設定済みになります。\n"
            "キーボードは Ctrl / Alt + ↑・↓・Home・End で移動できます。"
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.tree = ReadingRouteTree(self)
        layout.addWidget(self.tree, 1)
        self.empty_label = QLabel("有効な測定定義がありません。")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        actions = QHBoxLayout()
        self.up_button = QPushButton("上へ")
        self.down_button = QPushButton("下へ")
        self.first_button = QPushButton("先頭へ")
        self.last_button = QPushButton("末尾へ")
        for button in (
            self.up_button,
            self.down_button,
            self.first_button,
            self.last_button,
        ):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

        utility_actions = QHBoxLayout()
        self.reload_button = QPushButton("再読込")
        self.reset_button = QPushButton("既定の順に戻す")
        self.save_button = QPushButton("保存")
        utility_actions.addWidget(self.reload_button)
        utility_actions.addWidget(self.reset_button)
        utility_actions.addStretch()
        utility_actions.addWidget(self.save_button)
        layout.addLayout(utility_actions)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.up_button.clicked.connect(self.move_selected_up)
        self.down_button.clicked.connect(self.move_selected_down)
        self.first_button.clicked.connect(self.move_selected_first)
        self.last_button.clicked.connect(self.move_selected_last)
        self.reload_button.clicked.connect(lambda: run_guarded(self, self._request_reload))
        self.reset_button.clicked.connect(lambda: run_guarded(self, self._request_reset))
        self.save_button.clicked.connect(lambda: run_guarded(self, self._save))
        self.tree.currentItemChanged.connect(lambda *_: self._update_controls())
        self.tree.order_changed.connect(self._order_changed)

        self.reload()

    def reload(self) -> None:
        self._populate(self._service.load_route_editor())

    def has_unsaved_changes(self) -> bool:
        return self._current_order() != self._baseline

    def confirm_discard_changes(self) -> bool:
        return not self.has_unsaved_changes() or confirm(
            self,
            "未保存の検針順路",
            "未保存の検針順路があります。変更を破棄しますか？",
        )

    def move_selected_up(self) -> None:
        self._move_selected(-1)

    def move_selected_down(self) -> None:
        self._move_selected(1)

    def move_selected_first(self) -> None:
        self._move_selected_to(0)

    def move_selected_last(self) -> None:
        item = self.tree.currentItem()
        if item is not None:
            self._move_selected_to(self._sibling_count(item) - 1)

    def _populate(self, editor: ReadingRouteEditor) -> None:
        self.tree.clear()
        for location in editor.locations:
            location_item = self._new_item(
                "location",
                location.location_id,
                f"{location.location_name} [{location.location_id}]",
                location.is_configured,
            )
            self.tree.addTopLevelItem(location_item)
            for category in location.categories:
                category_item = self._new_item(
                    "category",
                    f"{location.location_id}:{category.category.value}",
                    CATEGORY_LABELS[category.category],
                    category.is_configured,
                    category=category.category,
                )
                location_item.addChild(category_item)
                for definition in category.definitions:
                    unit = f" [{definition.unit}]" if definition.unit else ""
                    category_item.addChild(
                        self._new_item(
                            "definition",
                            definition.measurement_definition_id,
                            f"{definition.display_name}{unit}",
                            definition.is_configured,
                        )
                    )
        self.tree.expandAll()
        has_items = self.tree.topLevelItemCount() > 0
        self.tree.setVisible(has_items)
        self.empty_label.setVisible(not has_items)
        if has_items:
            item = self.tree.topLevelItem(0)
            if item is None:
                raise RuntimeError("検針順路の場所項目を取得できません。")
            self.tree.setCurrentItem(item)
        self._has_saved_order = editor.has_saved_order
        self._baseline = self._current_order()
        self.status_label.setText(
            "既定の順です。項目は未設定です。"
            if not self._has_saved_order
            else "保存済みの順です。"
        )
        self._update_controls()

    @staticmethod
    def _new_item(
        kind: str,
        item_id: str,
        label: str,
        is_configured: bool,
        *,
        category: Category | None = None,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem((label, "設定済み" if is_configured else "未設定", item_id))
        item.setData(0, _ITEM_KIND_ROLE, kind)
        item.setData(0, _ITEM_ID_ROLE, item_id)
        if category is not None:
            item.setData(0, _CATEGORY_ROLE, category.value)
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsDropEnabled
        )
        return item

    def _move_selected(self, offset: int) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        self._move_selected_to(self._sibling_index(item) + offset)

    def _move_selected_to(self, index: int) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        current = self._sibling_index(item)
        last = self._sibling_count(item) - 1
        index = max(0, min(index, last))
        if index == current:
            return
        parent = item.parent()
        if parent is None:
            moved = self.tree.takeTopLevelItem(current)
            if moved is None:
                raise RuntimeError("移動する検針順路の場所項目を取得できません。")
            self.tree.insertTopLevelItem(index, moved)
        else:
            moved = parent.takeChild(current)
            if moved is None:
                raise RuntimeError("移動する検針順路の項目を取得できません。")
            parent.insertChild(index, moved)
        self.tree.setCurrentItem(moved)
        self._order_changed()

    def _order_changed(self) -> None:
        message = (
            "未保存の変更があります。"
            if self.has_unsaved_changes()
            else "未保存の変更はありません。"
        )
        self.status_label.setText(message)
        self._update_controls()

    def _update_controls(self) -> None:
        item = self.tree.currentItem()
        position = self._sibling_index(item) if item is not None else -1
        count = self._sibling_count(item) if item is not None else 0
        can_move = item is not None and count > 1
        self.up_button.setEnabled(can_move and position > 0)
        self.down_button.setEnabled(can_move and position < count - 1)
        self.first_button.setEnabled(can_move and position > 0)
        self.last_button.setEnabled(can_move and position < count - 1)
        self.save_button.setEnabled(
            bool(self.tree.topLevelItemCount()) and self.has_unsaved_changes()
        )
        self.reset_button.setEnabled(self._has_saved_order)

    def _request_reload(self) -> None:
        if self.confirm_discard_changes():
            self.reload()

    def _request_reset(self) -> None:
        if not confirm(
            self,
            "検針順路を既定に戻す",
            "保存済みの検針順路を削除し、すべて既定の順に戻します。よろしいですか？",
        ):
            return
        self._service.reset_route()
        self.reload()
        self.status_label.setText("検針順路を既定の順に戻しました。")

    def _save(self) -> None:
        self._service.save_route(self._save_request())
        self.reload()
        self.status_label.setText("検針順路を保存しました。")

    def _save_request(self) -> ReadingRouteSaveRequest:
        locations: list[ReadingRouteSaveLocation] = []
        for location_index in range(self.tree.topLevelItemCount()):
            location_item = self.tree.topLevelItem(location_index)
            if location_item is None:
                raise RuntimeError("検針順路の場所項目を取得できません。")
            categories: list[ReadingRouteSaveCategory] = []
            for category_index in range(location_item.childCount()):
                category_item = location_item.child(category_index)
                if category_item is None:
                    raise RuntimeError("検針順路のカテゴリ項目を取得できません。")
                definition_ids = tuple(
                    self._item_id(category_item.child(definition_index))
                    for definition_index in range(category_item.childCount())
                )
                categories.append(
                    ReadingRouteSaveCategory(
                        Category(str(category_item.data(0, _CATEGORY_ROLE))),
                        definition_ids,
                    )
                )
            locations.append(
                ReadingRouteSaveLocation(
                    str(location_item.data(0, _ITEM_ID_ROLE)), tuple(categories)
                )
            )
        return ReadingRouteSaveRequest(tuple(locations))

    def _current_order(
        self,
    ) -> tuple[tuple[object, ...], ...]:
        result: list[tuple[object, ...]] = []
        for location_index in range(self.tree.topLevelItemCount()):
            location_item = self.tree.topLevelItem(location_index)
            if location_item is None:
                raise RuntimeError("検針順路の場所項目を取得できません。")
            categories: list[tuple[Category, tuple[str, ...]]] = []
            for category_index in range(location_item.childCount()):
                category_item = location_item.child(category_index)
                if category_item is None:
                    raise RuntimeError("検針順路のカテゴリ項目を取得できません。")
                categories.append(
                    (
                        Category(str(category_item.data(0, _CATEGORY_ROLE))),
                        tuple(
                            self._item_id(category_item.child(index))
                            for index in range(category_item.childCount())
                        ),
                    )
                )
            result.append(
                (
                    str(location_item.data(0, _ITEM_ID_ROLE)),
                    tuple(categories),
                )
            )
        return tuple(result)

    @staticmethod
    def _item_id(item: QTreeWidgetItem | None) -> str:
        if item is None:
            raise RuntimeError("検針順路の測定定義項目を取得できません。")
        return str(item.data(0, _ITEM_ID_ROLE))

    @staticmethod
    def _sibling_index(item: QTreeWidgetItem) -> int:
        parent = item.parent()
        tree = item.treeWidget()
        if parent is not None:
            return parent.indexOfChild(item)
        if tree is None:
            return -1
        return tree.indexOfTopLevelItem(item)

    @staticmethod
    def _sibling_count(item: QTreeWidgetItem) -> int:
        parent = item.parent()
        tree = item.treeWidget()
        if parent is not None:
            return parent.childCount()
        if tree is None:
            return 0
        return tree.topLevelItemCount()


__all__ = ["ReadingRoutePage"]
