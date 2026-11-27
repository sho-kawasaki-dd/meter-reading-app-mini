from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest

from building_manager_mini.paths import AppPaths
from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.core.timezone_settings import DEFAULT_APP_TIMEZONE

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "create_manual_test_data.py"

spec = importlib.util.spec_from_file_location("create_manual_test_data", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
generator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = generator
spec.loader.exec_module(generator)


def _run_generator(
    data_dir: Path,
    *,
    days_back: int = 7,
    measured_percent: int = 50,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--data-dir",
            str(data_dir),
            "--days-back",
            str(days_back),
            "--measured-percent",
            str(measured_percent),
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )


def test_create_manual_test_data_builds_representative_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "manual-check"
    result = _run_generator(data_dir)
    database_path = AppPaths.from_root(data_dir).db_path

    assert result.returncode == 0, result.stderr
    assert "測定定義: 4 件" in result.stdout
    assert "予定枠:" in result.stdout
    assert "実測入力割合: 50%" in result.stdout
    assert "'open':" in result.stdout
    assert "'auto_closed':" in result.stdout

    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(DISTINCT location_id) FROM physical_meters"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT COUNT(*) FROM measurement_definitions").fetchone()[0] == 4
        past_slots = connection.execute(
            "SELECT COUNT(*) FROM planned_readings WHERE task_state IN ('completed', 'auto_closed')"
        ).fetchone()[0]
        measured_count = connection.execute(
            "SELECT COUNT(*) FROM reading_transactions WHERE result_status = 'measured'"
        ).fetchone()[0]
        assert measured_count == (past_slots * 50 + 50) // 100
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM reading_transactions WHERE device_id = 'pc-direct'"
            ).fetchone()[0]
            == measured_count
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM planned_readings WHERE task_state = 'auto_closed'"
            ).fetchone()[0]
            == past_slots - measured_count
        )

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_create_manual_test_data_refuses_to_overwrite_existing_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "manual-check"
    assert _run_generator(data_dir).returncode == 0

    result = _run_generator(data_dir)

    assert result.returncode != 0
    assert "既存 DB を上書きしません" in result.stderr


@pytest.mark.parametrize("days_back", [2, 91])
def test_create_manual_test_data_validates_backfill_range(
    tmp_path: Path,
    days_back: int,
) -> None:
    result = _run_generator(tmp_path / "manual-check", days_back=days_back)

    assert result.returncode != 0
    assert "--days-back は 3 から 90 の範囲" in result.stderr


@pytest.mark.parametrize(("measured_percent", "expected_auto_closed"), [(0, True), (100, False)])
def test_create_manual_test_data_applies_measured_percentage(
    tmp_path: Path,
    measured_percent: int,
    expected_auto_closed: bool,
) -> None:
    data_dir = tmp_path / f"manual-check-{measured_percent}"
    result = _run_generator(data_dir, measured_percent=measured_percent)

    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(AppPaths.from_root(data_dir).db_path)) as connection:
        past_slots = connection.execute(
            "SELECT COUNT(*) FROM planned_readings WHERE task_state IN ('completed', 'auto_closed')"
        ).fetchone()[0]
        measured_count = connection.execute(
            "SELECT COUNT(*) FROM reading_transactions WHERE result_status = 'measured'"
        ).fetchone()[0]
        auto_closed_count = connection.execute(
            "SELECT COUNT(*) FROM planned_readings WHERE task_state = 'auto_closed'"
        ).fetchone()[0]

    assert past_slots > 0
    assert measured_count == (past_slots * measured_percent + 50) // 100
    assert (auto_closed_count > 0) is expected_auto_closed


@pytest.mark.parametrize(("hour", "minute", "yesterday_is_open"), [(6, 59, True), (7, 0, False)])
@pytest.mark.parametrize("measured_percent", [0, 50, 100])
def test_create_manual_test_data_respects_cutoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hour: int,
    minute: int,
    yesterday_is_open: bool,
    measured_percent: int,
) -> None:
    now = datetime(2026, 11, 28, hour, minute, tzinfo=DEFAULT_APP_TIMEZONE)
    monkeypatch.setattr(generator, "utc_now", lambda: now)
    monkeypatch.setattr(
        generator,
        "create_app_context",
        lambda connection: create_app_context(connection, clock=lambda: now),
    )
    generator.create_manual_test_data(tmp_path, days_back=7, measured_percent=measured_percent)

    with closing(sqlite3.connect(AppPaths.from_root(tmp_path).db_path)) as connection:
        eligible_slots = connection.execute(
            "SELECT COUNT(*) FROM planned_readings WHERE business_date <= ?",
            ("2026-11-26" if yesterday_is_open else "2026-11-27",),
        ).fetchone()[0]
        measured_count = connection.execute(
            "SELECT COUNT(*) FROM reading_transactions WHERE result_status = 'measured'"
        ).fetchone()[0]
        auto_closed_count = connection.execute(
            "SELECT COUNT(*) FROM planned_readings WHERE task_state = 'auto_closed'"
        ).fetchone()[0]
        yesterday_states = {
            row[0]
            for row in connection.execute(
                "SELECT task_state FROM planned_readings WHERE business_date = '2026-11-27'"
            )
        }

    assert eligible_slots > 0
    assert measured_count == (eligible_slots * measured_percent + 50) // 100
    assert auto_closed_count == eligible_slots - measured_count
    assert yesterday_states
    if yesterday_is_open:
        assert yesterday_states == {"open"}
    else:
        assert "open" not in yesterday_states


@pytest.mark.parametrize("measured_percent", [-1, 101])
def test_create_manual_test_data_validates_measured_percentage(
    tmp_path: Path,
    measured_percent: int,
) -> None:
    result = _run_generator(tmp_path / "manual-check", measured_percent=measured_percent)

    assert result.returncode != 0
    assert "--measured-percent は 0 から 100 の範囲" in result.stderr
