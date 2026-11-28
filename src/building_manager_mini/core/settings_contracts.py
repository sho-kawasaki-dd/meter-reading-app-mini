from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class CommonSettingsError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Staff:
    staff_id: str
    display_name: str
    is_active: bool
    sender_address: str


class SettingsRepository(Protocol):
    def list_staff(self, *, include_inactive: bool) -> list[Staff]: ...
    def get_staff(self, staff_id: str) -> Staff | None: ...
    def insert_staff(self, staff: Staff) -> None: ...
    def update_staff(self, staff: Staff) -> None: ...
    def get_timezone(self) -> str: ...
    def set_timezone(self, timezone: str) -> None: ...
