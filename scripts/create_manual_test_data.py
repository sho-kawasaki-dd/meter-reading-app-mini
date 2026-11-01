"""手動確認用の新しいデータベースを作成する。"""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from shlex import quote

from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.config import AppPaths, ensure_dirs
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ReadingSession,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateInput,
    MeterMasterCreateInput,
    StaffCreateRequest,
)
from meter_reading_mini.core.timeutil import local_timestamp_for_filename, utc_now
from meter_reading_mini.core.timezone_settings import DEFAULT_APP_TIMEZONE
from meter_reading_mini.db.migrations import open_database

MAX_DAYS_BACK = 90
MIN_DAYS_BACK = 3


@dataclass(frozen=True, slots=True)
class ManualTestDataSummary:
    data_dir: Path
    definition_count: int
    planned_reading_count: int
    task_states: dict[str, int]
    saved_result_count: int


def create_manual_test_data(
    data_dir: Path,
    *,
    days_back: int = 14,
) -> ManualTestDataSummary:
    """新しい DB に手動確認用マスタ・予定枠・一部の結果を作成する。"""

    if not MIN_DAYS_BACK <= days_back <= MAX_DAYS_BACK:
        raise ValueError(
            f"days_back は {MIN_DAYS_BACK} から {MAX_DAYS_BACK} の範囲で指定してください。"
        )

    resolved_dir = data_dir.expanduser().resolve()
    paths = AppPaths.from_dirs(resolved_dir, resolved_dir / "logs")
    if paths.db_path.exists():
        raise FileExistsError(f"既存 DB を上書きしません: {paths.db_path}")

    ensure_dirs(paths)
    connection = open_database(
        paths.db_path,
        paths.backup_dir,
        backup_timestamp=local_timestamp_for_filename,
    )
    try:
        context = create_app_context(connection)
        context.startup_preparation.prepare()

        today = utc_now().astimezone(DEFAULT_APP_TIMEZONE).date()
        generation_start_date = today - timedelta(days=days_back)

        staff = context.staff.create_staff(StaffCreateRequest(display_name="確認担当"))

        main_meter = context.meter_master.create_meter(
            MeterMasterCreateInput(
                meter_name="受電盤メーター",
                category=Category.ELECTRICITY,
                new_location_name="本館設備室",
                meter_serial="TEST-E-001",
            )
        )
        water_meter = context.meter_master.create_meter(
            MeterMasterCreateInput(
                meter_name="給水メーター",
                category=Category.WATER,
                location_id=main_meter.location_id,
                meter_serial="TEST-W-001",
            )
        )
        boiler_meter = context.meter_master.create_meter(
            MeterMasterCreateInput(
                meter_name="ボイラーメーター",
                category=Category.BOILER,
                location_id=main_meter.location_id,
                meter_serial="TEST-B-001",
            )
        )
        annex_meter = context.meter_master.create_meter(
            MeterMasterCreateInput(
                meter_name="別館力率計",
                category=Category.ELECTRICITY,
                new_location_name="別館",
                meter_serial="TEST-PF-001",
            )
        )

        definitions = (
            context.measurement_definitions.create_measurement_definition(
                MeasurementDefinitionCreateInput(
                    location_id=main_meter.location_id,
                    display_name="受電電力量",
                    category=Category.ELECTRICITY,
                    unit="kWh",
                    value_schema=ValueSchema.DECIMAL_STRING,
                    multiplier=Decimal("1"),
                    decimal_places=2,
                    warning_threshold=Decimal("10"),
                    rollover_value=Decimal("99999"),
                    calculation_method=CalculationMethod.DELTA,
                    reading_cycle=ReadingCycle.DAILY_1X,
                    purpose=Purpose.OPERATIONAL,
                    generation_start_date=generation_start_date,
                    initial_schedule_json=None,
                    initial_meter_id=main_meter.meter_id,
                )
            ),
            context.measurement_definitions.create_measurement_definition(
                MeasurementDefinitionCreateInput(
                    location_id=water_meter.location_id,
                    display_name="給水使用量",
                    category=Category.WATER,
                    unit="m3",
                    value_schema=ValueSchema.DECIMAL_STRING,
                    multiplier=Decimal("1"),
                    decimal_places=2,
                    warning_threshold=Decimal("5"),
                    rollover_value=None,
                    calculation_method=CalculationMethod.DELTA,
                    reading_cycle=ReadingCycle.DAILY_2X,
                    purpose=Purpose.OPERATIONAL,
                    generation_start_date=generation_start_date,
                    initial_schedule_json=None,
                    initial_meter_id=water_meter.meter_id,
                )
            ),
            context.measurement_definitions.create_measurement_definition(
                MeasurementDefinitionCreateInput(
                    location_id=boiler_meter.location_id,
                    display_name="ボイラー燃料",
                    category=Category.BOILER,
                    unit="m3",
                    value_schema=ValueSchema.DECIMAL_STRING,
                    multiplier=Decimal("1"),
                    decimal_places=2,
                    warning_threshold=None,
                    rollover_value=None,
                    calculation_method=CalculationMethod.DELTA,
                    reading_cycle=ReadingCycle.WEEKLY,
                    purpose=Purpose.OPERATIONAL,
                    generation_start_date=generation_start_date,
                    initial_schedule_json=None,
                    initial_meter_id=boiler_meter.meter_id,
                )
            ),
            context.measurement_definitions.create_measurement_definition(
                MeasurementDefinitionCreateInput(
                    location_id=annex_meter.location_id,
                    display_name="別館力率",
                    category=Category.ELECTRICITY,
                    unit="%",
                    value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
                    multiplier=Decimal("1"),
                    decimal_places=None,
                    warning_threshold=None,
                    rollover_value=None,
                    calculation_method=CalculationMethod.RAW_ONLY,
                    reading_cycle=ReadingCycle.DAILY_3X,
                    purpose=Purpose.OPERATIONAL,
                    generation_start_date=generation_start_date,
                    initial_schedule_json=None,
                    initial_meter_id=annex_meter.meter_id,
                )
            ),
        )

        context.planned_reading_maintenance.refresh()
        _save_example_readings(
            connection,
            context,
            definition_ids=tuple(
                definition.measurement_definition_id for definition in definitions
            ),
            staff_id=staff.staff_id,
            today=today,
        )

        task_states = {
            str(state): int(count)
            for state, count in connection.execute(
                "SELECT task_state, COUNT(*) FROM planned_readings GROUP BY task_state"
            )
        }
        planned_reading_count = int(
            connection.execute("SELECT COUNT(*) FROM planned_readings").fetchone()[0]
        )
        saved_result_count = int(
            connection.execute("SELECT COUNT(*) FROM reading_transactions").fetchone()[0]
        )
        return ManualTestDataSummary(
            data_dir=resolved_dir,
            definition_count=len(definitions),
            planned_reading_count=planned_reading_count,
            task_states=task_states,
            saved_result_count=saved_result_count,
        )
    finally:
        connection.close()


