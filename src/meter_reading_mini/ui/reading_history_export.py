"""表示中の検針履歴を CSV に出力する。"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import tzinfo
from pathlib import Path

from meter_reading_mini.core.errors import FileOutputError, ValidationError
from meter_reading_mini.core.reading_history_contracts import ReadingHistory
from meter_reading_mini.ui.atomic_write import atomic_write_text
from meter_reading_mini.ui.csv_export import escape_csv_text
from meter_reading_mini.ui.models.reading_history_model import decimal_text, local_datetime

CSV_HEADERS = (
    "測定定義ID",
    "測定定義名",
    "場所ID",
    "場所名",
    "単位",
    "行種別",
    "業務日",
    "検針時間",
    "予定日時",
    "枠状態",
    "結果状態",
    "メーターID",
    "メーター名",
    "製造番号",
    "読値",
    "前回値",
    "前回値の出所",
    "前回値の参照元",
    "倍率",
    "ロールオーバー値",
    "使用量",
    "力率",
    "記録日時",
    "担当者",
    "取込元",
    "備考",
    "交換ID",
    "予定枠ID",
    "警告・情報コード",
)
_NUMERIC_COLUMNS = frozenset({14, 15, 18, 19, 20})


def _safe_filename_component(value: str) -> str:
    value = re.sub(r"^[\s.]+|[\s.]+$", "", value)
    value = "".join(
        "_" if char in '\\/:*?"<>|' or unicodedata.category(char) == "Cc" else char
        for char in value
    )
    return re.sub(r"[\s_]+", "_", value)[:40].rstrip(" .")


def _usable_filename_component(value: str) -> bool:
    first = value.split(".", 1)[0].rstrip(" ").upper()
    return bool(value) and re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", first) is None


def history_filename_prefix(history: ReadingHistory) -> str:
    definition = history.definition
    safe = _safe_filename_component(definition.display_name)
    if not _usable_filename_component(definition.display_name.strip()) or not (
        _usable_filename_component(safe)
    ):
        safe = _safe_filename_component(definition.measurement_definition_id)
        if not _usable_filename_component(definition.measurement_definition_id.strip()):
            raise ValidationError("測定定義のファイル名を生成できません。")
    if not _usable_filename_component(safe):
        raise ValidationError("測定定義のファイル名を生成できません。")
    start = history.query.start_date.strftime("%Y%m%d")
    end = history.query.end_date.strftime("%Y%m%d")
    return f"meter_reading_history_{safe}_{start}-{end}"


def reading_history_csv(history: ReadingHistory, timezone: tzinfo) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)
    definition = history.definition
    for row in history.rows:
        values = (
            definition.measurement_definition_id,
            definition.display_name,
            definition.location_id,
            definition.location_name,
            definition.unit,
            row.row_kind.value,
            row.business_date.isoformat(),
            row.reading_session.value if row.reading_session else "",
            local_datetime(row.at, timezone),
            row.task_state.value if row.task_state else "",
            row.result_status.value if row.result_status else "",
            row.meter_id,
            row.meter_name,
            row.meter_serial or "",
            decimal_text(row.raw_reading),
            decimal_text(row.last_reading),
            row.previous_source.value if row.previous_source else "",
            row.previous_reference_id or "",
            decimal_text(row.multiplier),
            decimal_text(row.rollover_value),
            decimal_text(row.usage_delta),
            row.power_factor or "",
            local_datetime(row.recorded_at, timezone),
            row.updated_by or "",
            row.result_source or "",
            row.note or "",
            row.replacement_event_id or "",
            row.planned_reading_id or "",
            ";".join(issue.code.value for issue in row.issues),
        )
        writer.writerow(
            value if index in _NUMERIC_COLUMNS else escape_csv_text(value)
            for index, value in enumerate(values)
        )
    return "\ufeff" + output.getvalue()


def save_reading_history_csv(path: Path, history: ReadingHistory, timezone: tzinfo) -> None:
    try:
        atomic_write_text(path, reading_history_csv(history, timezone))
    except OSError as error:
        raise FileOutputError(
            f"検針履歴 CSV を保存できませんでした。\n保存先: {path}\n"
            "保存先やアクセス権を確認してください。"
        ) from error
