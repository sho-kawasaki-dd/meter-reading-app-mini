from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from functools import partial

import pytest

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ValueSchema,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.measurement_definition_service import MeasurementDefinitionService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
    MeasurementDefinitionUpdateRequest,
    ReadingRouteCategoryRecord,
    ReadingRouteDefinitionRecord,
    ReadingRouteLocationRecord,
    ReadingRouteTargetRecord,
)
from meter_reading_mini.core.reading_route import calculate_reading_route_order
from meter_reading_mini.core.reading_route_service import (
    ReadingRouteSaveCategory,
    ReadingRouteSaveLocation,
    ReadingRouteSaveRequest,
    ReadingRouteService,
)
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.db.repositories import SQLiteRepository


def _target(
    definition_id: str,
    location_id: str,
    category: Category,
    display_name: str,
) -> ReadingRouteTargetRecord:
    return ReadingRouteTargetRecord(
        measurement_definition_id=definition_id,
        location_id=location_id,
        category=category,
        display_name=display_name,
        unit="試験単位",
        is_active=True,
    )


def test_default_order_uses_location_category_and_definition_tie_breakers() -> None:
    targets = [
        _target("same-z", "loc-a", Category.ELECTRICITY, "同名"),
        _target("same-a", "loc-a", Category.ELECTRICITY, "同名"),
        _target("boiler", "loc-a", Category.BOILER, "ボイラー"),
        _target("others", "loc-a", Category.OTHERS, "その他"),
        _target("water", "loc-b", Category.WATER, "水道"),
    ]

    order = calculate_reading_route_order(
        [],
        [],
        [],
        targets,
        {"loc-a": ("A棟",), "loc-b": ()},
    )

    assert order.location_order == ("loc-a", "loc-b")
    assert order.category_order["loc-a"] == (
        Category.ELECTRICITY,
        Category.BOILER,
        Category.OTHERS,
    )
    assert order.definition_order[("loc-a", Category.ELECTRICITY)] == ("same-a", "same-z")
    assert order.definition_ranks == {
        "same-a": 0,
        "same-z": 1,
        "boiler": 2,
        "others": 3,
        "water": 0,
    }


def test_saved_order_precedes_unsaved_defaults_and_ignores_out_of_scope_rows() -> None:
    targets = [
        _target("same-z", "loc-a", Category.ELECTRICITY, "同名"),
        _target("same-a", "loc-a", Category.ELECTRICITY, "同名"),
        _target("boiler", "loc-a", Category.BOILER, "ボイラー"),
        _target("others", "loc-a", Category.OTHERS, "その他"),
        _target("water", "loc-b", Category.WATER, "水道"),
    ]

    order = calculate_reading_route_order(
        [
            ReadingRouteLocationRecord("loc-b", 0),
            ReadingRouteLocationRecord("not-visible", 1),
        ],
        [ReadingRouteCategoryRecord("loc-a", Category.OTHERS, 0)],
        [ReadingRouteDefinitionRecord("same-z", 0)],
        targets,
        {"loc-a": ("A棟",), "loc-b": ()},
    )

    assert order.location_order == ("loc-b", "loc-a")
    assert order.category_order["loc-a"] == (
        Category.OTHERS,
        Category.ELECTRICITY,
        Category.BOILER,
    )
    assert order.definition_order[("loc-a", Category.ELECTRICITY)] == ("same-z", "same-a")
    assert order.location_ranks == {"loc-b": 0, "loc-a": 1}
    assert sorted(order.definition_ranks.values()) == [0, 0, 1, 2, 3]


def _create_definition(
    repository: SQLiteRepository,
    definition_id: str,
    location_id: str,
    category: Category,
    *,
    display_name: str | None = None,
    is_active: bool = True,
) -> None:
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id=definition_id,
            location_id=location_id,
            display_name=display_name or definition_id,
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
    )
    if not is_active:
        repository.set_measurement_definition_active(definition_id, False)


