from __future__ import annotations

from datetime import date, timedelta

import pytest
from PySide6.QtCore import QDate
from pytestqt.qtbot import QtBot

from meter_reading_mini.core.contract_models import ReadingCycle, ReadingSession
from meter_reading_mini.core.errors import ScheduleValidationError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    DailySchedule,
    EveryTwoMonthsSchedule,
    MonthlySchedule,
    MonthlyScheduleSelectorType,
    MonthParity,
    ScheduleItem,
    ScheduleSettings,
    WeeklySchedule,
)
from meter_reading_mini.ui.widgets.schedule_editor import ScheduleEditor, schedule_description

TODAY = date(2026, 2, 4)


@pytest.mark.parametrize(
    ("cycle", "schedule"),
    [
        (
            ReadingCycle.DAILY_1X,
            DailySchedule((ScheduleItem(ReadingSession.SESSION_0000, -1),)),
        ),
        (
            ReadingCycle.DAILY_2X,
            DailySchedule(
                (
                    ScheduleItem(ReadingSession.SESSION_2200, 1),
                    ScheduleItem(ReadingSession.SESSION_0600, -1),
                )
            ),
        ),
        (
            ReadingCycle.DAILY_3X,
            DailySchedule(
                (
                    ScheduleItem(ReadingSession.SESSION_1700, 2),
                    ScheduleItem(ReadingSession.SESSION_0800, 0),
                    ScheduleItem(ReadingSession.SESSION_0000, -2),
                )
            ),
        ),
        (ReadingCycle.WEEKLY, WeeklySchedule(3, ReadingSession.SESSION_1400, -2)),
        (
            ReadingCycle.MONTHLY,
            MonthlySchedule(
                MonthlyScheduleSelectorType.DAY_OF_MONTH,
                day_of_month=31,
                reading_session=ReadingSession.SESSION_2200,
                day_offset=1,
            ),
        ),
        (
            ReadingCycle.MONTHLY,
            MonthlySchedule(
                MonthlyScheduleSelectorType.NTH_WEEKDAY,
                nth=5,
                nth_weekday=6,
                reading_session=ReadingSession.SESSION_0600,
                day_offset=-1,
            ),
        ),
        (
            ReadingCycle.MONTHLY,
            MonthlySchedule(
                MonthlyScheduleSelectorType.MONTH_EDGE,
                edge="last",
                reading_session=ReadingSession.SESSION_1700,
                day_offset=3,
            ),
        ),
        (
            ReadingCycle.EVERY_TWO_MONTHS,
            EveryTwoMonthsSchedule(
                MonthlyScheduleSelectorType.DAY_OF_MONTH,
                MonthParity.EVEN,
                day_of_month=29,
                reading_session=ReadingSession.SESSION_0000,
                day_offset=-2,
            ),
        ),
        (
            ReadingCycle.EVERY_TWO_MONTHS,
            EveryTwoMonthsSchedule(
                MonthlyScheduleSelectorType.NTH_WEEKDAY,
                MonthParity.ODD,
                nth=2,
                nth_weekday=4,
                reading_session=ReadingSession.SESSION_1400,
                day_offset=1,
            ),
        ),
        (
            ReadingCycle.EVERY_TWO_MONTHS,
            EveryTwoMonthsSchedule(
                MonthlyScheduleSelectorType.MONTH_EDGE,
                MonthParity.EVEN,
                edge="first",
                reading_session=ReadingSession.SESSION_0800,
                day_offset=-3,
            ),
        ),
    ],
)
def test_schedule_editor_round_trips_all_fields(
    qtbot: QtBot, cycle: ReadingCycle, schedule: ScheduleSettings
) -> None:
    editor = ScheduleEditor(lambda: TODAY)
    qtbot.addWidget(editor)
    editor.set_schedule(cycle, schedule)

    assert editor.schedule() == schedule
    assert schedule_description(schedule)
    assert editor.effective_date() == TODAY


def test_daily_count_duplicate_and_invalid_offset_are_rejected(qtbot: QtBot) -> None:
    editor = ScheduleEditor(lambda: TODAY)
    qtbot.addWidget(editor)
    editor.set_schedule(
        ReadingCycle.DAILY_2X,
        DailySchedule((ScheduleItem(ReadingSession.SESSION_0800),)),
    )
    with pytest.raises(ScheduleValidationError, match="2 件"):
        editor.schedule()

    editor.add_daily_item()
    with pytest.raises(ScheduleValidationError, match="重複"):
        editor.schedule()
    offset = editor.daily_table.cellWidget(0, 1)
    assert offset is not None
    offset.setProperty("text", "途中入力")
    with pytest.raises(ValidationError, match="整数"):
        editor.schedule()


def test_effective_date_is_checked_again_after_business_day_changes(qtbot: QtBot) -> None:
    today = TODAY
    editor = ScheduleEditor(lambda: today)
    qtbot.addWidget(editor)
    assert editor.effective_date_edit.minimumDate() == QDate(2026, 2, 4)
    today += timedelta(days=1)
    with pytest.raises(ValidationError, match="過去"):
        editor.effective_date()


def test_day_offset_must_produce_a_representable_date(qtbot: QtBot) -> None:
    editor = ScheduleEditor(lambda: TODAY)
    qtbot.addWidget(editor)
    editor.set_schedule(
        ReadingCycle.DAILY_1X, DailySchedule((ScheduleItem(ReadingSession.SESSION_0800),))
    )
    editor.daily_table.cellWidget(0, 1).setProperty("text", "999999999")
    with pytest.raises(ValidationError, match="日付.*対応範囲外"):
        editor.schedule()
