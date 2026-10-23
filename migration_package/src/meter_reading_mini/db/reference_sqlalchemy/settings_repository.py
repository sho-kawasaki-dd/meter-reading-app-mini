"""Settings repository adapters for app_settings access."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.database import AppSettingsORM, AppSettingsRepository


@runtime_checkable
class APP_SETTINGS_REPOSITORY_PROTOCOL(Protocol):
	"""Settings-style repository contract used by application services."""

	def get_setting(self, key: str) -> str | None: ...
	def upsert_setting(self, key: str, value: str) -> None: ...
	def delete_setting(self, key: str) -> None: ...


class SQLAlchemySettingsRepository(AppSettingsRepository):
	"""Concrete settings repository with flush-on-write semantics."""

	def get_value(self, key: str) -> str | None:
		return self.get_setting(key)

	def set_value(self, key: str, value: str) -> None:
		self.upsert_setting(key, value)

	def get_setting(self, key: str) -> str | None:
		row = self._session.get(AppSettingsORM, key)
		if row is None:
			return None
		return row.setting_value

	def upsert_setting(self, key: str, value: str) -> None:
		row = self._session.get(AppSettingsORM, key)
		if row is None:
			self._session.add(AppSettingsORM(setting_key=key, setting_value=value))
		else:
			row.setting_value = value
		self._session.flush()

	def delete_setting(self, key: str) -> None:
		row = self._session.get(AppSettingsORM, key)
		if row is not None:
			self._session.delete(row)
			self._session.flush()
