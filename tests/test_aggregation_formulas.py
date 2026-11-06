from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from itertools import count
from pathlib import Path

import pytest

from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.core.aggregation_contracts import (
    AggregationCycleGroup,
    AggregationFormulaCreateInput,
    AggregationFormulaTerm,
    AggregationFormulaUpdateInput,
    AggregationFormulaVersionInput,
    parse_aggregation_coefficient,
    resolve_aggregation_cycle_group,
    serialize_aggregation_coefficient,
)
from meter_reading_mini.core.aggregation_formulas import AggregationFormulaService
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ValueSchema,
)
from meter_reading_mini.core.errors import (
    FormulaReferencedDefinitionError,
    FormulaValidationError,
    ValidationError,
)
from meter_reading_mini.core.measurement_definition_service import MeasurementDefinitionService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
    MeasurementDefinitionUpdateRequest,
)
from meter_reading_mini.db.backup import copy_database
from meter_reading_mini.db.connection import connect_readonly, transaction
from meter_reading_mini.db.integrity import check_business_integrity
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.db.repositories import SQLiteRepository

DEFINITION = "definition-test-001"
TODAY = date(2026, 2, 4)


@pytest.fixture
def formula_service(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
    fixed_clock: datetime,
) -> AggregationFormulaService:
    ids = count(1)
    return AggregationFormulaService(
        sqlite_repository,
        sqlite_repository,
        transaction_factory=partial(transaction, memory_conn),
        current_business_date_provider=lambda: TODAY,
        clock=lambda: fixed_clock,
        id_provider=lambda: f"formula-{next(ids)}",
    )


def _term(
    definition_id: str = DEFINITION,
    coefficient: str = "1",
    sort_order: int = 0,
) -> AggregationFormulaTerm:
    return AggregationFormulaTerm(definition_id, Decimal(coefficient), sort_order)


def _version(
    effective: date,
    *terms: AggregationFormulaTerm,
    is_active: bool = True,
) -> AggregationFormulaVersionInput:
    return AggregationFormulaVersionInput(effective, is_active, terms)


def _create(
    service: AggregationFormulaService,
    version: AggregationFormulaVersionInput | None = None,
    *,
    cycle_group: AggregationCycleGroup = AggregationCycleGroup.DAILY,
) -> str:
    formula = service.create_formula(
        AggregationFormulaCreateInput(
            display_name="オーナー使用量",
            unit="kWh",
            category=Category.ELECTRICITY,
            cycle_group=cycle_group,
            purpose=Purpose.OPERATIONAL,
            decimal_places=2,
            initial_version=version or _version(date(2026, 1, 1), _term()),
        )
    )
    return formula.formula_id


def _add_definition(
    repository: SQLiteRepository,
    definition_id: str,
    *,
    reading_cycle: ReadingCycle = ReadingCycle.DAILY_3X,
    purpose: Purpose = Purpose.BILLING,
) -> None:
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id=definition_id,
            location_id="another-location",
            display_name="試験項目",
            category=Category.ELECTRICITY,
            unit="kWh",
            value_schema=ValueSchema.DECIMAL_STRING,
            multiplier=Decimal("1"),
            decimal_places=2,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.DELTA,
            reading_cycle=reading_cycle,
            purpose=purpose,
            generation_start_date=date(2026, 1, 1),
            initial_schedule_json=None,
            initial_meter_id=None,
        )
    )


def test_retroactive_formula_roundtrips_exact_terms_and_metadata(
    formula_service: AggregationFormulaService,
    sqlite_repository: SQLiteRepository,
    memory_conn: sqlite3.Connection,
    fixed_clock: datetime,
) -> None:
    _add_definition(sqlite_repository, "second")
    formula_id = _create(
        formula_service,
        _version(
            date(2025, 1, 1),
            _term("second", "-0.12500", 2),
            _term(DEFINITION, "123456789012345678901234567890.000001", 0),
        ),
    )
    formula = formula_service.get_formula(formula_id)
    assert formula is not None
    assert formula_service.list_formulas() == [formula]
    assert formula_service.current_business_date() == TODAY
    assert formula_service.get_version_for_date(formula_id, date(2024, 12, 31)) is None
    version = formula_service.get_version_for_date(formula_id, date(2025, 1, 1))
    assert version is not None
    assert version.is_initial and version.is_retroactive
    assert version.registered_business_date == TODAY
    assert version.created_at == fixed_clock
    assert version.terms == (
        _term(DEFINITION, "123456789012345678901234567890.000001", 0),
        _term("second", "-0.12500", 2),
    )
    assert memory_conn.execute(
        "SELECT coefficient FROM aggregation_formula_terms ORDER BY sort_order"
    ).fetchall() == [("123456789012345678901234567890.000001",), ("-0.12500",)]
    updated = formula_service.update_formula(
        formula_id, AggregationFormulaUpdateInput(" 新しい名称 ", None)
    )
    assert updated.display_name == "新しい名称"
    assert updated.decimal_places is None
    assert (updated.unit, updated.category, updated.cycle_group, updated.purpose) == (
        formula.unit,
        formula.category,
        formula.cycle_group,
        formula.purpose,
    )
    assert formula_service.list_versions(formula_id) == [version]
    assert check_business_integrity(memory_conn) == []


