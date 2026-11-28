from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from building_manager_mini.settings import (
    ALLOWED_FONT_SCALES,
    DEFAULT_FONT_SCALE_PERCENT,
    UISettings,
    load_ui_settings,
    save_ui_settings,
)

logger = logging.getLogger(__name__)

# 起動時に取得した OS 既定フォント。倍率はこのフォントに対して適用する。
_base_font: QFont | None = None


@dataclass(frozen=True, slots=True)
class DisplaySettings:
    font_family: str | None = None
    font_scale_percent: int = DEFAULT_FONT_SCALE_PERCENT


def default_display_settings() -> DisplaySettings:
    return DisplaySettings()


def load_display_settings(path: Path) -> DisplaySettings:
    settings = load_ui_settings(path)
    return DisplaySettings(
        font_family=_parse_font_family(settings.font_family),
        font_scale_percent=_parse_font_scale(settings.font_scale_percent),
    )


def _parse_font_family(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    logger.warning("UI 表示設定のフォント名が不正なため OS 既定を使用します")
    return None


def _parse_font_scale(value: object) -> int:
    if value is None:
        return DEFAULT_FONT_SCALE_PERCENT
    # bool は int のサブクラスだが数値として受け付けない
    if isinstance(value, int) and not isinstance(value, bool) and value in ALLOWED_FONT_SCALES:
        return value
    logger.warning(
        "UI 表示設定の文字サイズが不正なため %d%% を使用します", DEFAULT_FONT_SCALE_PERCENT
    )
    return DEFAULT_FONT_SCALE_PERCENT


def save_display_settings(path: Path, settings: DisplaySettings) -> None:
    current = load_ui_settings(path)
    save_ui_settings(
        path,
        UISettings(
            font_family=settings.font_family,
            font_scale_percent=settings.font_scale_percent,
            last_application=current.last_application,
            applications=current.applications,
        ),
    )


def capture_base_font() -> QFont:
    global _base_font
    if _base_font is None:
        _base_font = QFont(QApplication.font())
    return QFont(_base_font)


def apply_display_settings(settings: DisplaySettings) -> None:
    font = capture_base_font()

    family = settings.font_family
    if family is not None:
        if family in QFontDatabase.families():
            font.setFamily(family)
        else:
            logger.warning("フォントが見つからないため OS 既定を使用します: %s", family)

    _scale_font(font, settings.font_scale_percent)
    QApplication.setFont(font)


def reset_display_settings() -> DisplaySettings:
    settings = default_display_settings()
    apply_display_settings(settings)
    return settings


def _scale_font(font: QFont, percent: int) -> None:
    point_size = font.pointSizeF()
    if point_size > 0:
        font.setPointSizeF(point_size * percent / 100)
        return
    # ポイント指定が使えない場合はピクセルサイズで拡縮する
    pixel_size = font.pixelSize()
    if pixel_size > 0:
        font.setPixelSize(max(1, round(pixel_size * percent / 100)))
