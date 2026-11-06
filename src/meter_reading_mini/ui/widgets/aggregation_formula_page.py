"""集計式の表示設定と業務日単位の版を管理する。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from PySide6.QtCore import QDate, QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.aggregation_contracts import (
    AggregationCycleGroup,
    AggregationFormulaCreateInput,
    AggregationFormulaTerm,
    AggregationFormulaUpdateInput,
    AggregationFormulaVersionInput,
    AggregationFormulaVersionRecord,
    parse_aggregation_coefficient,
    resolve_aggregation_cycle_group,
)
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ValueSchema,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import MeasurementDefinitionRecord
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.models.reading_overview_model import CYCLE_GROUP_LABELS, PURPOSE_LABELS
from meter_reading_mini.ui.widgets.form_helpers import (
    python_date,
    qdate,
    select_value,
    selected_value,
)
from meter_reading_mini.ui.widgets.master_common import (
    category_combo,
    confirm,
    make_table,
    populate_table,
    selected_id,
)

_MetadataDraft = tuple[str, str, int, int, int, int]
_VersionDraft = tuple[date, bool, tuple[tuple[str, str], ...]]


class AggregationFormulaPage(QWidget):
    saved = Signal()

    def __init__(self, services: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._services = services
        self._editing_id: str | None = None
        self._history: list[AggregationFormulaVersionRecord] = []
        self._version_mode = "new"
        self._definitions: dict[str, MeasurementDefinitionRecord] = {}
        self._locations: dict[str, str] = {}
        self._selected_history: str | None = None
        self._metadata_baseline: _MetadataDraft | None = None
        self._version_baseline: _VersionDraft | None = None

        layout = QVBoxLayout(self)
        self.table = make_table(("集計式", "単位", "目的", "現在の状態", "ID"))
        self.table.setObjectName("aggregationFormulaTable")
        layout.addWidget(self.table)
        self.new_button = QPushButton("新規集計式")
        layout.addWidget(self.new_button)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.unit_edit = QLineEdit()
        self.category_combo = category_combo()
        self.cycle_combo = QComboBox()
        for group in AggregationCycleGroup:
            self.cycle_combo.addItem(CYCLE_GROUP_LABELS[group.value], group.value)
        self.purpose_combo = QComboBox()
        for purpose in Purpose:
            self.purpose_combo.addItem(PURPOSE_LABELS[purpose], purpose.value)
        self.places_combo = QComboBox()
        self.places_combo.addItem("指定なし（無丸め）", "")
        for places in range(11):
            self.places_combo.addItem(f"{places} 桁", str(places))
        for label, widget in (
            ("集計式名", self.name_edit),
            ("単位", self.unit_edit),
            ("カテゴリ", self.category_combo),
            ("周期グループ", self.cycle_combo),
            ("式の目的", self.purpose_combo),
            ("表示小数桁数", self.places_combo),
        ):
            form.addRow(label, widget)
        self.metadata_notice = QLabel(
            "作成後は単位・カテゴリ・周期グループ・目的を変更できません。"
            "名称と表示桁数は過去の表示にも適用されます。"
        )
        self.metadata_notice.setWordWrap(True)
        form.addRow(self.metadata_notice)
        self.save_formula_button = QPushButton("集計式と初版を登録")
        form.addRow(self.save_formula_button)
        layout.addLayout(form)

        layout.addWidget(QLabel("版履歴"))
        self.history_table = make_table(("適用開始業務日", "状態", "初版", "後付け", "登録業務日"))
        self.history_table.setObjectName("aggregationFormulaHistory")
        layout.addWidget(self.history_table)
        self.new_version_button = QPushButton("この項目を使って新しい版を追加")
        layout.addWidget(self.new_version_button)

        version_form = QFormLayout()
        self.effective_date = QDateEdit()
        self.effective_date.setCalendarPopup(True)
        self.effective_date.setDisplayFormat("yyyy/MM/dd")
        self.effective_date.setDateRange(QDate(1, 1, 1), QDate(9999, 12, 31))
        self.active_check = QCheckBox("この版を有効にする")
        self.active_check.setChecked(True)
        version_form.addRow("適用開始業務日", self.effective_date)
        version_form.addRow("状態", self.active_check)
        layout.addLayout(version_form)
        self.version_notice = QLabel()
        self.version_notice.setWordWrap(True)
        layout.addWidget(self.version_notice)
        condition = QLabel(
            "項目は差分計算の数値で、式と単位・カテゴリ・周期グループが一致する"
            "測定定義です。係数には 0 以外の通常の10進数（負数可）を入力します。"
            "無効版には項目を保存しません。"
        )
        condition.setWordWrap(True)
        layout.addWidget(condition)
        self.terms_table = QTableWidget(0, 2)
        self.terms_table.setObjectName("aggregationFormulaTerms")
        self.terms_table.setHorizontalHeaderLabels(["測定定義・場所", "係数"])
        self.terms_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.terms_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.terms_table.setMinimumHeight(150)
        layout.addWidget(self.terms_table)
        term_actions = QHBoxLayout()
        self.add_term_button = QPushButton("項目を追加")
        self.remove_term_button = QPushButton("選択項目を削除")
        term_actions.addWidget(self.add_term_button)
        term_actions.addWidget(self.remove_term_button)
        term_actions.addStretch()
        layout.addLayout(term_actions)
        self.save_version_button = QPushButton("版を保存")
        layout.addWidget(self.save_version_button)

        self.new_button.clicked.connect(lambda: run_guarded(self, self._request_new_formula))
        self.table.currentCellChanged.connect(self._selection_changed)
        self.history_table.currentCellChanged.connect(self._history_selection_changed)
        self.save_formula_button.clicked.connect(lambda: run_guarded(self, self._save_formula))
        self.new_version_button.clicked.connect(lambda: run_guarded(self, self._new_version))
        self.save_version_button.clicked.connect(lambda: run_guarded(self, self._save_version))
        self.add_term_button.clicked.connect(lambda: self._add_term())
        self.remove_term_button.clicked.connect(self._remove_term)
        self.active_check.toggled.connect(self._active_changed)
        self.unit_edit.textChanged.connect(self._refresh_term_choices)
        self.category_combo.currentIndexChanged.connect(self._refresh_term_choices)
        self.cycle_combo.currentIndexChanged.connect(self._refresh_term_choices)

        self.refresh_definitions()
        self.reload()
        self._new_formula()

    def _metadata_draft(self) -> _MetadataDraft:
        return (
            self.name_edit.text(),
            self.unit_edit.text(),
            self.category_combo.currentIndex(),
            self.cycle_combo.currentIndex(),
            self.purpose_combo.currentIndex(),
            self.places_combo.currentIndex(),
        )

    def _version_draft(self) -> _VersionDraft:
        terms: list[tuple[str, str]] = []
        for row in range(self.terms_table.rowCount()):
            combo = self.terms_table.cellWidget(row, 0)
            coefficient = self.terms_table.cellWidget(row, 1)
            if not isinstance(combo, QComboBox) or not isinstance(coefficient, QLineEdit):
                raise ValidationError("式項目の入力欄を取得できません。")
            definition_id = combo.currentData()
            if not isinstance(definition_id, str):
                raise ValidationError("式項目の測定定義を取得できません。")
            terms.append((definition_id, coefficient.text()))
        return python_date(self.effective_date.date()), self.active_check.isChecked(), tuple(terms)

    def _version_changed(self) -> bool:
        return (
            self._version_baseline is not None and self._version_draft() != self._version_baseline
        )

    def confirm_discard_changes(self) -> bool:
        if self._metadata_baseline == self._metadata_draft() and not self._version_changed():
            return True
        return confirm(
            self,
            "編集中の集計式",
            "未保存の集計式・版の入力があります。変更を破棄しますか？",
        )

    @staticmethod
    def _restore_table_selection(table: QTableWidget, key: str | None) -> None:
        with QSignalBlocker(table):
            if key is None:
                table.clearSelection()
                table.setCurrentCell(-1, -1)
                return
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                    table.selectRow(row)
                    return
        raise ValidationError(f"選択中の集計式または版が見つかりません: {key}")

    def refresh_definitions(self) -> None:
        self._definitions = {
            definition.measurement_definition_id: definition
            for definition in self._services.measurement_definitions.list_measurement_definitions(
                show_inactive=True
            )
        }
        self._locations = {
            location.location_id: " / ".join(location.location_names) or location.location_name
            for location in self._services.meter_master.list_locations()
        }
        self._refresh_term_choices()

    def reload(self) -> None:
        today = self._services.aggregation_formulas.current_business_date()
        rows: list[tuple[str, tuple[str, ...]]] = []
        for formula in self._services.aggregation_formulas.list_formulas():
            version = self._services.aggregation_formulas.get_version_for_date(
                formula.formula_id, today
            )
            state = "未適用" if version is None else "有効" if version.is_active else "無効"
            rows.append(
                (
                    formula.formula_id,
                    (
                        formula.display_name,
                        formula.unit,
                        PURPOSE_LABELS[formula.purpose],
                        state,
                        formula.formula_id,
                    ),
                )
            )
        found = populate_table(self.table, rows, self._editing_id)
        if self._editing_id is not None and not found:
            self._new_formula()

    def _set_immutable(self, immutable: bool) -> None:
        for widget in (self.unit_edit, self.category_combo, self.cycle_combo, self.purpose_combo):
            widget.setEnabled(not immutable)

    def _new_formula(self) -> None:
        self._editing_id = None
        self._history = []
        self._selected_history = None
        self._version_mode = "new"
        with QSignalBlocker(self.table), QSignalBlocker(self.history_table):
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.history_table.setRowCount(0)
        self._set_immutable(False)
        self.name_edit.clear()
        self.unit_edit.clear()
        self.category_combo.setCurrentIndex(0)
        self.cycle_combo.setCurrentIndex(0)
        self.purpose_combo.setCurrentIndex(0)
        self.places_combo.setCurrentIndex(0)
        self.effective_date.setDate(
            qdate(self._services.aggregation_formulas.current_business_date())
        )
        self.active_check.setChecked(True)
        self.terms_table.setRowCount(0)
        self.save_formula_button.setText("集計式と初版を登録")
        self.new_version_button.setEnabled(False)
        self.save_version_button.setEnabled(False)
        self.version_notice.setText(
            "初版は過去日から登録できます。過去への適用は後付け設定による再集計です。"
        )
        self._active_changed()
        self._metadata_baseline = self._metadata_draft()
        self._version_baseline = self._version_draft()

    def _request_new_formula(self) -> None:
        if self.confirm_discard_changes():
            self._new_formula()

    def _selection_changed(self, *_args: object) -> None:
        formula_id = selected_id(self.table)
        if formula_id is None or formula_id == self._editing_id:
            return
        succeeded, accepted = run_guarded(self, self.confirm_discard_changes)
        if not succeeded or not accepted:
            run_guarded(self, lambda: self._restore_table_selection(self.table, self._editing_id))
            return
        succeeded, _ = run_guarded(self, lambda: self._load_formula(formula_id))
        if not succeeded:
            self._new_formula()

    def _load_formula(self, formula_id: str, *, selected_date: date | None = None) -> None:
        formula = self._services.aggregation_formulas.get_formula(formula_id)
        if formula is None:
            raise ValidationError(f"集計式が見つかりません: {formula_id}")
        history = self._services.aggregation_formulas.list_versions(formula_id)
        self._editing_id = formula_id
        self._history = history
        with (
            QSignalBlocker(self.unit_edit),
            QSignalBlocker(self.category_combo),
            QSignalBlocker(self.cycle_combo),
            QSignalBlocker(self.purpose_combo),
        ):
            self.name_edit.setText(formula.display_name)
            self.unit_edit.setText(formula.unit)
            select_value(self.category_combo, formula.category.value, "カテゴリ")
            select_value(self.cycle_combo, formula.cycle_group.value, "周期グループ")
            select_value(self.purpose_combo, formula.purpose.value, "目的")
            select_value(
                self.places_combo,
                "" if formula.decimal_places is None else str(formula.decimal_places),
                "表示桁数",
            )
        self._set_immutable(True)
        self.save_formula_button.setText("名称・表示桁数を保存")
        self.new_version_button.setEnabled(True)
        rows = [
            (
                version.effective_from_date.isoformat(),
                (
                    version.effective_from_date.isoformat(),
                    "有効" if version.is_active else "無効",
                    "初版" if version.is_initial else "",
                    "後付け" if version.is_retroactive else "",
                    version.registered_business_date.isoformat(),
                ),
            )
            for version in history
        ]
        selected = (
            history[-1]
            if selected_date is None
            else next((item for item in history if item.effective_from_date == selected_date), None)
        )
        if selected is None:
            raise ValidationError(f"保存した集計式の版を表示できません: {selected_date}")
        populate_table(self.history_table, rows, selected.effective_from_date.isoformat())
        self._load_version(selected)
        self._metadata_baseline = self._metadata_draft()

    def _history_selection_changed(self, *_args: object) -> None:
        key = selected_id(self.history_table)
        if key is None or key == self._selected_history:
            return

        def change_version() -> None:
            if self._version_changed() and not confirm(
                self, "編集中の版", "未保存の版の入力を破棄して別の版を表示しますか？"
            ):
                self._restore_table_selection(self.history_table, self._selected_history)
                return
            version = next(
                (item for item in self._history if item.effective_from_date.isoformat() == key),
                None,
            )
            if version is None:
                raise ValidationError(f"選択した集計式の版が見つかりません: {key}")
            self._load_version(version)

        succeeded, _ = run_guarded(self, change_version)
        if not succeeded:
            run_guarded(
                self,
                lambda: self._restore_table_selection(self.history_table, self._selected_history),
            )

    def _load_version(self, version: AggregationFormulaVersionRecord) -> None:
        self._version_mode = (
            "initial" if version.is_initial and len(self._history) == 1 else "existing"
        )
        self.effective_date.setDate(qdate(version.effective_from_date))
        self.active_check.setChecked(version.is_active)
        self.terms_table.setRowCount(0)
        for term in version.terms:
            self._add_term(term.measurement_definition_id, term.coefficient)
        locked = version.is_initial and len(self._history) > 1
        self.save_version_button.setEnabled(not locked)
        self.version_notice.setText(
            "2 版以上の初版は変更できません。誤りは新しい式を作り、旧式を無効化してください。"
            if locked
            else "同じ開始日は再保存、異なる開始日は新しい版として追加します。"
            "現在業務日の再保存は同日の結果にも影響します。"
        )
        self._active_changed()
        self._selected_history = version.effective_from_date.isoformat()
        self._version_baseline = self._version_draft()

    def _new_version(self) -> None:
        if self._editing_id is None:
            raise ValidationError("集計式を先に登録してください。")
        if self._version_changed() and not confirm(
            self,
            "編集中の版",
            "新しい版に項目の入力を引き継ぎますが、適用開始日は初期化します。続行しますか？",
        ):
            return
        self._version_mode = "new"
        self._selected_history = None
        with QSignalBlocker(self.history_table):
            self.history_table.clearSelection()
            self.history_table.setCurrentCell(-1, -1)
        self.effective_date.setDate(
            qdate(self._services.aggregation_formulas.current_business_date())
        )
        self.save_version_button.setEnabled(True)
        self.version_notice.setText(
            "新しい版を追加します。同じ開始日がある場合はその版を再保存します。"
            "無効な測定定義は新しい版に追加できません。"
        )

    def _active_changed(self, *_args: object) -> None:
        active = self.active_check.isChecked()
        self.terms_table.setEnabled(active)
        self.add_term_button.setEnabled(active)
        self.remove_term_button.setEnabled(active)

    def _matches_formula(self, definition: MeasurementDefinitionRecord) -> bool:
        if (
            definition.value_schema is not ValueSchema.DECIMAL_STRING
            or definition.calculation_method is not CalculationMethod.DELTA
            or definition.unit != self.unit_edit.text().strip()
            or definition.category is None
            or definition.category.value != self.category_combo.currentData()
            or definition.reading_cycle is None
        ):
            return False
        return (
            resolve_aggregation_cycle_group(definition.reading_cycle).value
            == self.cycle_combo.currentData()
        )

    def _definition_label(self, definition: MeasurementDefinitionRecord) -> str:
        location = self._locations.get(definition.location_id or "", definition.location_id or "")
        state = "（現在無効）" if not definition.is_active else ""
        name = definition.display_name or definition.measurement_definition_id
        return f"{name} / {location}{state} [{definition.measurement_definition_id}]"

    def _populate_term_combo(self, combo: QComboBox, selected: str) -> None:
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItem("測定定義を選択", "")
            for definition in sorted(
                self._definitions.values(),
                key=lambda item: (item.display_name or "", item.measurement_definition_id),
            ):
                if (definition.is_active and self._matches_formula(definition)) or (
                    definition.measurement_definition_id == selected
                ):
                    label = self._definition_label(definition)
                    if not self._matches_formula(definition):
                        label += "（式の条件に不一致）"
                    combo.addItem(label, definition.measurement_definition_id)
            if selected and combo.findData(selected) < 0:
                combo.addItem(f"見つからない測定定義 [{selected}]", selected)
            combo.setCurrentIndex(max(combo.findData(selected), 0))

    def _refresh_term_choices(self, *_args: object) -> None:
        for row in range(self.terms_table.rowCount()):
            combo = self.terms_table.cellWidget(row, 0)
            if isinstance(combo, QComboBox):
                current = combo.currentData()
                self._populate_term_combo(combo, current if isinstance(current, str) else "")

    def _add_term(self, definition_id: str = "", coefficient: Decimal | None = None) -> None:
        row = self.terms_table.rowCount()
        self.terms_table.insertRow(row)
        combo = QComboBox()
        self._populate_term_combo(combo, definition_id)
        coefficient_edit = QLineEdit("" if coefficient is None else format(coefficient, "f"))
        coefficient_edit.setPlaceholderText("例: -1.25")
        self.terms_table.setCellWidget(row, 0, combo)
        self.terms_table.setCellWidget(row, 1, coefficient_edit)

    def _remove_term(self) -> None:
        row = self.terms_table.currentRow()
        if row >= 0:
            self.terms_table.removeRow(row)

    def _version_input(self) -> AggregationFormulaVersionInput:
        terms: list[AggregationFormulaTerm] = []
        if self.active_check.isChecked():
            for row in range(self.terms_table.rowCount()):
                combo = self.terms_table.cellWidget(row, 0)
                coefficient = self.terms_table.cellWidget(row, 1)
                if not isinstance(combo, QComboBox) or not isinstance(coefficient, QLineEdit):
                    raise ValidationError("式項目の入力欄を取得できません。")
                terms.append(
                    AggregationFormulaTerm(
                        selected_value(combo, "測定定義"),
                        parse_aggregation_coefficient(coefficient.text().strip()),
                        row,
                    )
                )
        return AggregationFormulaVersionInput(
            python_date(self.effective_date.date()), self.active_check.isChecked(), tuple(terms)
        )

    def _places(self) -> int | None:
        value = self.places_combo.currentData()
        if not isinstance(value, str):
            raise ValidationError("表示小数桁数を選択してください。")
        return int(value) if value else None

    def _terms_description(self, terms: tuple[AggregationFormulaTerm, ...]) -> str:
        if not terms:
            return "項目なし"
        return "\n".join(
            f"  {format(term.coefficient, 'f')} × "
            f"{self._definition_label(self._definitions[term.measurement_definition_id])}"
            if term.measurement_definition_id in self._definitions
            else f"  {format(term.coefficient, 'f')} × {term.measurement_definition_id}"
            for term in terms
        )

    def _confirm_version(self, version: AggregationFormulaVersionInput, today: date) -> bool:
        history = (
            "\n".join(
                f"- {item.effective_from_date}: {'有効' if item.is_active else '無効'}"
                f"{' / 初版' if item.is_initial else ''}"
                f"{' / 後付け' if item.is_retroactive else ''}\n"
                f"{self._terms_description(item.terms)}"
                for item in self._history
            )
            or "まだありません"
        )
        retroactive = self._editing_id is None or self._version_mode == "initial"
        notice = (
            "\n過去の初版は後から設定した式による再集計であり、"
            "当時その式が存在した証明ではありません。"
            if retroactive and version.effective_from_date < today
            else ""
        )
        return confirm(
            self,
            "集計式の適用を確認",
            f"判定する現在業務日: {today}\n"
            f"適用開始業務日: {version.effective_from_date}\n"
            f"保存する状態: {'有効' if version.is_active else '無効'}\n"
            f"保存後の項目:\n{self._terms_description(version.terms)}\n\n"
            f"変更前の版履歴:\n{history}\n\n"
            "同日の再保存は同日の結果にも影響します。将来版は削除・変更しません。"
            "無効版の期間は式を適用しません。"
            f"{notice}\n保存しますか？",
        )

    def _save_formula(self) -> None:
        service = self._services.aggregation_formulas
        editing_id = self._editing_id
        creating = editing_id is None
        if editing_id is None:
            version = self._version_input()
            today = service.current_business_date()
            if not self._confirm_version(version, today):
                return
            if service.current_business_date() != today:
                raise ValidationError("現在業務日が変わりました。内容を再確認してください。")
            formula = service.create_formula(
                AggregationFormulaCreateInput(
                    self.name_edit.text(),
                    self.unit_edit.text(),
                    Category(selected_value(self.category_combo, "カテゴリ")),
                    AggregationCycleGroup(selected_value(self.cycle_combo, "周期グループ")),
                    Purpose(selected_value(self.purpose_combo, "目的")),
                    self._places(),
                    version,
                )
            )
        else:
            formula = service.update_formula(
                editing_id, AggregationFormulaUpdateInput(self.name_edit.text(), self._places())
            )
        self._editing_id = formula.formula_id
        self.saved.emit()
        self.reload()
        if creating:
            self._load_formula(formula.formula_id)
        else:
            self._metadata_baseline = self._metadata_draft()

    def _save_version(self) -> None:
        if self._editing_id is None:
            raise ValidationError("集計式を先に登録してください。")
        service = self._services.aggregation_formulas
        current = service.get_formula(self._editing_id)
        if current is None:
            raise ValidationError("選択した集計式が見つかりません。")
        if (
            current.display_name != self.name_edit.text().strip()
            or current.decimal_places != self._places()
        ):
            raise ValidationError("名称・表示桁数の変更を先に保存してください。")
        version = self._version_input()
        today = service.current_business_date()
        if not self._confirm_version(version, today):
            return
        if service.current_business_date() != today:
            raise ValidationError("現在業務日が変わりました。内容を再確認してください。")
        if self._version_mode == "initial":
            service.resave_initial_version(self._editing_id, version)
        else:
            service.save_version(self._editing_id, version)
        formula_id = self._editing_id
        self.saved.emit()
        self.reload()
        self._load_formula(formula_id, selected_date=version.effective_from_date)
