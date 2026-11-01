from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "create_manual_test_data.py"


def _run_generator(data_dir: Path, *, days_back: int = 7) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--data-dir",
            str(data_dir),
            "--days-back",
            str(days_back),
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def test_create_manual_test_data_builds_representative_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "manual-check"
    result = _run_generator(data_dir)
    database_path = data_dir / "meter_reading.db"

    assert result.returncode == 0, result.stderr
    assert "測定定義: 4 件" in result.stdout
    assert "予定枠:" in result.stdout
    assert "結果: 3 件" in result.stdout
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
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM reading_transactions WHERE result_status = 'measured'"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM reading_transactions WHERE result_status = 'meter_fault'"
            ).fetchone()[0]
            == 1
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
