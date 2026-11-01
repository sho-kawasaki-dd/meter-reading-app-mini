"""アプリケーションで共有するサービスを組み立てる。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial

from meter_reading_mini.core.assignment_calculation_settings import (
    AssignmentCalculationSettingsService,
)
from meter_reading_mini.core.measurement_definition_service import MeasurementDefinitionService
from meter_reading_mini.core.meter_master_service import MeterMasterService
from meter_reading_mini.core.meter_reading_sync import MeterReadingSyncService
from meter_reading_mini.core.meter_replacement_service import MeterReplacementService
from meter_reading_mini.core.mobile_reading_import import MobileReadingImportService
from meter_reading_mini.core.mobile_reading_sheet import MobileReadingSheetService
from meter_reading_mini.core.planned_reading_maintenance import PlannedReadingMaintenanceService
from meter_reading_mini.core.reading_overview import ReadingOverviewQueryService
from meter_reading_mini.core.reading_preview import ReadingPreviewService
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.core.schedule_settings import (
    CutoffSettingsService,
    MeasurementDefinitionScheduleService,
    ReleaseOffsetSettingsService,
    ScheduleSettingsService,
)
from meter_reading_mini.core.staff_service import StaffService
from meter_reading_mini.core.startup_preparation import StartupPreparationService
from meter_reading_mini.core.timeutil import utc_now
from meter_reading_mini.core.timezone_settings import TimezoneSettingsService, normalize_to_utc
from meter_reading_mini.core.work_package_download import (
    PlannedReadingGeneratorService,
    PlannedReadingLifecycleService,
)
from meter_reading_mini.db.bootstrap_seeds import bootstrap_default_settings
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository


@dataclass(frozen=True, slots=True)
class AppContext:
    startup_preparation: StartupPreparationService
    planned_reading_maintenance: PlannedReadingMaintenanceService
    reading_overview: ReadingOverviewQueryService
    assignment_calculation_settings: AssignmentCalculationSettingsService
    meter_master: MeterMasterService
    measurement_definitions: MeasurementDefinitionService
    staff: StaffService
    reading_preview: ReadingPreviewService
    reading_writer: ReadingWriteOrchestrationService
    meter_replacement: MeterReplacementService
    mobile_reading_sheet: MobileReadingSheetService
    mobile_reading_import: MobileReadingImportService
    schedule_defaults: ScheduleSettingsService
    measurement_schedules: MeasurementDefinitionScheduleService
    release_offsets: ReleaseOffsetSettingsService
    timezone_settings: TimezoneSettingsService
    cutoff_settings: CutoffSettingsService
    current_datetime: Callable[[], datetime]


def create_app_context(
    connection: sqlite3.Connection, *, clock: Callable[[], datetime] = utc_now
) -> AppContext:
    transaction_factory = partial(transaction, connection)
    repository = SQLiteRepository(connection, clock=clock)
    timezone_settings = TimezoneSettingsService(repository, transaction_factory=transaction_factory)

    def current_datetime() -> datetime:
        return normalize_to_utc(clock()).astimezone(timezone_settings.get_timezone())

    def business_date() -> date:
        return current_datetime().date()

    cutoff_settings = CutoffSettingsService(repository, transaction_factory=transaction_factory)
    release_offsets = ReleaseOffsetSettingsService(
        repository,
        recomposition_repository=repository,
        transaction_factory=transaction_factory,
        current_business_date_provider=business_date,
        clock=clock,
    )
    generator = PlannedReadingGeneratorService(
        repository,
        release_date_calculator=release_offsets,
        scheduled_timezone_provider=timezone_settings.get_timezone,
    )
    lifecycle = PlannedReadingLifecycleService(
        repository,
        current_datetime_provider=current_datetime,
        cutoff_time_provider=cutoff_settings.get_cutoff_time,
    )
    startup = StartupPreparationService(
        bootstrapper=lambda: bootstrap_default_settings(connection, clock=clock),
        planned_reading_generator=generator,
        planned_reading_lifecycle_service=lifecycle,
        transaction_factory=transaction_factory,
        business_date_provider=business_date,
    )
    maintenance = PlannedReadingMaintenanceService(
        generator,
        lifecycle,
        transaction_factory=transaction_factory,
        business_date_provider=business_date,
    )
    assignment_settings = AssignmentCalculationSettingsService(
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        transaction_factory=transaction_factory,
        now_provider=clock,
    )
    meter_master = MeterMasterService(repository, transaction_factory=transaction_factory)
    reading_writer = ReadingWriteOrchestrationService(
        repository,
        repository,
        repository,
        planned_reading_repository=repository,
        assignment_repository=repository,
        boundary_repository=repository,
        boundary_maintenance_repository=repository,
        staff_repository=repository,
        transaction_factory=transaction_factory,
        now_provider=clock,
    )
    mobile_sync = MeterReadingSyncService(
        connection,
        repository,
        repository,
        reading_writer,
        repository,
        repository,
        replacement_reflection_repository=repository,
        assignment_repository=repository,
    )
    reading_preview = ReadingPreviewService(
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        now_provider=clock,
    )
    meter_replacement = MeterReplacementService(
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        meter_master,
        reading_writer,
        transaction_factory=transaction_factory,
        now_provider=clock,
        timezone_provider=timezone_settings.get_timezone,
    )
    return AppContext(
        startup_preparation=startup,
        planned_reading_maintenance=maintenance,
        reading_overview=ReadingOverviewQueryService(
            repository, business_date_provider=business_date
        ),
        assignment_calculation_settings=assignment_settings,
        meter_master=meter_master,
        measurement_definitions=MeasurementDefinitionService(
            repository, repository, repository, transaction_factory=transaction_factory
        ),
        staff=StaffService(repository, transaction_factory=transaction_factory),
        reading_preview=reading_preview,
        reading_writer=reading_writer,
        meter_replacement=meter_replacement,
        mobile_reading_sheet=MobileReadingSheetService(
            planned_reading_generator=generator,
            planned_reading_lifecycle_service=lifecycle,
            task_repository=repository,
            master_repository=repository,
            historical_reading_repository=repository,
            assignment_repository=repository,
            boundary_repository=repository,
            transaction_factory=transaction_factory,
            timezone_provider=timezone_settings.get_timezone,
            clock=clock,
        ),
        mobile_reading_import=MobileReadingImportService(
            repository,
            sync_service=mobile_sync,
            transaction_factory=transaction_factory,
        ),
        schedule_defaults=ScheduleSettingsService(
            repository,
            recomposition_repository=repository,
            transaction_factory=transaction_factory,
            current_business_date_provider=business_date,
            clock=clock,
        ),
        measurement_schedules=MeasurementDefinitionScheduleService(
            repository,
            schedule_defaults_repository=repository,
            recomposition_repository=repository,
            transaction_factory=transaction_factory,
            current_business_date_provider=business_date,
            clock=clock,
        ),
        release_offsets=release_offsets,
        timezone_settings=timezone_settings,
        cutoff_settings=cutoff_settings,
        current_datetime=current_datetime,
    )
