"""検針順路の保存順と未設定項目の既定順を統一して計算する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from meter_reading_mini.core.contract_models import Category
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ReadingRouteCategoryRecord,
    ReadingRouteDefinitionRecord,
    ReadingRouteLocationRecord,
    ReadingRouteTargetRecord,
)

_CATEGORY_ORDER = (
    Category.ELECTRICITY,
    Category.GAS,
    Category.WATER,
    Category.BOILER,
    Category.OTHERS,
)
_CATEGORY_RANK = {category: index for index, category in enumerate(_CATEGORY_ORDER)}


@dataclass(frozen=True, slots=True)
class ReadingRouteOrder:
    location_order: tuple[str, ...]
    category_order: Mapping[str, tuple[Category, ...]]
    definition_order: Mapping[tuple[str, Category], tuple[str, ...]]
    location_ranks: Mapping[str, int]
    definition_ranks: Mapping[str, int]


def calculate_reading_route_order(
    locations: Sequence[ReadingRouteLocationRecord],
    categories: Sequence[ReadingRouteCategoryRecord],
    definitions: Sequence[ReadingRouteDefinitionRecord],
    targets: Sequence[ReadingRouteTargetRecord],
    location_names: Mapping[str, Sequence[str]],
) -> ReadingRouteOrder:
    """対象項目を保存順、未設定項目を業務上の既定順で並べる。"""

    targets_by_id = {target.measurement_definition_id: target for target in targets}
    location_ids = {target.location_id for target in targets}
    saved_location_order = {
        item.location_id: item.sort_order for item in locations if item.location_id in location_ids
    }

    def location_key(location_id: str) -> tuple[str, str]:
        candidates = sorted(location_names.get(location_id, ()))
        return (candidates[0] if candidates else location_id, location_id)

    location_order = tuple(
        sorted(
            location_ids,
            key=lambda location_id: (
                0 if location_id in saved_location_order else 1,
                saved_location_order.get(location_id, 0),
                location_key(location_id),
            ),
        )
    )

    categories_by_location: dict[str, set[Category]] = {}
    definitions_by_group: dict[tuple[str, Category], list[ReadingRouteTargetRecord]] = {}
    for target in targets:
        group = (target.location_id, target.category)
        categories_by_location.setdefault(target.location_id, set()).add(target.category)
        definitions_by_group.setdefault(group, []).append(target)

    saved_categories: dict[tuple[str, Category], int] = {
        (item.location_id, item.category): item.sort_order
        for item in categories
        if item.location_id in categories_by_location
        and item.category in categories_by_location[item.location_id]
    }
    saved_definitions = {
        item.measurement_definition_id: item.sort_order
        for item in definitions
        if item.measurement_definition_id in targets_by_id
    }

    category_order: dict[str, tuple[Category, ...]] = {}
    definition_order: dict[tuple[str, Category], tuple[str, ...]] = {}
    definition_ranks: dict[str, int] = {}
    for location_id in location_order:
        category_order[location_id] = tuple(
            sorted(
                categories_by_location[location_id],
                key=lambda category: (
                    0 if (location_id, category) in saved_categories else 1,
                    saved_categories.get((location_id, category), 0),
                    _CATEGORY_RANK[category],
                ),
            )
        )
        rank = 0
        for category in category_order[location_id]:
            group = (location_id, category)
            saved = sorted(
                (
                    target
                    for target in definitions_by_group[group]
                    if target.measurement_definition_id in saved_definitions
                ),
                key=lambda target: (
                    saved_definitions[target.measurement_definition_id],
                    target.measurement_definition_id,
                ),
            )
            unsaved = sorted(
                (
                    target
                    for target in definitions_by_group[group]
                    if target.measurement_definition_id not in saved_definitions
                ),
                key=lambda target: (target.display_name, target.measurement_definition_id),
            )
            ordered_ids = tuple(target.measurement_definition_id for target in (*saved, *unsaved))
            definition_order[group] = ordered_ids
            for definition_id in ordered_ids:
                definition_ranks[definition_id] = rank
                rank += 1

    return ReadingRouteOrder(
        location_order=location_order,
        category_order=category_order,
        definition_order=definition_order,
        location_ranks={location_id: rank for rank, location_id in enumerate(location_order)},
        definition_ranks=definition_ranks,
    )


__all__ = ["ReadingRouteOrder", "calculate_reading_route_order"]
