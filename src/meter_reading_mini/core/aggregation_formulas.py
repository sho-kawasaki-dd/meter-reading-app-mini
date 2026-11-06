"""集計式マスタと適用版をトランザクション単位で管理する。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import date, datetime
from uuid import uuid4

from meter_reading_mini.core.aggregation_contracts import (
    AggregationCycleGroup,
    AggregationFormulaCreateInput,
    AggregationFormulaRecord,
    AggregationFormulaTerm,
    AggregationFormulaUpdateInput,
    AggregationFormulaVersionInput,
    AggregationFormulaVersionRecord,
    parse_aggregation_coefficient,
    serialize_aggregation_coefficient,
    validate_aggregation_term_attributes,
)
from meter_reading_mini.core.contract_models import Category, Purpose
from meter_reading_mini.core.errors import FormulaValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AggregationFormulaRepository,
    MeasurementDefinitionLookupRepository,
)
from meter_reading_mini.core.timeutil import utc_now
from meter_reading_mini.core.timezone_settings import (
    build_business_date_provider,
    normalize_to_utc,
)

logger = logging.getLogger(__name__)
TransactionFactory = Callable[[], AbstractContextManager[object]]


class AggregationFormulaService:
    def __init__(
        self,
        repository: AggregationFormulaRepository,
        definition_repository: MeasurementDefinitionLookupRepository,
        *,
        transaction_factory: TransactionFactory,
        current_business_date_provider: Callable[[], date] | None = None,
        clock: Callable[[], datetime] = utc_now,
        id_provider: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._repository = repository
        self._definition_repository = definition_repository
        self._transaction_factory = transaction_factory
        self._current_business_date_provider = (
            current_business_date_provider or build_business_date_provider()
        )
        self._clock = clock
        self._id_provider = id_provider

    def current_business_date(self) -> date:
        value = self._current_business_date_provider()
        if type(value) is not date:
            raise FormulaValidationError("現在業務日は日付でなければなりません。")
        return value

    def get_formula(self, formula_id: str) -> AggregationFormulaRecord | None:
        return self._repository.get_aggregation_formula(_required(formula_id, "集計式 ID"))

    def list_formulas(self) -> list[AggregationFormulaRecord]:
        return self._repository.list_aggregation_formulas()

    def list_versions(self, formula_id: str) -> list[AggregationFormulaVersionRecord]:
        normalized_id = _required(formula_id, "集計式 ID")
        self._require_formula(normalized_id)
        return self._history(normalized_id)

    def get_version_for_date(
        self, formula_id: str, business_date: date
    ) -> AggregationFormulaVersionRecord | None:
        """指定日の最新履歴を返す。無効版も返すため、適用時は有効状態を確認する。"""

        if type(business_date) is not date:
            raise FormulaValidationError("対象業務日は日付で指定してください。")
        versions = self.list_versions(formula_id)
        return next(
            (
                version
                for version in reversed(versions)
                if version.effective_from_date <= business_date
            ),
            None,
        )

    def create_formula(self, request: AggregationFormulaCreateInput) -> AggregationFormulaRecord:
        formula = _normalize_formula(request, self._id_provider())
        initial = _normalize_version(request.initial_version)
        with self._transaction_factory():
            business_date = self.current_business_date()
            if self._repository.get_aggregation_formula(formula.formula_id) is not None:
                raise FormulaValidationError(
                    f"集計式 ID は既に登録されています: {formula.formula_id}"
                )
            self._validate_terms(formula, initial, retained_ids=set())
            version = self._version_record(
                formula.formula_id, initial, business_date, is_initial=True
            )
            self._repository.create_aggregation_formula(formula)
            self._repository.insert_aggregation_formula_version(version)
            created = self._require_formula(formula.formula_id)
            self._history(formula.formula_id)
        logger.info("集計式を登録しました: %s", formula.formula_id)
        return created

    def update_formula(
        self, formula_id: str, request: AggregationFormulaUpdateInput
    ) -> AggregationFormulaRecord:
        normalized_id = _required(formula_id, "集計式 ID")
        normalized = AggregationFormulaUpdateInput(
            display_name=_required(request.display_name, "集計式名"),
            decimal_places=_decimal_places(request.decimal_places),
        )
        with self._transaction_factory():
            self._require_formula(normalized_id)
            self._repository.update_aggregation_formula(normalized_id, normalized)
            updated = self._require_formula(normalized_id)
        logger.info("集計式の表示設定を更新しました: %s", normalized_id)
        return updated

    def save_version(
        self, formula_id: str, request: AggregationFormulaVersionInput
    ) -> AggregationFormulaVersionRecord:
        """同日の版は置換し、別の日は新しい版として追加する。"""

        return self._save_version(formula_id, request, replace_initial=False)

    def resave_initial_version(
        self, formula_id: str, request: AggregationFormulaVersionInput
    ) -> AggregationFormulaVersionRecord:
        """版が一つだけの場合に、開始日を含め初版を訂正する。"""

        return self._save_version(formula_id, request, replace_initial=True)

    def _save_version(
        self,
        formula_id: str,
        request: AggregationFormulaVersionInput,
        *,
        replace_initial: bool,
    ) -> AggregationFormulaVersionRecord:
        normalized_id = _required(formula_id, "集計式 ID")
        normalized = _normalize_version(request)
        with self._transaction_factory():
            business_date = self.current_business_date()
            formula = self._require_formula(normalized_id)
            history = self._history(normalized_id)
            if replace_initial:
                if len(history) != 1:
                    raise FormulaValidationError("2 版以上の集計式では初版を変更できません。")
                previous = history[0]
            else:
                previous = next(
                    (
                        version
                        for version in history
                        if version.effective_from_date == normalized.effective_from_date
                    ),
                    None,
                )
                if previous is not None and previous.is_initial and len(history) > 1:
                    raise FormulaValidationError("2 版以上の集計式では初版を変更できません。")
                if normalized.effective_from_date < business_date and not (
                    previous is not None and previous.is_initial and len(history) == 1
                ):
                    raise FormulaValidationError("2 版目以降は現在業務日より前に保存できません。")
            retained_ids: set[str] = (
                {term.measurement_definition_id for term in previous.terms}
                if previous is not None
                else set()
            )
            self._validate_terms(formula, normalized, retained_ids=retained_ids)
            version = self._version_record(
                normalized_id,
                normalized,
                business_date,
                is_initial=previous.is_initial if previous is not None else False,
            )
            if previous is None:
                self._repository.insert_aggregation_formula_version(version)
            else:
                self._repository.replace_aggregation_formula_version(
                    version, previous_effective_date=previous.effective_from_date
                )
            stored = next(
                (
                    item
                    for item in self._history(normalized_id)
                    if item.effective_from_date == normalized.effective_from_date
                ),
                None,
            )
            if stored is None:
                raise FormulaValidationError(f"保存した集計式の版を取得できません: {normalized_id}")
        logger.info(
            "集計式の版を保存しました: %s/%s", normalized_id, normalized.effective_from_date
        )
        return stored

    def _require_formula(self, formula_id: str) -> AggregationFormulaRecord:
        formula = self._repository.get_aggregation_formula(formula_id)
        if formula is None:
            raise FormulaValidationError(f"集計式が見つかりません: {formula_id}")
        return formula

    def _history(self, formula_id: str) -> list[AggregationFormulaVersionRecord]:
        history = self._repository.list_aggregation_formula_versions(formula_id)
        if not history or sum(version.is_initial for version in history) != 1:
            raise FormulaValidationError(f"集計式の初版履歴が不正です: {formula_id}")
        for version in history:
            if version.is_active != bool(version.terms):
                raise FormulaValidationError(f"集計式の版と項目が一致しません: {formula_id}")
        return history

    def _validate_terms(
        self,
        formula: AggregationFormulaRecord,
        version: AggregationFormulaVersionInput,
        *,
        retained_ids: set[str],
    ) -> None:
        for term in version.terms:
            definition = self._definition_repository.get_measurement_definition(
                term.measurement_definition_id
            )
            if definition is None:
                raise FormulaValidationError(
                    f"集計式の測定定義が見つかりません: {term.measurement_definition_id}"
                )
            if not definition.is_active and term.measurement_definition_id not in retained_ids:
                raise FormulaValidationError(
                    f"無効な測定定義を新たに使用できません: {term.measurement_definition_id}"
                )
            validate_aggregation_term_attributes(
                formula,
                measurement_definition_id=term.measurement_definition_id,
                calculation_method=definition.calculation_method,
                value_schema=definition.value_schema,
                unit=definition.unit,
                category=definition.category,
                reading_cycle=definition.reading_cycle,
            )

    def _version_record(
        self,
        formula_id: str,
        request: AggregationFormulaVersionInput,
        business_date: date,
        *,
        is_initial: bool,
    ) -> AggregationFormulaVersionRecord:
        return AggregationFormulaVersionRecord(
            formula_id=formula_id,
            effective_from_date=request.effective_from_date,
            is_active=request.is_active,
            is_initial=is_initial,
            created_at=normalize_to_utc(self._clock()),
            registered_business_date=business_date,
            terms=request.terms,
        )


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FormulaValidationError(f"{label}は必須です。")
    return value.strip()


def _decimal_places(value: int | None) -> int | None:
    if value is not None and (type(value) is not int or not 0 <= value <= 10):
        raise FormulaValidationError("表示小数桁数は 0〜10 の整数で指定してください。")
    return value


def _normalize_formula(
    request: AggregationFormulaCreateInput, formula_id: str
) -> AggregationFormulaRecord:
    try:
        category = Category(request.category)
        cycle_group = AggregationCycleGroup(request.cycle_group)
        purpose = Purpose(request.purpose)
    except (TypeError, ValueError) as exc:
        raise FormulaValidationError("集計式のカテゴリ・周期グループ・目的が不正です。") from exc
    return AggregationFormulaRecord(
        formula_id=_required(formula_id, "集計式 ID"),
        display_name=_required(request.display_name, "集計式名"),
        unit=_required(request.unit, "単位"),
        category=category,
        cycle_group=cycle_group,
        purpose=purpose,
        decimal_places=_decimal_places(request.decimal_places),
    )


def _normalize_version(request: AggregationFormulaVersionInput) -> AggregationFormulaVersionInput:
    if type(request.effective_from_date) is not date:
        raise FormulaValidationError("適用開始日は日付で指定してください。")
    if type(request.is_active) is not bool:
        raise FormulaValidationError("有効・無効を指定してください。")
    if request.is_active != bool(request.terms):
        raise FormulaValidationError("有効版には項目が必要で、無効版には項目を指定できません。")
    terms: list[AggregationFormulaTerm] = []
    definition_ids: set[str] = set()
    sort_orders: set[int] = set()
    for term in request.terms:
        definition_id = _required(term.measurement_definition_id, "測定定義 ID")
        if definition_id in definition_ids:
            raise FormulaValidationError("同じ測定定義を一つの版に重複して指定できません。")
        if type(term.sort_order) is not int or term.sort_order < 0:
            raise FormulaValidationError("項目の表示順は 0 以上の整数で指定してください。")
        if term.sort_order in sort_orders:
            raise FormulaValidationError("項目の表示順を重複して指定できません。")
        definition_ids.add(definition_id)
        sort_orders.add(term.sort_order)
        terms.append(
            replace(
                term,
                measurement_definition_id=definition_id,
                coefficient=parse_aggregation_coefficient(
                    serialize_aggregation_coefficient(term.coefficient)
                ),
            )
        )
    return replace(request, terms=tuple(sorted(terms, key=lambda term: term.sort_order)))