def test_version_history_resave_future_insertion_and_inactive_periods(
    formula_service: AggregationFormulaService, memory_conn: sqlite3.Connection
) -> None:
    formula_id = _create(formula_service)
    first = formula_service.save_version(
        formula_id, _version(date(2026, 1, 1), _term(coefficient="-2.5"))
    )
    assert first.is_retroactive and first.terms[0].coefficient == Decimal("-2.5")
    corrected = formula_service.resave_initial_version(
        formula_id, _version(date(2025, 12, 1), _term(coefficient="3"))
    )
    assert corrected.is_initial and corrected.is_retroactive
    assert len(formula_service.list_versions(formula_id)) == 1
    assert formula_service.get_version_for_date(formula_id, date(2025, 11, 30)) is None

    formula_service.save_version(formula_id, _version(TODAY, is_active=False))
    formula_service.save_version(formula_id, _version(date(2026, 3, 1), _term(coefficient="4")))
    formula_service.save_version(formula_id, _version(date(2026, 5, 1), _term(coefficient="5")))
    formula_service.save_version(formula_id, _version(date(2026, 4, 1), _term(coefficient="-1")))
    assert [
        version.effective_from_date for version in formula_service.list_versions(formula_id)
    ] == [
        date(2025, 12, 1),
        TODAY,
        date(2026, 3, 1),
        date(2026, 4, 1),
        date(2026, 5, 1),
    ]
    inactive = formula_service.get_version_for_date(formula_id, date(2026, 2, 5))
    assert inactive is not None and not inactive.is_active and inactive.terms == ()
    reactivated = formula_service.save_version(formula_id, _version(TODAY, _term(coefficient="7")))
    assert reactivated.is_active and reactivated.terms == (_term(coefficient="7"),)
    formula_service.save_version(formula_id, _version(TODAY, is_active=False))
    switched = formula_service.save_version(
        formula_id, _version(date(2026, 3, 1), _term(coefficient="9"))
    )
    assert switched.terms == (_term(coefficient="9"),)
    assert len(formula_service.list_versions(formula_id)) == 5
    may_version = formula_service.get_version_for_date(formula_id, date(2026, 5, 2))
    assert may_version is not None
    assert may_version.terms == (_term(coefficient="5"),)
    for action in (
        lambda: formula_service.resave_initial_version(
            formula_id, _version(date(2025, 1, 1), _term())
        ),
        lambda: formula_service.save_version(formula_id, _version(date(2025, 12, 1), _term())),
    ):
        with pytest.raises(FormulaValidationError, match="初版"):
            action()
    with pytest.raises(FormulaValidationError, match="現在業務日"):
        formula_service.save_version(formula_id, _version(date(2026, 2, 3), _term()))
    assert len(formula_service.list_versions(formula_id)) == 5
    assert check_business_integrity(memory_conn) == []


def test_initial_identity_survives_insertion_before_a_future_initial_version(
    formula_service: AggregationFormulaService, memory_conn: sqlite3.Connection
) -> None:
    future = date(2026, 6, 1)
    formula_id = _create(formula_service, _version(future, _term()))
    formula_service.save_version(formula_id, _version(TODAY, _term(coefficient="2")))
    versions = formula_service.list_versions(formula_id)
    assert [(item.effective_from_date, item.is_initial) for item in versions] == [
        (TODAY, False),
        (future, True),
    ]
    with pytest.raises(FormulaValidationError, match="初版"):
        formula_service.save_version(formula_id, _version(future, _term(coefficient="3")))
    assert check_business_integrity(memory_conn) == []


