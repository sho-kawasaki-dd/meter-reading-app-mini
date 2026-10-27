from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from meter_reading_mini.core.errors import (
    MigrationError,
    SchemaTooNewError,
    UnsupportedSchemaError,
)
from meter_reading_mini.db.backup import copy_database
from meter_reading_mini.db.connection import connect, transaction

SCHEMA_VERSION = 1
MIN_SUPPORTED_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class MigrationStep:
    apply: str | Callable[[sqlite3.Connection], None]
    precheck: Callable[[sqlite3.Connection], list[str]] | None = None
    rebuilds_tables: bool = False


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    type: str
    notnull: bool
    default: str | None
    primary_key_order: int


@dataclass(frozen=True, slots=True)
class ForeignKeySpec:
    from_column: str
    to_table: str
    to_column: str
    on_update: str
    on_delete: str
    match: str


@dataclass(frozen=True, slots=True)
class IndexSpec:
    name: str
    table: str
    unique: bool
    columns_or_expressions: tuple[str | None, ...]
    sql: str


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    name: str
    table: str
    sql: str


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    foreign_keys: tuple[ForeignKeySpec, ...]
    sql: str


@dataclass(frozen=True, slots=True)
class SchemaSpec:
    tables: tuple[TableSpec, ...]
    indexes: tuple[IndexSpec, ...]
    triggers: tuple[TriggerSpec, ...]


def _read_schema_spec(conn: sqlite3.Connection) -> SchemaSpec:
    table_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'table' AND name NOT GLOB 'sqlite_*' ORDER BY name"
    ).fetchall()
    tables: list[TableSpec] = []
    indexes: list[IndexSpec] = []
    triggers: list[TriggerSpec] = []

    for table_name, table_sql in table_rows:
        columns = tuple(
            ColumnSpec(
                name=str(row[1]),
                type=str(row[2]).upper(),
                notnull=bool(row[3]),
                default=None if row[4] is None else str(row[4]),
                primary_key_order=int(row[5]),
            )
            for row in conn.execute("SELECT * FROM pragma_table_xinfo(?)", (str(table_name),))
        )
        foreign_key_rows = conn.execute(
            "SELECT * FROM pragma_foreign_key_list(?)", (str(table_name),)
        ).fetchall()
        foreign_keys = tuple(
            sorted(
                (
                    ForeignKeySpec(
                        from_column=str(row[3]),
                        to_table=str(row[2]),
                        to_column=str(row[4]),
                        on_update=str(row[5]).upper(),
                        on_delete=str(row[6]).upper(),
                        match=str(row[7]).upper(),
                    )
                    for row in foreign_key_rows
                ),
                key=lambda spec: (
                    spec.from_column,
                    spec.to_table,
                    spec.to_column,
                    spec.on_update,
                    spec.on_delete,
                    spec.match,
                ),
            )
        )
        tables.append(
            TableSpec(
                name=str(table_name),
                columns=columns,
                foreign_keys=foreign_keys,
                sql=str(table_sql),
            )
        )

        for index_row in conn.execute("SELECT * FROM pragma_index_list(?)", (str(table_name),)):
            index_name = str(index_row[1])
            if str(index_row[3]) == "auto":
                continue
            index_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            if index_sql_row is None or index_sql_row[0] is None:
                continue
            index_columns = tuple(
                None if row[1] == -2 else str(row[2])
                for row in conn.execute("SELECT * FROM pragma_index_xinfo(?)", (index_name,))
                if row[5]
            )
            indexes.append(
                IndexSpec(
                    name=index_name,
                    table=str(table_name),
                    unique=bool(index_row[2]),
                    columns_or_expressions=index_columns,
                    sql=str(index_sql_row[0]),
                )
            )

    for trigger_name, table_name, trigger_sql in conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master "
        "WHERE type = 'trigger' AND name NOT GLOB 'sqlite_*' ORDER BY name"
    ):
        if trigger_sql is not None:
            triggers.append(TriggerSpec(str(trigger_name), str(table_name), str(trigger_sql)))

    return SchemaSpec(tuple(tables), tuple(indexes), tuple(triggers))


