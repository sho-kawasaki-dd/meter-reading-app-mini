from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError as PydanticValidationError
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QBrush

from meter_reading_mini.core.contract_models import (
    Category,
    ReadingCycle,
    ReadingSession,
    ResultStatus,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    PlannedReadingCloseReason,
    PlannedReadingTaskState,
    ReadingOverviewRecord,
)
from meter_reading_mini.ui.models.reading_overview_model import ReadingOverviewTableModel


def _reading() -> ReadingOverviewRecord:
    return ReadingOverviewRecord(
        planned_reading_id="slot",
        measurement_definition_id="definition",
        meter_id="meter",
        location_id="location",
        location_name="試験棟",
        meter_name="水道メーター",
        display_name="使用水量",
        category=Category.WATER,
        reading_cycle=ReadingCycle.DAILY_1X,
        unit="m3",
        business_date=date(2026, 2, 4),
        reading_session=ReadingSession.SESSION_0800,
        scheduled_at=datetime(2026, 2, 4, 1, tzinfo=UTC),
        task_state=PlannedReadingTaskState.OPEN,
        close_reason=None,
        superseded_by_planned_reading_id=None,
        result_status=None,
        raw_reading=None,
        structured_payload_json=None,
        last_reading=None,
        usage_delta=None,
        warning_threshold=None,
        recorded_at=None,
        note=None,
    )


def test_model_formats_values_statuses_colors_and_details(qtbot: object) -> None:
    base = _reading()
    warning = replace(
        base,
        planned_reading_id="warning",
        task_state=PlannedReadingTaskState.COMPLETED,
        result_status=ResultStatus.MEASURED,
        raw_reading=Decimal("100.250"),
        last_reading=Decimal("50.00"),
        usage_delta=Decimal("50.250"),
        warning_threshold=Decimal("50"),
    )
    power_factor = replace(
        base,
        planned_reading_id="power-factor",
        task_state=PlannedReadingTaskState.COMPLETED,
        result_status=ResultStatus.MEASURED,
        structured_payload_json='{"percent":"95.50","phase":"lag"}',
    )
    closed = replace(
        base,
        planned_reading_id="closed",
        task_state=PlannedReadingTaskState.AUTO_CLOSED,
        close_reason=PlannedReadingCloseReason.CUTOFF_ELAPSED,
    )
    old = replace(
        base,
        planned_reading_id="old",
        task_state=PlannedReadingTaskState.SUPERSEDED,
        close_reason=PlannedReadingCloseReason.METER_REPLACED,
        superseded_by_planned_reading_id="next",
        result_status=ResultStatus.METER_FAULT,
        note="故障を確認",
    )
    model = ReadingOverviewTableModel()

    model.set_readings([base, warning, power_factor, closed, old], ZoneInfo("Asia/Tokyo"))

    assert model.rowCount() == 5
    assert model.columnCount() == 12
    assert model.rowCount(model.index(0, 0)) == 0
    assert model.columnCount(model.index(0, 0)) == 0
    assert model.headerData(0, Qt.Orientation.Horizontal) == "予定日時"
    assert model.headerData(0, Qt.Orientation.Vertical) is None
    assert model.data(QModelIndex()) is None
    assert model.data(model.index(0, 0)) == "2026/02/04 10:00"
    assert model.data(model.index(1, 7)) == "100.250"
    assert model.data(model.index(1, 8)) == "50.00"
    assert model.data(model.index(1, 9)) == "50.250"
    assert model.data(model.index(2, 7)) == "95.50%（遅れ）"
    assert model.data(model.index(4, 5)) == "差し替え済み"
    assert model.data(model.index(4, 6)) == "旧結果: 計器不良"
    assert model.data(model.index(4, 11)) == "メーター交換"
    assert "後継枠 ID: next" in str(model.data(model.index(4, 0), Qt.ItemDataRole.ToolTipRole))
    assert "故障を確認" in str(model.data(model.index(4, 0), Qt.ItemDataRole.ToolTipRole))
    for row, color in ((0, "#dceeff"), (1, "#fff1a8"), (3, "#e4e4e4")):
        brush = model.data(model.index(row, 0), Qt.ItemDataRole.BackgroundRole)
        assert isinstance(brush, QBrush)
        assert brush.color().name() == color
    assert model.data(model.index(2, 0), Qt.ItemDataRole.BackgroundRole) is None
    assert model.row_for_id("old") == 4
    assert model.row_for_id("missing") is None
    assert model.reading_at(-1) is None
    assert model.reading_at(5) is None


def test_model_rejects_bad_power_factor_without_replacing_rows(qtbot: object) -> None:
    model = ReadingOverviewTableModel()
    base = _reading()
    model.set_readings([base], UTC)
    invalid = replace(base, raw_reading=None, structured_payload_json='{"percent":"110"}')

    with pytest.raises(PydanticValidationError):
        model.set_readings([invalid], UTC)
    with pytest.raises(ValueError, match="形式が重複"):
        model.set_readings([replace(invalid, raw_reading=Decimal("1"))], UTC)

    assert model.rowCount() == 1
    assert model.reading_at(0) == base
