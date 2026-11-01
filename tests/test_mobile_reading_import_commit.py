from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import cast

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    SHEET_ID,
    STAFF_ID,
    build_services,
    confirmation_context,
    create_plan,
)

from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    MeterReadingSubmission,
    MeterReadingSyncRequest,
    Purpose,
    ReadingCycle,
    ResultStatus,
    SyncItemRejectCode,
    ValueSchema,
)
from meter_reading_mini.core.errors import InactiveMasterError, ValidationError
from meter_reading_mini.core.meter_reading_sync import (
    MeterReadingSyncService,
    ReadingSyncBatchResult,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    CalculatedUsageUpdate,
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    PlannedReadingTaskState,
    ReadingConfirmationContext,
    ReadingUpsertRequest,
    ReplacementBoundaryCalculationUpdate,
    ReplacementDefinitionReading,
    ReplacementUpsertRequest,
    UpsertDisposition,
    UpsertOutcome,
)
from meter_reading_mini.core.mobile_reading import MobileReadingResult, SheetFilter
from meter_reading_mini.core.mobile_reading_import import (
    MobileReadingDisposition,
    MobileReadingImportService,
    MobileReadingNothingToCommitError,
    MobileReadingPreviewChangedError,
    MobileReadingRejectCode,
    MobileReadingRejectReason,
    describe_mobile_reading_rejection,
)
from meter_reading_mini.core.reading_signature import verify_meter_reading_signature
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository


@dataclass(frozen=True)
class ImportServices:
    conn: sqlite3.Connection
    repository: SQLiteRepository
    writer: ReadingWriteOrchestrationService
    sync: MeterReadingSyncService
    importer: MobileReadingImportService


@pytest.fixture
def services(
    memory_conn: sqlite3.Connection, sqlite_repository: SQLiteRepository
) -> ImportServices:
    writer, sync = build_services(memory_conn, sqlite_repository)
    return ImportServices(
        memory_conn,
        sqlite_repository,
        writer,
        sync,
        MobileReadingImportService(
            sqlite_repository,
            sync_service=sync,
            transaction_factory=partial(transaction, memory_conn),
        ),
    )


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


def _entry(
    slot: str,
    raw: str | None,
    hour: int,
    *,
    updated_at: datetime | None = None,
    status: ResultStatus = ResultStatus.MEASURED,
    note: str | None = None,
    structured: dict[str, object] | None = None,
) -> MeterReadingSubmission:
    return MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": slot,
            "result_status": status,
            "raw_reading": raw,
            "structured_payload": structured,
            "recorded_at": _at(hour),
            "client_updated_at": updated_at or datetime(2026, 2, 6, hour, tzinfo=UTC),
            "note": note,
        }
    )


def _result(*entries: MeterReadingSubmission) -> MobileReadingResult:
    return MobileReadingResult(sheet_id=SHEET_ID, entries=entries)


def _save_mobile(services: ImportServices, *entries: MeterReadingSubmission) -> None:
    response = services.sync.sync(
        MeterReadingSyncRequest(reading_items=list(entries), replacement_items=[]),
        confirmation_context(),
    )
    assert response.accepted_reading_items == len(entries)


def _save_manual(services: ImportServices, slot: str, raw: str, hour: int) -> None:
    services.writer.save_manual_reading(
        ManualReadingInput(slot, ResultStatus.MEASURED, _at(hour), Decimal(raw), None),
        staff_id=STAFF_ID,
    )


def _create_structured_definition(services: ImportServices) -> None:
    services.repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="structured",
            location_id=LOCATION_ID,
            display_name="力率",
            category=Category.WATER,
            unit="%",
            value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            multiplier=Decimal("1"),
            decimal_places=None,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY,
            reading_cycle=ReadingCycle.MONTHLY,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=date(2026, 2, 1),
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )


def _replace_meter(
    services: ImportServices, initial: str | None, *, include_structured: bool = False
) -> None:
    services.repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id="new-meter",
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name="交換後メーター",
            category=Category.WATER,
        )
    )
    request = ReplacementUpsertRequest(
        replacement_event_id="replacement",
        location_id=LOCATION_ID,
        old_meter_id=METER_ID,
        new_meter_id="new-meter",
        replaced_at=_at(6),
        client_updated_at=_at(6),
        old_final_reading=None,
        new_initial_reading=None,
        updated_by="試験担当",
        note=None,
        definition_readings=(
            ReplacementDefinitionReading(
                DEFINITION_ID, Decimal("120"), Decimal(initial) if initial is not None else None
            ),
        )
        + ((ReplacementDefinitionReading("structured", None, None),) if include_structured else ()),
    )
    with transaction(services.conn):
        services.repository.upsert_replacement(request)
        services.repository.reflect_replacement(request)
        services.writer.recalculate_after_replacement(request)


