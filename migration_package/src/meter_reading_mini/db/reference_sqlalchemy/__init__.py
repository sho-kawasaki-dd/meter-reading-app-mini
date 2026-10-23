"""Split repository adapters for the PC app."""

from .measurement_definition_repository import (
	SQLAlchemyMeasurementDefinitionLookupRepository,
	SQLAlchemyMeasurementDefinitionQueryRepository,
	SQLAlchemyMeasurementDefinitionScheduleRepository,
)
from .meter_reading_sync_repository_adapter import SQLAlchemyMeterReadingSyncRepositoryAdapter
from .meter_repository import SQLAlchemyMeterMasterQueryRepository, SQLAlchemyRecentDeviceIdRepository
from .planned_reading_repository import (
	SQLAlchemyPlannedReadingGenerationRepository,
	SQLAlchemyPlannedReadingLifecycleRepository,
	SQLAlchemyPlannedReadingLookupRepository,
	SQLAlchemyPlannedReadingRecompositionRepository,
	SQLAlchemyPlannedReadingTaskQueryRepository,
)
from .reading_repository import (
	SQLAlchemyHistoricalReadingLookupRepository,
	SQLAlchemyReadingPersistenceRepository,
)
from .replacement_repository import (
	SQLAlchemyReplacementBoundaryLookupRepository,
	SQLAlchemyReplacementEventUpsertRepository,
	SQLAlchemyReplacementReflectionRepository,
)
from .settings_repository import APP_SETTINGS_REPOSITORY_PROTOCOL, SQLAlchemySettingsRepository

__all__ = [
	"APP_SETTINGS_REPOSITORY_PROTOCOL",
	"SQLAlchemyMeasurementDefinitionLookupRepository",
	"SQLAlchemyMeasurementDefinitionQueryRepository",
	"SQLAlchemyMeasurementDefinitionScheduleRepository",
	"SQLAlchemyMeterReadingSyncRepositoryAdapter",
	"SQLAlchemyMeterMasterQueryRepository",
	"SQLAlchemyRecentDeviceIdRepository",
	"SQLAlchemyPlannedReadingGenerationRepository",
	"SQLAlchemyPlannedReadingLifecycleRepository",
	"SQLAlchemyPlannedReadingLookupRepository",
	"SQLAlchemyPlannedReadingRecompositionRepository",
	"SQLAlchemyPlannedReadingTaskQueryRepository",
	"SQLAlchemyHistoricalReadingLookupRepository",
	"SQLAlchemyReadingPersistenceRepository",
	"SQLAlchemyReplacementBoundaryLookupRepository",
	"SQLAlchemyReplacementEventUpsertRepository",
	"SQLAlchemyReplacementReflectionRepository",
	"SQLAlchemySettingsRepository",
]