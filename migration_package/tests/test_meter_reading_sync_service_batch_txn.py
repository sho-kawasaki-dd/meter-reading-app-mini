from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import cast

import pytest
from reading_service_helpers import (
    build_services,
    confirmation_context,
    create_plan,
    mobile_submission,
    sync_request,
)

from meter_reading_mini.core.meter_reading_sync import MeterReadingSyncService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ReadingUpsertRequest,
    ReadingWriteService,
    UpsertOutcome,
)
from meter_reading_mini.db.repositories import SQLiteRepository


def _scheduled(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, 0, tzinfo=UTC)


def _build_sync_with_connection(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    writer: ReadingWriteService,
) -> MeterReadingSyncService:
    return MeterReadingSyncService(
        conn,
        repository,
        repository,
        writer,
        repository,
        repository,
        replacement_reflection_repository=repository,
    )


def test_unexpected_error_rolls_back_every_prior_item(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    service = _build_sync_with_connection(conn, repository, writer)
    create_plan(repository, "plan-accepted", _scheduled(4))
    create_plan(repository, "plan-fails", _scheduled(5))
    original_upsert = writer.upsert_reading

    def write_then_fail(request: ReadingUpsertRequest) -> UpsertOutcome:
        outcome = original_upsert(request)
        if request.planned_reading_id == "plan-fails":
            raise RuntimeError("予期しない障害")
        return outcome

    monkeypatch.setattr(writer, "upsert_reading", write_then_fail)
    with pytest.raises(RuntimeError, match="予期しない障害"):
        service.sync(
            sync_request(
                mobile_submission("plan-accepted", "10"),
                mobile_submission("plan-fails", "20"),
            ),
            confirmation_context(),
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM reading_transactions WHERE planned_reading_id IN (?, ?)",
        ("plan-accepted", "plan-fails"),
    ).fetchone() == (0,)


class _FailingCommitConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql == "COMMIT":
            raise sqlite3.OperationalError("simulated COMMIT failure")
        return self._conn.execute(sql, parameters)


def test_commit_failure_rolls_back_completed_batch(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    create_plan(repository, "plan-commit-fails", _scheduled(4))
    failing_conn = cast(sqlite3.Connection, _FailingCommitConnection(conn))
    service = _build_sync_with_connection(failing_conn, repository, writer)

    with pytest.raises(sqlite3.OperationalError, match="simulated COMMIT failure"):
        service.sync(
            sync_request(mobile_submission("plan-commit-fails", "10")),
            confirmation_context(),
        )

    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT COUNT(*) FROM reading_transactions WHERE planned_reading_id = 'plan-commit-fails'"
    ).fetchone() == (0,)
