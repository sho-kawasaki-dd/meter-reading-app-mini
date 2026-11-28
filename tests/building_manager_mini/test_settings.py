from __future__ import annotations

import json
from pathlib import Path

from building_manager_mini.settings import (
    SETTINGS_FORMAT_VERSION,
    UISettings,
    load_ui_settings,
    save_ui_settings,
    set_application_settings,
    set_last_application,
)
from meter_reading_mini.ui.display_settings import DisplaySettings, save_display_settings


def test_versioned_settings_round_trip_shared_and_application_keys(tmp_path: Path) -> None:
    path = tmp_path / "ui_settings.json"
    settings = UISettings(
        font_family="Yu Gothic",
        font_scale_percent=125,
        last_application="meter_reading",
        applications={"meter_reading": {"compact_mode": True}},
    )

    save_ui_settings(path, settings)

    assert load_ui_settings(path) == settings
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "format_version": SETTINGS_FORMAT_VERSION,
        "shared": {"font_family": "Yu Gothic", "font_scale_percent": 125},
        "last_application": "meter_reading",
        "applications": {"meter_reading": {"compact_mode": True}},
    }


def test_setting_last_application_preserves_other_application_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ui_settings.json"
    save_ui_settings(
        path,
        UISettings(
            font_family="Meiryo",
            font_scale_percent=150,
            applications={"meter_reading": {"compact_mode": False}},
        ),
    )

    set_last_application(path, "work_log")
    set_application_settings(path, "work_log", {"selected_view": "calendar"})

    result = load_ui_settings(path)
    assert result.font_family == "Meiryo"
    assert result.font_scale_percent == 150
    assert result.last_application == "work_log"
    assert result.applications == {
        "meter_reading": {"compact_mode": False},
        "work_log": {"selected_view": "calendar"},
    }


def test_saving_shared_display_values_preserves_last_app_and_app_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ui_settings.json"
    save_ui_settings(
        path,
        UISettings(
            last_application="work_log",
            applications={"work_log": {"selected_view": "calendar"}},
        ),
    )

    save_display_settings(path, DisplaySettings("Meiryo", 125))

    result = load_ui_settings(path)
    assert result.font_family == "Meiryo"
    assert result.font_scale_percent == 125
    assert result.last_application == "work_log"
    assert result.applications == {"work_log": {"selected_view": "calendar"}}


def test_missing_settings_start_with_defaults(tmp_path: Path) -> None:
    result = load_ui_settings(tmp_path / "missing.json")

    assert result == UISettings(applications={})
