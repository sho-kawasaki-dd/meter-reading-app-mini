from __future__ import annotations

import sqlite3
from functools import partial

import pytest

from meter_reading_mini.core.contract_models import Category
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.measurement_definition_service import MeasurementDefinitionService
from meter_reading_mini.core.meter_reading_sync_repositories import (
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
from meter_reading_mini.db.integrity import check_business_integrity
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


def _create_extra_definition(
    repository: SQLiteRepository,
    definition_id: str,
    location_id: str,
    meter_id: str,
    name: str,
) -> None:
    from datetime import date
    from decimal import Decimal

    from meter_reading_mini.core.contract_models import (
        CalculationMethod,
        Purpose,
        ReadingCycle,
        ValueSchema,
    )
    from meter_reading_mini.core.meter_reading_sync_repositories import (
        MeasurementDefinitionCreateRequest,
    )

    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id=definition_id,
            location_id=location_id,
            display_name=name,
            category=Category.ELECTRICITY,
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
            initial_meter_id=meter_id,
        )
    )


def test_save_reorders_visible_items_preserves_hidden_order_and_reset_clears_all(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, first_id = sample_master
    location_id = "location-test-001"
    second_id = "definition-route-second"
    hidden_id = "definition-route-hidden"
    _create_extra_definition(sqlite_repository, second_id, location_id, meter_id, "二番")
    _create_extra_definition(sqlite_repository, hidden_id, location_id, meter_id, "無効")
    memory_conn.execute(
        "UPDATE measurement_definitions SET is_active = 0 WHERE measurement_definition_id = ?",
        (hidden_id,),
    )
    with transaction(memory_conn):
        sqlite_repository.replace_reading_route(
            [ReadingRouteLocationRecord(location_id, 0)],
            [ReadingRouteCategoryRecord(location_id, Category.ELECTRICITY, 0)],
            [ReadingRouteDefinitionRecord(hidden_id, 0)],
        )
    service = ReadingRouteService(
        sqlite_repository, transaction_factory=partial(transaction, memory_conn)
    )
    request = ReadingRouteSaveRequest(
        locations=(
            ReadingRouteSaveLocation(
                location_id,
                (ReadingRouteSaveCategory(Category.ELECTRICITY, (second_id, first_id)),),
            ),
        )
    )

    service.save_route(request)

    assert [
        item.measurement_definition_id
        for item in sqlite_repository.list_reading_route_definitions()
    ] == [second_id, first_id, hidden_id]
    editor = service.load_route_editor()
    assert [
        item.measurement_definition_id for item in (editor.locations[0].categories[0].definitions)
    ] == [second_id, first_id]
    assert all(item.is_configured for item in editor.locations[0].categories[0].definitions)
    assert editor.has_saved_order

    service.reset_route()

    assert sqlite_repository.list_reading_route_locations() == []
    assert sqlite_repository.list_reading_route_categories() == []
    assert sqlite_repository.list_reading_route_definitions() == []


def test_save_rejects_stale_display_set_without_changing_saved_order(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    _meter_id, first_id = sample_master
    location_id = "location-test-001"
    second_id = "definition-route-second"
    _create_extra_definition(sqlite_repository, second_id, location_id, _meter_id, "二番")
    service = ReadingRouteService(
        sqlite_repository, transaction_factory=partial(transaction, memory_conn)
    )
    original = [ReadingRouteDefinitionRecord(first_id, 0)]
    with transaction(memory_conn):
        sqlite_repository.replace_reading_route(
            [ReadingRouteLocationRecord(location_id, 0)],
            [ReadingRouteCategoryRecord(location_id, Category.ELECTRICITY, 0)],
            original,
        )
    stale_request = ReadingRouteSaveRequest(
        locations=(
            ReadingRouteSaveLocation(
                location_id,
                (ReadingRouteSaveCategory(Category.ELECTRICITY, (first_id,)),),
            ),
        )
    )

    with pytest.raises(ValidationError, match="再読込"):
        service.save_route(stale_request)

    assert sqlite_repository.list_reading_route_definitions() == original
    assert not memory_conn.in_transaction


def test_route_save_rolls_back_if_repository_fails_after_deleting_rows(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _meter_id, definition_id = sample_master
    location_id = "location-test-001"
    original = [ReadingRouteDefinitionRecord(definition_id, 0)]
    with transaction(memory_conn):
        sqlite_repository.replace_reading_route(
            [ReadingRouteLocationRecord(location_id, 0)],
            [ReadingRouteCategoryRecord(location_id, Category.ELECTRICITY, 0)],
            original,
        )
    service = ReadingRouteService(
        sqlite_repository, transaction_factory=partial(transaction, memory_conn)
    )
    request = ReadingRouteSaveRequest(
        locations=(
            ReadingRouteSaveLocation(
                location_id,
                (ReadingRouteSaveCategory(Category.ELECTRICITY, (definition_id,)),),
            ),
        )
    )

    def fail_after_deleting(*_args: object, **_kwargs: object) -> None:
        memory_conn.execute("DELETE FROM reading_route_definitions")
        raise RuntimeError("insert failed")

    monkeypatch.setattr(sqlite_repository, "replace_reading_route", fail_after_deleting)
    with pytest.raises(RuntimeError, match="insert failed"):
        service.save_route(request)

    assert sqlite_repository.list_reading_route_definitions() == original
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


def test_integrity_rejects_orphan_route_location_and_duplicate_definition_order(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    _meter_id, definition_id = sample_master
    memory_conn.execute(
        "INSERT INTO reading_route_locations (location_id, sort_order) VALUES ('missing', 0)"
    )
    assert any("存在しません" in reason for reason in check_business_integrity(memory_conn))
    memory_conn.execute("DELETE FROM reading_route_locations")
    memory_conn.execute(
        "INSERT INTO reading_route_definitions (measurement_definition_id, sort_order) "
        "VALUES (?, 0)",
        (definition_id,),
    )
    memory_conn.execute(
        "INSERT INTO measurement_definitions "
        "(measurement_definition_id, location_id, display_name, category, unit, value_schema, "
        "multiplier, calculation_method, reading_cycle, purpose, generation_start_date, is_active) "
        "SELECT 'route-duplicate', location_id, '別定義', category, unit, value_schema, "
        "multiplier, calculation_method, reading_cycle, purpose, generation_start_date, 1 "
        "FROM measurement_definitions WHERE measurement_definition_id = ?",
        (definition_id,),
    )
    memory_conn.execute(
        "INSERT INTO reading_route_definitions (measurement_definition_id, sort_order) "
        "VALUES ('route-duplicate', 0)"
    )

    reasons = check_business_integrity(memory_conn)

    assert any("測定定義順が重複" in reason for reason in reasons)


def test_category_change_deletes_only_that_definition_route_order_atomically(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _meter_id, definition_id = sample_master
    with transaction(memory_conn):
        sqlite_repository.replace_reading_route(
            [],
            [],
            [ReadingRouteDefinitionRecord(definition_id, 0)],
        )
    service = MeasurementDefinitionService(
        sqlite_repository,
        sqlite_repository,
        sqlite_repository,
        reading_route_repository=sqlite_repository,
        transaction_factory=partial(transaction, memory_conn),
    )
    update = MeasurementDefinitionUpdateRequest(
        display_name="変更後",
        category=Category.GAS,
        unit="kWh",
        decimal_places=2,
        warning_threshold=None,
        purpose="operational",
    )

    service.update_measurement_definition(definition_id, update)

    assert sqlite_repository.list_reading_route_definitions() == []
    with transaction(memory_conn):
        sqlite_repository.replace_reading_route(
            [],
            [],
            [ReadingRouteDefinitionRecord(definition_id, 0)],
        )
    original_update = sqlite_repository.update_measurement_definition

    def fail_update(*args: object, **kwargs: object) -> None:
        raise RuntimeError("update failed")

    monkeypatch.setattr(sqlite_repository, "update_measurement_definition", fail_update)
    with pytest.raises(RuntimeError, match="update failed"):
        service.update_measurement_definition(
            definition_id,
            MeasurementDefinitionUpdateRequest(
                display_name="変更後",
                category=Category.ELECTRICITY,
                unit="kWh",
                decimal_places=2,
                warning_threshold=None,
                purpose="operational",
            ),
        )
    monkeypatch.setattr(sqlite_repository, "update_measurement_definition", original_update)
    assert sqlite_repository.list_reading_route_definitions() == [
        ReadingRouteDefinitionRecord(definition_id, 0)
    ]
