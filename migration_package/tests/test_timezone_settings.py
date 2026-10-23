# pyright: reportMissingImports=false

"""app timezone settings と timezone-aware helper の検証テスト。"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from application.timezone_settings import (  # noqa: E402
	APP_TIMEZONE_SETTINGS_KEY,
	DEFAULT_APP_TIMEZONE,
	DEFAULT_APP_TIMEZONE_NAME,
	TimezoneSettingsService,
)
from core.bootstrap import bootstrap_default_settings  # noqa: E402
from core.database import AppSettingsORM, build_scheduled_at, create_database_schema, create_session_factory, create_sqlite_engine  # noqa: E402
from core.repositories.settings_repository import SQLAlchemySettingsRepository  # noqa: E402
from core.startup_diagnostics import log_app_timezone_mismatch  # noqa: E402
import core.startup_diagnostics as startup_diagnostics_module  # noqa: E402


def build_session_factory():
	engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
	create_database_schema(engine)
	return create_session_factory(engine)


def test_timezone_settings_service_returns_default_timezone_when_db_empty() -> None:
	"""DB が空なら Asia/Tokyo を既定 timezone として返す。"""

	session_factory = build_session_factory()

	with session_factory() as session:
		repository = SQLAlchemySettingsRepository(session)
		service = TimezoneSettingsService(repository)

		assert service.get_timezone().key == DEFAULT_APP_TIMEZONE_NAME
		assert service.get_timezone() == DEFAULT_APP_TIMEZONE


def test_timezone_settings_service_saves_and_reads_timezone() -> None:
	"""timezone 設定を保存して読み戻せることを確認する。"""

	session_factory = build_session_factory()

	with session_factory() as session:
		repository = SQLAlchemySettingsRepository(session)
		service = TimezoneSettingsService(repository)

		saved_timezone = service.save_timezone("UTC")
		assert saved_timezone.key == "UTC"
		assert service.get_timezone().key == "UTC"

		setting = session.get(AppSettingsORM, APP_TIMEZONE_SETTINGS_KEY)
		assert setting is not None
		assert setting.setting_value == "UTC"


def test_build_scheduled_at_defaults_to_app_timezone() -> None:
	"""scheduled_at の既定 timezone が host local ではなく app timezone になることを確認する。"""

	assert build_scheduled_at(date(2026, 4, 1), "08:00") == datetime(2026, 3, 31, 23, 0, tzinfo=timezone.utc)


def test_build_scheduled_at_applies_day_offset_before_combining_time() -> None:
	"""day_offset を足した日付に対して scheduled_at を組み立てる。"""

	assert build_scheduled_at(date(2026, 4, 1), "00:00", day_offset=1) == datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)


def test_log_app_timezone_mismatch_warns_when_local_timezone_differs(caplog: pytest.LogCaptureFixture) -> None:
	"""OS timezone と app timezone が異なると WARN を出す。"""

	session_factory = build_session_factory()
	bootstrap_default_settings(session_factory)

	with caplog.at_level("WARNING"):
		log_app_timezone_mismatch(session_factory, local_timezone=timezone.utc)

	assert "OS timezone" in caplog.text
	assert DEFAULT_APP_TIMEZONE_NAME in caplog.text


def test_log_app_timezone_mismatch_does_not_warn_when_timezones_match(
	caplog: pytest.LogCaptureFixture,
) -> None:
	"""OS timezone と app timezone が一致すると WARN を出さない。"""

	session_factory = build_session_factory()

	with caplog.at_level("WARNING"):
		log_app_timezone_mismatch(session_factory, local_timezone=DEFAULT_APP_TIMEZONE)

	assert caplog.records == []


def test_log_app_timezone_mismatch_returns_early_when_local_timezone_is_unavailable(
	monkeypatch: pytest.MonkeyPatch,
	caplog: pytest.LogCaptureFixture,
) -> None:
	"""local timezone を取得できないときは DB を触らず early-return する。"""

	class _NoTimezoneNow:
		def astimezone(self):
			return type("_NoTimezoneAware", (), {"tzinfo": None})()

	class _FakeDatetime:
		@staticmethod
		def now() -> _NoTimezoneNow:
			return _NoTimezoneNow()

	def failing_session_factory():
		raise AssertionError("session_factory should not be called when local timezone is unavailable")

	monkeypatch.setattr(startup_diagnostics_module, "datetime", _FakeDatetime)

	with caplog.at_level("WARNING"):
		log_app_timezone_mismatch(failing_session_factory)  # type: ignore[arg-type]

	assert caplog.records == []