def _schema_spec_from_ddl(ddl: str) -> SchemaSpec:
    conn = sqlite3.connect(":memory:", autocommit=True)
    try:
        conn.executescript(ddl)
        return _read_schema_spec(conn)
    finally:
        conn.close()


MIGRATIONS: dict[int, MigrationStep | str] = {}

SCHEMA_SPECS: dict[int, SchemaSpec] = {
    1: _schema_spec_from_ddl(
        files("meter_reading_mini.db").joinpath("schema.sql").read_text(encoding="utf-8")
    ),
}


def normalize_sql(sql: str) -> str:
    tokens: list[str] = []
    position = 0
    length = len(sql)

    while position < length:
        char = sql[position]
        if char.isspace():
            position += 1
            continue
        if sql.startswith("--", position):
            newline = sql.find("\n", position + 2)
            position = length if newline == -1 else newline + 1
            continue
        if sql.startswith("/*", position):
            end = sql.find("*/", position + 2)
            position = length if end == -1 else end + 2
            continue

        if char in {"'", '"', "`", "["}:
            closing = "]" if char == "[" else char
            start = position
            position += 1
            while position < length:
                if sql[position] == closing:
                    if position + 1 < length and sql[position + 1] == closing:
                        position += 2
                        continue
                    position += 1
                    break
                position += 1
            token = sql[start:position]
            if char == '"':
                identifier = token[1:-1].replace('""', '"')
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
                    tokens.append(identifier.lower())
                else:
                    tokens.append(token)
            else:
                tokens.append(token)
            continue

        if char.isalnum() or char in {"_", "$"} or ord(char) > 127:
            start = position
            position += 1
            while position < length and (
                sql[position].isalnum() or sql[position] in {"_", "$"} or ord(sql[position]) > 127
            ):
                position += 1
            tokens.append(sql[start:position].lower())
            continue

        tokens.append(char)
        position += 1

    return " ".join(tokens)


