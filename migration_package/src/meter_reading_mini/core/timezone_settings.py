"""app_settings からアプリ固定タイムゾーンを読み書きする service。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

APP_TIMEZONE_SETTINGS_KEY = "app.timezone.v1"
DEFAULT_APP_TIMEZONE_NAME = "Asia/Tokyo"
DEFAULT_APP_TIMEZONE = ZoneInfo(DEFAULT_APP_TIMEZONE_NAME)


class TimezoneSettingsRepository(Protocol):
	"""app_settings から timezone 設定を読み書きする repository。"""

	def get_setting(self, key: str) -> str | None: ...
	def upsert_setting(self, key: str, value: str) -> None: ...


def _normalize_timezone_name(timezone_name: str | ZoneInfo) -> str:
	"""保存前の timezone 名を検証し、canonical 文字列へ正規化する。"""

	if isinstance(timezone_name, ZoneInfo):
		normalized = timezone_name.key
	else:
		normalized = timezone_name.strip()

	if not normalized:
		raise ValueError("timezone_name must not be blank.")

	try:
		ZoneInfo(normalized)
	except ZoneInfoNotFoundError as exc:  # pragma: no cover - validated by callers
		raise ValueError(f"Invalid timezone: {normalized}") from exc

	return normalized


def build_current_datetime_provider(timezone_provider: Callable[[], tzinfo] | None = None) -> Callable[[], datetime]:
	"""timezone-aware な現在時刻 provider を組み立てる。"""

	resolved_timezone_provider = timezone_provider or (lambda: DEFAULT_APP_TIMEZONE)
	return lambda: datetime.now(tz=resolved_timezone_provider())


def build_business_date_provider(timezone_provider: Callable[[], tzinfo] | None = None) -> Callable[[], date]:
	"""timezone-aware な business_date provider を組み立てる。"""

	resolved_timezone_provider = timezone_provider or (lambda: DEFAULT_APP_TIMEZONE)
	return lambda: datetime.now(tz=resolved_timezone_provider()).date()


class TimezoneSettingsService:
	"""app timezone を読み書きする service。"""

	def __init__(self, repository: TimezoneSettingsRepository) -> None:
		self._repository = repository

	def get_timezone(self) -> ZoneInfo:
		"""現在の app timezone を返す。未設定なら Asia/Tokyo。"""

		raw = self._repository.get_setting(APP_TIMEZONE_SETTINGS_KEY)
		if raw is None:
			return DEFAULT_APP_TIMEZONE

		try:
			return ZoneInfo(raw)
		except ZoneInfoNotFoundError as exc:
			raise ValueError(f"Invalid timezone stored in app_settings: {raw}") from exc

	def save_timezone(self, timezone_name: str | ZoneInfo) -> ZoneInfo:
		"""app timezone を保存し、canonical ZoneInfo を返す。"""

		normalized = _normalize_timezone_name(timezone_name)
		self._repository.upsert_setting(APP_TIMEZONE_SETTINGS_KEY, normalized)
		logger.info("Saved app timezone: %s", normalized)
		return ZoneInfo(normalized)


__all__ = [
	"APP_TIMEZONE_SETTINGS_KEY",
	"DEFAULT_APP_TIMEZONE",
	"DEFAULT_APP_TIMEZONE_NAME",
	"TimezoneSettingsService",
	"build_business_date_provider",
	"build_current_datetime_provider",
]