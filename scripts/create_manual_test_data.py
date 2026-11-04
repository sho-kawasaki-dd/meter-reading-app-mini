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
MAX_MEASURED_PERCENT = 100
MIN_MEASURED_PERCENT = 0


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
    measured_percent: int = 50,
) -> ManualTestDataSummary:
    """新しい DB に手動確認用マスタ・予定枠・一部の結果を作成する。"""

    if not MIN_DAYS_BACK <= days_back <= MAX_DAYS_BACK:
        raise ValueError(
            f"days_back は {MIN_DAYS_BACK} から {MAX_DAYS_BACK} の範囲で指定してください。"
        )
    if not MIN_MEASURED_PERCENT <= measured_percent <= MAX_MEASURED_PERCENT:
        raise ValueError(
            "measured_percent は "
            f"{MIN_MEASURED_PERCENT} から {MAX_MEASURED_PERCENT} の範囲で指定してください。"
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
                    rollover_value=Decimal("100000"),
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
                    rollover_value=Decimal("1000000"),
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
            staff_id=staff.staff_id,
            today=today,
            measured_percent=measured_percent,
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
    staff_id: str,
    today: date,
    measured_percent: int,
) -> None:
    slots = connection.execute(
        "SELECT p.planned_reading_id, p.measurement_definition_id, "
        "p.business_date, p.reading_session, d.value_schema "
        "FROM planned_readings AS p "
        "JOIN measurement_definitions AS d "
        "ON d.measurement_definition_id = p.measurement_definition_id "
        "WHERE p.business_date < ? AND p.task_state = 'auto_closed' "
        "ORDER BY p.business_date, p.reading_session, p.planned_reading_id",
        (today.isoformat(),),
    ).fetchall()
    measured_count = (len(slots) * measured_percent + 50) // 100
    selected_slots = (
        [slots[((index + 1) * len(slots) - 1) // measured_count] for index in range(measured_count)]
        if measured_count
        else []
    )

    writer = context.reading_writer
    readings_per_definition: dict[str, int] = {}
    for row in selected_slots:
        planned_reading_id = str(row[0])
        definition_id = str(row[1])
        business_date = date.fromisoformat(str(row[2]))
        reading_session = time.fromisoformat(str(row[3]))
        value_schema = ValueSchema(str(row[4]))
        reading_number = readings_per_definition.get(definition_id, 0)
        readings_per_definition[definition_id] = reading_number + 1
        is_power_factor = value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE
        writer.save_manual_reading(
            ManualReadingInput(
                planned_reading_id=planned_reading_id,
                result_status=ResultStatus.MEASURED,
                recorded_at=datetime.combine(
                    business_date,
                    reading_session,
                    tzinfo=DEFAULT_APP_TIMEZONE,
                ),
                raw_reading=(
                    None
                    if is_power_factor
                    else Decimal("1000") + Decimal(reading_number) * Decimal("1.25")
                ),
                structured_payload_json=(
                    {"percent": "95", "phase": "lag"} if is_power_factor else None
                ),
                note="手動確認用のサンプル実測 (PC入力)",
            ),
            staff_id=staff_id,
        )


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
    parser.add_argument(
        "--measured-percent",
        type=int,
        default=50,
        help="過去の自動クローズ枠のうち PC 入力の実測にする割合 (0〜100、既定: 50)",
    )
    args = parser.parse_args()
    if not MIN_DAYS_BACK <= args.days_back <= MAX_DAYS_BACK:
        parser.error(
            f"--days-back は {MIN_DAYS_BACK} から {MAX_DAYS_BACK} の範囲で指定してください。"
        )
    if not MIN_MEASURED_PERCENT <= args.measured_percent <= MAX_MEASURED_PERCENT:
        parser.error(
            "--measured-percent は "
            f"{MIN_MEASURED_PERCENT} から {MAX_MEASURED_PERCENT} の範囲で指定してください。"
        )
    data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="meter-reading-mini-manual-"))

    try:
        summary = create_manual_test_data(
            data_dir,
            days_back=args.days_back,
            measured_percent=args.measured_percent,
        )
    except FileExistsError as error:
        parser.error(str(error))

    print(f"手動確認用 DB を作成しました: {summary.data_dir / 'meter_reading.db'}")
    print(f"測定定義: {summary.definition_count} 件")
    print(f"予定枠: {summary.planned_reading_count} 件")
    print(f"実測入力割合: {args.measured_percent}%")
    print(f"結果: {summary.saved_result_count} 件")
    print(f"枠状態: {summary.task_states}")
    print("次のコマンドで起動してください:")
    print(_format_launch_instructions(summary.data_dir))


if __name__ == "__main__":
    main()