def _save_example_readings(
    connection: sqlite3.Connection,
    context: AppContext,
    *,
    definition_ids: tuple[str, ...],
    staff_id: str,
    today: date,
) -> None:
    writer = context.reading_writer
    electricity_definition_id, water_definition_id, *_ = definition_ids

    for days_ago, value in ((3, "1000"), (2, "1012.5")):
        business_date = today - timedelta(days=days_ago)
        planned_reading_id = _find_planned_reading(
            connection,
            electricity_definition_id,
            business_date,
            ReadingSession.SESSION_0800,
        )
        writer.save_manual_reading(
            ManualReadingInput(
                planned_reading_id=planned_reading_id,
                result_status=ResultStatus.MEASURED,
                recorded_at=datetime.combine(
                    business_date,
                    time(9),
                    tzinfo=DEFAULT_APP_TIMEZONE,
                ),
                raw_reading=Decimal(value),
                structured_payload_json=None,
                note="手動確認用のサンプル実測",
            ),
            staff_id=staff_id,
        )

    fault_date = today - timedelta(days=1)
    water_reading_id = _find_planned_reading(
        connection,
        water_definition_id,
        fault_date,
        ReadingSession.SESSION_0800,
    )
    context.reading_writer.save_manual_reading(
        ManualReadingInput(
            planned_reading_id=water_reading_id,
            result_status=ResultStatus.METER_FAULT,
            recorded_at=datetime.combine(
                fault_date,
                time(9),
                tzinfo=DEFAULT_APP_TIMEZONE,
            ),
            raw_reading=None,
            structured_payload_json=None,
            note="手動確認用の計器不良サンプル",
        ),
        staff_id=staff_id,
    )


def _find_planned_reading(
    connection: sqlite3.Connection,
    definition_id: str,
    business_date: date,
    reading_session: ReadingSession,
) -> str:
    row = connection.execute(
        "SELECT planned_reading_id FROM planned_readings "
        "WHERE measurement_definition_id = ? AND business_date = ? AND reading_session = ?",
        (definition_id, business_date.isoformat(), reading_session.value),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "サンプル結果用の予定枠が見つかりません: "
            f"{definition_id}, {business_date.isoformat()}, {reading_session.value}"
        )
    return str(row[0])


def _format_launch_instructions(data_dir: Path) -> str:
    path = str(data_dir).replace("'", "''")
    if os.name == "nt":
        return f"$env:METER_READING_MINI_DATA_DIR = '{path}'\nuv run python -m meter_reading_mini"
    return (
        f"export METER_READING_MINI_DATA_DIR={quote(str(data_dir))}\n"
        "uv run python -m meter_reading_mini"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="実データと分離した手動確認用 DB を新規作成します。"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="作成先 (既定: OS の一時フォルダー内に新規作成)",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=14,
        help=f"予定枠を補完する過去日数 ({MIN_DAYS_BACK}〜{MAX_DAYS_BACK}、既定: 14)",
    )
    args = parser.parse_args()
    if not MIN_DAYS_BACK <= args.days_back <= MAX_DAYS_BACK:
        parser.error(
            f"--days-back は {MIN_DAYS_BACK} から {MAX_DAYS_BACK} の範囲で指定してください。"
        )
    data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="meter-reading-mini-manual-"))

    try:
        summary = create_manual_test_data(data_dir, days_back=args.days_back)
    except FileExistsError as error:
        parser.error(str(error))

    print(f"手動確認用 DB を作成しました: {summary.data_dir / 'meter_reading.db'}")
    print(f"測定定義: {summary.definition_count} 件")
    print(f"予定枠: {summary.planned_reading_count} 件")
    print(f"結果: {summary.saved_result_count} 件")
    print(f"枠状態: {summary.task_states}")
    print("次のコマンドで起動してください:")
    print(_format_launch_instructions(summary.data_dir))


if __name__ == "__main__":
    main()