def test_commit_saves_only_accepted_rows_and_reports_original_rows(
    services: ImportServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    for slot, hour in (
        ("previous", 3),
        ("early", 8),
        ("revived", 10),
        ("duplicate", 14),
        ("stale", 15),
        ("overwrite", 16),
    ):
        create_plan(services.repository, slot, _at(hour))
    _save_manual(services, "previous", "100", 3)
    duplicate = _entry("duplicate", "120", 14, note="既存")
    stale = _entry("stale", "130", 15)
    overwrite = _entry("overwrite", "140", 16)
    _save_mobile(services, duplicate, stale, overwrite)
    before_duplicate = services.repository.get_mobile_reading_import_targets(["duplicate"])[
        "duplicate"
    ].existing_result
    before_stale = services.repository.get_mobile_reading_import_targets(["stale"])[
        "stale"
    ].existing_result
    assert before_duplicate is not None
    assert before_stale is not None
    services.conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', close_reason = 'cutoff_elapsed', "
        "closed_at = ? WHERE planned_reading_id = ?",
        ("2026-02-04 07:00:00", "revived"),
    )
    result = _result(
        _entry("missing", "1", 4),
        duplicate,
        _entry("early", "110", 8),
        _entry("stale", "131", 15, updated_at=stale.client_updated_at - timedelta(seconds=1)),
        _entry("revived", "115", 10),
        _entry("overwrite", "145", 16),
    )
    preview = services.importer.preview(result)
    written: list[str] = []
    original_upsert = services.writer.upsert_reading

    def record_write(request: ReadingUpsertRequest) -> UpsertOutcome:
        written.append(request.planned_reading_id)
        return original_upsert(request)

    monkeypatch.setattr(services.writer, "upsert_reading", record_write)
    committed = services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert written == ["early", "revived", "overwrite"]
    assert [row.row_number for row in committed.rows] == [1, 2, 3, 4, 5, 6]
    assert [row.disposition for row in committed.rows] == [
        MobileReadingDisposition.REJECTED,
        MobileReadingDisposition.DUPLICATE,
        MobileReadingDisposition.NEW,
        MobileReadingDisposition.STALE,
        MobileReadingDisposition.REVIVED,
        MobileReadingDisposition.OVERWRITTEN,
    ]
    assert [row.save_disposition for row in committed.rows] == [
        None,
        None,
        UpsertDisposition.INSERTED,
        None,
        UpsertDisposition.INSERTED,
        UpsertDisposition.REPLACED_ON_TIE,
    ]
    assert committed.rows[0].reason_message == "予定枠が見つかりません"
    summary = committed.summary
    assert (
        summary.confirmed,
        summary.new,
        summary.revived,
        summary.overwritten,
        summary.duplicate,
        summary.stale,
        summary.rejected,
    ) == (3, 1, 1, 1, 1, 1, 1)
    assert summary.recalculated_existing == 2
    assert summary.message == (
        "確定 3 件、重複 1 件、古い入力 1 件、拒否 1 件\n拒否理由: 予定枠が見つかりません 1 件"
    )
    assert [
        (reason.code, reason.message, reason.count) for reason in summary.rejection_reasons
    ] == [(MobileReadingRejectCode.UNKNOWN_PLANNED_READING, "予定枠が見つかりません", 1)]

    targets = services.repository.get_mobile_reading_import_targets(
        ["early", "revived", "duplicate", "stale", "overwrite"]
    )
    after_duplicate = targets["duplicate"].existing_result
    assert after_duplicate is not None
    assert (after_duplicate.last_reading, after_duplicate.usage_delta) == (
        Decimal("115"),
        Decimal("5"),
    )
    assert (
        after_duplicate.signature,
        after_duplicate.updated_by,
        after_duplicate.device_id,
        after_duplicate.arrival_sequence,
    ) == (
        before_duplicate.signature,
        before_duplicate.updated_by,
        before_duplicate.device_id,
        before_duplicate.arrival_sequence,
    )
    after_stale = targets["stale"].existing_result
    assert after_stale is not None
    assert (
        after_stale.raw_reading,
        after_stale.signature,
        after_stale.updated_by,
        after_stale.device_id,
        after_stale.arrival_sequence,
    ) == (
        before_stale.raw_reading,
        before_stale.signature,
        before_stale.updated_by,
        before_stale.device_id,
        before_stale.arrival_sequence,
    )
    for slot in ("early", "revived", "overwrite"):
        stored = targets[slot].existing_result
        assert stored is not None
        assert stored.device_id == SHEET_ID
        assert stored.updated_by == "試験担当"
        assert stored.result_source == "mobile_html"
        verify_meter_reading_signature(
            planned_reading_id=slot,
            result_status=stored.result_status,
            raw_reading=stored.raw_reading,
            structured_payload_json=None,
            recorded_at=stored.recorded_at,
            device_id=stored.device_id,
            signature=stored.signature,
        )
    assert targets["revived"].task_state == "completed"
    assert services.conn.execute(
        "SELECT COUNT(*) FROM reading_transactions WHERE planned_reading_id = ?", ("missing",)
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    "change",
    [
        "sheet",
        "date",
        "generated",
        "filter",
        "order",
        "raw",
        "status",
        "note",
        "recorded",
        "updated",
    ],
)
def test_commit_rejects_input_changed_after_preview(services: ImportServices, change: str) -> None:
    create_plan(services.repository, "first", _at(8))
    create_plan(services.repository, "second", _at(14))
    result = _result(_entry("first", "110", 8), _entry("second", "120", 14))
    preview = services.importer.preview(result)
    if change == "sheet":
        changed = replace(result, sheet_id="b" * 32)
    elif change == "date":
        changed = replace(result, business_date=date(2026, 2, 4))
    elif change == "generated":
        changed = replace(result, generated_at="2026-02-04 00:00:00")
    elif change == "filter":
        changed = replace(result, filter=SheetFilter.create(["14:00"]))
    elif change == "order":
        changed = replace(result, entries=tuple(reversed(result.entries)))
    else:
        entry = result.entries[0]
        updates: dict[str, dict[str, object]] = {
            "raw": {"raw_reading": Decimal("111")},
            "status": {"result_status": ResultStatus.SKIPPED, "raw_reading": None},
            "note": {"note": "変更"},
            "recorded": {"recorded_at": entry.recorded_at + timedelta(microseconds=1)},
            "updated": {"client_updated_at": entry.client_updated_at + timedelta(seconds=1)},
        }
        changed_entry = entry.model_copy(update=updates[change])
        changed = replace(result, entries=(changed_entry, result.entries[1]))
    before = tuple(services.conn.iterdump())

    with pytest.raises(MobileReadingPreviewChangedError, match="再度プレビュー"):
        services.importer.commit(changed, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before
    assert not services.conn.in_transaction


@pytest.mark.parametrize(
    "change", ["target", "previous", "following", "assignment", "new_following"]
)
def test_commit_rejects_relevant_database_changes(services: ImportServices, change: str) -> None:
    for slot, hour in (("previous", 3), ("target", 8), ("following", 14)):
        create_plan(services.repository, slot, _at(hour))
    _save_manual(services, "previous", "100", 3)
    _save_manual(services, "target", "105", 8)
    _save_manual(services, "following", "120", 14)
    result = _result(_entry("target", "110", 8))
    preview = services.importer.preview(result)
    if change == "target":
        services.conn.execute(
            "UPDATE reading_transactions SET raw_reading = ? WHERE planned_reading_id = ?",
            ("106", "target"),
        )
    elif change == "previous":
        services.conn.execute(
            "UPDATE reading_transactions SET raw_reading = ? WHERE planned_reading_id = ?",
            ("101", "previous"),
        )
    elif change == "following":
        services.conn.execute(
            "UPDATE reading_transactions SET last_reading = ? WHERE planned_reading_id = ?",
            ("99", "following"),
        )
    elif change == "assignment":
        services.conn.execute(
            "UPDATE meter_assignments SET multiplier = ? WHERE measurement_definition_id = ?",
            ("2", DEFINITION_ID),
        )
    else:
        create_plan(services.repository, "new-following", _at(16))
        _save_manual(services, "new-following", "130", 16)
    before = tuple(services.conn.iterdump())

    with pytest.raises(MobileReadingPreviewChangedError):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before
    assert not services.conn.in_transaction


@pytest.mark.parametrize("part", ["input", "database", "rows", "summary", "effects"])
def test_commit_compares_every_part_of_expected_preview(
    services: ImportServices, part: str
) -> None:
    create_plan(services.repository, "target", _at(8))
    result = _result(_entry("target", "10", 8))
    preview = services.importer.preview(result)
    if part == "input":
        expected = replace(preview, input=replace(preview.input, sheet_id="b" * 32))
    elif part == "database":
        expected = replace(preview, database=replace(preview.database, reads=frozenset()))
    elif part == "rows":
        expected = replace(preview, rows=())
    elif part == "summary":
        expected = replace(preview, summary=replace(preview.summary, new=99))
    else:
        expected = replace(preview, effects=replace(preview.effects, results=()))
    before = tuple(services.conn.iterdump())

    with pytest.raises(MobileReadingPreviewChangedError):
        services.importer.commit(result, expected, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before


def test_unrelated_database_change_does_not_prevent_commit(services: ImportServices) -> None:
    _create_structured_definition(services)
    create_plan(services.repository, "target", _at(8))
    result = _result(_entry("target", "10", 8))
    preview = services.importer.preview(result)
    services.conn.execute(
        "UPDATE measurement_definitions SET display_name = ? WHERE measurement_definition_id = ?",
        ("別の名前", "structured"),
    )
    services.conn.execute(
        "UPDATE meter_assignments SET multiplier = ? WHERE measurement_definition_id = ?",
        ("9", "structured"),
    )

    committed = services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert committed.summary.confirmed == 1
    assert (
        services.repository.get_mobile_reading_import_targets(["target"])["target"].existing_result
        is not None
    )


@pytest.mark.parametrize("staff_case", ["missing", "inactive", "blank"])
def test_commit_validates_staff_before_repreview(
    services: ImportServices, monkeypatch: pytest.MonkeyPatch, staff_case: str
) -> None:
    create_plan(services.repository, "target", _at(8))
    result = _result(_entry("target", "10", 8))
    preview = services.importer.preview(result)
    staff_id = STAFF_ID
    if staff_case == "missing":
        staff_id = "9999"
    elif staff_case == "inactive":
        services.conn.execute("UPDATE staff SET is_active = 0 WHERE id = ?", (STAFF_ID,))
    else:
        services.conn.execute("UPDATE staff SET name = '  ' WHERE id = ?", (STAFF_ID,))
    before = tuple(services.conn.iterdump())

    def unexpected_preview(_: MobileReadingResult) -> None:
        pytest.fail("無効な担当者では再プレビューを実行しません")

    monkeypatch.setattr(services.importer, "preview", unexpected_preview)
    with pytest.raises(InactiveMasterError):
        services.importer.commit(result, preview, staff_id=staff_id)

    assert tuple(services.conn.iterdump()) == before
    assert not services.conn.in_transaction


def test_commit_rejects_zero_accepted_rows(services: ImportServices) -> None:
    result = _result(_entry("missing", "1", 8))
    preview = services.importer.preview(result)
    assert not preview.can_commit
    before = tuple(services.conn.iterdump())

    with pytest.raises(MobileReadingNothingToCommitError, match="確定できる検針結果"):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before


def test_preview_only_service_fails_explicitly_when_commit_is_not_configured(
    services: ImportServices,
) -> None:
    create_plan(services.repository, "target", _at(8))
    importer = MobileReadingImportService(services.repository)
    result = _result(_entry("target", "10", 8))
    preview = importer.preview(result)

    with pytest.raises(RuntimeError, match="同期サービスとトランザクション"):
        importer.commit(result, preview, staff_id=STAFF_ID)


def test_nested_structured_input_is_frozen_and_mutation_prevents_commit(
    services: ImportServices,
) -> None:
    _create_structured_definition(services)
    create_plan(services.repository, "target", _at(8))
    create_plan(services.repository, "factor", _at(14), measurement_definition_id="structured")
    factor = _entry(
        "factor",
        None,
        14,
        structured={"percent": "95", "phase": "lag", "nested": {"values": [1, {"x": "old"}]}},
    )
    result = _result(factor, _entry("target", "10", 8))
    preview = services.importer.preview(result)
    assert preview.summary.accepted == 1
    frozen_json = preview.input.entries[0].structured_payload_json
    assert factor.structured_payload is not None
    factor.structured_payload["nested"]["values"][1]["x"] = "new"
    assert preview.input.entries[0].structured_payload_json == frozen_json
    before = tuple(services.conn.iterdump())

    with pytest.raises(MobileReadingPreviewChangedError):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before


@pytest.mark.parametrize("failure", ["rejected", "ignored", "wrong_disposition", "unexpected"])
def test_commit_rolls_back_all_rows_on_write_failure(
    services: ImportServices, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    for slot, hour in (("first", 8), ("second", 14)):
        create_plan(services.repository, slot, _at(hour))
    result = _result(_entry("first", "10", 8), _entry("second", "20", 14))
    preview = services.importer.preview(result)
    before = tuple(services.conn.iterdump())
    original_upsert = services.writer.upsert_reading

    def fail_after_write(request: ReadingUpsertRequest) -> UpsertOutcome:
        outcome = original_upsert(request)
        if request.planned_reading_id == "second":
            if failure == "rejected":
                raise ValidationError("保存時の業務拒否")
            if failure == "ignored":
                return UpsertOutcome(UpsertDisposition.IGNORED_STALE)
            if failure == "wrong_disposition":
                return UpsertOutcome(UpsertDisposition.UPDATED)
            raise RuntimeError("予期しない障害")
        return outcome

    monkeypatch.setattr(services.writer, "upsert_reading", fail_after_write)
    error = RuntimeError if failure == "unexpected" else MobileReadingPreviewChangedError
    with pytest.raises(error):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before
    assert not services.conn.in_transaction


@pytest.mark.parametrize(
    "fault",
    ["count", "dropped", "index", "source_row", "planned_id", "state", "reject_code", "none"],
)
def test_commit_rejects_mismatched_individual_sync_results(
    services: ImportServices, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    create_plan(services.repository, "first", _at(8))
    create_plan(services.repository, "second", _at(14))
    result = _result(
        _entry("missing", "1", 3), _entry("first", "10", 8), _entry("second", "20", 14)
    )
    preview = services.importer.preview(result)
    before = tuple(services.conn.iterdump())
    original_sync = services.sync.sync_in_transaction

    def corrupt_batch(
        request: MeterReadingSyncRequest,
        confirmation: ReadingConfirmationContext,
        *,
        source_row_numbers: Sequence[int] | None = None,
    ) -> ReadingSyncBatchResult:
        batch = original_sync(request, confirmation, source_row_numbers=source_row_numbers)
        items = list(batch.reading_items)
        response = batch.response
        if fault == "count":
            response = response.model_copy(update={"accepted_reading_items": 1})
        elif fault == "dropped":
            items.pop()
        elif fault == "index":
            items[0] = replace(items[0], item_index=1)
        elif fault == "source_row":
            items[0] = replace(items[0], source_row_number=1)
        elif fault == "planned_id":
            items[0] = replace(items[0], planned_reading_id="other")
        elif fault == "state":
            items[0] = replace(items[0], task_state_before=PlannedReadingTaskState.COMPLETED)
        elif fault == "reject_code":
            items[0] = replace(items[0], reject_code=SyncItemRejectCode.INVALID_READING_SUBMISSION)
        else:
            items[0] = replace(items[0], disposition=None)
        return replace(batch, response=response, reading_items=tuple(items))

    monkeypatch.setattr(services.sync, "sync_in_transaction", corrupt_batch)
    with pytest.raises(MobileReadingPreviewChangedError):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before
    assert not services.conn.in_transaction


@pytest.mark.parametrize(
    "field",
    [
        "device",
        "staff",
        "signature",
        "source",
        "raw",
        "last",
        "usage",
        "arrival",
        "closed_at",
        "missing_result",
        "missing_target",
    ],
)
def test_commit_rolls_back_when_a_saved_target_differs_from_projection(
    services: ImportServices, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    create_plan(services.repository, "first", _at(8))
    create_plan(services.repository, "second", _at(14))
    result = _result(_entry("first", "10", 8), _entry("second", "20", 14))
    preview = services.importer.preview(result)
    before = tuple(services.conn.iterdump())
    original_upsert = services.writer.upsert_reading
    mutations: dict[str, tuple[str, tuple[object, ...]]] = {
        "device": (
            "UPDATE reading_transactions SET device_id = ? WHERE planned_reading_id = ?",
            ("b" * 32, "second"),
        ),
        "staff": (
            "UPDATE reading_transactions SET updated_by = ? WHERE planned_reading_id = ?",
            ("別担当者", "second"),
        ),
        "signature": (
            "UPDATE reading_transactions SET signature = ? WHERE planned_reading_id = ?",
            ("誤署名", "second"),
        ),
        "source": (
            "UPDATE reading_transactions SET result_source = ? WHERE planned_reading_id = ?",
            ("manual", "second"),
        ),
        "raw": (
            "UPDATE reading_transactions SET raw_reading = ? WHERE planned_reading_id = ?",
            ("21", "second"),
        ),
        "last": (
            "UPDATE reading_transactions SET last_reading = ? WHERE planned_reading_id = ?",
            ("9", "second"),
        ),
        "usage": (
            "UPDATE reading_transactions SET usage_delta = ? WHERE planned_reading_id = ?",
            ("99", "second"),
        ),
        "arrival": (
            "UPDATE reading_transactions SET arrival_sequence = ? WHERE planned_reading_id = ?",
            (9, "second"),
        ),
        "closed_at": (
            "UPDATE planned_readings SET closed_at = ? WHERE planned_reading_id = ?",
            ("2026-02-04 13:00:00", "second"),
        ),
        "missing_result": (
            "DELETE FROM reading_transactions WHERE planned_reading_id = ?",
            ("second",),
        ),
        "missing_target": (
            "DELETE FROM reading_transactions WHERE planned_reading_id = ?",
            ("second",),
        ),
    }

    def corrupt_after_write(request: ReadingUpsertRequest) -> UpsertOutcome:
        outcome = original_upsert(request)
        if request.planned_reading_id == "second":
            sql, parameters = mutations[field]
            services.conn.execute(sql, parameters)
            if field == "missing_target":
                services.conn.execute(
                    "DELETE FROM planned_readings WHERE planned_reading_id = ?", ("second",)
                )
        return outcome

    monkeypatch.setattr(services.writer, "upsert_reading", corrupt_after_write)
    with pytest.raises(MobileReadingPreviewChangedError):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before
    assert not services.conn.in_transaction


@pytest.mark.parametrize("field", ["last", "usage", "signature", "arrival", "task_state"])
def test_commit_verifies_indirectly_recalculated_existing_result(
    services: ImportServices, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    for slot, hour in (("previous", 3), ("target", 8), ("following", 14)):
        create_plan(services.repository, slot, _at(hour))
    _save_manual(services, "previous", "100", 3)
    _save_manual(services, "following", "120", 14)
    result = _result(_entry("target", "110", 8))
    preview = services.importer.preview(result)
    assert preview.effects.recalculated_existing_ids == frozenset({"following"})
    before = tuple(services.conn.iterdump())
    original_upsert = services.writer.upsert_reading
    mutations: dict[str, tuple[str, tuple[object, ...]]] = {
        "last": (
            "UPDATE reading_transactions SET last_reading = ? WHERE planned_reading_id = ?",
            ("101", "following"),
        ),
        "usage": (
            "UPDATE reading_transactions SET usage_delta = ? WHERE planned_reading_id = ?",
            ("99", "following"),
        ),
        "signature": (
            "UPDATE reading_transactions SET signature = ? WHERE planned_reading_id = ?",
            ("誤署名", "following"),
        ),
        "arrival": (
            "UPDATE reading_transactions SET arrival_sequence = ? WHERE planned_reading_id = ?",
            (9, "following"),
        ),
        "task_state": (
            "UPDATE planned_readings SET task_state = 'auto_closed', close_reason = "
            "'cutoff_elapsed' WHERE planned_reading_id = ?",
            ("following",),
        ),
    }

    def corrupt_after_write(request: ReadingUpsertRequest) -> UpsertOutcome:
        outcome = original_upsert(request)
        sql, parameters = mutations[field]
        services.conn.execute(sql, parameters)
        return outcome

    monkeypatch.setattr(services.writer, "upsert_reading", corrupt_after_write)
    with pytest.raises(MobileReadingPreviewChangedError):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before


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


def test_commit_failure_rolls_back_verified_writes(services: ImportServices) -> None:
    create_plan(services.repository, "first", _at(8))
    create_plan(services.repository, "second", _at(14))
    failing_conn = cast(sqlite3.Connection, _FailingCommitConnection(services.conn))
    importer = MobileReadingImportService(
        services.repository,
        sync_service=services.sync,
        transaction_factory=partial(transaction, failing_conn),
    )
    result = _result(_entry("first", "10", 8), _entry("second", "20", 14))
    preview = importer.preview(result)
    before = tuple(services.conn.iterdump())

    with pytest.raises(sqlite3.OperationalError, match="simulated COMMIT failure"):
        importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before
    assert not services.conn.in_transaction


def test_commit_preserves_payload_order_but_checks_final_recalculated_values(
    services: ImportServices,
) -> None:
    for slot, hour in (("previous", 3), ("early", 8), ("late", 14)):
        create_plan(services.repository, slot, _at(hour))
    _save_manual(services, "previous", "100", 3)
    result = _result(_entry("late", "130", 14), _entry("early", "110", 8))
    preview = services.importer.preview(result)

    committed = services.importer.commit(result, preview, staff_id=STAFF_ID)

    targets = services.repository.get_mobile_reading_import_targets(["early", "late"])
    early = targets["early"].existing_result
    late = targets["late"].existing_result
    assert committed.summary.confirmed == 2
    assert committed.summary.message == "確定 2 件、重複 0 件、古い入力 0 件、拒否 0 件"
    assert [row.planned_reading_id for row in committed.rows] == ["late", "early"]
    assert early is not None and late is not None
    assert (early.last_reading, early.usage_delta) == (Decimal("100"), Decimal("10"))
    assert (late.last_reading, late.usage_delta) == (Decimal("110"), Decimal("20"))


@pytest.mark.parametrize("initial", ["5", None])
@pytest.mark.parametrize("order", [("early", "late"), ("late", "early")])
def test_commit_replacement_initial_and_virtual_previous(
    services: ImportServices, initial: str | None, order: tuple[str, str]
) -> None:
    create_plan(services.repository, "old", _at(3))
    _save_manual(services, "old", "100", 3)
    _replace_meter(services, initial)
    create_plan(services.repository, "early", _at(8), meter_id="new-meter")
    create_plan(services.repository, "late", _at(14), meter_id="new-meter")
    entries = {"early": _entry("early", "8", 8), "late": _entry("late", "12", 14)}
    result = _result(*(entries[slot] for slot in order))
    preview = services.importer.preview(result)

    committed = services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert committed.summary.confirmed == 2
    targets = services.repository.get_mobile_reading_import_targets(["early", "late"])
    early = targets["early"].existing_result
    late = targets["late"].existing_result
    assert early is not None and late is not None
    assert (early.last_reading, early.usage_delta) == (
        Decimal("5") if initial is not None else None,
        Decimal("3") if initial is not None else Decimal("0"),
    )
    assert (late.last_reading, late.usage_delta) == (Decimal("8"), Decimal("4"))


@pytest.mark.parametrize("initial", ["5", None])
def test_commit_nonmeasured_first_result_recalculates_next_from_boundary(
    services: ImportServices, initial: str | None
) -> None:
    _replace_meter(services, initial)
    create_plan(services.repository, "early", _at(8), meter_id="new-meter")
    create_plan(services.repository, "late", _at(14), meter_id="new-meter")
    early = _entry("early", "8", 8)
    _save_mobile(services, early, _entry("late", "12", 14))
    before_late = services.repository.get_mobile_reading_import_targets(["late"])[
        "late"
    ].existing_result
    assert before_late is not None
    result = _result(
        _entry(
            "early",
            None,
            8,
            status=ResultStatus.SKIPPED,
            updated_at=early.client_updated_at + timedelta(seconds=1),
        )
    )
    preview = services.importer.preview(result)
    assert preview.summary.recalculated_existing == 1

    committed = services.importer.commit(result, preview, staff_id=STAFF_ID)

    after_late = services.repository.get_mobile_reading_import_targets(["late"])[
        "late"
    ].existing_result
    assert committed.summary.overwritten == 1
    assert after_late is not None
    assert (after_late.last_reading, after_late.usage_delta) == (
        Decimal("5") if initial is not None else None,
        Decimal("7") if initial is not None else Decimal("0"),
    )
    assert (after_late.signature, after_late.updated_by, after_late.arrival_sequence) == (
        before_late.signature,
        before_late.updated_by,
        before_late.arrival_sequence,
    )


def _boundary_setup(services: ImportServices) -> MobileReadingResult:
    create_plan(services.repository, "old", _at(3))
    create_plan(services.repository, "target", _at(4))
    _save_manual(services, "old", "100", 3)
    _replace_meter(services, "5")
    create_plan(services.repository, "new", _at(8), meter_id="new-meter")
    _save_mobile(services, _entry("new", "8", 8))
    return _result(_entry("target", "110", 4))


def test_commit_verifies_and_saves_replacement_boundary_effects(services: ImportServices) -> None:
    result = _boundary_setup(services)
    preview = services.importer.preview(result)
    assert preview.summary.recalculated_boundaries == 1
    assert preview.summary.recalculated_existing == 1
    expected = preview.effects.boundary_updates[0]

    committed = services.importer.commit(result, preview, staff_id=STAFF_ID)

    actual = services.repository.list_replacement_boundaries_for_event("replacement")[0]
    assert committed.summary.recalculated_boundaries == 1
    assert (
        actual.previous_measured_reading,
        actual.previous_measured_planned_reading_id,
        actual.old_meter_usage_delta,
    ) == (
        expected.previous_measured_reading,
        expected.previous_measured_planned_reading_id,
        expected.old_meter_usage_delta,
    )
    assert (actual.previous_measured_reading, actual.old_meter_usage_delta) == (
        Decimal("110"),
        Decimal("10"),
    )


def test_commit_rejects_replacement_boundary_changed_after_preview(
    services: ImportServices,
) -> None:
    result = _boundary_setup(services)
    preview = services.importer.preview(result)
    services.conn.execute(
        "UPDATE replacement_boundaries SET old_final_reading = ? "
        "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
        ("125", "replacement", DEFINITION_ID),
    )
    before = tuple(services.conn.iterdump())

    with pytest.raises(MobileReadingPreviewChangedError):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before


def test_unrelated_boundary_in_same_replacement_does_not_prevent_commit(
    services: ImportServices,
) -> None:
    _create_structured_definition(services)
    create_plan(services.repository, "old", _at(3))
    create_plan(services.repository, "target", _at(4))
    _save_manual(services, "old", "100", 3)
    _replace_meter(services, "5", include_structured=True)
    result = _result(_entry("target", "110", 4))
    preview = services.importer.preview(result)
    services.conn.execute(
        "UPDATE replacement_boundaries SET old_meter_usage_delta = ? "
        "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
        ("7", "replacement", "structured"),
    )

    committed = services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert committed.summary.confirmed == 1
    boundaries = {
        boundary.measurement_definition_id: boundary
        for boundary in services.repository.list_replacement_boundaries_for_event("replacement")
    }
    assert boundaries["structured"].old_meter_usage_delta == Decimal("7")
    assert boundaries[DEFINITION_ID].old_meter_usage_delta == Decimal("10")


@pytest.mark.parametrize(
    "field", ["previous", "previous_id", "usage", "old_final", "new_initial", "missing"]
)
def test_commit_rolls_back_if_saved_boundary_differs_from_projection(
    services: ImportServices, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    result = _boundary_setup(services)
    preview = services.importer.preview(result)
    before = tuple(services.conn.iterdump())
    original_upsert = services.writer.upsert_reading
    mutations: dict[str, tuple[str, tuple[object, ...]]] = {
        "previous": (
            "UPDATE replacement_boundaries SET previous_measured_reading = ? "
            "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
            ("99", "replacement", DEFINITION_ID),
        ),
        "previous_id": (
            "UPDATE replacement_boundaries SET previous_measured_planned_reading_id = ? "
            "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
            ("old", "replacement", DEFINITION_ID),
        ),
        "usage": (
            "UPDATE replacement_boundaries SET old_meter_usage_delta = ? "
            "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
            ("99", "replacement", DEFINITION_ID),
        ),
        "old_final": (
            "UPDATE replacement_boundaries SET old_final_reading = ? "
            "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
            ("125", "replacement", DEFINITION_ID),
        ),
        "new_initial": (
            "UPDATE replacement_boundaries SET new_initial_reading = ? "
            "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
            ("6", "replacement", DEFINITION_ID),
        ),
        "missing": (
            "DELETE FROM replacement_boundaries "
            "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
            ("replacement", DEFINITION_ID),
        ),
    }

    def corrupt_after_write(request: ReadingUpsertRequest) -> UpsertOutcome:
        outcome = original_upsert(request)
        sql, parameters = mutations[field]
        services.conn.execute(sql, parameters)
        return outcome

    monkeypatch.setattr(services.writer, "upsert_reading", corrupt_after_write)
    with pytest.raises(MobileReadingPreviewChangedError):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before


@pytest.mark.parametrize("failure", ["business", "unexpected"])
def test_boundary_update_failure_rolls_back_the_whole_commit(
    services: ImportServices, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    result = _boundary_setup(services)
    preview = services.importer.preview(result)
    before = tuple(services.conn.iterdump())
    original_update = services.repository.update_replacement_boundary_calculation

    def update_then_fail(update: ReplacementBoundaryCalculationUpdate) -> None:
        original_update(update)
        if failure == "business":
            raise ValidationError("交換境界の障害")
        raise RuntimeError("予期しない交換境界の障害")

    monkeypatch.setattr(
        services.repository, "update_replacement_boundary_calculation", update_then_fail
    )
    error = MobileReadingPreviewChangedError if failure == "business" else RuntimeError
    with pytest.raises(error):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before
    assert not services.conn.in_transaction


@pytest.mark.parametrize("failure", ["business", "unexpected"])
def test_downstream_update_failure_rolls_back_the_whole_commit(
    services: ImportServices, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    for slot, hour in (("previous", 3), ("target", 8), ("following", 14)):
        create_plan(services.repository, slot, _at(hour))
    _save_manual(services, "previous", "100", 3)
    _save_manual(services, "following", "120", 14)
    result = _result(_entry("target", "110", 8))
    preview = services.importer.preview(result)
    before = tuple(services.conn.iterdump())
    original_update = services.repository.save_calculated_usage

    def update_then_fail(update: CalculatedUsageUpdate) -> None:
        original_update(update)
        if failure == "business":
            raise ValidationError("後続再計算の障害")
        raise RuntimeError("予期しない後続再計算の障害")

    monkeypatch.setattr(services.repository, "save_calculated_usage", update_then_fail)
    error = MobileReadingPreviewChangedError if failure == "business" else RuntimeError
    with pytest.raises(error):
        services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert tuple(services.conn.iterdump()) == before
    assert not services.conn.in_transaction


def test_commit_retains_downstream_rejection_reason_in_japanese(services: ImportServices) -> None:
    for slot, hour in (("previous", 3), ("early", 8), ("late", 14)):
        create_plan(services.repository, slot, _at(hour))
    _save_manual(services, "previous", "100", 3)
    result = _result(_entry("late", "110", 14), _entry("early", "120", 8))
    preview = services.importer.preview(result)
    assert preview.rows[1].reason is not None
    assert preview.rows[1].reason.code is MobileReadingRejectCode.DOWNSTREAM_RECALCULATION

    committed = services.importer.commit(result, preview, staff_id=STAFF_ID)

    rejected = committed.rows[1]
    assert rejected.reason is not None
    assert rejected.reason.cause is MobileReadingRejectCode.NEGATIVE_USAGE
    assert (
        rejected.reason_message == "後続実測値の再計算に失敗しました（原因: 使用量が負になります）"
    )
    assert committed.summary.confirmed == 1 and committed.summary.rejected == 1
    targets = services.repository.get_mobile_reading_import_targets(["early", "late"])
    assert targets["early"].existing_result is None
    late = targets["late"].existing_result
    assert late is not None and late.usage_delta == Decimal("10")


def test_rejection_summary_groups_codes_and_preserves_input_order(services: ImportServices) -> None:
    create_plan(services.repository, "superseded", _at(8))
    create_plan(services.repository, "valid", _at(14))
    services.conn.execute(
        "UPDATE planned_readings SET task_state = 'superseded', "
        "close_reason = 'meter_replaced', closed_at = ? WHERE planned_reading_id = ?",
        ("2026-02-04 06:00:00", "superseded"),
    )
    result = _result(
        _entry("missing-one", "1", 3),
        _entry("superseded", "2", 8),
        _entry("missing-two", "3", 9),
        _entry("valid", "10", 14),
    )
    preview = services.importer.preview(result)

    committed = services.importer.commit(result, preview, staff_id=STAFF_ID)

    assert [
        (item.code, item.message, item.count) for item in committed.summary.rejection_reasons
    ] == [
        (MobileReadingRejectCode.UNKNOWN_PLANNED_READING, "予定枠が見つかりません", 2),
        (MobileReadingRejectCode.SUPERSEDED, "置き換え済みの予定枠です", 1),
    ]
    assert [row.row_number for row in committed.rows] == [1, 2, 3, 4]
    assert committed.summary.confirmed == 1 and committed.summary.rejected == 3


@pytest.mark.parametrize("code", list(MobileReadingRejectCode))
def test_every_typed_rejection_has_a_japanese_message(code: MobileReadingRejectCode) -> None:
    message = describe_mobile_reading_rejection(MobileReadingRejectReason(code, "target"))
    assert message
    assert any("\u3040" <= char <= "\u9fff" for char in message)
    assert code.value not in message


def test_commit_structured_result_preserves_normalized_payload_and_signature(
    services: ImportServices,
) -> None:
    _create_structured_definition(services)
    create_plan(services.repository, "factor", _at(8), measurement_definition_id="structured")
    result = _result(_entry("factor", None, 8, structured={"percent": "95.50", "phase": "lag"}))
    preview = services.importer.preview(result)

    services.importer.commit(result, preview, staff_id=STAFF_ID)

    stored = services.repository.get_mobile_reading_import_targets(["factor"])[
        "factor"
    ].existing_result
    assert stored is not None
    assert stored.structured_payload_json == '{"percent":"95.50","phase":"lag"}'
    assert stored.usage_delta is None
    verify_meter_reading_signature(
        planned_reading_id="factor",
        result_status=stored.result_status,
        raw_reading=stored.raw_reading,
        structured_payload_json=json.loads(stored.structured_payload_json),
        recorded_at=stored.recorded_at,
        device_id=stored.device_id,
        signature=stored.signature,
    )


def test_app_context_wires_sheet_and_commit_with_business_clock(services: ImportServices) -> None:
    current = datetime(2026, 2, 4, 15, 30, tzinfo=UTC)
    context = create_app_context(services.conn, clock=lambda: current)
    create_plan(services.repository, "target", datetime(2026, 2, 5, 8, tzinfo=UTC))

    sheet = context.mobile_reading_sheet.create_sheet(date(2026, 2, 5))

    assert sheet.slot_count >= 1
    assert "20260205" in sheet.filename and "003000" in sheet.filename
    result = MobileReadingResult(sheet_id=sheet.sheet_id, entries=(_entry("target", "10", 8),))
    preview = context.mobile_reading_import.preview(result)
    committed = context.mobile_reading_import.commit(result, preview, staff_id=STAFF_ID)
    target = services.repository.get_mobile_reading_import_targets(["target"])["target"]
    assert committed.summary.confirmed == 1
    assert target.existing_result is not None
    assert target.existing_result.device_id == sheet.sheet_id
