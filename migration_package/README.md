# 検針管理アプリ 移行パッケージ (`migration_package`)

このフォルダは、現行の「FastAPI サーバー + ネイティブ Android アプリ」構成から、**「ローカル完結型 PySide6 デスクトップアプリ + 自己完結型単一 HTML(Quick Share 送受信) + 標準 `sqlite3`」** 構成の新リポジトリへ移行するためのスターターパッケージです。

新規リポジトリを作成する際は、**この `migration_package/` フォルダの中身をそのまま新しいリポジトリのルートへコピー**して開発を開始できます。

---

## 1. 同梱されているファイル構成

```text
migration_package/
├─ uv.lock                                # uv の依存ロックファイル
├─ pyproject.toml                         # PySide6 + 標準 sqlite3 + Playwright 構成のプロジェクト定義
├─ README.md                              # 本ガイド
├─ docs/
│  └─ plan/
│     └─ オフライン対応・Wi-Fi同期型_検針システム企画書_v4.md # 旧企画書(業務ルールの参考記録)
├─ src/
│  └─ meter_reading_mini/
│     ├─ __init__.py
│     ├─ app.py                              # Phase 0 起動オーケストレーション
│     ├─ config.py                          # 【同梱済】パス解決 (platformdirs / METER_READING_MINI_DATA_DIR)
│     ├─ core/                              # 純 Python の業務ロジック・データ契約層
│     │  ├─ contract_models.py              # 旧 api/models.py (Enum・Decimal文字列検証・WorkPackage/Sync DTO)
│     │  ├─ errors.py                       # 【同梱済】DomainError 基底と共通業務例外
│     │  ├─ timeutil.py                     # 【同梱済】UTC 現在時刻・ローカル変換・ファイル名日時生成
│     │  ├─ mobile_reading.py               # 【同梱済】MR1:<Base64>.<CRC32>:END エンベロープ符号化/復号・下書き拒否・JSONエスケープ
│     │  ├─ meter_reading_sync_repositories.py # Repository Protocol 定義と入出力 dataclass
│     │  ├─ usage_delta_calculation.py      # 差分計算・ロールオーバー補正・倍率計算 (純関数)
│     │  ├─ reading_write_orchestration.py  # 保存前履歴照合・交換境界適用・後続実測値の連鎖再計算
│     │  ├─ meter_reading_sync.py           # 検針結果・メーター交換の一括 Upsert (client_updated_at 判定)
│     │  ├─ work_package_download.py        # 予定枠 backfill 生成・auto_closed 判定・周期別グループ構築
│     │  ├─ schedule_settings.py            # 日次/週次/月次/隔月スケジュール設定・配信オフセット計算
│     │  ├─ timezone_settings.py            # タイムゾーン・業務日付算出
│     │  ├─ reading_signature.py            # SHA-256 監査署名生成・検証
│     │  ├─ meter_master_service.py         # 物理メーター CRUD バリデーション (Repository 経由へ書き換え対象)
│     │  └─ logging_config.py               # ローテーション付きログ設定
│     ├─ db/                                # 標準 sqlite3 データベース層
│     │  ├─ connection.py                  # 【同梱済】autocommit=True・WAL・読み取り専用接続・transaction()
│     │  ├─ schema.sql                     # 【同梱済】標準 sqlite3 用の最新 DDL (将来統合対応)
│     │  ├─ migrations.py                  # 【同梱済】SCHEMA_VERSION=1・DDL/PRAGMA 厳密照合エンジン・open_database()
│     │  ├─ backup.py                      # 【同梱済】SQLite 整合性検査・Connection.backup() コピー
│     │  ├─ repositories.py                # 【実装済】標準 sqlite3 Repository
│     │  └─ bootstrap_seeds.py             # 【実装済】既定設定の初期投入
│     └─ ui/                                # PySide6 UI 層 (Phase 0 共通部品同梱済 / Phase 3〜5 で画面実装)
│        ├─ single_instance.py              # 【同梱済】QLockFile による多重起動防止
│        ├─ display_settings.py             # 【同梱済】フォント・文字サイズ設定 (ui_settings.json)
│        ├─ dialog_placement.py             # 【同梱済】マルチモニタ・画面端対応ダイアログ中央配置
│        ├─ error_handling.py               # 【同梱済】DomainError ガード実行・未捕捉例外ダイアログ
│        └─ csv_export.py                   # 【同梱済】拡張子補完・上書き確認付きファイル出力ダイアログ
└─ tests/                                   # ドメイン単体テスト群 (10ファイル) + 共通基盤単体テスト群 (12ファイル + conftest.py)
   ├─ conftest.py
   ├─ test_config.py
   ├─ test_connection.py
   ├─ test_backup.py
   ├─ test_migrations.py
   ├─ test_errors.py
   ├─ test_timeutil.py
   ├─ test_mobile_reading_envelope.py
   ├─ test_single_instance.py
   ├─ test_display_settings.py
   ├─ test_dialog_placement.py
   ├─ test_error_handling.py
   ├─ test_csv_export.py
   ├─ test_api_models.py
   ├─ test_usage_delta_calculation.py
   ├─ test_reading_write_orchestration.py
   ├─ test_meter_reading_sync_service.py
   ├─ test_meter_reading_sync_service_batch_txn.py
   ├─ test_work_package_download_service.py
   ├─ test_measurement_definition_schedule_service.py
   ├─ test_reading_signature.py
   ├─ test_timezone_settings.py
   └─ test_meter_master_service.py
```

