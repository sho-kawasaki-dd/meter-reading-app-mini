"""復元前の一時 DB を対象とする読取専用の業務整合性検査。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError as PydanticValidationError

from meter_reading_mini.core.contract_models import (
    SIGNED_DECIMAL_STRING_PATTERN,
    PowerFactorReading,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.reading_values import validate_reading_value
from meter_reading_mini.core.timeutil import parse_utc
from meter_reading_mini.db.backup import check_sqlite_integrity
from meter_reading_mini.db.connection import read_transaction
from meter_reading_mini.db.migrations import SCHEMA_VERSION, inspect_schema

_DECIMAL_FIELDS: dict[str, tuple[tuple[str, bool, bool], ...]] = {
    "measurement_definitions": (
        ("multiplier", True, True),
        ("warning_threshold", False, False),
        ("rollover_value", False, True),
    ),
    "meter_assignments": (
        ("multiplier", True, True),
        ("rollover_value", False, True),
    ),
    "assignment_calculation_corrections": (
        ("old_multiplier", True, True),
        ("old_rollover_value", False, True),
        ("new_multiplier", True, True),
        ("new_rollover_value", False, True),
    ),
    "reading_transactions": (
        ("raw_reading", False, False),
        ("last_reading", False, False),
        ("usage_delta", False, False),
    ),
    "replacement_events": (
        ("old_final_reading", False, False),
        ("new_initial_reading", False, False),
    ),
    "replacement_boundaries": (
        ("previous_measured_reading", False, False),
        ("old_final_reading", False, False),
        ("old_meter_usage_delta", False, False),
        ("new_initial_reading", False, False),
    ),
}

_Numbers = dict[tuple[str, int, str], Decimal]


@dataclass(frozen=True, slots=True)
class _Assignment:
    definition_id: str
    meter_id: str
    start_text: str
    start: datetime
    end: datetime | None
    decimal_places: int | None
    rollover: Decimal | None


def _read_datetime(value: object, label: str, reasons: list[str]) -> datetime | None:
    try:
        if not isinstance(value, str):
            raise ValueError("日時が文字列ではありません。")
        return parse_utc(value)
    except ValueError:
        reasons.append(f"{label}は UTC 秒単位の日時ではありません。")
        return None


def _check_decimal_fields(conn: sqlite3.Connection, reasons: list[str]) -> _Numbers:
    numbers: _Numbers = {}
    for table, fields in _DECIMAL_FIELDS.items():
        columns = ", ".join(field[0] for field in fields)
        for row in conn.execute(f"SELECT rowid, {columns} FROM {table}"):
            rowid = int(row[0])
            for (column, required, positive), raw in zip(fields, row[1:], strict=True):
                label = f"{table}.{column} (行 {rowid})"
                if raw is None:
                    if required:
                        reasons.append(f"{label}がありません。")
                    continue
                if not isinstance(raw, str) or not SIGNED_DECIMAL_STRING_PATTERN.fullmatch(raw):
                    reasons.append(f"{label}は有限の plain decimal 文字列ではありません。")
                    continue
                try:
                    number = Decimal(raw)
                except InvalidOperation:
                    reasons.append(f"{label}は有限の plain decimal 文字列ではありません。")
                    continue
                if not number.is_finite() or format(number, "f") != raw:
                    reasons.append(f"{label}は有限の plain decimal 文字列ではありません。")
                elif number < 0 or (positive and number == 0):
                    qualifier = "正数" if positive else "0 以上"
                    reasons.append(f"{label}は{qualifier}でなければなりません。")
                else:
                    numbers[(table, rowid, column)] = number
    return numbers


def _check_assignments(
    conn: sqlite3.Connection, numbers: _Numbers, reasons: list[str]
) -> tuple[dict[tuple[str, str, str], _Assignment], dict[str, list[_Assignment]]]:
    by_key: dict[tuple[str, str, str], _Assignment] = {}
    by_definition: dict[str, list[_Assignment]] = {}
    rows = conn.execute(
        "SELECT a.rowid, a.measurement_definition_id, a.meter_id, a.effective_from, "
        "a.effective_to, d.decimal_places, d.location_id, m.location_id "
        "FROM meter_assignments AS a "
        "JOIN measurement_definitions AS d USING (measurement_definition_id) "
        "JOIN physical_meters AS m ON m.meter_id = a.meter_id "
        "ORDER BY a.measurement_definition_id, a.effective_from, a.meter_id"
    )
    for (
        rowid,
        definition_id,
        meter_id,
        start_text,
        end_text,
        places,
        def_location,
        meter_location,
    ) in rows:
        label = f"割当 {definition_id}/{meter_id}/{start_text}"
        start = _read_datetime(start_text, f"{label}の開始日時", reasons)
        end = (
            _read_datetime(end_text, f"{label}の終了日時", reasons)
            if end_text is not None
            else None
        )
        if def_location != meter_location:
            reasons.append(f"{label}の測定定義とメーターの場所が一致しません。")
        if start is None or (end_text is not None and end is None):
            continue
        if end is not None and end <= start:
            reasons.append(f"{label}の終了日時は開始日時より後でなければなりません。")
            continue
        assignment = _Assignment(
            definition_id=str(definition_id),
            meter_id=str(meter_id),
            start_text=str(start_text),
            start=start,
            end=end,
            decimal_places=int(places) if places is not None else None,
            rollover=numbers.get(("meter_assignments", int(rowid), "rollover_value")),
        )
        by_key[(assignment.definition_id, assignment.meter_id, assignment.start_text)] = assignment
        by_definition.setdefault(assignment.definition_id, []).append(assignment)

    for definition_id, assignments in by_definition.items():
        previous = assignments[0]
        for current in assignments[1:]:
            if previous.end is None or current.start < previous.end:
                reasons.append(
                    f"測定定義 {definition_id} の割当期間が重複しています: "
                    f"{previous.meter_id}/{current.meter_id}"
                )
            if previous.end is not None and (current.end is None or current.end > previous.end):
                previous = current
    return by_key, by_definition


def _check_reading_value(
    value: Decimal | None, assignment: _Assignment, label: str, reasons: list[str]
) -> None:
    if value is None:
        return
    try:
        validate_reading_value(
            value, decimal_places=assignment.decimal_places, rollover_value=assignment.rollover
        )
    except ValidationError as exc:
        reasons.append(f"{label}: {exc}")


def _check_planned_readings(
    conn: sqlite3.Connection,
    numbers: _Numbers,
    by_definition: dict[str, list[_Assignment]],
    reasons: list[str],
) -> None:
    rows = conn.execute(
        "SELECT p.planned_reading_id, p.measurement_definition_id, p.meter_id, "
        "p.location_id, p.scheduled_at, p.task_state, p.close_reason, "
        "d.location_id, d.value_schema, d.calculation_method, "
        "r.rowid, r.result_status, r.raw_reading, r.last_reading, r.usage_delta, "
        "r.structured_payload_json FROM planned_readings AS p "
        "JOIN measurement_definitions AS d USING (measurement_definition_id) "
        "LEFT JOIN reading_transactions AS r USING (planned_reading_id) "
        "ORDER BY p.scheduled_at, p.planned_reading_id"
    )
    for row in rows:
        (
            plan_id,
            definition_id,
            meter_id,
            location,
            scheduled_text,
            state,
            close_reason,
            def_location,
            value_schema,
            method,
            result_rowid,
            status,
            raw,
            last,
            usage,
            payload,
        ) = row
        label = f"予定枠 {plan_id}"
        scheduled = _read_datetime(scheduled_text, f"{label}の予定日時", reasons)
        if location != def_location:
            reasons.append(f"{label}と測定定義の場所が一致しません。")
        matching: list[_Assignment] = []
        if scheduled is not None and state != "superseded":
            matching = [
                assignment
                for assignment in by_definition.get(str(definition_id), [])
                if assignment.start <= scheduled
                and (assignment.end is None or scheduled < assignment.end)
            ]
            if len(matching) != 1 or matching[0].meter_id != meter_id:
                reasons.append(f"{label}に対応する割当を一意に解決できません。")
                matching = []

        if state == "completed" and result_rowid is None:
            reasons.append(f"{label}は完了ですが結果がありません。")
        if state in ("open", "auto_closed") and result_rowid is not None:
            reasons.append(f"{label}は未完了ですが結果があります。")
        if result_rowid is None:
            continue
        if state == "superseded" and close_reason == "meter_replaced" and status == "measured":
            reasons.append(f"{label}には交換で差し替えられた実測結果があります。")
        if status != "measured":
            if any(value is not None for value in (raw, last, usage, payload)):
                reasons.append(f"{label}の非実測結果に値が保存されています。")
            continue
        if value_schema == "decimal_string":
            if raw is None or payload is not None:
                reasons.append(f"{label}の数値実測結果と入力形式が一致しません。")
            if method == "delta" and usage is None:
                reasons.append(f"{label}の使用量がありません。")
            if matching:
                for column in ("raw_reading", "last_reading"):
                    _check_reading_value(
                        numbers.get(("reading_transactions", int(result_rowid), column)),
                        matching[0],
                        f"{label}の{column}",
                        reasons,
                    )
        elif value_schema == "power_factor_percent_phase":
            if raw is not None or not isinstance(payload, str):
                reasons.append(f"{label}の力率実測結果と入力形式が一致しません。")
            else:
                try:
                    PowerFactorReading.model_validate_json(payload)
                except PydanticValidationError:
                    reasons.append(f"{label}の力率データが不正です。")
            if last is not None or usage is not None:
                reasons.append(f"{label}の力率結果に前回値または使用量があります。")
        if method == "raw_only" and (last is not None or usage is not None):
            reasons.append(f"{label}の raw_only 結果に前回値または使用量があります。")


def _check_corrections(
    conn: sqlite3.Connection,
    by_key: dict[tuple[str, str, str], _Assignment],
    reasons: list[str],
) -> None:
    rows = conn.execute(
        "SELECT correction_id, measurement_definition_id, meter_id, assignment_effective_from, "
        "corrected_at, staff_name, reason FROM assignment_calculation_corrections "
        "ORDER BY corrected_at, correction_id"
    )
    for correction_id, definition_id, meter_id, start, corrected_at, staff_name, reason in rows:
        label = f"計算条件訂正 {correction_id}"
        if (str(definition_id), str(meter_id), str(start)) not in by_key:
            reasons.append(f"{label}の対象割当がありません。")
        _read_datetime(corrected_at, f"{label}の訂正日時", reasons)
        if not str(staff_name).strip() or not str(reason).strip():
            reasons.append(f"{label}の担当者または理由がありません。")


def _check_boundaries(
    conn: sqlite3.Connection,
    numbers: _Numbers,
    by_key: dict[tuple[str, str, str], _Assignment],
    reasons: list[str],
) -> None:
    for (event_id,) in conn.execute(
        "SELECT e.replacement_event_id FROM replacement_events AS e "
        "WHERE NOT EXISTS (SELECT 1 FROM replacement_boundaries AS b "
        "WHERE b.replacement_event_id = e.replacement_event_id)"
    ):
        reasons.append(f"交換イベント {event_id} に交換境界がありません。")

    rows = conn.execute(
        "SELECT b.rowid, b.replacement_event_id, b.measurement_definition_id, b.location_id, "
        "b.old_meter_id, b.new_meter_id, b.replaced_at, b.old_assignment_effective_from, "
        "b.new_assignment_effective_from, b.previous_measured_reading, "
        "b.previous_measured_planned_reading_id, b.old_final_reading, b.old_meter_usage_delta, "
        "b.new_initial_reading, e.rowid, e.location_id, e.old_meter_id, e.new_meter_id, "
        "e.replaced_at, e.old_final_reading, e.new_initial_reading, "
        "d.location_id, d.calculation_method "
        "FROM replacement_boundaries AS b "
        "JOIN replacement_events AS e USING (replacement_event_id) "
        "JOIN measurement_definitions AS d USING (measurement_definition_id) "
        "ORDER BY b.replaced_at, b.replacement_event_id, b.measurement_definition_id"
    )
    for row in rows:
        (
            rowid,
            event_id,
            definition_id,
            location,
            old_meter,
            new_meter,
            replaced_text,
            old_start,
            new_start,
            previous_value,
            previous_id,
            old_final,
            old_usage,
            new_initial,
            event_rowid,
            event_location,
            event_old,
            event_new,
            event_time,
            event_final,
            event_initial,
            def_location,
            method,
        ) = row
        label = f"交換境界 {event_id}/{definition_id}"
        replaced_at = _read_datetime(replaced_text, f"{label}の交換日時", reasons)
        if (
            (location, old_meter, new_meter, replaced_text)
            != (event_location, event_old, event_new, event_time)
            or location != def_location
            or old_meter == new_meter
        ):
            reasons.append(f"{label}と交換イベントまたは測定定義が一致しません。")
        for column, boundary_value, event_value in (
            ("old_final_reading", old_final, event_final),
            ("new_initial_reading", new_initial, event_initial),
        ):
            boundary_number = numbers.get(("replacement_boundaries", int(rowid), column))
            event_number = numbers.get(("replacement_events", int(event_rowid), column))
            if (boundary_value is None) != (event_value is None) or (
                boundary_number is not None
                and event_number is not None
                and boundary_number != event_number
            ):
                reasons.append(f"{label}の{column}が交換イベントと一致しません。")
        old = by_key.get((str(definition_id), str(old_meter), str(old_start)))
        new = by_key.get((str(definition_id), str(new_meter), str(new_start)))
        if (
            old is None
            or new is None
            or replaced_at is None
            or old.start >= replaced_at
            or old.end != replaced_at
            or new.start != replaced_at
            or (new.end is not None and new.end <= replaced_at)
        ):
            reasons.append(f"{label}と旧・新割当の期間が一致しません。")
        else:
            for column, assignment in (
                ("previous_measured_reading", old),
                ("old_final_reading", old),
                ("new_initial_reading", new),
            ):
                _check_reading_value(
                    numbers.get(("replacement_boundaries", int(rowid), column)),
                    assignment,
                    f"{label}の{column}",
                    reasons,
                )
        if (previous_id is None) != (previous_value is None):
            reasons.append(f"{label}の前回実測値と参照先が一致しません。")
        elif previous_id is not None:
            source = conn.execute(
                "SELECT p.measurement_definition_id, p.meter_id, p.scheduled_at, "
                "r.rowid, r.result_status FROM planned_readings AS p "
                "LEFT JOIN reading_transactions AS r USING (planned_reading_id) "
                "WHERE p.planned_reading_id = ?",
                (previous_id,),
            ).fetchone()
            source_time = (
                _read_datetime(source[2], f"{label}の前回実測日時", reasons)
                if source is not None
                else None
            )
            source_value = (
                numbers.get(("reading_transactions", int(source[3]), "raw_reading"))
                if source is not None and source[3] is not None
                else None
            )
            boundary_value = numbers.get(
                ("replacement_boundaries", int(rowid), "previous_measured_reading")
            )
            if (
                source is None
                or source[0] != definition_id
                or source[1] != old_meter
                or source[4] != "measured"
                or source_time is None
                or replaced_at is None
                or source_time >= replaced_at
                or (old is not None and source_time < old.start)
                or source_value is None
                or boundary_value is None
                or source_value != boundary_value
            ):
                reasons.append(f"{label}の前回実測の参照先が不正です。")
        if (old_final is None and old_usage is not None) or (
            method == "raw_only" and old_usage is not None
        ):
            reasons.append(f"{label}の旧メーター使用量と読値が一致しません。")
        elif method == "delta" and old_final is not None and old_usage is None:
            reasons.append(f"{label}の旧メーター使用量がありません。")


def check_business_integrity(conn: sqlite3.Connection) -> list[str]:
    """最新版へ移行した DB を変更せず、復元に必要な業務整合性を検査する。"""

    with read_transaction(conn):
        version_row = conn.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0]) if version_row is not None else 0
        if version != SCHEMA_VERSION:
            return [f"業務整合性検査にはスキーマ版 {SCHEMA_VERSION} が必要です: 版 {version}"]
        schema_reasons = inspect_schema(conn, SCHEMA_VERSION)
        if schema_reasons:
            return [f"スキーマが不正です: {reason}" for reason in schema_reasons]
        sqlite_reasons = check_sqlite_integrity(conn)
        if sqlite_reasons:
            return sqlite_reasons

        reasons: list[str] = []
        numbers = _check_decimal_fields(conn, reasons)
        by_key, by_definition = _check_assignments(conn, numbers, reasons)
        _check_planned_readings(conn, numbers, by_definition, reasons)
        _check_corrections(conn, by_key, reasons)
        _check_boundaries(conn, numbers, by_key, reasons)
        return reasons


__all__ = ["check_business_integrity"]
