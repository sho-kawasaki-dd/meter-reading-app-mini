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
    PlannedReadingTaskState,
    ReadingUpsertRequest,
    ReadingWriteService,
    UpsertDisposition,
    UpsertOutcome,
)
from meter_reading_mini.db.connection import transaction
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
        assignment_repository=repository,
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


def test_internal_sync_preserves_source_rows_and_pre_write_states(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    for slot, hour in (("existing", 4), ("revived", 5), ("new", 6)):
        create_plan(repository, slot, _scheduled(hour))
    service.sync(sync_request(mobile_submission("existing", "10")), confirmation_context())
    conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', "
        "closed_at = ?, close_reason = 'cutoff_elapsed' WHERE planned_reading_id = ?",
        ("2026-02-04 07:00:00", "revived"),
    )
    request = sync_request(
        mobile_submission("missing", "1"),
        mobile_submission("revived", "20"),
        mobile_submission("existing", "12"),
        mobile_submission("new", "30"),
    )

    with transaction(conn):
        batch = service.sync_in_transaction(
            request, confirmation_context(), source_row_numbers=(2, 4, 7, 9)
        )
        assert conn.in_transaction

    assert batch.response.accepted_reading_items == 3
    assert batch.response.rejected_reading_items[0].item_index == 0
    assert [
        (
            item.item_index,
            item.source_row_number,
            item.planned_reading_id,
            item.task_state_before,
            item.disposition,
            item.reject_code is not None,
        )
        for item in batch.reading_items
    ] == [
        (0, 2, "missing", None, None, True),
        (1, 4, "revived", PlannedReadingTaskState.AUTO_CLOSED, UpsertDisposition.INSERTED, False),
        (
            2,
            7,
            "existing",
            PlannedReadingTaskState.COMPLETED,
            UpsertDisposition.REPLACED_ON_TIE,
            False,
        ),
        (3, 9, "new", PlannedReadingTaskState.OPEN, UpsertDisposition.INSERTED, False),
    ]


@pytest.mark.parametrize("source_rows", [(1,), (1, 1), (0, 2)])
def test_internal_sync_rejects_invalid_source_row_mapping(
    file_database_repository: tuple,
    source_rows: tuple[int, ...],
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    request = sync_request(mobile_submission("one", "1"), mobile_submission("two", "2"))
    with transaction(conn), pytest.raises(ValueError, match="行番号"):
        service.sync_in_transaction(request, confirmation_context(), source_row_numbers=source_rows)


def test_internal_sync_requires_an_existing_transaction(file_database_repository: tuple) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    with pytest.raises(RuntimeError, match="トランザクション"):
        service.sync_in_transaction(sync_request(), confirmation_context())