開発計画書の正本はリポジトリ直下の [`docs/plan/ローカル検針管理アプリ開発計画書.md`](../docs/plan/ローカル検針管理アプリ開発計画書.md) にあります。

---

## 2. 新リポジトリへの移行・開発手順 (Phase 0・Phase 1 完了)

### Step 1: 新リポジトリへのコピーと環境構築

1. 新しいフォルダ（例: `meter_reading_mini`）を作成し、`migration_package/` 内の全ファイル・フォルダをコピーします。
2. 備品在庫管理アプリ（`inventory-manager-mini`）由来の Phase 0 共通基盤モジュール（`config.py`, `core/errors.py`, `core/timeutil.py`, `core/mobile_reading.py`, `db/connection.py`, `db/backup.py`, `db/migrations.py`, `ui/single_instance.py`, `ui/display_settings.py`, `ui/dialog_placement.py`, `ui/error_handling.py`, `ui/csv_export.py`）およびその単体テストは**同梱済み**です。起動オーケストレーションと CI workflow も実装されています。[本体 PR #1](https://github.com/sho-kawasaki-dd/meter-reading-app-mini/pull/1) で両 OS の必須チェック成功後に merge し、[失敗系検証 PR #2](https://github.com/sho-kawasaki-dd/meter-reading-app-mini/pull/2) で未成功チェック時の merge ブロックも確認済みです。完了記録と作業計画書の凍結移動も済み、Phase 0 は完了しています。
3. コピー先のプロジェクトルートで `uv sync --locked` を実行して環境を構築します。依存関係を追加・変更するときは `uv lock` で `uv.lock` を更新し、差分を確認してから `uv sync --locked` で同期を検証してください。

### Step 2: `core/` の import 整理と日時ヘルパー移動 (実装済み)

1. 純粋な日時補助関数 `normalize_to_utc()` と `build_scheduled_at()` は `core/timezone_settings.py` に配置し、DB 実装への依存をなくしています。
2. `core/` と `tests/` の旧 import を移植先の契約・Service へ移行済みです。契約境界テストで契約から Service・UI・DB 実装への依存がないことを検証します。
3. 結果保存は標準 `sqlite3` の Repository Protocol と Service 所有のトランザクションに統合済みです。

### Step 3: 標準 `sqlite3` リポジトリ (`db/repositories.py`) の実装 (完了)

`db/repositories.py` が `db/schema.sql` に対する Repository Protocol を標準 `sqlite3` で実装しています。単体テストと Service 統合テストが成功した後、旧 SQLAlchemy 参考実装は削除済みです。新たな SQLAlchemy 依存はありません。
