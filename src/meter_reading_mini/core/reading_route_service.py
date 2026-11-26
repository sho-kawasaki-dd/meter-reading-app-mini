"""検針順路の編集・保存を担う service。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass

from meter_reading_mini.core.contract_models import Category
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ReadingRouteCategoryRecord,
    ReadingRouteDefinitionRecord,
    ReadingRouteLocationRecord,
    ReadingRouteRepository,
    ReadingRouteTargetRecord,
)
from meter_reading_mini.core.reading_route import calculate_reading_route_order

TransactionFactory = Callable[[], AbstractContextManager[object]]


@dataclass(frozen=True, slots=True)
class ReadingRouteEditorDefinition:
    measurement_definition_id: str
    display_name: str
    unit: str
    is_configured: bool


@dataclass(frozen=True, slots=True)
class ReadingRouteEditorCategory:
    category: Category
    is_configured: bool
    definitions: tuple[ReadingRouteEditorDefinition, ...]


@dataclass(frozen=True, slots=True)
class ReadingRouteEditorLocation:
    location_id: str
    location_name: str
    location_name_candidates: tuple[str, ...]
    is_configured: bool
    categories: tuple[ReadingRouteEditorCategory, ...]


@dataclass(frozen=True, slots=True)
class ReadingRouteEditor:
    locations: tuple[ReadingRouteEditorLocation, ...]
    has_saved_order: bool


@dataclass(frozen=True, slots=True)
class ReadingRouteSaveCategory:
    category: Category
    measurement_definition_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReadingRouteSaveLocation:
    location_id: str
    categories: tuple[ReadingRouteSaveCategory, ...]


@dataclass(frozen=True, slots=True)
class ReadingRouteSaveRequest:
    locations: tuple[ReadingRouteSaveLocation, ...]


class ReadingRouteService:
    """検針順路の順位計算結果を編集用に返し、全階層を原子的に保存する。"""

    def __init__(
        self,
        repository: ReadingRouteRepository,
        *,
        transaction_factory: TransactionFactory,
    ) -> None:
        self._repository = repository
        self._transaction_factory = transaction_factory

    def load_route_editor(self) -> ReadingRouteEditor:
        locations = self._repository.list_reading_route_locations()
        categories = self._repository.list_reading_route_categories()
        definitions = self._repository.list_reading_route_definitions()
        targets = self._repository.list_reading_route_targets()
        active_targets = [target for target in targets if target.is_active]
        location_names = self._repository.list_reading_route_location_names()
        order = calculate_reading_route_order(
            locations, categories, definitions, active_targets, location_names
        )
        configured_locations = {item.location_id for item in locations}
        configured_categories = {(item.location_id, item.category) for item in categories}
        configured_definitions = {item.measurement_definition_id for item in definitions}
        targets_by_id = {target.measurement_definition_id: target for target in active_targets}

        editor_locations: list[ReadingRouteEditorLocation] = []
        for location_id in order.location_order:
            candidates = tuple(sorted(location_names.get(location_id, ())))
            categories_for_location: list[ReadingRouteEditorCategory] = []
            for category in order.category_order[location_id]:
                definitions_for_category = tuple(
                    ReadingRouteEditorDefinition(
                        measurement_definition_id=definition_id,
                        display_name=targets_by_id[definition_id].display_name,
                        unit=targets_by_id[definition_id].unit,
                        is_configured=definition_id in configured_definitions,
                    )
                    for definition_id in order.definition_order[(location_id, category)]
                )
                categories_for_location.append(
                    ReadingRouteEditorCategory(
                        category=category,
                        is_configured=(location_id, category) in configured_categories,
                        definitions=definitions_for_category,
                    )
                )
            editor_locations.append(
                ReadingRouteEditorLocation(
                    location_id=location_id,
                    location_name=" / ".join(candidates) if candidates else location_id,
                    location_name_candidates=candidates,
                    is_configured=location_id in configured_locations,
                    categories=tuple(categories_for_location),
                )
            )
        return ReadingRouteEditor(
            locations=tuple(editor_locations),
            has_saved_order=bool(locations or categories or definitions),
        )

    def save_route(self, request: ReadingRouteSaveRequest) -> None:
        with self._transaction_factory():
            targets = self._repository.list_reading_route_targets()
            active_targets = [target for target in targets if target.is_active]
            self._validate_request(request, active_targets)
            self._replace_with_visible_and_hidden_orders(request, targets)

    def reset_route(self) -> None:
        with self._transaction_factory():
            self._repository.replace_reading_route((), (), ())

    @staticmethod
    def _validate_request(
        request: ReadingRouteSaveRequest,
        targets: Sequence[ReadingRouteTargetRecord],
    ) -> None:
        targets_by_id = {target.measurement_definition_id: target for target in targets}
        expected_categories: dict[str, set[Category]] = {}
        expected_definitions: dict[tuple[str, Category], set[str]] = {}
        for target in targets:
            expected_categories.setdefault(target.location_id, set()).add(target.category)
            expected_definitions.setdefault((target.location_id, target.category), set()).add(
                target.measurement_definition_id
            )

        location_ids = [location.location_id for location in request.locations]
        if len(location_ids) != len(set(location_ids)) or set(location_ids) != set(
            expected_categories
        ):
            raise ValidationError(
                "検針順路の場所が最新の測定定義と一致しません。再読込してください。"
            )

        actual_categories: dict[str, list[Category]] = {}
        actual_definitions: dict[tuple[str, Category], list[str]] = {}
        for location in request.locations:
            category_ids = [item.category for item in location.categories]
            if len(category_ids) != len(set(category_ids)):
                raise ValidationError("検針順路のカテゴリが重複しています。")
            actual_categories[location.location_id] = category_ids
            for category in location.categories:
                definition_ids = list(category.measurement_definition_ids)
                if len(definition_ids) != len(set(definition_ids)):
                    raise ValidationError("検針順路の測定定義が重複しています。")
                actual_definitions[(location.location_id, category.category)] = definition_ids

        if {
            location_id: set(categories) for location_id, categories in actual_categories.items()
        } != expected_categories:
            raise ValidationError(
                "検針順路のカテゴリが最新の測定定義と一致しません。再読込してください。"
            )
        for key, expected_ids in expected_definitions.items():
            requested_ids = actual_definitions.get(key, [])
            if len(requested_ids) != len(expected_ids) or set(requested_ids) != expected_ids:
                raise ValidationError(
                    "検針順路の測定定義が最新の状態と一致しません。再読込してください。"
                )
        for (location_id, category), definition_ids in actual_definitions.items():
            for definition_id in definition_ids:
                target = targets_by_id.get(definition_id)
                if (
                    target is None
                    or target.location_id != location_id
                    or target.category is not category
                ):
                    raise ValidationError("測定定義は現在の場所・カテゴリ内に配置してください。")

    def _replace_with_visible_and_hidden_orders(
        self,
        request: ReadingRouteSaveRequest,
        all_targets: Sequence[ReadingRouteTargetRecord],
    ) -> None:
        old_locations = self._repository.list_reading_route_locations()
        old_categories = self._repository.list_reading_route_categories()
        old_definitions = self._repository.list_reading_route_definitions()

        visible_locations = [location.location_id for location in request.locations]
        visible_location_set = set(visible_locations)
        location_order = visible_locations + [
            item.location_id
            for item in sorted(old_locations, key=lambda row: (row.sort_order, row.location_id))
            if item.location_id not in visible_location_set
        ]
        location_records = [
            ReadingRouteLocationRecord(location_id, index)
            for index, location_id in enumerate(location_order)
        ]

        requested_categories = {
            location.location_id: [category.category for category in location.categories]
            for location in request.locations
        }
        old_categories_by_location: dict[str, list[ReadingRouteCategoryRecord]] = {}
        for item in old_categories:
            old_categories_by_location.setdefault(item.location_id, []).append(item)
        category_records: list[ReadingRouteCategoryRecord] = []
        for location_id in dict.fromkeys(
            [*visible_locations, *(item.location_id for item in old_categories)]
        ):
            shown = requested_categories.get(location_id, [])
            shown_set = set(shown)
            hidden = sorted(
                (
                    item
                    for item in old_categories_by_location.get(location_id, [])
                    if item.category not in shown_set
                ),
                key=lambda item: (item.sort_order, item.category.value),
            )
            for index, category in enumerate([*shown, *(item.category for item in hidden)]):
                category_records.append(ReadingRouteCategoryRecord(location_id, category, index))

        targets_by_id = {item.measurement_definition_id: item for item in all_targets}
        requested_definitions: dict[tuple[str, Category], list[str]] = {}
        for location in request.locations:
            for category in location.categories:
                requested_definitions[(location.location_id, category.category)] = list(
                    category.measurement_definition_ids
                )
        old_definitions_by_group: dict[
            tuple[str, Category], list[ReadingRouteDefinitionRecord]
        ] = {}
        for item in old_definitions:
            target = targets_by_id.get(item.measurement_definition_id)
            if target is None:
                raise ValidationError(
                    f"順序が保存された測定定義が見つかりません: {item.measurement_definition_id}"
                )
            old_definitions_by_group.setdefault((target.location_id, target.category), []).append(
                item
            )

        definition_records: list[ReadingRouteDefinitionRecord] = []
        groups = dict.fromkeys([*requested_definitions, *old_definitions_by_group])
        for group in groups:
            shown = requested_definitions.get(group, [])
            shown_set = set(shown)
            hidden = sorted(
                (
                    item
                    for item in old_definitions_by_group.get(group, [])
                    if item.measurement_definition_id not in shown_set
                ),
                key=lambda item: (item.sort_order, item.measurement_definition_id),
            )
            for index, definition_id in enumerate(
                [*shown, *(item.measurement_definition_id for item in hidden)]
            ):
                definition_records.append(ReadingRouteDefinitionRecord(definition_id, index))

        self._repository.replace_reading_route(
            location_records, category_records, definition_records
        )


__all__ = [
    "ReadingRouteEditor",
    "ReadingRouteEditorCategory",
    "ReadingRouteEditorDefinition",
    "ReadingRouteEditorLocation",
    "ReadingRouteSaveCategory",
    "ReadingRouteSaveLocation",
    "ReadingRouteSaveRequest",
    "ReadingRouteService",
]
