"""PC 側バックエンドの永続化モデルと DB 補助関数をまとめるモジュール。

このモジュールでは、企画書で定義された主要な永続化概念を SQLAlchemy へ落とし込む。
特に `location_id`、`meter_id`、`measurement_definition_id` の分離、UTC 正規化、
Decimal を TEXT として保持する方針は、後続の料金計算や監査で誤差や意味の崩れを
起こさないための重要な前提になっている。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text, create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from application.timezone_settings import DEFAULT_APP_TIMEZONE


def normalize_to_utc(value: datetime) -> datetime:
	"""aware datetime を UTC へ正規化する。

	API では offset 付き時刻を受け取るが、DB には比較しやすい canonical time として
	UTC を保存する。`business_date` や `reading_session` は別軸で保持するため、ここでは
	純粋に監査・比較用の時刻へそろえる責務だけを担う。
	"""

	if value.tzinfo is None or value.utcoffset() is None:
		raise ValueError("Datetime values must be timezone-aware.")
	return value.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
	"""時刻を UTC に正規化して保存する TypeDecorator。"""

	impl = DateTime(timezone=True)
	cache_ok = True

	def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
		if value is None:
			return None
		return normalize_to_utc(value)

	def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
		if value is None:
			return None
		if value.tzinfo is None or value.utcoffset() is None:
			return value.replace(tzinfo=timezone.utc)
		return value.astimezone(timezone.utc)


def build_scheduled_at(
	business_date: date,
	reading_session: str,
	*,
	day_offset: int = 0,
	tzinfo: tzinfo | None = None,
) -> datetime:
	"""PC のローカルタイムゾーン基準で business_date と reading_session を組み、UTC の scheduled_at を返す。"""

	effective_tzinfo = tzinfo or DEFAULT_APP_TIMEZONE
	local_datetime = datetime.combine(
		business_date + timedelta(days=day_offset),
		time.fromisoformat(reading_session),
		tzinfo=effective_tzinfo,
	)
	return normalize_to_utc(local_datetime)


def _default_scheduled_at(context: Any) -> datetime:
	"""planned_readings の insert 時に scheduled_at を自動補完する。"""

	parameters = context.get_current_parameters()
	business_date = parameters.get("business_date")
	reading_session = parameters.get("reading_session")
	if business_date is None or reading_session is None:
		raise ValueError("scheduled_at requires business_date and reading_session.")
	return build_scheduled_at(business_date, reading_session)


class DecimalAsText(TypeDecorator[Decimal]):
	"""Decimal を文字列として保存し、SQLite の float 変換を避ける。"""

	impl = String(64)
	cache_ok = True

	def process_bind_param(self, value: Decimal | str | None, dialect: Any) -> str | None:
		if value is None:
			return None
		if isinstance(value, Decimal):
			decimal_value = value
		else:
			decimal_value = Decimal(value)
		return format(decimal_value, "f")

	def process_result_value(self, value: str | None, dialect: Any) -> Decimal | None:
		if value is None:
			return None
		return Decimal(value)


class Base(DeclarativeBase):
	"""PC アプリの ORM モデル群で共有する DeclarativeBase。"""


class PhysicalMeterORM(Base):
	"""物理メーターのマスターテーブル。

	ここには「どの機器か」を識別するための属性だけを置く。企画書上の
	 calculation_method や reading_cycle のような「何をどう読むか」は
	 `MeasurementDefinitionORM` 側で管理する。
	"""

	__tablename__ = "physical_meters"

	meter_id: Mapped[str] = mapped_column(String, primary_key=True)
	location_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
	location_name: Mapped[str] = mapped_column(String, nullable=False)
	meter_name: Mapped[str] = mapped_column(String, nullable=False)
	category: Mapped[str] = mapped_column(String, nullable=False)
	meter_serial: Mapped[str | None] = mapped_column(String, nullable=True)
	is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

	measurement_definitions: Mapped[list[MeasurementDefinitionORM]] = relationship(
		back_populates="meter",
	)
	planned_readings: Mapped[list[PlannedReadingORM]] = relationship(back_populates="meter")


class MeasurementDefinitionORM(Base):
	"""論理的な測定対象を表すテーブル。

	同じ location に対して「積算電力量」と「力率」のような別の測定対象を
	並立して持てるように、物理メーターとは分けて管理する。planned reading は
	この measurement_definition 単位で生成される。
	"""

	__tablename__ = "measurement_definitions"

	measurement_definition_id: Mapped[str] = mapped_column(String, primary_key=True)
	location_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
	meter_id: Mapped[str | None] = mapped_column(
		ForeignKey("physical_meters.meter_id"),
		nullable=True,
	)
	display_name: Mapped[str] = mapped_column(String, nullable=False)
	category: Mapped[str] = mapped_column(String, nullable=False)
	unit: Mapped[str] = mapped_column(String, nullable=False)
	value_schema: Mapped[str] = mapped_column(String, nullable=False)
	multiplier: Mapped[Decimal] = mapped_column(DecimalAsText(), nullable=False)
	decimal_places: Mapped[int | None] = mapped_column(Integer, nullable=True)
	warning_threshold: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	rollover_value: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	calculation_method: Mapped[str] = mapped_column(String, nullable=False)
	reading_cycle: Mapped[str] = mapped_column(String, nullable=False)
	purpose: Mapped[str] = mapped_column(String, nullable=False, default="operational")
	is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

	meter: Mapped[PhysicalMeterORM | None] = relationship(back_populates="measurement_definitions")
	planned_readings: Mapped[list[PlannedReadingORM]] = relationship(
		back_populates="measurement_definition",
	)
	measurement_definition_schedule: Mapped[MeasurementDefinitionScheduleORM | None] = relationship(
		back_populates="measurement_definition",
		cascade="all, delete-orphan",
		uselist=False,
	)
	meter_assignments: Mapped[list[MeterAssignmentORM]] = relationship(
		back_populates="measurement_definition",
	)


class MeasurementDefinitionScheduleORM(Base):
	"""measurement_definition ごとの planned reading スケジュール。"""

	__tablename__ = "measurement_definition_schedules"

	measurement_definition_id: Mapped[str] = mapped_column(
		ForeignKey("measurement_definitions.measurement_definition_id"),
		primary_key=True,
	)
	schedule_json: Mapped[str] = mapped_column(Text, nullable=False)
	updated_at: Mapped[datetime] = mapped_column(
		UTCDateTime(),
		nullable=False,
		default=lambda: datetime.now(timezone.utc),
		onupdate=lambda: datetime.now(timezone.utc),
	)

	measurement_definition: Mapped[MeasurementDefinitionORM] = relationship(
		back_populates="measurement_definition_schedule",
	)


class PlannedReadingORM(Base):
	"""Android へ事前配布する検針予定枠を表すテーブル。

	`planned_reading_id` はアップロード時の自然キーでもあり、現場で入力された
	結果は必ずこの予定枠へぶら下がる。`task_state` は result_status とは別の
	内部ライフサイクルで、open/completed/auto_closed/superseded を想定している。
	"""

	__tablename__ = "planned_readings"

	planned_reading_id: Mapped[str] = mapped_column(String, primary_key=True)
	measurement_definition_id: Mapped[str] = mapped_column(
		ForeignKey("measurement_definitions.measurement_definition_id"),
		nullable=False,
		index=True,
	)
	meter_id: Mapped[str] = mapped_column(ForeignKey("physical_meters.meter_id"), nullable=False)
	location_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
	business_date: Mapped[date] = mapped_column(Date(), nullable=False, index=True)
	release_date: Mapped[date] = mapped_column(Date(), nullable=False, index=True)
	reading_session: Mapped[str] = mapped_column(String, nullable=False)
	scheduled_at: Mapped[datetime] = mapped_column(
		UTCDateTime(),
		nullable=False,
		default=_default_scheduled_at,
	)
	task_state: Mapped[str] = mapped_column(String, nullable=False, default="open")
	closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
	close_reason: Mapped[str | None] = mapped_column(String, nullable=True)

	def __init__(self, **kwargs: Any) -> None:
		if "release_date" not in kwargs and "business_date" in kwargs:
			kwargs["release_date"] = kwargs["business_date"]
		for key, value in kwargs.items():
			setattr(self, key, value)

	measurement_definition: Mapped[MeasurementDefinitionORM] = relationship(
		back_populates="planned_readings",
	)
	meter: Mapped[PhysicalMeterORM] = relationship(back_populates="planned_readings")
	reading_transaction: Mapped[ReadingTransactionORM | None] = relationship(
		back_populates="planned_reading",
		uselist=False,
	)


class ReadingTransactionORM(Base):
	"""現場から返却された検針結果を保持するテーブル。

	`recorded_at` は実際の記録時刻、`client_updated_at` はクライアント側での更新順序を
	表す時刻であり、意味が異なる。後者は再送や訂正入力の優先順位判定に使う。
	`last_reading` と `usage_delta` は、後続フェーズで application 層が計算した
	監査スナップショットと確定差分を保存するための枠として先に持っておく。
	"""

	__tablename__ = "reading_transactions"

	planned_reading_id: Mapped[str] = mapped_column(
		ForeignKey("planned_readings.planned_reading_id"),
		primary_key=True,
	)
	result_status: Mapped[str] = mapped_column(String, nullable=False)
	recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
	client_updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
	raw_reading: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	last_reading: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	usage_delta: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	structured_payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
	device_id: Mapped[str] = mapped_column(String, nullable=False, default="")
	updated_by: Mapped[str] = mapped_column(String, nullable=False)
	signature: Mapped[str] = mapped_column(String, nullable=False)
	note: Mapped[str | None] = mapped_column(String, nullable=True)
	result_source: Mapped[str | None] = mapped_column(String, nullable=True)
	arrival_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

	planned_reading: Mapped[PlannedReadingORM] = relationship(back_populates="reading_transaction")


class AppSettingsORM(Base):
	"""PC 側の運用設定を保持する汎用 key/value テーブル。"""

	__tablename__ = "app_settings"

	setting_key: Mapped[str] = mapped_column(String, primary_key=True)
	setting_value: Mapped[str] = mapped_column(String, nullable=False)
	updated_at: Mapped[datetime] = mapped_column(
		UTCDateTime(),
		nullable=False,
		default=lambda: datetime.now(timezone.utc),
	)


class MeterAssignmentORM(Base):
	"""measurement definition と物理メーターの有効期間付き対応表。

	メーター交換時に「集計上は同じ測定対象だが、物理デバイスは切り替わった」状態を
	表現するための履歴テーブルで、将来の rewind/replay でも重要になる。
	"""

	__tablename__ = "meter_assignments"

	measurement_definition_id: Mapped[str] = mapped_column(
		ForeignKey("measurement_definitions.measurement_definition_id"),
		primary_key=True,
	)
	meter_id: Mapped[str] = mapped_column(
		ForeignKey("physical_meters.meter_id"),
		primary_key=True,
	)
	effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
	effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

	measurement_definition: Mapped[MeasurementDefinitionORM] = relationship(
		back_populates="meter_assignments",
	)


class ReplacementEventORM(Base):
	"""メーター交換イベントを保持するテーブル。"""

	__tablename__ = "replacement_events"

	replacement_event_id: Mapped[str] = mapped_column(String, primary_key=True)
	location_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
	old_meter_id: Mapped[str] = mapped_column(ForeignKey("physical_meters.meter_id"), nullable=False)
	new_meter_id: Mapped[str] = mapped_column(ForeignKey("physical_meters.meter_id"), nullable=False)
	replaced_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
	old_final_reading: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	new_initial_reading: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	client_updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
	updated_by: Mapped[str] = mapped_column(String, nullable=False)
	note: Mapped[str | None] = mapped_column(String, nullable=True)
	arrival_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ReplacementBoundaryORM(Base):
	"""measurement_definition 単位の交換境界 fact を保持するテーブル。

	delta かつ decimal_string の measurement_definition にだけ作成される。
	旧メーター側 usage_delta と新メーター初回計算基準を明示的に保存し、
	集計や新メーター初回 reading の計算に利用する。
	"""

	__tablename__ = "replacement_boundaries"

	replacement_event_id: Mapped[str] = mapped_column(
		ForeignKey("replacement_events.replacement_event_id"),
		primary_key=True,
	)
	measurement_definition_id: Mapped[str] = mapped_column(
		ForeignKey("measurement_definitions.measurement_definition_id"),
		primary_key=True,
	)
	location_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
	business_date: Mapped[date] = mapped_column(Date(), nullable=False)
	old_meter_id: Mapped[str] = mapped_column(ForeignKey("physical_meters.meter_id"), nullable=False)
	new_meter_id: Mapped[str] = mapped_column(ForeignKey("physical_meters.meter_id"), nullable=False)
	replaced_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
	previous_measured_reading: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	previous_measured_planned_reading_id: Mapped[str | None] = mapped_column(String, nullable=True)
	old_final_reading: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	old_meter_usage_delta: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	new_initial_reading: Mapped[Decimal | None] = mapped_column(DecimalAsText(), nullable=True)
	created_at: Mapped[datetime] = mapped_column(
		UTCDateTime(),
		nullable=False,
		default=lambda: datetime.now(timezone.utc),
	)
	updated_at: Mapped[datetime] = mapped_column(
		UTCDateTime(),
		nullable=False,
		default=lambda: datetime.now(timezone.utc),
		onupdate=lambda: datetime.now(timezone.utc),
	)


PHYSICAL_METER_COLUMNS = (
	"meter_id",
	"location_id",
	"location_name",
	"meter_name",
	"category",
	"meter_serial",
)

MEASUREMENT_DEFINITION_COLUMNS = (
	"measurement_definition_id",
	"location_id",
	"meter_id",
	"display_name",
	"category",
	"unit",
	"value_schema",
	"multiplier",
	"warning_threshold",
	"rollover_value",
	"calculation_method",
	"reading_cycle",
	"purpose",
)

PLANNED_READING_COLUMNS = (
	"planned_reading_id",
	"measurement_definition_id",
	"meter_id",
	"location_id",
	"business_date",
	"reading_session",
	"scheduled_at",
	"task_state",
	"closed_at",
	"close_reason",
)

MEASUREMENT_DEFINITION_SCHEDULE_COLUMNS = (
	"measurement_definition_id",
	"schedule_json",
	"updated_at",
)

READING_TRANSACTION_COLUMNS = (
	"planned_reading_id",
	"result_status",
	"recorded_at",
	"client_updated_at",
	"raw_reading",
	"last_reading",
	"usage_delta",
	"structured_payload_json",
	"device_id",
	"updated_by",
	"signature",
	"note",
	"result_source",
	"arrival_sequence",
)

APP_SETTINGS_COLUMNS = (
	"setting_key",
	"setting_value",
	"updated_at",
)

METER_ASSIGNMENT_COLUMNS = (
	"measurement_definition_id",
	"meter_id",
	"effective_from",
	"effective_to",
)

REPLACEMENT_EVENT_COLUMNS = (
	"replacement_event_id",
	"location_id",
	"old_meter_id",
	"new_meter_id",
	"replaced_at",
	"old_final_reading",
	"new_initial_reading",
	"client_updated_at",
	"updated_by",
	"note",
	"arrival_sequence",
)

REPLACEMENT_BOUNDARY_COLUMNS = (
	"replacement_event_id",
	"measurement_definition_id",
	"location_id",
	"business_date",
	"old_meter_id",
	"new_meter_id",
	"replaced_at",
	"previous_measured_reading",
	"previous_measured_planned_reading_id",
	"old_final_reading",
	"old_meter_usage_delta",
	"new_initial_reading",
	"created_at",
	"updated_at",
)

METER_MASTER_COLUMNS = PHYSICAL_METER_COLUMNS


def build_default_sqlite_url() -> str:
	"""既定の SQLite URL を返す。

	開発中でも本番相当でも、PC アプリ配下の `data/` を単一の永続化場所として扱う。
	"""

	database_path = Path(__file__).resolve().parents[2] / "data" / "meter_reading.sqlite3"
	database_path.parent.mkdir(parents=True, exist_ok=True)
	return f"sqlite+pysqlite:///{database_path.resolve().as_posix()}"


def _is_in_memory_sqlite_url(database_url: str) -> bool:
	"""SQLite URL が in-memory DB を指すかを返す。"""

	url = make_url(database_url)
	return url.get_backend_name() == "sqlite" and url.database in {None, "", ":memory:"}


def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any, *, enable_wal: bool) -> None:
	"""SQLite 接続に対して PRAGMA を一括設定する。"""

	cursor = dbapi_connection.cursor()
	try:
		if enable_wal:
			cursor.execute("PRAGMA journal_mode=WAL")
			cursor.fetchone()
		cursor.execute("PRAGMA busy_timeout=30000")
		cursor.execute("PRAGMA foreign_keys=ON")
	finally:
		cursor.close()


def create_sqlite_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
	"""アプリ本体とテストの両方で使える SQLite engine を作成する。"""

	resolved_url = database_url or build_default_sqlite_url()
	engine_kwargs: dict[str, Any] = {
		"echo": echo,
		"future": True,
	}
	if resolved_url.startswith("sqlite"):
		engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
	if resolved_url.endswith(":memory:"):
		engine_kwargs["poolclass"] = StaticPool
	engine = create_engine(resolved_url, **engine_kwargs)
	if resolved_url.startswith("sqlite"):
		event.listen(
			engine,
			"connect",
			partial(_configure_sqlite_connection, enable_wal=not _is_in_memory_sqlite_url(resolved_url)),
		)
	return engine


def create_session_factory(engine: Engine) -> sessionmaker:
	"""指定した engine に束縛された session factory を作る。"""

	return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_database_schema(engine: Engine) -> None:
	"""既知の ORM テーブルをすべて作成する。"""

	Base.metadata.create_all(engine)


class AppSettingsRepository:
	"""App settings のシンプルなリポジトリ。"""

	def __init__(self, session):
		self._session = session

	def get_value(self, key: str) -> str | None:
		"""指定キーの値を取得する。"""
		from sqlalchemy import select
		result = self._session.execute(
			select(AppSettingsORM).where(AppSettingsORM.setting_key == key)
		).scalar_one_or_none()
		return result.setting_value if result else None

	def set_value(self, key: str, value: str) -> None:
		"""指定キーの値を設定または更新する。"""
		from sqlalchemy import select
		existing = self._session.execute(
			select(AppSettingsORM).where(AppSettingsORM.setting_key == key)
		).scalar_one_or_none()
		if existing:
			existing.setting_value = value
		else:
			new_setting = AppSettingsORM(setting_key=key, setting_value=value)
			self._session.add(new_setting)

	def get_setting(self, key: str) -> str | None:
		"""settings-style の別名として値を取得する。"""

		return self.get_value(key)

	def upsert_setting(self, key: str, value: str) -> None:
		"""settings-style の別名として値を保存する。"""

		self.set_value(key, value)

	def delete_setting(self, key: str) -> None:
		"""指定キーの値を削除する。"""

		from sqlalchemy import select
		existing = self._session.execute(
			select(AppSettingsORM).where(AppSettingsORM.setting_key == key)
		).scalar_one_or_none()
		if existing is not None:
			self._session.delete(existing)
			self._session.flush()


__all__ = [
	"Base",
	"DecimalAsText",
	"APP_SETTINGS_COLUMNS",
	"AppSettingsORM",
	"AppSettingsRepository",
	"MEASUREMENT_DEFINITION_SCHEDULE_COLUMNS",
	"METER_ASSIGNMENT_COLUMNS",
	"MEASUREMENT_DEFINITION_COLUMNS",
	"METER_MASTER_COLUMNS",
	"MeasurementDefinitionORM",
	"MeasurementDefinitionScheduleORM",
	"MeterAssignmentORM",
	"PHYSICAL_METER_COLUMNS",
	"PLANNED_READING_COLUMNS",
	"PhysicalMeterORM",
	"PlannedReadingORM",
	"READING_TRANSACTION_COLUMNS",
	"REPLACEMENT_BOUNDARY_COLUMNS",
	"REPLACEMENT_EVENT_COLUMNS",
	"ReadingTransactionORM",
	"ReplacementBoundaryORM",
	"ReplacementEventORM",
	"UTCDateTime",
	"build_default_sqlite_url",
	"create_database_schema",
	"create_session_factory",
	"create_sqlite_engine",
	"build_scheduled_at",
	"normalize_to_utc",
]
