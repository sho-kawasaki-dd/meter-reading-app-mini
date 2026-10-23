"""スキーマ初期化後に使う master data bootstrap 補助関数。

企画書上の物理メーターと measurement definition の初期投入だけを担当し、
planned reading や reading transaction のような業務時系列データはここで生成しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from application.schedule_settings import MeasurementDefinitionScheduleService
from api.models import ReadingCycle
from core.database import AppSettingsORM, MeasurementDefinitionORM, MeterAssignmentORM, PhysicalMeterORM
from core.database import MeasurementDefinitionScheduleORM


@dataclass(frozen=True, slots=True)
class PhysicalMeterSeed:
	"""physical_meters テーブルへ投入する 1 件分の seed。"""

	meter_id: str
	location_id: str
	location_name: str
	meter_name: str
	category: str
	meter_serial: str | None = None


@dataclass(frozen=True, slots=True)
class MeasurementDefinitionSeed:
	"""measurement_definitions テーブルへ投入する 1 件分の seed。"""

	measurement_definition_id: str
	location_id: str
	meter_id: str | None
	display_name: str
	category: str
	unit: str
	value_schema: str
	multiplier: str
	warning_threshold: str | None
	rollover_value: str | None
	calculation_method: str
	reading_cycle: str
	purpose: str = "operational"
	decimal_places: int | None = None


@dataclass(frozen=True, slots=True)
class MasterDataBootstrapSeed:
	"""物理メーターと measurement definition をまとめた seed セット。"""

	physical_meters: tuple[PhysicalMeterSeed, ...]
	measurement_definitions: tuple[MeasurementDefinitionSeed, ...]


@dataclass(frozen=True, slots=True)
class BootstrapResult:
	"""bootstrap 実行結果の件数サマリー。"""

	inserted_physical_meters: int = 0
	updated_physical_meters: int = 0
	inserted_measurement_definitions: int = 0
	updated_measurement_definitions: int = 0
	inserted_measurement_definition_schedules: int = 0
	inserted_meter_assignments: int = 0
	updated_meter_assignments: int = 0

	@property
	def changed(self) -> bool:
		"""insert/update が 1 件でもあったかを返す。"""

		return any(
			(
				self.inserted_physical_meters,
				self.updated_physical_meters,
				self.inserted_measurement_definitions,
				self.updated_measurement_definitions,
				self.inserted_measurement_definition_schedules,
				self.inserted_meter_assignments,
				self.updated_meter_assignments,
			)
		)

	def summary(self) -> str:
		"""CLI やログで表示しやすい 1 行サマリーを返す。"""

		return (
			"physical_meters: inserted={inserted_physical_meters}, updated={updated_physical_meters}; "
			"measurement_definitions: inserted={inserted_measurement_definitions}, "
			"updated={updated_measurement_definitions}; "
			"measurement_definition_schedules: inserted={inserted_measurement_definition_schedules}; "
			"meter_assignments: inserted={inserted_meter_assignments}, "
			"updated={updated_meter_assignments}"
		).format(
			inserted_physical_meters=self.inserted_physical_meters,
			updated_physical_meters=self.updated_physical_meters,
			inserted_measurement_definitions=self.inserted_measurement_definitions,
			updated_measurement_definitions=self.updated_measurement_definitions,
			inserted_measurement_definition_schedules=self.inserted_measurement_definition_schedules,
			inserted_meter_assignments=self.inserted_meter_assignments,
			updated_meter_assignments=self.updated_meter_assignments,
		)


DEFAULT_MASTER_DATA_SEED = MasterDataBootstrapSeed(
	physical_meters=(
		# --- 電気 (7箇所) ---
		PhysicalMeterSeed(
			meter_id="meter-electric-incoming-001",
			location_id="loc-electric-incoming",
			location_name="引込電力量計",
			meter_name="引込電力量計",
			category="electricity",
			meter_serial="ELEC-IN-001",
		),
		PhysicalMeterSeed(
			meter_id="meter-electric-hotel-branch-001",
			location_id="loc-electric-hotel-branch",
			location_name="ホテル棟分岐盤電力量計",
			meter_name="ホテル棟分岐盤電力量計",
			category="electricity",
			meter_serial="ELEC-HB-001",
		),
		PhysicalMeterSeed(
			meter_id="meter-electric-hotel-lv-001",
			location_id="loc-electric-hotel-lv",
			location_name="ホテル棟低圧動力盤電力量計",
			meter_name="ホテル棟低圧動力盤電力量計",
			category="electricity",
			meter_serial="ELEC-HLV-001",
		),
		PhysicalMeterSeed(
			meter_id="meter-electric-disaster-001",
			location_id="loc-electric-disaster",
			location_name="防災動力盤電力量計",
			meter_name="防災動力盤電力量計",
			category="electricity",
			meter_serial="ELEC-DIS-001",
		),
		PhysicalMeterSeed(
			meter_id="meter-electric-cafe-001",
			location_id="loc-electric-cafe",
			location_name="カフェ動力盤電力量計",
			meter_name="カフェ動力盤電力量計",
			category="electricity",
			meter_serial="ELEC-CAFE-001",
		),
		PhysicalMeterSeed(
			meter_id="meter-electric-hotel-kitchen-light-001",
			location_id="loc-electric-hotel-kitchen-light",
			location_name="ホテル厨房電灯盤",
			meter_name="ホテル厨房電灯盤",
			category="electricity",
			meter_serial="ELEC-HKL-001",
		),
		PhysicalMeterSeed(
			meter_id="meter-electric-hotel-kitchen-power-001",
			location_id="loc-electric-hotel-kitchen-power",
			location_name="ホテル厨房動力盤",
			meter_name="ホテル厨房動力盤",
			category="electricity",
			meter_serial="ELEC-HKP-001",
		),
		# --- ガス (3箇所) ---
		PhysicalMeterSeed(
			meter_id="meter-gas-hq-001",
			location_id="loc-gas-hq",
			location_name="本社棟 ガス",
			meter_name="本社棟 ガスメーター",
			category="gas",
			meter_serial="GAS-HQ-001",
		),
		PhysicalMeterSeed(
			meter_id="meter-gas-cafe-001",
			location_id="loc-gas-cafe",
			location_name="カフェ ガス",
			meter_name="カフェ ガスメーター",
			category="gas",
			meter_serial="GAS-CAFE-001",
		),
		PhysicalMeterSeed(
			meter_id="meter-gas-hotel-001",
			location_id="loc-gas-hotel",
			location_name="ホテル ガス",
			meter_name="ホテル ガスメーター",
			category="gas",
			meter_serial="GAS-HOTEL-001",
		),
		# --- 水道 (2箇所) ---
		PhysicalMeterSeed(
			meter_id="meter-water-incoming-001",
			location_id="loc-water-incoming",
			location_name="引込量水器",
			meter_name="引込量水器",
			category="water",
			meter_serial="WATER-IN-001",
		),
		PhysicalMeterSeed(
			meter_id="meter-water-hotel-001",
			location_id="loc-water-hotel",
			location_name="ホテル量水器",
			meter_name="ホテル量水器",
			category="water",
			meter_serial="WATER-HOTEL-001",
		),
	),
	measurement_definitions=(
		# ===== Operational (13件) =====
		# --- 電気 kWh (7箇所) ---
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-incoming-kwh",
			location_id="loc-electric-incoming",
			meter_id="meter-electric-incoming-001",
			display_name="引込電力量計 積算電力量",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="10.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_3x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-incoming-kw",
			location_id="loc-electric-incoming",
			meter_id="meter-electric-incoming-001",
			display_name="引込電力量計 最大需要電力",
			category="electricity",
			unit="kW",
			value_schema="decimal_string",
			multiplier="10.0",
			warning_threshold=None,
			rollover_value=None,
			calculation_method="raw_only",
			reading_cycle="daily_3x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-hotel-branch-kwh",
			location_id="loc-electric-hotel-branch",
			meter_id="meter-electric-hotel-branch-001",
			display_name="ホテル棟分岐盤 積算電力量",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="100.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_3x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-hotel-lv-kwh",
			location_id="loc-electric-hotel-lv",
			meter_id="meter-electric-hotel-lv-001",
			display_name="ホテル棟低圧動力盤 積算電力量",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="10.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_3x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-disaster-kwh",
			location_id="loc-electric-disaster",
			meter_id="meter-electric-disaster-001",
			display_name="防災動力盤 積算電力量",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="10.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_3x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-cafe-kwh",
			location_id="loc-electric-cafe",
			meter_id="meter-electric-cafe-001",
			display_name="カフェ動力盤 積算電力量",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_3x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-hotel-kitchen-light-kwh",
			location_id="loc-electric-hotel-kitchen-light",
			meter_id="meter-electric-hotel-kitchen-light-001",
			display_name="ホテル厨房電灯盤 積算電力量",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_3x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-hotel-kitchen-power-kwh",
			location_id="loc-electric-hotel-kitchen-power",
			meter_id="meter-electric-hotel-kitchen-power-001",
			display_name="ホテル厨房動力盤 積算電力量",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="10.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_3x",
			purpose="operational",
		),
		# --- ガス (3箇所) ---
		MeasurementDefinitionSeed(
			measurement_definition_id="md-gas-hq-total",
			location_id="loc-gas-hq",
			meter_id="meter-gas-hq-001",
			display_name="本社棟 ガス使用量",
			category="gas",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_1x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-gas-cafe-total",
			location_id="loc-gas-cafe",
			meter_id="meter-gas-cafe-001",
			display_name="カフェ ガス使用量",
			category="gas",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_1x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-gas-hotel-total",
			location_id="loc-gas-hotel",
			meter_id="meter-gas-hotel-001",
			display_name="ホテル ガス使用量",
			category="gas",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_1x",
			purpose="operational",
		),
		# --- 水道 (2箇所) ---
		MeasurementDefinitionSeed(
			measurement_definition_id="md-water-incoming-total",
			location_id="loc-water-incoming",
			meter_id="meter-water-incoming-001",
			display_name="引込量水器 水使用量",
			category="water",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_1x",
			purpose="operational",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-water-hotel-total",
			location_id="loc-water-hotel",
			meter_id="meter-water-hotel-001",
			display_name="ホテル 水使用量",
			category="water",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="daily_1x",
			purpose="operational",
		),
		# ===== Billing (12件) =====
		# --- 電気 billing (7箇所) ---
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-incoming-kwh-billing",
			location_id="loc-electric-incoming",
			meter_id="meter-electric-incoming-001",
			display_name="引込電力量計 積算電力量 (billing)",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="10.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-hotel-branch-kwh-billing",
			location_id="loc-electric-hotel-branch",
			meter_id="meter-electric-hotel-branch-001",
			display_name="ホテル棟分岐盤 積算電力量 (billing)",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="100.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-hotel-lv-kwh-billing",
			location_id="loc-electric-hotel-lv",
			meter_id="meter-electric-hotel-lv-001",
			display_name="ホテル棟低圧動力盤 積算電力量 (billing)",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="10.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-disaster-kwh-billing",
			location_id="loc-electric-disaster",
			meter_id="meter-electric-disaster-001",
			display_name="防災動力盤 積算電力量 (billing)",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="10.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-cafe-kwh-billing",
			location_id="loc-electric-cafe",
			meter_id="meter-electric-cafe-001",
			display_name="カフェ動力盤 積算電力量 (billing)",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-hotel-kitchen-light-kwh-billing",
			location_id="loc-electric-hotel-kitchen-light",
			meter_id="meter-electric-hotel-kitchen-light-001",
			display_name="ホテル厨房電灯盤 積算電力量 (billing)",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-electric-hotel-kitchen-power-kwh-billing",
			location_id="loc-electric-hotel-kitchen-power",
			meter_id="meter-electric-hotel-kitchen-power-001",
			display_name="ホテル厨房動力盤 積算電力量 (billing)",
			category="electricity",
			unit="kWh",
			value_schema="decimal_string",
			multiplier="10.0",
			warning_threshold="5000",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		# --- ガス billing (3箇所) ---
		MeasurementDefinitionSeed(
			measurement_definition_id="md-gas-hq-total-billing",
			location_id="loc-gas-hq",
			meter_id="meter-gas-hq-001",
			display_name="本社棟 ガス使用量 (billing)",
			category="gas",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-gas-cafe-total-billing",
			location_id="loc-gas-cafe",
			meter_id="meter-gas-cafe-001",
			display_name="カフェ ガス使用量 (billing)",
			category="gas",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-gas-hotel-total-billing",
			location_id="loc-gas-hotel",
			meter_id="meter-gas-hotel-001",
			display_name="ホテル ガス使用量 (billing)",
			category="gas",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		# --- 水道 billing (2箇所) ---
		MeasurementDefinitionSeed(
			measurement_definition_id="md-water-incoming-total-billing",
			location_id="loc-water-incoming",
			meter_id="meter-water-incoming-001",
			display_name="引込量水器 水使用量 (billing)",
			category="water",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
		MeasurementDefinitionSeed(
			measurement_definition_id="md-water-hotel-total-billing",
			location_id="loc-water-hotel",
			meter_id="meter-water-hotel-001",
			display_name="ホテル 水使用量 (billing)",
			category="water",
			unit="m3",
			value_schema="decimal_string",
			multiplier="1.0",
			warning_threshold="500",
			rollover_value="100000",
			calculation_method="delta",
			reading_cycle="monthly",
			purpose="billing",
		),
	),
)


def bootstrap_default_master_data(
	session_factory: sessionmaker,
	*,
	force: bool = False,
	seed: MasterDataBootstrapSeed = DEFAULT_MASTER_DATA_SEED,
) -> BootstrapResult:
	"""既定の master data seed を投入する。

	`force=False` のときは既存行を触らず、空のキーだけを補う。`force=True` では
	bundled defaults で既存行を上書きし、開発環境の再初期化に使えるようにする。
	"""

	with session_factory() as session:
		result = _bootstrap_master_data(session, force=force, seed=seed)
		session.commit()
		return result


def _bootstrap_master_data(
	session: Session,
	*,
	force: bool,
	seed: MasterDataBootstrapSeed,
) -> BootstrapResult:
	"""1 つの Session 上で seed を insert / refresh する内部実装。"""

	inserted_physical_meters = 0
	updated_physical_meters = 0
	inserted_measurement_definitions = 0
	updated_measurement_definitions = 0
	inserted_meter_assignments = 0
	updated_meter_assignments = 0

	for item in seed.physical_meters:
		existing = session.get(PhysicalMeterORM, item.meter_id)
		if existing is None:
			session.add(
				PhysicalMeterORM(
					meter_id=item.meter_id,
					location_id=item.location_id,
					location_name=item.location_name,
					meter_name=item.meter_name,
					category=item.category,
					meter_serial=item.meter_serial,
				)
			)
			inserted_physical_meters += 1
			continue

		if not force:
			continue

		existing.location_id = item.location_id
		existing.location_name = item.location_name
		existing.meter_name = item.meter_name
		existing.category = item.category
		existing.meter_serial = item.meter_serial
		updated_physical_meters += 1

	# physical_meters を flush して FK 参照を解決する。
	session.flush()

	for item in seed.measurement_definitions:
		existing = session.get(MeasurementDefinitionORM, item.measurement_definition_id)
		if existing is None:
			session.add(
				MeasurementDefinitionORM(
					measurement_definition_id=item.measurement_definition_id,
					location_id=item.location_id,
					meter_id=item.meter_id,
					display_name=item.display_name,
					category=item.category,
					unit=item.unit,
					value_schema=item.value_schema,
					multiplier=item.multiplier,
					warning_threshold=item.warning_threshold,
					rollover_value=item.rollover_value,
					decimal_places=item.decimal_places,
					calculation_method=item.calculation_method,
					reading_cycle=item.reading_cycle,
					purpose=item.purpose,
				)
			)
			inserted_measurement_definitions += 1
			continue

		if not force:
			continue

		existing.location_id = item.location_id
		existing.meter_id = item.meter_id
		existing.display_name = item.display_name
		existing.category = item.category
		existing.unit = item.unit
		existing.value_schema = item.value_schema
		existing.multiplier = item.multiplier
		existing.warning_threshold = item.warning_threshold
		existing.rollover_value = item.rollover_value
		existing.decimal_places = item.decimal_places
		existing.calculation_method = item.calculation_method
		existing.reading_cycle = item.reading_cycle
		existing.purpose = item.purpose
		updated_measurement_definitions += 1

	# measurement_definitions を flush して MeterAssignment の FK 解決を確実にする。
	session.flush()

	inserted_measurement_definition_schedules = 0
	for item in seed.measurement_definitions:
		existing_schedule = session.get(MeasurementDefinitionScheduleORM, item.measurement_definition_id)
		if existing_schedule is not None:
			continue

		session.add(
			MeasurementDefinitionScheduleORM(
				measurement_definition_id=item.measurement_definition_id,
				schedule_json=MeasurementDefinitionScheduleService.serialize_default(ReadingCycle(item.reading_cycle)),
			)
		)
		inserted_measurement_definition_schedules += 1

	# meter_id を持つ measurement definition に対し MeterAssignment 初期レコードを生成する。
	default_effective_from = datetime(2026, 1, 1, tzinfo=timezone.utc)

	for item in seed.measurement_definitions:
		if item.meter_id is None:
			continue

		existing_assignment = session.get(
			MeterAssignmentORM,
			(item.measurement_definition_id, item.meter_id, default_effective_from),
		)
		if existing_assignment is None:
			session.add(
				MeterAssignmentORM(
					measurement_definition_id=item.measurement_definition_id,
					meter_id=item.meter_id,
					effective_from=default_effective_from,
					effective_to=None,
				)
			)
			inserted_meter_assignments += 1
			continue

		if not force:
			continue

		existing_assignment.effective_to = None
		updated_meter_assignments += 1

	return BootstrapResult(
		inserted_physical_meters=inserted_physical_meters,
		updated_physical_meters=updated_physical_meters,
		inserted_measurement_definitions=inserted_measurement_definitions,
		updated_measurement_definitions=updated_measurement_definitions,
		inserted_measurement_definition_schedules=inserted_measurement_definition_schedules,
		inserted_meter_assignments=inserted_meter_assignments,
		updated_meter_assignments=updated_meter_assignments,
	)


# ---------------------------------------------------------------------------
# settings bootstrap
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SettingsBootstrapResult:
	"""設定 bootstrap 実行結果の件数サマリー。"""

	inserted: int = 0
	skipped: int = 0
	overwritten: int = 0

	def summary(self) -> str:
		parts = [f"inserted={self.inserted}", f"skipped={self.skipped}"]
		if self.overwritten:
			parts.append(f"overwritten={self.overwritten}")
		return f"settings: {', '.join(parts)}"


def bootstrap_default_settings(
	session_factory: sessionmaker,
	*,
	force: bool = False,
) -> SettingsBootstrapResult:
	"""cutoff + legacy reading_cycle スケジュール + release offset のデフォルト値を app_settings へ投入する。

	既存キーはスキップし、`force=True` のときは上書きする。planned reading の source of truth は
	measurement_definition_schedules だが、`bootstrap --init-settings` は旧 app_settings 互換の表示・補助設定
	をまとめて投入する用途として残す。
	"""

	from application.schedule_settings import (
		CUTOFF_SETTINGS_KEY,
		DEFAULT_CUTOFF_TIME,
		SCHEDULE_SETTINGS_KEYS,
		RELEASE_OFFSET_SETTINGS_KEYS,
		ScheduleSettingsService,
		ReleaseOffsetSettingsService,
	)
	from application.timezone_settings import APP_TIMEZONE_SETTINGS_KEY, DEFAULT_APP_TIMEZONE_NAME

	# cutoff + app timezone + 6 reading_cycle + 6 release sessions = 14 settings
	settings_to_write: dict[str, str] = {
		CUTOFF_SETTINGS_KEY: DEFAULT_CUTOFF_TIME.strftime("%H:%M"),
		APP_TIMEZONE_SETTINGS_KEY: DEFAULT_APP_TIMEZONE_NAME,
	}
	for reading_cycle, key in SCHEDULE_SETTINGS_KEYS.items():
		settings_to_write[key] = ScheduleSettingsService.serialize_default(reading_cycle)
	for reading_session, key in RELEASE_OFFSET_SETTINGS_KEYS.items():
		settings_to_write[key] = ReleaseOffsetSettingsService.serialize_default(reading_session)

	inserted = 0
	skipped = 0
	overwritten = 0

	with session_factory() as session:
		for key, value in settings_to_write.items():
			existing = session.get(AppSettingsORM, key)
			if existing is None:
				session.add(
					AppSettingsORM(
						setting_key=key,
						setting_value=value,
						updated_at=datetime.now(timezone.utc),
					)
				)
				inserted += 1
			elif force:
				existing.setting_value = value
				existing.updated_at = datetime.now(timezone.utc)
				overwritten += 1
			else:
				skipped += 1
		session.commit()

	return SettingsBootstrapResult(inserted=inserted, skipped=skipped, overwritten=overwritten)


__all__ = [
	"BootstrapResult",
	"DEFAULT_MASTER_DATA_SEED",
	"MasterDataBootstrapSeed",
	"MeasurementDefinitionSeed",
	"PhysicalMeterSeed",
	"SettingsBootstrapResult",
	"bootstrap_default_master_data",
	"bootstrap_default_settings",
]