def _saved_route(
    repository: SQLiteRepository,
) -> tuple[
    list[ReadingRouteLocationRecord],
    list[ReadingRouteCategoryRecord],
    list[ReadingRouteDefinitionRecord],
]:
    return (
        repository.list_reading_route_locations(),
        repository.list_reading_route_categories(),
        repository.list_reading_route_definitions(),
    )


@pytest.fixture
def route_service(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> ReadingRouteService:
    return ReadingRouteService(
        sqlite_repository, transaction_factory=partial(transaction, memory_conn)
    )


def test_save_reorders_visible_items_preserves_hidden_order_and_reset_clears_all(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    route_service: ReadingRouteService,
) -> None:
    with transaction(memory_conn):
        for definition_id, location_id, category, is_active in (
            ("electric-a", "loc-a", Category.ELECTRICITY, True),
            ("electric-z", "loc-a", Category.ELECTRICITY, True),
            ("water-a", "loc-a", Category.WATER, True),
            ("water-b", "loc-b", Category.WATER, True),
            ("hidden-z-electric", "loc-a", Category.ELECTRICITY, False),
            ("hidden-a-electric", "loc-a", Category.ELECTRICITY, False),
            ("hidden-z-others", "loc-a", Category.OTHERS, False),
            ("hidden-a-others", "loc-a", Category.OTHERS, False),
            ("hidden-gas", "loc-a", Category.GAS, False),
            ("hidden-site-z", "loc-hidden-z", Category.WATER, False),
            ("hidden-site-a", "loc-hidden-a", Category.WATER, False),
        ):
            _create_definition(
                sqlite_repository, definition_id, location_id, category, is_active=is_active
            )
        sqlite_repository.replace_reading_route(
            [
                ReadingRouteLocationRecord("loc-hidden-z", 2),
                ReadingRouteLocationRecord("loc-a", 4),
                ReadingRouteLocationRecord("loc-hidden-a", 8),
            ],
            [
                ReadingRouteCategoryRecord("loc-a", Category.OTHERS, 1),
                ReadingRouteCategoryRecord("loc-a", Category.ELECTRICITY, 4),
                ReadingRouteCategoryRecord("loc-a", Category.GAS, 8),
                ReadingRouteCategoryRecord("loc-hidden-z", Category.WATER, 3),
            ],
            [
                ReadingRouteDefinitionRecord("hidden-z-electric", 3),
                ReadingRouteDefinitionRecord("hidden-a-electric", 7),
                ReadingRouteDefinitionRecord("hidden-z-others", 4),
                ReadingRouteDefinitionRecord("hidden-a-others", 8),
                ReadingRouteDefinitionRecord("hidden-site-z", 5),
            ],
        )
    request = ReadingRouteSaveRequest(
        locations=(
            ReadingRouteSaveLocation(
                "loc-b", (ReadingRouteSaveCategory(Category.WATER, ("water-b",)),)
            ),
            ReadingRouteSaveLocation(
                "loc-a",
                (
                    ReadingRouteSaveCategory(Category.WATER, ("water-a",)),
                    ReadingRouteSaveCategory(Category.ELECTRICITY, ("electric-z", "electric-a")),
                ),
            ),
        )
    )

    route_service.save_route(request)

    locations, categories, definitions = _saved_route(sqlite_repository)
    assert locations == [
        ReadingRouteLocationRecord("loc-b", 0),
        ReadingRouteLocationRecord("loc-a", 1),
        ReadingRouteLocationRecord("loc-hidden-z", 2),
        ReadingRouteLocationRecord("loc-hidden-a", 3),
    ]
    assert categories == [
        ReadingRouteCategoryRecord("loc-a", Category.WATER, 0),
        ReadingRouteCategoryRecord("loc-a", Category.ELECTRICITY, 1),
        ReadingRouteCategoryRecord("loc-a", Category.OTHERS, 2),
        ReadingRouteCategoryRecord("loc-a", Category.GAS, 3),
        ReadingRouteCategoryRecord("loc-b", Category.WATER, 0),
        ReadingRouteCategoryRecord("loc-hidden-z", Category.WATER, 0),
    ]
    assert {item.measurement_definition_id: item.sort_order for item in definitions} == {
        "electric-z": 0,
        "electric-a": 1,
        "water-a": 0,
        "water-b": 0,
        "hidden-z-electric": 2,
        "hidden-a-electric": 3,
        "hidden-z-others": 0,
        "hidden-a-others": 1,
        "hidden-site-z": 0,
    }
    editor = route_service.load_route_editor()
    assert editor.has_saved_order
    for loaded, saved in zip(editor.locations, request.locations, strict=True):
        assert loaded.location_id == saved.location_id
        assert loaded.is_configured
        for loaded_category, saved_category in zip(
            loaded.categories, saved.categories, strict=True
        ):
            assert loaded_category.category is saved_category.category
            assert loaded_category.is_configured
            assert (
                tuple(item.measurement_definition_id for item in loaded_category.definitions)
                == saved_category.measurement_definition_ids
            )
            assert all(item.is_configured for item in loaded_category.definitions)

    route_service.reset_route()

    assert _saved_route(sqlite_repository) == ([], [], [])
    reset = route_service.load_route_editor()
    assert not reset.has_saved_order
    assert [location.location_id for location in reset.locations] == ["loc-a", "loc-b"]


def _electric(*definition_ids: str) -> ReadingRouteSaveCategory:
    return ReadingRouteSaveCategory(Category.ELECTRICITY, definition_ids)


def _loc_a(*categories: ReadingRouteSaveCategory) -> ReadingRouteSaveLocation:
    return ReadingRouteSaveLocation("loc-a", categories)


_ELECTRIC_A = _electric("electric-a")
_GAS_A = ReadingRouteSaveCategory(Category.GAS, ("gas-a",))
_ELECTRIC_B = _electric("electric-b")
_LOC_A = _loc_a(_ELECTRIC_A, _GAS_A)
_LOC_B = ReadingRouteSaveLocation("loc-b", (_ELECTRIC_B,))


@pytest.fixture
def saved_route_request(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> ReadingRouteSaveRequest:
    with transaction(memory_conn):
        _create_definition(sqlite_repository, "electric-a", "loc-a", Category.ELECTRICITY)
        _create_definition(sqlite_repository, "gas-a", "loc-a", Category.GAS)
        _create_definition(sqlite_repository, "electric-b", "loc-b", Category.ELECTRICITY)
        _create_definition(
            sqlite_repository, "disabled", "loc-a", Category.ELECTRICITY, is_active=False
        )
        sqlite_repository.replace_reading_route(
            [ReadingRouteLocationRecord("loc-b", 2), ReadingRouteLocationRecord("loc-a", 5)],
            [
                ReadingRouteCategoryRecord("loc-a", Category.GAS, 1),
                ReadingRouteCategoryRecord("loc-a", Category.ELECTRICITY, 3),
                ReadingRouteCategoryRecord("loc-b", Category.ELECTRICITY, 4),
            ],
            [
                ReadingRouteDefinitionRecord("electric-a", 2),
                ReadingRouteDefinitionRecord("disabled", 5),
                ReadingRouteDefinitionRecord("gas-a", 3),
                ReadingRouteDefinitionRecord("electric-b", 7),
            ],
        )
    return ReadingRouteSaveRequest((_LOC_A, _LOC_B))


@pytest.mark.parametrize(
    ("locations", "message"),
    [
        pytest.param((_LOC_A, _LOC_B, _LOC_A), "場所.*再読込", id="duplicate-location"),
        pytest.param(
            (_LOC_A, replace(_LOC_B, location_id="missing")), "場所.*再読込", id="unknown-location"
        ),
        pytest.param((_LOC_A,), "場所.*再読込", id="missing-location"),
        pytest.param(
            (_loc_a(_ELECTRIC_A, _GAS_A, _GAS_A), _LOC_B),
            "カテゴリ.*重複",
            id="duplicate-category",
        ),
        pytest.param(
            (_loc_a(_ELECTRIC_A, replace(_GAS_A, category=Category.WATER)), _LOC_B),
            "カテゴリ.*再読込",
            id="unknown-category",
        ),
        pytest.param(
            (_loc_a(_ELECTRIC_A), _LOC_B),
            "カテゴリ.*再読込",
            id="missing-category",
        ),
        pytest.param(
            (_loc_a(_electric("electric-a", "electric-a"), _GAS_A), _LOC_B),
            "測定定義.*重複",
            id="duplicate-definition",
        ),
        *[
            pytest.param(
                (_loc_a(_electric(*definition_ids), _GAS_A), _LOC_B),
                "測定定義.*再読込",
                id=case,
            )
            for case, definition_ids in (
                ("unknown-definition", ("missing",)),
                ("inactive-definition", ("disabled",)),
                ("missing-definition", ()),
            )
        ],
        pytest.param(
            (_loc_a(_ELECTRIC_B, _GAS_A), replace(_LOC_B, categories=(_ELECTRIC_A,))),
            "測定定義.*再読込",
            id="wrong-location",
        ),
        pytest.param(
            (
                _loc_a(_electric("gas-a"), ReadingRouteSaveCategory(Category.GAS, ("electric-a",))),
                _LOC_B,
            ),
            "測定定義.*再読込",
            id="wrong-category",
        ),
    ],
)
def test_save_rejects_invalid_route_without_changing_any_saved_order(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    route_service: ReadingRouteService,
    saved_route_request: ReadingRouteSaveRequest,
    locations: tuple[ReadingRouteSaveLocation, ...],
    message: str,
) -> None:
    before = _saved_route(sqlite_repository)
    with pytest.raises(ValidationError, match=message):
        route_service.save_route(replace(saved_route_request, locations=locations))

    assert _saved_route(sqlite_repository) == before
    assert not memory_conn.in_transaction


@pytest.mark.parametrize("failure_point", ["after-delete", "mid-insert", "commit"])
def test_route_save_rolls_back_all_three_tables(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    route_service: ReadingRouteService,
    saved_route_request: ReadingRouteSaveRequest,
    failure_point: str,
) -> None:
    before = _saved_route(sqlite_repository)

    if failure_point == "commit":

        def deny_commit(action: int, operation: str | None, *_: object) -> int:
            if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        memory_conn.set_authorizer(deny_commit)
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                route_service.save_route(saved_route_request)
        finally:
            memory_conn.set_authorizer(None)
    else:
        if failure_point == "after-delete":
            memory_conn.execute(
                "CREATE TEMP TRIGGER fail_route_insert BEFORE INSERT ON reading_route_locations "
                "BEGIN SELECT RAISE(ABORT, 'injected route insert failure'); END"
            )
        else:
            memory_conn.execute(
                "CREATE TEMP TRIGGER fail_route_insert AFTER INSERT ON reading_route_definitions "
                "WHEN NEW.measurement_definition_id = 'gas-a' "
                "BEGIN SELECT RAISE(ABORT, 'injected route insert failure'); END"
            )
        with pytest.raises(ValidationError, match="保存できません.*injected route insert failure"):
            route_service.save_route(saved_route_request)

    assert _saved_route(sqlite_repository) == before
    assert not memory_conn.in_transaction


def test_reset_route_commit_failure_restores_all_saved_rows() -> None:
    class CommitFailingConnection(sqlite3.Connection):
        fail_commit = False

        def execute(self, sql, parameters=()):
            if self.fail_commit and sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("simulated commit failure")
            return super().execute(sql, parameters)

    conn = sqlite3.connect(":memory:", autocommit=True, factory=CommitFailingConnection)
    try:
        create_schema(conn)
        repository = SQLiteRepository(conn)
        with transaction(conn):
            repository.replace_reading_route([ReadingRouteLocationRecord("location", 0)], (), ())
        conn.fail_commit = True
        service = ReadingRouteService(repository, transaction_factory=partial(transaction, conn))

        with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
            service.reset_route()

        assert repository.list_reading_route_locations() == [
            ReadingRouteLocationRecord("location", 0)
        ]
        assert not conn.in_transaction
    finally:
        conn.close()


def test_category_change_deletes_only_that_definition_route_order_atomically(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    route_service: ReadingRouteService,
) -> None:
    with transaction(memory_conn):
        for definition_id, category, name in (
            ("electric-stays", Category.ELECTRICITY, "既存"),
            ("moving", Category.ELECTRICITY, "20 移動対象"),
            ("gas-saved-first", Category.GAS, "80 設定済み"),
            ("gas-saved-second", Category.GAS, "70 設定済み"),
            ("gas-unset-first", Category.GAS, "10 未設定"),
            ("gas-unset-last", Category.GAS, "30 未設定"),
        ):
            _create_definition(
                sqlite_repository, definition_id, "loc-a", category, display_name=name
            )
        sqlite_repository.replace_reading_route(
            [ReadingRouteLocationRecord("loc-a", 2)],
            [
                ReadingRouteCategoryRecord("loc-a", Category.GAS, 3),
                ReadingRouteCategoryRecord("loc-a", Category.ELECTRICITY, 6),
            ],
            [
                ReadingRouteDefinitionRecord("moving", 0),
                ReadingRouteDefinitionRecord("electric-stays", 4),
                ReadingRouteDefinitionRecord("gas-saved-first", 2),
                ReadingRouteDefinitionRecord("gas-saved-second", 7),
            ],
        )
    locations, categories, definitions = _saved_route(sqlite_repository)
    service = MeasurementDefinitionService(
        sqlite_repository,
        sqlite_repository,
        sqlite_repository,
        reading_route_repository=sqlite_repository,
        transaction_factory=partial(transaction, memory_conn),
    )
    update = MeasurementDefinitionUpdateRequest(
        display_name="20 移動対象",
        category=Category.GAS,
        unit="kWh",
        decimal_places=2,
        warning_threshold=None,
        purpose=Purpose.OPERATIONAL,
    )

    with pytest.raises(ValidationError, match="集計場所"):
        service.update_measurement_definition("moving", replace(update, location_id="loc-b"))
    assert _saved_route(sqlite_repository) == (locations, categories, definitions)

    memory_conn.execute(
        "CREATE TEMP TRIGGER fail_category_update BEFORE UPDATE OF category "
        "ON measurement_definitions WHEN OLD.measurement_definition_id = 'moving' "
        "BEGIN SELECT RAISE(ABORT, 'injected category update failure'); END"
    )
    with pytest.raises(ValidationError, match="injected category update failure"):
        service.update_measurement_definition("moving", update)
    assert _saved_route(sqlite_repository) == (locations, categories, definitions)
    original = sqlite_repository.get_measurement_definition("moving")
    assert original is not None and original.category is Category.ELECTRICITY
    memory_conn.execute("DROP TRIGGER fail_category_update")

    updated = service.update_measurement_definition("moving", update)

    assert updated.category is Category.GAS
    assert _saved_route(sqlite_repository) == (
        locations,
        categories,
        [item for item in definitions if item.measurement_definition_id != "moving"],
    )
    (location,) = route_service.load_route_editor().locations
    gas, electricity = location.categories
    assert (gas.category, electricity.category) == (Category.GAS, Category.ELECTRICITY)
    assert [(item.measurement_definition_id, item.is_configured) for item in gas.definitions] == [
        ("gas-saved-first", True),
        ("gas-saved-second", True),
        ("gas-unset-first", False),
        ("moving", False),
        ("gas-unset-last", False),
    ]
    assert [item.measurement_definition_id for item in electricity.definitions] == [
        "electric-stays"
    ]
