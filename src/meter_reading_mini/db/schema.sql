-- 検針管理アプリ 標準 sqlite3 スキーマ (SCHEMA_VERSION = 1)
-- 将来の設備管理アプリ統合時にも備品在庫管理テーブルと衝突しない命名とする

CREATE TABLE staff (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE physical_meters (
  meter_id TEXT PRIMARY KEY,
  location_id TEXT NOT NULL,
  location_name TEXT NOT NULL,
  meter_name TEXT NOT NULL,
  category TEXT NOT NULL CHECK (category IN ('electricity','gas','water','boiler','others')),
  meter_serial TEXT,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);
CREATE INDEX idx_physical_meters_location ON physical_meters(location_id);

CREATE TABLE measurement_definitions (
  measurement_definition_id TEXT PRIMARY KEY,
  location_id TEXT NOT NULL,
  meter_id TEXT REFERENCES physical_meters(meter_id),
  display_name TEXT NOT NULL,
  category TEXT NOT NULL CHECK (category IN ('electricity','gas','water','boiler','others')),
  unit TEXT NOT NULL,
  value_schema TEXT NOT NULL CHECK (value_schema IN ('decimal_string','power_factor_percent_phase')),
  multiplier TEXT NOT NULL,
  decimal_places INTEGER CHECK (
    decimal_places IS NULL OR (typeof(decimal_places) = 'integer' AND decimal_places BETWEEN 0 AND 10)
  ),
  warning_threshold TEXT,
  rollover_value TEXT,
  calculation_method TEXT NOT NULL CHECK (calculation_method IN ('delta','raw_only')),
  reading_cycle TEXT NOT NULL CHECK (
    reading_cycle IN ('daily_1x','daily_2x','daily_3x','weekly','monthly','every_two_months')
  ),
  purpose TEXT NOT NULL DEFAULT 'operational' CHECK (purpose IN ('operational','billing','audit')),
  generation_start_date TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);
CREATE INDEX idx_measurement_defs_location ON measurement_definitions(location_id);

CREATE TABLE measurement_definition_schedules (
  measurement_definition_id TEXT NOT NULL
    REFERENCES measurement_definitions(measurement_definition_id),
  effective_from_date TEXT NOT NULL,
  schedule_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (measurement_definition_id, effective_from_date)
);

CREATE TABLE reading_cycle_schedule_defaults (
  reading_cycle TEXT NOT NULL CHECK (
    reading_cycle IN ('daily_1x','daily_2x','daily_3x','weekly','monthly','every_two_months')
  ),
  effective_from_date TEXT NOT NULL,
  schedule_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (reading_cycle, effective_from_date)
);

CREATE TABLE reading_session_release_offsets (
  reading_session TEXT NOT NULL CHECK (
    reading_session IN ('00:00','06:00','08:00','14:00','17:00','22:00')
  ),
  effective_from_date TEXT NOT NULL,
  offset_days INTEGER NOT NULL CHECK (typeof(offset_days) = 'integer'),
  created_at TEXT NOT NULL,
  PRIMARY KEY (reading_session, effective_from_date)
);

CREATE TABLE meter_assignments (
  measurement_definition_id TEXT NOT NULL
    REFERENCES measurement_definitions(measurement_definition_id),
  meter_id TEXT NOT NULL REFERENCES physical_meters(meter_id),
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  PRIMARY KEY (measurement_definition_id, meter_id, effective_from)
);

CREATE TABLE planned_readings (
  planned_reading_id TEXT PRIMARY KEY,
  measurement_definition_id TEXT NOT NULL
    REFERENCES measurement_definitions(measurement_definition_id),
  meter_id TEXT NOT NULL REFERENCES physical_meters(meter_id),
  location_id TEXT NOT NULL,
  business_date TEXT NOT NULL,
  release_date TEXT NOT NULL,
  reading_session TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  task_state TEXT NOT NULL DEFAULT 'open' CHECK (
    task_state IN ('open','completed','auto_closed','superseded')
  ),
  closed_at TEXT,
  close_reason TEXT CHECK (
    close_reason IS NULL OR close_reason IN (
      'cutoff_elapsed','meter_replaced','schedule_recomposed','definition_deactivated'
    )
  ),
  superseded_by_planned_reading_id TEXT REFERENCES planned_readings(planned_reading_id)
);
CREATE INDEX idx_planned_readings_def_sched
  ON planned_readings(measurement_definition_id, scheduled_at, planned_reading_id);
CREATE INDEX idx_planned_readings_business_date ON planned_readings(business_date);
CREATE INDEX idx_planned_readings_release_date ON planned_readings(release_date);

CREATE TABLE reading_transactions (
  planned_reading_id TEXT PRIMARY KEY
    REFERENCES planned_readings(planned_reading_id),
  result_status TEXT NOT NULL CHECK (
    result_status IN ('measured','skipped','unreachable','meter_fault','replaced')
  ),
  recorded_at TEXT NOT NULL,
  client_updated_at TEXT NOT NULL,
  raw_reading TEXT,
  last_reading TEXT,
  usage_delta TEXT,
  structured_payload_json TEXT,
  device_id TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL,
  signature TEXT NOT NULL,
  note TEXT,
  result_source TEXT NOT NULL CHECK (result_source IN ('mobile_html','manual')),
  arrival_sequence INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE replacement_events (
  replacement_event_id TEXT PRIMARY KEY,
  location_id TEXT NOT NULL,
  old_meter_id TEXT NOT NULL REFERENCES physical_meters(meter_id),
  new_meter_id TEXT NOT NULL REFERENCES physical_meters(meter_id),
  replaced_at TEXT NOT NULL,
  old_final_reading TEXT,
  new_initial_reading TEXT,
  client_updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  note TEXT,
  arrival_sequence INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_replacement_events_location ON replacement_events(location_id);

CREATE TABLE replacement_boundaries (
  replacement_event_id TEXT NOT NULL REFERENCES replacement_events(replacement_event_id),
  measurement_definition_id TEXT NOT NULL
    REFERENCES measurement_definitions(measurement_definition_id),
  location_id TEXT NOT NULL,
  business_date TEXT NOT NULL,
  old_meter_id TEXT NOT NULL REFERENCES physical_meters(meter_id),
  new_meter_id TEXT NOT NULL REFERENCES physical_meters(meter_id),
  replaced_at TEXT NOT NULL,
  previous_measured_reading TEXT,
  previous_measured_planned_reading_id TEXT,
  old_final_reading TEXT,
  old_meter_usage_delta TEXT,
  new_initial_reading TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (replacement_event_id, measurement_definition_id)
);

CREATE TABLE meter_app_settings (
  setting_key TEXT PRIMARY KEY,
  setting_value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
