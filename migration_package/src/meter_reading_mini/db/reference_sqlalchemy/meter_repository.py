"""Meter repository implementations."""

from __future__ import annotations

from sqlalchemy import func, select

from api.models import Category, MeterMasterData
from core.database import PhysicalMeterORM, ReadingTransactionORM

from ._shared import SQLAlchemyRepositoryContext


class SQLAlchemyMeterMasterQueryRepository(SQLAlchemyRepositoryContext):
	"""Query physical meter master data for Android downloads."""

	def list_meter_master_data(self) -> list[MeterMasterData]:
		rows = self._session.scalars(
			select(PhysicalMeterORM)
			.where(PhysicalMeterORM.is_active == True)  # noqa: E712
			.order_by(PhysicalMeterORM.location_id, PhysicalMeterORM.meter_id)
		).all()
		return [
			MeterMasterData(
				meter_id=row.meter_id,
				location_id=row.location_id,
				location_name=row.location_name,
				meter_name=row.meter_name,
				category=Category(row.category),
				meter_serial=row.meter_serial,
			)
			for row in rows
		]


class SQLAlchemyRecentDeviceIdRepository(SQLAlchemyRepositoryContext):
	"""List recent device IDs ordered by latest reading activity."""

	def list_recent_device_ids(self, *, limit: int) -> list[str]:
		if limit <= 0:
			return []

		last_seen = func.max(ReadingTransactionORM.client_updated_at)
		rows = self._session.execute(
			select(ReadingTransactionORM.device_id)
			.where(ReadingTransactionORM.device_id != "")
			.group_by(ReadingTransactionORM.device_id)
			.order_by(last_seen.desc(), ReadingTransactionORM.device_id.asc())
			.limit(limit)
		).all()
		return [row[0] for row in rows]
