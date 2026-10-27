"""確定担当者の登録・更新・論理削除を扱う service。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace

from meter_reading_mini.core.errors import DomainError, InactiveMasterError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    StaffCreateRequest,
    StaffRecord,
    StaffRepository,
    StaffUpdateRequest,
)

logger = logging.getLogger(__name__)

TransactionFactory = Callable[[], AbstractContextManager[object]]


class StaffValidationError(DomainError):
    """担当者入力の検証エラー。"""


class StaffNotFoundError(DomainError):
    """指定された担当者が存在しないことを表す。"""


class StaffService:
    """Repository を通じて担当者を管理し、更新操作ごとに transaction を持つ。"""

    def __init__(
        self,
        repository: StaffRepository,
        *,
        transaction_factory: TransactionFactory,
    ) -> None:
        self._repository = repository
        self._transaction_factory = transaction_factory

    def list_staff(self, *, include_inactive: bool = False) -> list[StaffRecord]:
        return self._repository.list_staff(include_inactive=include_inactive)

    def get_staff(self, staff_id: str) -> StaffRecord | None:
        return self._repository.get_staff(_normalize_id(staff_id))

    def require_active_staff(self, staff_id: str) -> StaffRecord:
        normalized_id = _normalize_id(staff_id)
        staff = self._repository.get_staff(normalized_id)
        if staff is None or not staff.is_active:
            raise InactiveMasterError(f"有効な担当者を指定してください: {normalized_id}")
        return staff

    def create_staff(self, request: StaffCreateRequest) -> StaffRecord:
        normalized = replace(
            request,
            staff_id=_normalize_id(request.staff_id),
            display_name=_normalize_name(request.display_name),
        )
        with self._transaction_factory():
            if self._repository.get_staff(normalized.staff_id) is not None:
                raise StaffValidationError(
                    f"担当者 ID は既に登録されています: {normalized.staff_id}"
                )
            created = self._repository.create_staff(normalized)
        logger.info("担当者を登録しました: %s", created.staff_id)
        return created

    def update_staff(self, staff_id: str, request: StaffUpdateRequest) -> StaffRecord:
        normalized_id = _normalize_id(staff_id)
        normalized = replace(request, display_name=_normalize_name(request.display_name))
        with self._transaction_factory():
            if self._repository.get_staff(normalized_id) is None:
                raise StaffNotFoundError(f"担当者が見つかりません: {normalized_id}")
            updated = self._repository.update_staff(normalized_id, normalized)
        logger.info("担当者を更新しました: %s", normalized_id)
        return updated

    def deactivate_staff(self, staff_id: str) -> None:
        normalized_id = _normalize_id(staff_id)
        with self._transaction_factory():
            if self._repository.get_staff(normalized_id) is None:
                raise StaffNotFoundError(f"担当者が見つかりません: {normalized_id}")
            self._repository.set_staff_active(normalized_id, False)
        logger.info("担当者を無効化しました: %s", normalized_id)

    def activate_staff(self, staff_id: str) -> None:
        normalized_id = _normalize_id(staff_id)
        with self._transaction_factory():
            if self._repository.get_staff(normalized_id) is None:
                raise StaffNotFoundError(f"担当者が見つかりません: {normalized_id}")
            self._repository.set_staff_active(normalized_id, True)
        logger.info("担当者を再有効化しました: %s", normalized_id)


def _normalize_id(staff_id: str) -> str:
    normalized = staff_id.strip()
    try:
        numeric_id = int(normalized)
    except ValueError as exc:
        raise StaffValidationError("担当者 ID は整数で指定してください。") from exc
    if numeric_id <= 0:
        raise StaffValidationError("担当者 ID は正の整数で指定してください。")
    return str(numeric_id)


def _normalize_name(display_name: str) -> str:
    normalized = display_name.strip()
    if not normalized:
        raise StaffValidationError("担当者名は必須です。")
    return normalized


__all__ = [
    "StaffNotFoundError",
    "StaffService",
    "StaffValidationError",
]
