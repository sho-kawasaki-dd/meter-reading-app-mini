from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TypeGuard

logger = logging.getLogger(__name__)

SETTINGS_FORMAT_VERSION = 1
ALLOWED_FONT_SCALES: tuple[int, ...] = (90, 100, 125, 150)
DEFAULT_FONT_SCALE_PERCENT = 100

type ApplicationSettings = dict[str, dict[str, object]]
type StringKeyedObject = dict[str, object]


def _empty_application_settings() -> ApplicationSettings:
    return {}


def _is_string_keyed_object(value: object) -> TypeGuard[StringKeyedObject]:
    return isinstance(value, dict)


@dataclass(frozen=True, slots=True)
class UISettings:
    font_family: str | None = None
    font_scale_percent: int = DEFAULT_FONT_SCALE_PERCENT
    last_application: str | None = None
    applications: ApplicationSettings = field(default_factory=_empty_application_settings)


def default_ui_settings() -> UISettings:
    return UISettings()


def _parse_application_settings(value: object) -> ApplicationSettings:
    if not _is_string_keyed_object(value):
        return {}
    return {
        app_id: dict(settings)
        for app_id, settings in value.items()
        if app_id and _is_string_keyed_object(settings)
    }


def load_ui_settings(path: Path) -> UISettings:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_ui_settings()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        logger.warning("共通 UI 設定を読み込めません。既定値を使用します: %s (%s)", path, error)
        return default_ui_settings()

    if not _is_string_keyed_object(data) or data.get("format_version") != SETTINGS_FORMAT_VERSION:
        logger.warning("共通 UI 設定の形式が未対応です。既定値を使用します: %s", path)
        return default_ui_settings()

    shared = data.get("shared")
    if not _is_string_keyed_object(shared):
        shared = {}
    font_family_value = shared.get("font_family")
    if font_family_value is None:
        font_family = None
    elif isinstance(font_family_value, str) and font_family_value.strip():
        font_family = font_family_value.strip()
    else:
        logger.warning("共通 UI 設定のフォント名が不正なため OS 既定を使用します")
        font_family = None
    scale_value = shared.get("font_scale_percent")
    if scale_value is None:
        font_scale = DEFAULT_FONT_SCALE_PERCENT
    elif type(scale_value) is int and scale_value in ALLOWED_FONT_SCALES:
        font_scale = scale_value
    else:
        logger.warning(
            "共通 UI 設定の文字サイズが不正なため %d%% を使用します",
            DEFAULT_FONT_SCALE_PERCENT,
        )
        font_scale = DEFAULT_FONT_SCALE_PERCENT
    last_application_value = data.get("last_application")
    last_application = (
        last_application_value
        if isinstance(last_application_value, str) and last_application_value
        else None
    )
    return UISettings(
        font_family=font_family,
        font_scale_percent=font_scale,
        last_application=last_application,
        applications=_parse_application_settings(data.get("applications")),
    )


def save_ui_settings(path: Path, settings: UISettings) -> None:
    applications = _parse_application_settings(settings.applications)
    payload = {
        "format_version": SETTINGS_FORMAT_VERSION,
        "shared": {
            "font_family": settings.font_family,
            "font_scale_percent": settings.font_scale_percent,
        },
        "last_application": settings.last_application,
        "applications": applications,
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def set_last_application(path: Path, application_id: str) -> None:
    if not application_id:
        raise ValueError("アプリ識別子は空にできません。")
    save_ui_settings(path, replace(load_ui_settings(path), last_application=application_id))


def set_application_settings(path: Path, application_id: str, values: dict[str, object]) -> None:
    if not application_id:
        raise ValueError("アプリ識別子は空にできません。")
    settings = load_ui_settings(path)
    applications = _parse_application_settings(settings.applications)
    applications[application_id] = dict(values)
    save_ui_settings(path, replace(settings, applications=applications))


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary_path.unlink()
        raise
