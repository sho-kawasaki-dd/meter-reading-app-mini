from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from meter_reading_mini.ui.display_settings import (
    DisplaySettings,
    apply_display_settings,
    capture_base_font,
    default_display_settings,
    load_display_settings,
    reset_display_settings,
    save_display_settings,
)

LOGGER_NAME = "meter_reading_mini.ui.display_settings"


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.WARNING]


def test_default_settings() -> None:
    settings = default_display_settings()

    assert settings == DisplaySettings(
        font_family=None,
        font_scale_percent=100,
    )


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ui_settings.json"
    settings = DisplaySettings(
        font_family="日本語フォント",
        font_scale_percent=125,
    )

    save_display_settings(path, settings)

    assert load_display_settings(path) == settings
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "font_family": "日本語フォント",
        "font_scale_percent": 125,
    }
    assert [p.name for p in tmp_path.iterdir()] == ["ui_settings.json"]


def test_save_and_load_default_font(tmp_path: Path) -> None:
    path = tmp_path / "ui_settings.json"

    save_display_settings(path, DisplaySettings(None, 90))

    assert load_display_settings(path) == DisplaySettings(None, 90)


def test_load_missing_file_returns_defaults_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    result = load_display_settings(tmp_path / "missing.json")

    assert result == default_display_settings()
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize(
    "raw",
    [b"{not json", b"\xff\xfe\x00bad", b""],
    ids=["syntax", "invalid-utf8", "empty"],
)
def test_load_broken_file_returns_defaults_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, raw: bytes
) -> None:
    path = tmp_path / "ui_settings.json"
    path.write_bytes(raw)
    caplog.set_level(logging.WARNING)

    result = load_display_settings(path)

    assert result == default_display_settings()
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize(
    "scale_json",
    ['"150"', "150.0", "true", "false", "110", "0", "-100", '{"a": 1}', "[]"],
)
def test_load_invalid_scale_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, scale_json: str
) -> None:
    path = _write(
        tmp_path / "ui_settings.json",
        f'{{"font_family": "Meiryo", "font_scale_percent": {scale_json}}}',
    )
    caplog.set_level(logging.WARNING)

    result = load_display_settings(path)

    assert result == DisplaySettings("Meiryo", 100)
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("family_json", ['""', '"   "', "123", "true", "[]"])
def test_load_invalid_family_falls_back_and_keeps_valid_scale(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, family_json: str
) -> None:
    path = _write(
        tmp_path / "ui_settings.json",
        f'{{"font_family": {family_json}, "font_scale_percent": 125}}',
    )
    caplog.set_level(logging.WARNING)

    result = load_display_settings(path)

    assert result == DisplaySettings(None, 125)
    assert len(_warnings(caplog)) == 1


def test_load_missing_keys_use_defaults_without_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path / "ui_settings.json", "{}")
    caplog.set_level(logging.WARNING)

    assert load_display_settings(path) == default_display_settings()
    assert _warnings(caplog) == []


def test_save_failure_keeps_existing_file_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ui_settings.json"
    save_display_settings(path, DisplaySettings("Meiryo", 125))
    before = path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "replace", fail)

    with pytest.raises(PermissionError):
        save_display_settings(path, DisplaySettings(None, 150))

    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["ui_settings.json"]


def test_apply_scales_base_font_and_resets(restore_app_font: QFont) -> None:
    base_size = capture_base_font().pointSizeF()

    apply_display_settings(DisplaySettings(None, 150))
    assert QApplication.font().pointSizeF() == pytest.approx(base_size * 1.5)

    apply_display_settings(DisplaySettings(None, 90))
    assert QApplication.font().pointSizeF() == pytest.approx(base_size * 0.9)

    families = QFontDatabase.families()
    assert families
    apply_display_settings(DisplaySettings(families[-1], 125))
    assert QApplication.font().family() == families[-1]

    result = reset_display_settings()
    assert result == default_display_settings()
    assert QApplication.font().family() == restore_app_font.family()
