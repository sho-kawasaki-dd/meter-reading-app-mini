from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest

from building_manager_mini.core.settings_contracts import CommonSettingsError
from building_manager_mini.core.settings_service import SettingsService
from building_manager_mini.db.migrations import open_database
from building_manager_mini.db.settings_repository import SQLiteSettingsRepository, transaction


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    connection = open_database(tmp_path / "common.db")
    yield connection
    connection.close()


def _service(conn: sqlite3.Connection, *, dependencies: bool = False) -> SettingsService:
    return SettingsService(
        SQLiteSettingsRepository(conn),
        transaction_factory=lambda: transaction(conn),
        has_business_date_dependencies=lambda: dependencies,
    )


def test_staff_lifecycle_preserves_names_and_sender_addresses(conn: sqlite3.Connection) -> None:
    service = _service(conn)
    first = service.create_staff(" 担当者 ", sender_address=" User@Example.com ")
    second = service.create_staff("別人", sender_address="User@Example.com")
    assert first.sender_address == second.sender_address == "User@Example.com"
    assert first.display_name == "担当者"
    edited = service.update_staff(first.staff_id, "改名", is_active=False, sender_address="")
    assert service.get_staff(first.staff_id) == edited
    assert service.list_staff() == [second]
    assert {staff.staff_id for staff in service.list_staff(include_inactive=True)} == {
        first.staff_id,
        second.staff_id,
    }
    assert service.get_staff(str(uuid4())) is None


def test_duplicate_names_and_missing_staff_roll_back(conn: sqlite3.Connection) -> None:
    service = _service(conn)
    first = service.create_staff("担当者")
    second = service.create_staff("別人")
    with pytest.raises(CommonSettingsError, match="重複"):
        service.create_staff("担当者")
    with pytest.raises(CommonSettingsError, match="重複"):
        service.update_staff(second.staff_id, "担当者", is_active=False, sender_address="")
    with pytest.raises(CommonSettingsError, match="見つかりません"):
        service.update_staff(str(uuid4()), "未登録", is_active=True, sender_address="")
    assert service.get_staff(first.staff_id) == first
    assert service.get_staff(second.staff_id) == second
    assert not conn.in_transaction


@pytest.mark.parametrize(
    ("name", "address"),
    [("", ""), ("   ", ""), ("担当者", "not-an-address")],
)
def test_invalid_staff_input_is_rejected(conn: sqlite3.Connection, name: str, address: str) -> None:
    with pytest.raises(CommonSettingsError):
        _service(conn).create_staff(name, sender_address=address)
    assert conn.execute("SELECT COUNT(*) FROM staff").fetchone() == (0,)


@pytest.mark.parametrize("staff_id", ["bad", str(uuid4()).upper(), "0" * 32])
def test_staff_id_must_be_canonical_uuid(conn: sqlite3.Connection, staff_id: str) -> None:
    with pytest.raises(CommonSettingsError, match="担当者 ID"):
        _service(conn).get_staff(staff_id)


def test_timezone_change_requires_no_dependency_and_rejects_bad_values(
    conn: sqlite3.Connection,
) -> None:
    service = _service(conn)
    assert service.get_timezone().key == "Asia/Tokyo"
    assert service.save_timezone(" America/New_York ").key == "America/New_York"
    blocked = _service(conn, dependencies=True)
    assert blocked.save_timezone("America/New_York").key == "America/New_York"
    with pytest.raises(CommonSettingsError, match="変更できません"):
        blocked.save_timezone("Asia/Tokyo")
    with pytest.raises(CommonSettingsError, match="不正"):
        service.save_timezone("Invalid/Zone")
    assert service.get_timezone().key == "America/New_York"
    assert not conn.in_transaction


def test_dependency_check_failure_preserves_timezone(conn: sqlite3.Connection) -> None:
    def fail() -> bool:
        raise RuntimeError("検査不能")

    service = SettingsService(
        SQLiteSettingsRepository(conn),
        transaction_factory=lambda: transaction(conn),
        has_business_date_dependencies=fail,
    )
    with pytest.raises(RuntimeError, match="検査不能"):
        service.save_timezone("America/New_York")
    assert service.get_timezone().key == "Asia/Tokyo"
    assert not conn.in_transaction


def test_missing_settings_are_never_silently_created(conn: sqlite3.Connection) -> None:
    repo = SQLiteSettingsRepository(conn)
    conn.execute("DELETE FROM common_settings")
    with pytest.raises(CommonSettingsError, match="見つかりません"):
        repo.get_timezone()
    with pytest.raises(CommonSettingsError, match="見つかりません"):
        repo.set_timezone("Asia/Tokyo")


def test_nested_transactions_are_rejected(conn: sqlite3.Connection) -> None:
    with transaction(conn), pytest.raises(CommonSettingsError, match="ネスト"), transaction(conn):
        pass


def test_commit_failure_rolls_back_every_change(conn: sqlite3.Connection) -> None:
    def authorize(
        action: int, arg1: str | None, _arg2: str | None, _db: str | None, _source: str | None
    ) -> int:
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    with pytest.raises(CommonSettingsError, match="共通 DB の操作"):
        _service(conn).create_staff("担当者")
    conn.set_authorizer(None)
    assert conn.execute("SELECT COUNT(*) FROM staff").fetchone() == (0,)
    assert not conn.in_transaction


def test_repository_translates_database_error(conn: sqlite3.Connection) -> None:
    repo = SQLiteSettingsRepository(conn)
    conn.execute("DROP TABLE staff")
    with pytest.raises(CommonSettingsError, match="共通 DB の操作"):
        repo.list_staff(include_inactive=True)
