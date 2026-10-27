from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from meter_reading_mini.core import timeutil


def test_utc_local_round_trip_and_date_rollover() -> None:
    tokyo = ZoneInfo("Asia/Tokyo")
    utc_value = "2026-03-31 15:00:00"

    local_value = timeutil.to_local(utc_value, tokyo)

    assert local_value == datetime(2026, 4, 1, 0, 0, tzinfo=tokyo)
    assert timeutil.local_date(utc_value, tokyo) == date(2026, 4, 1)
    assert timeutil.parse_utc(timeutil.utc_now_str(local_value)) == timeutil.parse_utc(utc_value)


@pytest.mark.parametrize(
    ("zone", "start", "end", "hours"),
    [
        ("America/New_York", date(2026, 3, 1), date(2026, 4, 1), 743),
        ("America/New_York", date(2026, 11, 1), date(2026, 12, 1), 721),
        ("Europe/London", date(2026, 3, 1), date(2026, 4, 1), 743),
        ("Europe/London", date(2026, 10, 1), date(2026, 11, 1), 745),
    ],
)
def test_month_range_respects_dst(zone: str, start: date, end: date, hours: int) -> None:
    begin_utc, end_utc = timeutil.local_range_to_utc(start, end, ZoneInfo(zone))

    duration = timeutil.parse_utc(end_utc) - timeutil.parse_utc(begin_utc)
    assert duration == timedelta(hours=hours)


def test_range_boundaries_and_invalid_inputs() -> None:
    tokyo = ZoneInfo("Asia/Tokyo")
    assert timeutil.local_range_to_utc(date(2026, 4, 1), date(2026, 5, 1), tokyo) == (
        "2026-03-31 15:00:00",
        "2026-04-30 15:00:00",
    )
    assert timeutil.local_range_to_utc(date(2026, 4, 1), date(2027, 4, 1), tokyo) == (
        "2026-03-31 15:00:00",
        "2027-03-31 15:00:00",
    )

    with pytest.raises(ValueError):
        timeutil.local_range_to_utc(date(2026, 4, 1), date(2026, 4, 1), tokyo)
    with pytest.raises(ValueError):
        timeutil.local_range_to_utc(date(2026, 5, 1), date(2026, 4, 1), tokyo)
    with pytest.raises(ValueError):
        timeutil.utc_now_str(datetime(2026, 4, 1, 0, 0))
    with pytest.raises(ValueError):
        timeutil.parse_utc("2026-4-01 00:00:00")
    with pytest.raises(ValueError):
        timeutil.parse_utc("not a datetime")


def test_utc_now_and_string_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    actual_now = timeutil.utc_now()
    assert actual_now.tzinfo is UTC
    assert actual_now.utcoffset() == timedelta(0)

    fixed_now = datetime(2026, 4, 1, 3, 4, 5, tzinfo=UTC)
    monkeypatch.setattr(timeutil, "utc_now", lambda: fixed_now)

    assert timeutil.utc_now() is fixed_now
    assert timeutil.utc_now().tzinfo is UTC
    assert timeutil.utc_now_str() == "2026-04-01 03:04:05"
    assert (
        timeutil.utc_now_str(datetime(2026, 4, 1, 12, 4, 5, tzinfo=timezone(timedelta(hours=9))))
        == "2026-04-01 03:04:05"
    )


def test_local_today_and_filename_timestamp() -> None:
    now = datetime(2026, 3, 31, 15, 0, tzinfo=UTC)
    tokyo = ZoneInfo("Asia/Tokyo")

    assert timeutil.local_today(now, tokyo) == date(2026, 4, 1)
    assert timeutil.local_timestamp_for_filename(now, tokyo) == "20260401_000000"
    with pytest.raises(ValueError):
        timeutil.local_today(datetime(2026, 4, 1))


def test_os_local_display_and_boundaries() -> None:
    utc_value = "2026-03-31 15:00:00"
    expected_local = timeutil.parse_utc(utc_value).astimezone()
    assert timeutil.to_local(utc_value) == expected_local
    assert timeutil.format_local(utc_value) == expected_local.strftime(timeutil.DB_DATETIME_FORMAT)

    start, end = date(2026, 4, 1), date(2026, 5, 1)
    expected = tuple(
        datetime.combine(day, datetime.min.time())
        .astimezone(UTC)
        .strftime(timeutil.DB_DATETIME_FORMAT)
        for day in (start, end)
    )
    assert timeutil.local_range_to_utc(start, end) == expected


@pytest.mark.skipif(sys.platform == "win32", reason="time.tzset() は Windows で利用不可")
def test_os_local_dst_boundaries_in_isolated_process() -> None:
    script = """
import os
import time
from datetime import date
from meter_reading_mini.core.timeutil import local_range_to_utc, parse_utc

os.environ["TZ"] = "America/New_York"
time.tzset()
for start, end, expected_hours in (
    (date(2026, 3, 1), date(2026, 4, 1), 743),
    (date(2026, 11, 1), date(2026, 12, 1), 721),
):
    begin, finish = local_range_to_utc(start, end)
    assert (parse_utc(finish) - parse_utc(begin)).total_seconds() == expected_hours * 3600
"""
    environment = os.environ.copy()
    environment.pop("TZ", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