def inspect_schema(
    conn: sqlite3.Connection,
    version: int,
    specs: dict[int, SchemaSpec] = SCHEMA_SPECS,
) -> list[str]:
    expected = specs.get(version)
    if expected is None:
        return [f"スキーマ版 {version} の検査定義がありません"]

    actual = _read_schema_spec(conn)
    reasons: list[str] = []
    expected_tables = {table.name: table for table in expected.tables}
    actual_tables = {table.name: table for table in actual.tables}
    if expected_tables.keys() != actual_tables.keys():
        missing = sorted(expected_tables.keys() - actual_tables.keys())
        unexpected = sorted(actual_tables.keys() - expected_tables.keys())
        if missing:
            reasons.append("不足しているテーブル: " + ", ".join(missing))
        if unexpected:
            reasons.append("未定義のテーブル: " + ", ".join(unexpected))

    for name in sorted(expected_tables.keys() & actual_tables.keys()):
        expected_table = expected_tables[name]
        actual_table = actual_tables[name]
        if expected_table.columns != actual_table.columns:
            reasons.append(f"テーブル {name} の列定義が一致しません")
        if expected_table.foreign_keys != actual_table.foreign_keys:
            reasons.append(f"テーブル {name} の外部キーが一致しません")
        if normalize_sql(expected_table.sql) != normalize_sql(actual_table.sql):
            reasons.append(f"テーブル {name} のDDLが一致しません")

    expected_indexes = {index.name: index for index in expected.indexes}
    actual_indexes = {index.name: index for index in actual.indexes}
    if expected_indexes.keys() != actual_indexes.keys():
        missing = sorted(expected_indexes.keys() - actual_indexes.keys())
        unexpected = sorted(actual_indexes.keys() - expected_indexes.keys())
        if missing:
            reasons.append("不足しているインデックス: " + ", ".join(missing))
        if unexpected:
            reasons.append("未定義のインデックス: " + ", ".join(unexpected))
    for name in sorted(expected_indexes.keys() & actual_indexes.keys()):
        expected_index = expected_indexes[name]
        actual_index = actual_indexes[name]
        if (
            expected_index.table != actual_index.table
            or expected_index.unique != actual_index.unique
            or expected_index.columns_or_expressions != actual_index.columns_or_expressions
        ):
            reasons.append(f"インデックス {name} の属性が一致しません")
        if normalize_sql(expected_index.sql) != normalize_sql(actual_index.sql):
            reasons.append(f"インデックス {name} のDDLが一致しません")

    expected_triggers = {trigger.name: trigger for trigger in expected.triggers}
    actual_triggers = {trigger.name: trigger for trigger in actual.triggers}
    if expected_triggers.keys() != actual_triggers.keys():
        missing = sorted(expected_triggers.keys() - actual_triggers.keys())
        unexpected = sorted(actual_triggers.keys() - expected_triggers.keys())
        if missing:
            reasons.append("不足しているトリガー: " + ", ".join(missing))
        if unexpected:
            reasons.append("未定義のトリガー: " + ", ".join(unexpected))
    for name in sorted(expected_triggers.keys() & actual_triggers.keys()):
        expected_trigger = expected_triggers[name]
        actual_trigger = actual_triggers[name]
        if expected_trigger.table != actual_trigger.table:
            reasons.append(f"トリガー {name} の対象テーブルが一致しません")
        if normalize_sql(expected_trigger.sql) != normalize_sql(actual_trigger.sql):
            reasons.append(f"トリガー {name} のDDLが一致しません")

    views = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'view' AND name NOT GLOB 'sqlite_*'"
    ).fetchall()
    if views:
        reasons.append("未定義のビュー: " + ", ".join(sorted(str(row[0]) for row in views)))
    return reasons


def check_migration_path(
    from_version: int,
    to_version: int,
    migrations: Mapping[int, MigrationStep | str] = MIGRATIONS,
) -> None:
    if from_version < 0 or to_version < from_version:
        raise UnsupportedSchemaError("不正なマイグレーション経路です")
    missing = [
        version for version in range(from_version + 1, to_version + 1) if version not in migrations
    ]
    if missing:
        raise UnsupportedSchemaError(
            "マイグレーション経路がありません: " + ", ".join(map(str, missing))
        )


def _migration_step(value: MigrationStep | str) -> MigrationStep:
    return value if isinstance(value, MigrationStep) else MigrationStep(value)


def check_migration_prechecks(
    conn: sqlite3.Connection,
    from_version: int,
    to_version: int,
    migrations: Mapping[int, MigrationStep | str] = MIGRATIONS,
) -> None:
    violations: list[str] = []
    for target_version in range(from_version + 1, to_version + 1):
        precheck = _migration_step(migrations[target_version]).precheck
        if precheck is not None:
            violations.extend(precheck(conn))
    violations = list(dict.fromkeys(violations))
    if violations:
        details = "、".join(violations[:20])
        if len(violations) > 20:
            details += f"、ほか {len(violations) - 20} 件"
        raise MigrationError(
            f"移行前検査に失敗しました: {details}。データベースは変更されていません"
        )


