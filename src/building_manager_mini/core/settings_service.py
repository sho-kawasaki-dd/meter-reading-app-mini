from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from building_manager_mini.core.sender_address import normalize_sender_address
from building_manager_mini.core.settings_contracts import (
    CommonSettingsError,
    SettingsRepository,
    Staff,
)


class SettingsService:
    def __init__(
        self,
        repository: SettingsRepository,
        *,
        transaction_factory: Callable[[], AbstractContextManager[object]],
        has_business_date_dependencies: Callable[[], bool],
    ) -> None:
        self._repository = repository
        self._transaction_factory = transaction_factory
        self._has_business_date_dependencies = has_business_date_dependencies

    def list_staff(self, *, include_inactive: bool = False) -> list[Staff]:
        return self._repository.list_staff(include_inactive=include_inactive)

    def get_staff(self, staff_id: str) -> Staff | None:
        return self._repository.get_staff(_validate_id(staff_id))

    def create_staff(self, display_name: str, *, sender_address: str = "") -> Staff:
        staff = Staff(
            str(uuid4()), _validate_name(display_name), True, _validate_address(sender_address)
        )
        with self._transaction_factory():
            self._repository.insert_staff(staff)
        return staff

    def update_staff(
        self, staff_id: str, display_name: str, *, is_active: bool, sender_address: str
    ) -> Staff:
        staff = Staff(
            _validate_id(staff_id),
            _validate_name(display_name),
            is_active,
            _validate_address(sender_address),
        )
        with self._transaction_factory():
            self._repository.update_staff(staff)
        return staff

    def get_timezone(self) -> ZoneInfo:
        return _validate_timezone(self._repository.get_timezone())

    def save_timezone(self, timezone: str) -> ZoneInfo:
        value = _validate_timezone(timezone.strip())
        with self._transaction_factory():
            if value.key != self.get_timezone().key and self._has_business_date_dependencies():
                raise CommonSettingsError(
                    "業務日依存データがあるためタイムゾーンを変更できません。"
                )
            self._repository.set_timezone(value.key)
        return value


def _validate_id(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError("UUID の表記が不正です。")
    except ValueError as error:
        raise CommonSettingsError("担当者 ID が不正です。") from error
    return value


def _validate_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise CommonSettingsError("担当者名は必須です。")
    return name


def _validate_address(value: str) -> str:
    try:
        return normalize_sender_address(value)
    except ValueError as error:
        raise CommonSettingsError(str(error)) from error


def _validate_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise CommonSettingsError(f"業務タイムゾーンが不正です: {value}") from error