@pytest.mark.parametrize(
    ("cycle", "group"),
    [
        (ReadingCycle.DAILY_1X, AggregationCycleGroup.DAILY),
        (ReadingCycle.DAILY_2X, AggregationCycleGroup.DAILY),
        (ReadingCycle.DAILY_3X, AggregationCycleGroup.DAILY),
        (ReadingCycle.WEEKLY, AggregationCycleGroup.WEEKLY),
        (ReadingCycle.MONTHLY, AggregationCycleGroup.MONTHLY),
        (ReadingCycle.EVERY_TWO_MONTHS, AggregationCycleGroup.EVERY_TWO_MONTHS),
    ],
)
def test_all_cycle_groups_can_be_saved(
    cycle: ReadingCycle,
    group: AggregationCycleGroup,
    formula_service: AggregationFormulaService,
    sqlite_repository: SQLiteRepository,
    memory_conn: sqlite3.Connection,
) -> None:
    _add_definition(sqlite_repository, "cycle-test", reading_cycle=cycle)
    formula_id = _create(
        formula_service,
        _version(TODAY, _term("cycle-test", "-0.2")),
        cycle_group=group,
    )
    formula = formula_service.get_formula(formula_id)
    assert formula is not None and formula.cycle_group == group
    assert resolve_aggregation_cycle_group(cycle) == group
    assert check_business_integrity(memory_conn) == []


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("unit", "m3", "単位・カテゴリ"),
        ("category", "gas", "単位・カテゴリ"),
        ("reading_cycle", "monthly", "周期グループ"),
        ("calculation_method", "raw_only", "差分計算"),
        ("value_schema", "power_factor_percent_phase", "差分計算"),
        ("is_active", 0, "無効な測定定義"),
    ],
)
def test_incompatible_definition_is_rejected_without_partial_formula(
    column: str,
    value: str | int,
    message: str,
    formula_service: AggregationFormulaService,
    memory_conn: sqlite3.Connection,
) -> None:
    memory_conn.execute(
        f"UPDATE measurement_definitions SET {column} = ? WHERE measurement_definition_id = ?",
        (value, DEFINITION),
    )
    with pytest.raises(FormulaValidationError, match=message):
        _create(formula_service)
    assert memory_conn.execute("SELECT COUNT(*) FROM aggregation_formulas").fetchone() == (0,)


