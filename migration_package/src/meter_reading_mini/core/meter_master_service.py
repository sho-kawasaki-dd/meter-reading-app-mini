"""物理メーター CRUD を扱う service。"""

from __future__ import annotations

import logging

from meter_reading_mini.core.contract_models import Category
from core.database import PhysicalMeterORM
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


METER_CATEGORY_VALUES = [category.value for category in Category]


class MeterMasterServiceError(ValueError):
    """物理メーター service の基底例外。"""


class MeterValidationError(MeterMasterServiceError):
    """フォーム入力検証エラー。"""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("Invalid meter form data.")
        self.errors = errors


class MeterNotFoundError(MeterMasterServiceError):
    """指定された meter_id の物理メーターが見つからない。"""

    def __init__(self, meter_id: str) -> None:
        super().__init__(f"meter_id '{meter_id}' が見つかりません。")
        self.meter_id = meter_id


class MeterCreateRequest(BaseModel):
    """物理メーター新規登録リクエスト。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    meter_id: str
    location_id: str
    location_name: str
    meter_name: str
    category: str
    meter_serial: str | None = None


class MeterUpdateRequest(BaseModel):
    """物理メーター更新リクエスト。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    location_id: str
    location_name: str
    meter_name: str
    category: str
    meter_serial: str | None = None


class MeterMasterService:
    """物理メーターの一覧・作成・更新・論理削除を扱う service。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_meters(self, *, show_inactive: bool) -> list[PhysicalMeterORM]:
        """物理メーター一覧を返す。"""

        query = select(PhysicalMeterORM).order_by(
            PhysicalMeterORM.location_id, PhysicalMeterORM.meter_id
        )
        if not show_inactive:
            query = query.where(PhysicalMeterORM.is_active == True)  # noqa: E712
        return list(self._session.scalars(query).all())

    def get_meter(self, meter_id: str) -> PhysicalMeterORM | None:
        """meter_id で物理メーターを取得する。"""

        return self._session.get(PhysicalMeterORM, meter_id)

    def create_meter(self, form_data: MeterCreateRequest) -> PhysicalMeterORM:
        """新しい物理メーターを追加する。"""

        meter_id = self._normalize_required(form_data.meter_id)
        location_id = self._normalize_required(form_data.location_id)
        location_name = self._normalize_required(form_data.location_name)
        meter_name = self._normalize_required(form_data.meter_name)
        category = self._normalize_required(form_data.category)
        meter_serial = self._normalize_optional(form_data.meter_serial)

        errors = self._collect_errors(
            meter_id=meter_id,
            location_id=location_id,
            location_name=location_name,
            meter_name=meter_name,
            category=category,
        )
        if errors:
            raise MeterValidationError(errors)

        if self._session.get(PhysicalMeterORM, meter_id):
            raise MeterValidationError([f"meter_id '{meter_id}' は既に存在します。"])

        new_meter = PhysicalMeterORM(
            meter_id=meter_id,
            location_id=location_id,
            location_name=location_name,
            meter_name=meter_name,
            category=category,
            meter_serial=meter_serial,
        )
        self._session.add(new_meter)
        logger.info("Physical meter created: %s", meter_id)
        return new_meter

    def update_meter(self, meter_id: str, form_data: MeterUpdateRequest) -> PhysicalMeterORM:
        """既存の物理メーターを更新する。"""

        meter = self.get_meter(meter_id)
        if meter is None:
            raise MeterNotFoundError(meter_id)

        location_id = self._normalize_required(form_data.location_id)
        location_name = self._normalize_required(form_data.location_name)
        meter_name = self._normalize_required(form_data.meter_name)
        category = self._normalize_required(form_data.category)
        meter_serial = self._normalize_optional(form_data.meter_serial)

        errors = self._collect_errors(
            location_id=location_id,
            location_name=location_name,
            meter_name=meter_name,
            category=category,
        )
        if errors:
            raise MeterValidationError(errors)

        meter.location_id = location_id
        meter.location_name = location_name
        meter.meter_name = meter_name
        meter.category = category
        meter.meter_serial = meter_serial
        logger.info("Physical meter updated: %s", meter_id)
        return meter

    def delete_meter(self, meter_id: str) -> None:
        """物理メーターを論理削除する。"""

        meter = self.get_meter(meter_id)
        if meter is None:
            return
        meter.is_active = False
        logger.info("Physical meter deactivated: %s", meter_id)

    def activate_meter(self, meter_id: str) -> None:
        """物理メーターを有効化する。"""

        meter = self.get_meter(meter_id)
        if meter is None:
            return
        meter.is_active = True
        logger.info("Physical meter activated: %s", meter_id)

    @staticmethod
    def _normalize_optional(value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @classmethod
    def _normalize_required(cls, value: str | None) -> str:
        normalized = cls._normalize_optional(value)
        return normalized or ""

    @staticmethod
    def _collect_errors(**values: str) -> list[str]:
        errors: list[str] = []
        for field_name, value in values.items():
            if field_name == "category":
                if value in METER_CATEGORY_VALUES:
                    continue
                errors.append(f"category は {METER_CATEGORY_VALUES} のいずれかを指定してください。")
            elif value:
                continue
            else:
                errors.append(f"{field_name} は必須です。")
        return errors


__all__ = [
    "METER_CATEGORY_VALUES",
    "MeterCreateRequest",
    "MeterMasterService",
    "MeterMasterServiceError",
    "MeterNotFoundError",
    "MeterUpdateRequest",
    "MeterValidationError",
]