def migrate_schema(
    conn: sqlite3.Connection,
    from_version: int,
    to_version: int,
    migrations: Mapping[int, MigrationStep | str] = MIGRATIONS,
    specs: dict[int, SchemaSpec] = SCHEMA_SPECS,
) -> None:
    check_migration_path(from_version, to_version, migrations)
    for target_version in range(from_version + 1, to_version + 1):
        step = _migration_step(migrations[target_version])
        if step.rebuilds_tables:
            conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with transaction(conn):
                if isinstance(step.apply, str):
                    conn.executescript(step.apply)
                else:
                    step.apply(conn)
                if step.rebuilds_tables:
                    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if foreign_key_errors:
                        raise UnsupportedSchemaError(
                            "外部キー検査に失敗しました: "
                            + ", ".join(str(row) for row in foreign_key_errors[:20])
                        )
                reasons = inspect_schema(conn, target_version, specs)
                if reasons:
                    raise UnsupportedSchemaError(
                        f"スキーマ版 {target_version} の検査に失敗しました: " + "、".join(reasons)
                    )
                _set_user_version(conn, target_version)
        finally:
            if step.rebuilds_tables:
                conn.execute("PRAGMA foreign_keys = ON")


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    if type(version) is not int or version < 0:
        raise ValueError("スキーマ版数は 0 以上の整数で指定してください")
    conn.execute(f"PRAGMA user_version = {version}")


def create_schema(
    conn: sqlite3.Connection,
    schema_sql: str | None = None,
    *,
    version: int = SCHEMA_VERSION,
    specs: dict[int, SchemaSpec] = SCHEMA_SPECS,
) -> None:
    if schema_sql is None:
        schema_sql = (
            files("meter_reading_mini.db").joinpath("schema.sql").read_text(encoding="utf-8")
        )
    with transaction(conn):
        conn.executescript(schema_sql)
        reasons = inspect_schema(conn, version, specs)
        if reasons:
            raise UnsupportedSchemaError("新規スキーマの検査に失敗しました: " + "、".join(reasons))
        _set_user_version(conn, version)


def _remove_database_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


def _validate_version_configuration(schema_version: int, min_supported: int) -> None:
    for value, label in (
        (schema_version, "schema_version"),
        (min_supported, "min_supported"),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{label} は 1 以上の整数で指定してください")
    if min_supported > schema_version:
        raise ValueError("min_supported は schema_version 以下にしてください")


def open_database(
    path: Path,
    backup_dir: Path,
    *,
    backup_timestamp: Callable[[], str],
    schema_version: int = SCHEMA_VERSION,
    min_supported: int = MIN_SUPPORTED_SCHEMA_VERSION,
    migrations: Mapping[int, MigrationStep | str] = MIGRATIONS,
    specs: dict[int, SchemaSpec] = SCHEMA_SPECS,
) -> sqlite3.Connection:
    _validate_version_configuration(schema_version, min_supported)
    path = Path(path)
    backup_dir = Path(backup_dir)
    existed_before_connect = path.exists()
    conn: sqlite3.Connection | None = None

    try:
        conn = connect(path)
        if not existed_before_connect:
            create_schema(conn, version=schema_version, specs=specs)
            return conn

        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version > schema_version:
            raise SchemaTooNewError(
                f"データベースのスキーマ版 {current_version} は、このアプリの対応版 "
                f"{schema_version} より新しいため開けません"
            )
        if current_version == 0 or current_version < min_supported:
            raise UnsupportedSchemaError(
                f"データベースのスキーマ版 {current_version} はサポートされていません"
            )

        reasons = inspect_schema(conn, current_version, specs)
        if reasons:
            raise UnsupportedSchemaError("スキーマが一致しません: " + "、".join(reasons))
        if current_version == schema_version:
            return conn

        check_migration_path(current_version, schema_version, migrations)
        check_migration_prechecks(conn, current_version, schema_version, migrations)
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / (f"pre-migrate_v{current_version}_{backup_timestamp()}.db")
            copy_database(conn, backup_path)
        except Exception as error:
            raise MigrationError(
                f"マイグレーション前のバックアップを作成できません: {error}"
            ) from error

        try:
            migrate_schema(conn, current_version, schema_version, migrations, specs)
        except Exception as error:
            raise MigrationError(
                f"スキーマ版 {schema_version} への移行に失敗しました: {error}",
                backup_path=backup_path,
            ) from error
        return conn
    except BaseException:
        if conn is not None:
            conn.close()
        if not existed_before_connect:
            _remove_database_files(path)
        raise