@pytest.mark.parametrize(
    "coefficient",
    [
        Decimal("0"),
        Decimal("-0.000"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        "1",
        1.5,
    ],
)
def test_invalid_coefficients_are_rejected(
    coefficient: object, formula_service: AggregationFormulaService, memory_conn: sqlite3.Connection
) -> None:
    with pytest.raises(FormulaValidationError, match="係数"):
        serialize_aggregation_coefficient(coefficient)
    assert memory_conn.execute("SELECT COUNT(*) FROM aggregation_formulas").fetchone() == (0,)


@pytest.mark.parametrize("coefficient", [Decimal("0"), Decimal("-0"), Decimal("NaN")])
def test_invalid_coefficients_cannot_be_saved_by_service(
    coefficient: Decimal,
    formula_service: AggregationFormulaService,
    memory_conn: sqlite3.Connection,
) -> None:
    term = AggregationFormulaTerm(DEFINITION, coefficient, 0)
    with pytest.raises(FormulaValidationError, match="係数"):
        _create(formula_service, _version(TODAY, term))
    assert memory_conn.execute("SELECT COUNT(*) FROM aggregation_formulas").fetchone() == (0,)


@pytest.mark.parametrize(
    "coefficient", ["", "01", "-00.1", "1e2", "+1", " 1", "1.", ".1", "0", "-0.0", "NaN"]
)
def test_invalid_stored_coefficient_strings_are_rejected(coefficient: str) -> None:
    with pytest.raises(FormulaValidationError, match="係数"):
        parse_aggregation_coefficient(coefficient)


def test_version_input_validation_and_missing_definitions(
    formula_service: AggregationFormulaService, memory_conn: sqlite3.Connection
) -> None:
    invalid_versions = (
        _version(TODAY),
        _version(TODAY, _term(), is_active=False),
        _version(TODAY, _term(), _term(coefficient="2", sort_order=1)),
        _version(TODAY, _term(), _term("other", sort_order=0)),
        _version(TODAY, _term(sort_order=-1)),
        _version(TODAY, _term(" ")),
        _version(TODAY, _term("missing")),
    )
    for version in invalid_versions:
        with pytest.raises(FormulaValidationError):
            _create(formula_service, version)
    assert memory_conn.execute("SELECT COUNT(*) FROM aggregation_formulas").fetchone() == (0,)
    with pytest.raises(FormulaValidationError, match="集計式が見つかりません"):
        formula_service.list_versions("missing")
    with pytest.raises(FormulaValidationError, match="集計式 ID"):
        formula_service.get_formula(" ")


def test_formula_metadata_and_version_dates_are_validated(
    formula_service: AggregationFormulaService, memory_conn: sqlite3.Connection
) -> None:
    request = AggregationFormulaCreateInput(
        display_name="式",
        unit="kWh",
        category=Category.ELECTRICITY,
        cycle_group=AggregationCycleGroup.DAILY,
        purpose=Purpose.OPERATIONAL,
        decimal_places=2,
        initial_version=_version(TODAY, _term()),
    )
    for invalid in (
        replace(request, display_name=" "),
        replace(request, unit=" "),
        replace(request, decimal_places=11),
        replace(request, decimal_places=-1),
        replace(request, decimal_places=True),
        replace(
            request,
            initial_version=replace(
                request.initial_version, effective_from_date=datetime(2026, 2, 4)
            ),
        ),
    ):
        with pytest.raises(FormulaValidationError):
            formula_service.create_formula(invalid)
    assert memory_conn.execute("SELECT COUNT(*) FROM aggregation_formulas").fetchone() == (0,)
    formula_id = _create(formula_service)
    with pytest.raises(FormulaValidationError, match="表示小数桁数"):
        formula_service.update_formula(formula_id, AggregationFormulaUpdateInput("式", 11))
    with pytest.raises(FormulaValidationError, match="対象業務日"):
        formula_service.get_version_for_date(formula_id, datetime(2026, 2, 4))


def test_duplicate_formula_identifier_does_not_change_existing_formula(
    formula_service: AggregationFormulaService,
    sqlite_repository: SQLiteRepository,
    memory_conn: sqlite3.Connection,
) -> None:
    formula_id = _create(formula_service)
    before = formula_service.list_versions(formula_id)
    duplicate = AggregationFormulaService(
        sqlite_repository,
        sqlite_repository,
        transaction_factory=partial(transaction, memory_conn),
        current_business_date_provider=lambda: TODAY,
        id_provider=lambda: formula_id,
    )
    with pytest.raises(FormulaValidationError, match="既に登録"):
        _create(duplicate)
    assert formula_service.list_versions(formula_id) == before


def test_inactive_definition_history_is_preserved_but_not_added_to_new_version(
    formula_service: AggregationFormulaService,
    sqlite_repository: SQLiteRepository,
    memory_conn: sqlite3.Connection,
) -> None:
    formula_id = _create(formula_service)
    sqlite_repository.set_measurement_definition_active(DEFINITION, False)
    retained = formula_service.resave_initial_version(
        formula_id, _version(date(2025, 1, 1), _term(coefficient="2"))
    )
    assert retained.terms == (_term(coefficient="2"),)
    with pytest.raises(FormulaValidationError, match="無効な測定定義"):
        formula_service.save_version(formula_id, _version(TODAY, _term()))
    with pytest.raises(FormulaValidationError, match="無効な測定定義"):
        _create(formula_service)
    formula_service.save_version(formula_id, _version(TODAY, is_active=False))
    assert sqlite_repository.is_measurement_definition_referenced_by_formula(DEFINITION)
    assert check_business_integrity(memory_conn) == []


def test_definition_attributes_are_protected_by_historical_formula_terms(
    formula_service: AggregationFormulaService,
    sqlite_repository: SQLiteRepository,
    memory_conn: sqlite3.Connection,
) -> None:
    formula_id = _create(formula_service)
    formula_service.save_version(formula_id, _version(TODAY, is_active=False))
    service = MeasurementDefinitionService(
        sqlite_repository,
        sqlite_repository,
        sqlite_repository,
        transaction_factory=partial(transaction, memory_conn),
    )
    request = MeasurementDefinitionUpdateRequest(
        display_name="変更後",
        category=Category.ELECTRICITY,
        unit="kWh",
        decimal_places=2,
        warning_threshold=None,
        purpose=Purpose.BILLING,
    )
    for changed in (
        replace(request, unit="MWh"),
        replace(request, category=Category.GAS),
    ):
        with pytest.raises(FormulaReferencedDefinitionError, match="新しい測定定義"):
            service.update_measurement_definition(DEFINITION, changed)
    updated = service.update_measurement_definition(DEFINITION, request)
    assert updated.purpose == Purpose.BILLING
    assert updated.unit == "kWh" and updated.category == Category.ELECTRICITY
    assert check_business_integrity(memory_conn) == []


def test_unreferenced_definition_attributes_remain_editable(
    formula_service: AggregationFormulaService,
    sqlite_repository: SQLiteRepository,
    memory_conn: sqlite3.Connection,
) -> None:
    _create(formula_service)
    _add_definition(sqlite_repository, "unreferenced")
    service = MeasurementDefinitionService(
        sqlite_repository,
        sqlite_repository,
        sqlite_repository,
        transaction_factory=partial(transaction, memory_conn),
    )
    updated = service.update_measurement_definition(
        "unreferenced",
        MeasurementDefinitionUpdateRequest(
            display_name="変更後",
            category=Category.GAS,
            unit="m3",
            decimal_places=2,
            warning_threshold=None,
            purpose=Purpose.AUDIT,
        ),
    )
    assert (updated.category, updated.unit, updated.purpose) == (
        Category.GAS,
        "m3",
        Purpose.AUDIT,
    )
    assert check_business_integrity(memory_conn) == []


def test_app_context_uses_injected_clock_and_business_timezone(
    memory_conn: sqlite3.Connection, sample_master: tuple[str, str]
) -> None:
    now = datetime(2026, 2, 4, 14, 59, tzinfo=UTC)
    context = create_app_context(memory_conn, clock=lambda: now)
    service = context.aggregation_formulas
    assert service.current_business_date() == date(2026, 2, 4)
    formula_id = _create(service)
    assert service.list_versions(formula_id)[0].created_at == now
    now = datetime(2026, 2, 4, 15, 0, tzinfo=UTC)
    assert service.current_business_date() == date(2026, 2, 5)
    with pytest.raises(FormulaValidationError, match="現在業務日"):
        service.save_version(formula_id, _version(date(2026, 2, 4), _term()))
    now = datetime(2026, 2, 4, 21, 59, tzinfo=UTC)
    assert service.current_business_date() == date(2026, 2, 5)
    now = datetime(2026, 2, 4, 22, 0, tzinfo=UTC)
    assert service.current_business_date() == date(2026, 2, 5)


def test_app_context_uses_business_date_across_daylight_saving_transition(
    memory_conn: sqlite3.Connection, sample_master: tuple[str, str]
) -> None:
    now = datetime(2026, 3, 8, 4, 59, tzinfo=UTC)
    context = create_app_context(memory_conn, clock=lambda: now)
    context.timezone_settings.save_timezone("America/New_York")
    assert context.aggregation_formulas.current_business_date() == date(2026, 3, 7)
    now = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert context.aggregation_formulas.current_business_date() == date(2026, 3, 8)
    now = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert context.aggregation_formulas.current_business_date() == date(2026, 3, 8)


def test_write_and_readback_failures_roll_back_whole_formula(
    formula_service: AggregationFormulaService,
    sqlite_repository: SQLiteRepository,
    memory_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = sqlite_repository._insert_aggregation_formula_terms

    def fail_after_terms(version) -> None:
        original(version)
        raise RuntimeError("保存失敗")

    monkeypatch.setattr(sqlite_repository, "_insert_aggregation_formula_terms", fail_after_terms)
    with pytest.raises(RuntimeError, match="保存失敗"):
        _create(formula_service)
    assert memory_conn.execute("SELECT COUNT(*) FROM aggregation_formulas").fetchone() == (0,)
    assert memory_conn.execute("SELECT COUNT(*) FROM aggregation_formula_versions").fetchone() == (
        0,
    )
    monkeypatch.setattr(sqlite_repository, "_insert_aggregation_formula_terms", original)
    formula_id = _create(formula_service)
    before = formula_service.list_versions(formula_id)
    monkeypatch.setattr(sqlite_repository, "_insert_aggregation_formula_terms", fail_after_terms)
    with pytest.raises(RuntimeError, match="保存失敗"):
        formula_service.resave_initial_version(
            formula_id, _version(date(2025, 1, 1), _term(coefficient="5"))
        )
    assert formula_service.list_versions(formula_id) == before
    assert not memory_conn.in_transaction


def test_commit_failure_rolls_back_create_and_version_replacement() -> None:
    class CommitFailingConnection(sqlite3.Connection):
        fail_commit = False

        def execute(self, sql, parameters=()):
            if self.fail_commit and sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("simulated commit failure")
            return super().execute(sql, parameters)

    conn = sqlite3.connect(":memory:", autocommit=True, factory=CommitFailingConnection)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        repository = SQLiteRepository(conn, clock=lambda: datetime(2026, 2, 4, tzinfo=UTC))
        _add_definition(repository, "cycle-test")
        ids = count(1)
        service = AggregationFormulaService(
            repository,
            repository,
            transaction_factory=partial(transaction, conn),
            current_business_date_provider=lambda: TODAY,
            clock=lambda: datetime(2026, 2, 4, tzinfo=UTC),
            id_provider=lambda: f"formula-{next(ids)}",
        )
        conn.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            _create(service, _version(TODAY, _term("cycle-test")))
        assert conn.execute("SELECT COUNT(*) FROM aggregation_formulas").fetchone() == (0,)
        conn.fail_commit = False
        formula_id = _create(service, _version(TODAY, _term("cycle-test")))
        before = service.list_versions(formula_id)
        conn.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            service.save_version(formula_id, _version(TODAY, _term("cycle-test", coefficient="-2")))
        assert service.list_versions(formula_id) == before
        assert not conn.in_transaction
    finally:
        conn.close()


def test_repository_translates_formula_constraint_violations(
    sqlite_repository: SQLiteRepository,
    formula_service: AggregationFormulaService,
) -> None:
    formula_id = _create(formula_service)
    formula = formula_service.get_formula(formula_id)
    assert formula is not None
    with pytest.raises(ValidationError, match="データベース制約"):
        sqlite_repository.create_aggregation_formula(formula)


@pytest.mark.parametrize(
    ("sql", "value", "message"),
    [
        (
            "UPDATE aggregation_formula_versions SET created_at = ?",
            "not-utc",
            "登録日時",
        ),
        (
            "UPDATE aggregation_formula_versions SET registered_business_date = ?",
            "2026-02-30",
            "登録業務日",
        ),
        ("UPDATE aggregation_formula_versions SET is_initial = ?", 0, "初版履歴"),
        ("UPDATE aggregation_formula_versions SET is_active = ?", 0, "有効状態と項目数"),
        ("UPDATE aggregation_formula_terms SET coefficient = ?", "01", "係数"),
        ("UPDATE measurement_definitions SET unit = ?", "m3", "単位・カテゴリ"),
        ("UPDATE measurement_definitions SET category = ?", "gas", "単位・カテゴリ"),
        ("UPDATE measurement_definitions SET reading_cycle = ?", "weekly", "周期グループ"),
        ("UPDATE measurement_definitions SET calculation_method = ?", "raw_only", "差分計算"),
        (
            "UPDATE measurement_definitions SET value_schema = ?",
            "power_factor_percent_phase",
            "差分計算",
        ),
    ],
)
def test_restore_integrity_rejects_formula_corruption(
    sql: str,
    value: str | int,
    message: str,
    formula_service: AggregationFormulaService,
    memory_conn: sqlite3.Connection,
) -> None:
    _create(formula_service)
    memory_conn.execute(sql, (value,))
    changes = memory_conn.total_changes
    reasons = check_business_integrity(memory_conn)
    assert any(message in reason for reason in reasons)
    assert memory_conn.total_changes == changes


def test_restore_integrity_rejects_missing_versions_and_missing_terms(
    formula_service: AggregationFormulaService, memory_conn: sqlite3.Connection
) -> None:
    _create(formula_service)
    memory_conn.execute("DELETE FROM aggregation_formula_terms")
    assert any("項目数" in reason for reason in check_business_integrity(memory_conn))
    memory_conn.execute("DELETE FROM aggregation_formula_versions")
    assert any("初版履歴" in reason for reason in check_business_integrity(memory_conn))


def test_restore_integrity_rejects_retroactive_noninitial_version(
    formula_service: AggregationFormulaService, memory_conn: sqlite3.Connection
) -> None:
    formula_id = _create(formula_service)
    formula_service.save_version(formula_id, _version(TODAY, _term()))
    memory_conn.execute(
        "UPDATE aggregation_formula_versions SET registered_business_date = ? WHERE is_initial = 0",
        ("2026-02-05",),
    )
    assert any("初版以外" in reason for reason in check_business_integrity(memory_conn))


@pytest.mark.parametrize("coefficient", ["0", "-0.00", "1e2", ".1", "1.", "--1", "1-2"])
def test_sql_constraints_reject_invalid_coefficients(
    coefficient: str, formula_service: AggregationFormulaService, memory_conn: sqlite3.Connection
) -> None:
    _create(formula_service)
    with pytest.raises(sqlite3.IntegrityError):
        memory_conn.execute("UPDATE aggregation_formula_terms SET coefficient = ?", (coefficient,))
    assert check_business_integrity(memory_conn) == []


def test_sql_constraints_protect_references_order_and_initial_identity(
    formula_service: AggregationFormulaService,
    sqlite_repository: SQLiteRepository,
    memory_conn: sqlite3.Connection,
) -> None:
    formula_id = _create(formula_service)
    _add_definition(sqlite_repository, "second")
    for definition_id, order in ((DEFINITION, 1), ("second", 0), ("missing", 2)):
        with pytest.raises(sqlite3.IntegrityError):
            memory_conn.execute(
                "INSERT INTO aggregation_formula_terms "
                "(formula_id, effective_from_date, measurement_definition_id, coefficient, "
                "sort_order) VALUES (?, ?, ?, ?, ?)",
                (formula_id, "2026-01-01", definition_id, "1", order),
            )
    with pytest.raises(sqlite3.IntegrityError):
        memory_conn.execute(
            "INSERT INTO aggregation_formula_versions "
            "(formula_id, effective_from_date, is_active, is_initial, created_at, "
            "registered_business_date) VALUES (?, ?, ?, ?, ?, ?)",
            (formula_id, "2026-03-01", 0, 1, "2026-02-04 03:00:00", "2026-02-04"),
        )
    assert check_business_integrity(memory_conn) == []


@pytest.mark.parametrize(
    ("sql", "value", "message"),
    [
        ("UPDATE aggregation_formulas SET category = ?", "unknown", "集計式属性"),
        ("UPDATE aggregation_formulas SET decimal_places = ?", 11, "小数桁数"),
        ("UPDATE aggregation_formulas SET display_name = ?", " ", "名称"),
        ("UPDATE aggregation_formula_versions SET created_at = ?", "bad", "日付・日時"),
        ("UPDATE aggregation_formula_versions SET is_active = ?", 2, "有効状態"),
        ("UPDATE aggregation_formula_versions SET is_initial = ?", 2, "初版状態"),
        ("UPDATE aggregation_formula_terms SET sort_order = ?", -1, "表示順"),
        ("UPDATE aggregation_formula_terms SET coefficient = ?", "01", "係数"),
    ],
)
def test_corrupt_formula_reads_raise_domain_errors(
    sql: str,
    value: str | int,
    message: str,
    formula_service: AggregationFormulaService,
    memory_conn: sqlite3.Connection,
) -> None:
    formula_id = _create(formula_service)
    memory_conn.execute("PRAGMA ignore_check_constraints = ON")
    memory_conn.execute(sql, (value,))
    memory_conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(FormulaValidationError, match=message):
        formula_service.list_versions(formula_id)
    assert check_business_integrity(memory_conn)


def test_readonly_backup_checks_formula_history_without_changing_candidate(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository], tmp_path: Path
) -> None:
    conn, repository = file_database_repository
    _add_definition(repository, "cycle-test")
    service = AggregationFormulaService(
        repository,
        repository,
        transaction_factory=partial(transaction, conn),
        current_business_date_provider=lambda: TODAY,
        clock=lambda: datetime(2026, 2, 4, tzinfo=UTC),
        id_provider=lambda: "formula",
    )
    formula_id = _create(service, _version(date(2025, 1, 1), _term("cycle-test", "-0.5")))
    service.save_version(formula_id, _version(TODAY, is_active=False))
    candidate = tmp_path / "candidate.db"
    copy_database(conn, candidate)
    before = hashlib.sha256(candidate.read_bytes()).digest()
    readonly = connect_readonly(candidate)
    try:
        assert check_business_integrity(readonly) == []
        assert readonly.total_changes == 0
    finally:
        readonly.close()
    assert hashlib.sha256(candidate.read_bytes()).digest() == before
