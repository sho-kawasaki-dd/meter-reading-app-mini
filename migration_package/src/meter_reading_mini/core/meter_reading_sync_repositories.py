"""`POST /api/readings` 用の repository interface と保存要求モデルを定義する。

service 層は SQLAlchemy を直接知らず、lookup と upsert に必要な最小責務だけへ依存する。
このモジュールの dataclass は「永続化へ渡す確定済み要求」を表し、transport contract を
そのまま DB へ流し込まないための境界になっている。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from api.models import (
	CalculationMethod,
	MeasurementDefinitionData,
	MeterMasterData,
	PlannedReadingTask,
	ReadingCycle,
	ReadingSession,
	ResultStatus,
	ValueSchema,
)

if TYPE_CHECKING:
	from application.schedule_settings import ScheduleSettings
	from core.database import MeasurementDefinitionORM


@dataclass(frozen=True, slots=True)
class PlannedReadingRecord:
	"""planned_reading lookup の最小結果。

	strict validation に入る前に、planned reading がどの measurement definition を
	指しているかだけを知れれば十分なので、この段階では必要最小限の情報に絞る。
	boundary lookup では meter_id も必要になるため保持する。
	"""

	planned_reading_id: str
	measurement_definition_id: str
	meter_id: str
	scheduled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MeasurementDefinitionRecord:
	"""measurement definition lookup の最小結果。"""

	measurement_definition_id: str
	value_schema: ValueSchema
	calculation_method: CalculationMethod = CalculationMethod.DELTA
	multiplier: Decimal = Decimal("1.0")
	decimal_places: int | None = None
	rollover_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PlannedReadingGenerationDefinitionRecord:
	"""planned reading 生成に必要な measurement definition 情報。"""

	measurement_definition_id: str
	location_id: str
	meter_id: str
	reading_cycle: ReadingCycle
	schedule: ScheduleSettings | None = None


@dataclass(frozen=True, slots=True)
class PlannedReadingCreateRequest:
	"""planned reading を冪等に作るための保存要求。"""

	planned_reading_id: str
	measurement_definition_id: str
	meter_id: str
	location_id: str
	business_date: date
	release_date: date
	reading_session: ReadingSession
	scheduled_at: datetime
	day_offset: int = 0


@dataclass(frozen=True, slots=True)
class PlannedReadingRecompositionRecord:
	"""future planned reading の再構成に必要な最小結果。"""

	planned_reading_id: str
	measurement_definition_id: str
	meter_id: str
	location_id: str
	business_date: date
	reading_session: ReadingSession
	scheduled_at: datetime
	task_state: PlannedReadingTaskState


class ResultSource(StrEnum):
	"""reading transaction の監査用ソース語彙。"""

	CLIENT = "client"
	SYSTEM = "system"
	MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class ReadingUpsertRequest:
	"""resolved validation 後の検針結果を保存するための要求オブジェクト。"""

	planned_reading_id: str
	measurement_definition_id: str
	result_status: ResultStatus
	recorded_at: datetime
	client_updated_at: datetime
	raw_reading: Decimal | None
	structured_payload_json: dict[str, Any] | None
	updated_by: str
	signature: str
	note: str | None
	device_id: str = ""
	result_source: ResultSource = ResultSource.CLIENT
	last_reading: Decimal | None = None
	usage_delta: Decimal | None = None


@dataclass(frozen=True, slots=True)
class HistoricalMeasuredReadingRecord:
	"""usage_delta 計算に使う直近 measured 読値。"""

	planned_reading_id: str
	raw_reading: Decimal
	recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ReplacementUpsertRequest:
	"""メーター交換イベントを保存するための要求オブジェクト。"""

	replacement_event_id: str
	location_id: str
	old_meter_id: str
	new_meter_id: str
	replaced_at: datetime
	client_updated_at: datetime
	old_final_reading: Decimal | None
	new_initial_reading: Decimal | None
	updated_by: str
	note: str | None


@dataclass(frozen=True, slots=True)
class ReplacementBoundaryRecord:
	"""replacement boundary lookup の結果。"""

	replacement_event_id: str
	measurement_definition_id: str
	new_initial_reading: Decimal | None


class UpsertDisposition(StrEnum):
	"""upsert の結果を service が解釈しやすい語彙で表したもの。"""

	INSERTED = "inserted"
	UPDATED = "updated"
	REPLACED_ON_TIE = "replaced_on_tie"
	IGNORED_STALE = "ignored_stale"


@dataclass(frozen=True, slots=True)
class UpsertOutcome:
	"""repository が返す upsert 結果。"""

	disposition: UpsertDisposition
	detail: str | None = None

	@property
	def accepted(self) -> bool:
		return self.disposition is not UpsertDisposition.IGNORED_STALE


class PlannedReadingTaskState(StrEnum):
	"""planned reading の内部ライフサイクル状態。"""

	OPEN = "open"
	COMPLETED = "completed"
	AUTO_CLOSED = "auto_closed"
	SUPERSEDED = "superseded"


class PlannedReadingCloseReason(StrEnum):
	"""planned reading を close した理由の内部語彙。"""

	CUTOFF_ELAPSED = "cutoff_elapsed"


class PlannedReadingLookupRepository(Protocol):
	"""planned_reading_id から予定枠の最小メタデータを引く repository。"""

	def get_planned_reading(self, planned_reading_id: str) -> PlannedReadingRecord | None:
		"""Return planned reading metadata for the provided natural key."""
		...


class MeasurementDefinitionLookupRepository(Protocol):
	"""strict validation に必要な measurement definition 情報を引く repository。"""

	def get_measurement_definition(
		self,
		measurement_definition_id: str,
	) -> MeasurementDefinitionRecord | None:
		"""Return measurement definition metadata for the provided identifier."""
		...


class PlannedReadingGenerationRepository(Protocol):
	"""planned reading の backfill 生成に必要な repository。"""

	def get_latest_planned_business_date(self) -> date | None:
		"""Return the latest generated business_date, if any."""
		...

	def list_planned_reading_generation_definitions(
		self,
	) -> list[PlannedReadingGenerationDefinitionRecord]:
		"""Return measurement definitions eligible for planned reading generation."""
		...

	def list_measurement_definitions_without_active_assignment(self) -> list["MeasurementDefinitionORM"]:
		"""Return active measurement definitions that have no active meter assignment."""
		...

	def create_planned_reading_if_missing(self, request: PlannedReadingCreateRequest) -> bool:
		"""Create a planned reading when its stable identifier is not present yet."""
		...

	def create_planned_readings_if_missing(self, requests: list[PlannedReadingCreateRequest]) -> int:
		"""Create multiple planned readings in one batch and return the number inserted."""
		...


class MeasurementDefinitionScheduleRepository(Protocol):
	"""measurement_definition_schedules の読み書きに必要な repository。"""

	def get_measurement_definition_schedule(self, measurement_definition_id: str) -> str | None:
		"""Return schedule JSON for the provided measurement definition, if any."""
		...

	def upsert_measurement_definition_schedule(self, measurement_definition_id: str, schedule_json: str) -> None:
		"""Persist schedule JSON for the provided measurement definition."""
		...


class MeterMasterQueryRepository(Protocol):
	"""work package download 用の物理メーターマスター取得境界。"""

	def list_meter_master_data(self) -> list[MeterMasterData]:
		"""Return all physical meters required by the Android client."""
		...


class MeasurementDefinitionQueryRepository(Protocol):
	"""work package download 用の measurement definition 取得境界。"""

	def list_measurement_definition_data(self) -> list[MeasurementDefinitionData]:
		"""Return all measurement definitions required by the Android client."""
		...


class PlannedReadingTaskQueryRepository(Protocol):
	"""work package download 用の open planned reading 一覧取得境界。"""

	def list_open_planned_reading_tasks_for_release_date(self, release_date: date) -> list[PlannedReadingTask]:
		"""Return open tasks whose release_date is on or before the specified date."""
		...

	def list_open_planned_reading_tasks_for_business_date(self, business_date: date) -> list[PlannedReadingTask]:
		"""Return open tasks for the specified business_date."""
		...

	def list_open_planned_reading_tasks(self, business_date: date) -> list[PlannedReadingTask]:
		"""Deprecated alias for business_date-specific queries."""
		...


class ReleaseDateCalculator(Protocol):
	"""planned reading の配布可能日を計算する境界。"""

	def compute_release_date(self, business_date: date, reading_session: ReadingSession) -> date:
		"""Return the release date for the given planned reading slot."""
		...


class PlannedReadingLifecycleRepository(Protocol):
	"""planned reading の lifecycle reconciliation を扱う repository。"""

	def auto_close_open_planned_readings(
		self,
		through_business_date: date,
		*,
		closed_at: datetime,
		close_reason: PlannedReadingCloseReason,
	) -> int:
		"""Auto-close open planned readings up to the provided business_date inclusive."""
		...

	def list_planned_reading_recomposition_candidates(
		self,
		measurement_definition_id: str,
		from_business_date: date,
	) -> list[PlannedReadingRecompositionRecord]:
		"""Return planned readings that may need future recomposition."""
		...

	def supersede_planned_readings(self, planned_reading_ids: list[str], *, superseded_at: datetime) -> int:
		"""Mark the provided planned readings as superseded when they are no longer valid."""
		...


class PlannedReadingRecompositionRepository(Protocol):
	"""schedule 変更時の future planned reading 再構成を扱う repository。"""

	def recompose_future_planned_readings(
		self,
		measurement_definition_id: str,
		reading_cycle: ReadingCycle,
		schedule: "ScheduleSettings",
		*,
		from_business_date: date | None = None,
	) -> tuple[int, int]:
		"""Return `(superseded_count, generated_count)` after recomposition."""
		...


class ReadingUpsertRepository(Protocol):
	"""planned_reading_id を自然キーとして検針結果を保存する repository。"""

	def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
		"""Persist a resolved reading submission with conflict resolution handled internally."""
		...


class HistoricalMeasuredReadingLookupRepository(Protocol):
	"""usage_delta 計算用に直近 measured 読値を返す repository。"""

	def get_latest_measured_reading(
		self,
		measurement_definition_id: str,
		*,
		before_scheduled_at: datetime,
		before_planned_reading_id: str | None = None,
		excluding_planned_reading_id: str | None = None,
	) -> HistoricalMeasuredReadingRecord | None:
		"""Return the latest prior measured reading with a decimal raw_reading."""
		...


class ReadingPersistenceRepository(Protocol):
	"""計算済み reading request を永続化する low-level repository。"""

	def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
		"""Persist a calculated reading submission with conflict resolution handled internally."""
		...

	def recalculate_following_measured_reading(
		self,
		measurement_definition_id: str,
		*,
		after_scheduled_at: datetime,
		after_planned_reading_id: str | None = None,
	) -> int:
		"""Recalculate the immediate next measured reading after the provided planned slot."""
		...


class ReplacementEventUpsertRepository(Protocol):
	"""replacement_event_id を自然キーとして交換イベントを保存する repository。"""

	def upsert_replacement(self, request: ReplacementUpsertRequest) -> UpsertOutcome:
		"""Persist a replacement event with conflict resolution handled internally."""
		...


class ReplacementReflectionRepository(Protocol):
	"""accepted replacement を業務状態へ反映する repository。"""

	def reflect_replacement(self, request: ReplacementUpsertRequest) -> None:
		"""Apply accepted replacement side effects to assignments and planned readings."""
		...


class ReplacementBoundaryLookupRepository(Protocol):
	"""新メーター初回 measured の計算基準を返す repository。"""

	def get_replacement_boundary_for_new_meter_first_reading(
		self,
		measurement_definition_id: str,
		new_meter_id: str,
	) -> ReplacementBoundaryRecord | None:
		"""Return the boundary record if the first accepted new-meter measured reading has not yet been recorded."""
		...


__all__ = [
	"MeasurementDefinitionData",
	"MeasurementDefinitionLookupRepository",
	"MeasurementDefinitionQueryRepository",
	"MeasurementDefinitionRecord",
	"HistoricalMeasuredReadingLookupRepository",
	"HistoricalMeasuredReadingRecord",
	"MeterMasterData",
	"MeterMasterQueryRepository",
	"MeasurementDefinitionScheduleRepository",
	"PlannedReadingCloseReason",
	"PlannedReadingLookupRepository",
	"PlannedReadingCreateRequest",
	"PlannedReadingGenerationDefinitionRecord",
	"PlannedReadingRecompositionRecord",
	"PlannedReadingGenerationRepository",
	"PlannedReadingRecord",
	"PlannedReadingLifecycleRepository",
	"PlannedReadingRecompositionRepository",
	"PlannedReadingTask",
	"PlannedReadingTaskState",
	"PlannedReadingTaskQueryRepository",
	"ReleaseDateCalculator",
	"ReadingPersistenceRepository",
	"ReadingUpsertRepository",
	"ReadingUpsertRequest",
	"ReplacementBoundaryLookupRepository",
	"ReplacementBoundaryRecord",
	"ReplacementReflectionRepository",
	"ReplacementEventUpsertRepository",
	"ReplacementUpsertRequest",
	"UpsertDisposition",
	"UpsertOutcome",
]