"""Composite adapter that aggregates the split SQLAlchemy repositories."""

from __future__ import annotations

from collections.abc import Callable
from datetime import tzinfo

from sqlalchemy.orm import Session

from application.meter_reading_sync_repositories import ReleaseDateCalculator
from application.timezone_settings import DEFAULT_APP_TIMEZONE

from .measurement_definition_repository import (
	SQLAlchemyMeasurementDefinitionLookupRepository,
	SQLAlchemyMeasurementDefinitionQueryRepository,
	SQLAlchemyMeasurementDefinitionScheduleRepository,
)
from .meter_repository import SQLAlchemyMeterMasterQueryRepository, SQLAlchemyRecentDeviceIdRepository
from .planned_reading_repository import (
	SQLAlchemyPlannedReadingGenerationRepository,
	SQLAlchemyPlannedReadingLookupRepository,
	SQLAlchemyPlannedReadingRecompositionRepository,
	SQLAlchemyPlannedReadingTaskQueryRepository,
)
from .reading_repository import SQLAlchemyHistoricalReadingLookupRepository, SQLAlchemyReadingPersistenceRepository
from .replacement_repository import (
	SQLAlchemyReplacementBoundaryLookupRepository,
	SQLAlchemyReplacementEventUpsertRepository,
	SQLAlchemyReplacementReflectionRepository,
)
from .settings_repository import SQLAlchemySettingsRepository


class SQLAlchemyMeterReadingSyncRepositoryAdapter(
	SQLAlchemySettingsRepository,
	SQLAlchemyRecentDeviceIdRepository,
	SQLAlchemyPlannedReadingLookupRepository,
	SQLAlchemyMeasurementDefinitionLookupRepository,
	SQLAlchemyMeasurementDefinitionScheduleRepository,
	SQLAlchemyHistoricalReadingLookupRepository,
	SQLAlchemyPlannedReadingGenerationRepository,
	SQLAlchemyPlannedReadingRecompositionRepository,
	SQLAlchemyMeterMasterQueryRepository,
	SQLAlchemyMeasurementDefinitionQueryRepository,
	SQLAlchemyPlannedReadingTaskQueryRepository,
	SQLAlchemyReadingPersistenceRepository,
	SQLAlchemyReplacementEventUpsertRepository,
	SQLAlchemyReplacementReflectionRepository,
	SQLAlchemyReplacementBoundaryLookupRepository,
):
	"""Aggregate split repository implementations behind a single Session-backed adapter."""

	def __init__(
		self,
		session: Session,
		*,
		release_date_calculator: ReleaseDateCalculator | None = None,
		scheduled_timezone_provider: Callable[[], tzinfo] | None = None,
	) -> None:
		self._session = session
		self._release_date_calculator = release_date_calculator
		self._scheduled_timezone_provider = scheduled_timezone_provider or (lambda: DEFAULT_APP_TIMEZONE)

	def set_release_date_calculator(self, release_date_calculator: ReleaseDateCalculator | None) -> None:
		self._release_date_calculator = release_date_calculator

	def set_scheduled_timezone_provider(self, scheduled_timezone_provider: Callable[[], tzinfo] | None) -> None:
		self._scheduled_timezone_provider = scheduled_timezone_provider or (lambda: DEFAULT_APP_TIMEZONE)

	@property
	def session(self) -> Session:
		return self._session


__all__ = ["SQLAlchemyMeterReadingSyncRepositoryAdapter"]