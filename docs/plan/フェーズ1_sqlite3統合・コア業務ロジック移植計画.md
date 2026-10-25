# Phase 1 標準 sqlite3 統合・コア業務ロジック移植計画

- 作成日: 2026-10-25
- ステータス: 計画(未着手)
- 対象: `migration_package/` を基点とする Phase 1
- 正本: [ローカル検針管理アプリ開発計画書](ローカル検針管理アプリ開発計画書.md) 8 章 Phase 1
- 前提: [Phase 0 計画](implemented/フェーズ0_共通基盤・CI整備計画.md) 完了済み(`uv.lock`、CI、`main` 保護)
- GitHub: `sho-kawasaki-dd/meter-reading-app-mini`

## 1. 目的

旧構成から同梱した `core/` の業務ロジックを、標準 `sqlite3` の Repository と Service のトランザクション境界へ統合する。Phase 0 で検査対象から外したファイルをすべて対象へ戻し、全テスト・全品質ゲートを成功させる。参考資産 `db/reference_sqlalchemy/` は統合テスト成功後に削除する。

## 2. 現状(2026-10-25 調査)

- [schema.sql](../../migration_package/src/meter_reading_mini/db/schema.sql) は v1 で、正本 4.2 章と一致している。[connection.py](../../migration_package/src/meter_reading_mini/db/connection.py) に `transaction()` / `read_transaction()` はあるが、SAVEPOINT 補助はない。
- 未移植の `core/` 10 ファイル(`contract_models`、`meter_master_service`、`meter_reading_sync`、`meter_reading_sync_repositories`、`reading_signature`、`reading_write_orchestration`、`schedule_settings`、`timezone_settings`、`usage_delta_calculation`、`work_package_download`)には次が残る。
  - 旧 import(`api.models`、`application.*`、`core.database`)
  - `sqlalchemy.orm.Session` 依存
  - タブインデント
- [meter_reading_sync.py](../../migration_package/src/meter_reading_mini/core/meter_reading_sync.py) は結果 1 件ごとに `session.begin_nested()` を使う SQLAlchemy 前提の実装。
- [meter_master_service.py](../../migration_package/src/meter_reading_mini/core/meter_master_service.py) は Repository を介さず ORM を直接使う。
- 参考実装は `datetime.now()` / `date.today()` を直接呼ぶ。`build_scheduled_at()` と `normalize_to_utc()` は DB 層に置くべきでない純粋な日時補助である。
- 業務タイムゾーンの基準が分かれている。
  - [timezone_settings.py](../../migration_package/src/meter_reading_mini/core/timezone_settings.py): 設定値(既定 Asia/Tokyo)を使い、内部で `datetime.now` を呼ぶ。
  - [timeutil.py](../../migration_package/src/meter_reading_mini/core/timeutil.py): `tz=None` のとき OS のローカル時刻を使う。
- 旧テスト 10 ファイルは、存在しない `core.startup_diagnostics`、`core.bootstrap`、`core.repositories.*` も参照する。
- スキーマには臨時検針枠(`ad_hoc`)を区別する列がなく、旧実装は `ad_hoc_tasks=[]` 固定である。

## 3. 決定事項(2026-10-25 確認済み)

| # | 論点 | 決定 |
| --- | --- | --- |
| D1 | PR の分け方 | 4 段階の PR(1a〜1d)＋完了記録 PR |
| D2 | 一括確定のトランザクション | 全体を 1 トランザクションにし、その中で検針結果 1 件ごとに SAVEPOINT を置く。業務条件で拒否(`DomainError` 派生)した結果だけを戻し、残りは確定する。予期しない例外は握りつぶさず、全体を ROLLBACK して再送出する |
| D3 | 古い更新の無視 | `client_updated_at` が古い入力は無視する。同時刻のときは `arrival_sequence` を進め、後から届いた入力を採用する |
| D4 | 監査署名 | PC の確定時に `reading_signature.py` で生成する。スマホ側署名の検証は行わない |
| D5 | 業務タイムゾーン | `meter_app_settings` の `app.timezone.v1`(既定 Asia/Tokyo)に統一する。`timeutil` には tz を必ず明示して渡す |
| D6 | DB の時刻文字列 | `timeutil` の `YYYY-MM-DD HH:MM:SS`(UTC、秒単位)に統一する |
| D7 | 初期データ | 設定の既定値のみ投入する。サンプルのマスタは架空のものをテスト用フィクスチャに限って用意し、実施設名のデータは移さない |
| D8 | スキーマ修正 | 未配布のため v1 を直接修正し、`SCHEMA_VERSION` は 1 のままにする |
| D9 | 担当者(`staff`) | Repository と Service を Phase 1 に含める |

## 4. スコープ

### 対象

- `core/` 10 ファイルの import 修正・整形・strict 型付け
- `db/repositories.py`(Protocol の標準 sqlite3 実装)と `db/bootstrap_seeds.py`
- `connection.py` への SAVEPOINT 補助の追加
- `staff` の Repository と Service
- 旧テストの移植と Repository 統合テスト
- 検査対象外設定の解除と `db/reference_sqlalchemy/` の削除
- 予定枠の補完・自動クローズの Service 実装と、`app.py` の起動順への組み込み
- 関連文書の更新

### 対象外

- UI 全般(Phase 3 / 4)、`__main__.py`
- HTML シート生成と Playwright テスト(Phase 2)
- 集計・CSV・`db/integrity.py`・復元(Phase 5)
- 新規依存の追加(SQLAlchemy、Alembic、FastAPI 等)

## 5. 実装タスク

各タスクは、完了を検証してから `[x]` にする。ファイルが存在するだけでは完了としない。

### 5.1 PR-1a `feature/phase1-contracts-pure-logic`: 契約と純粋ロジック

- [ ] `migration_package/` で `uv sync --locked` を実行し、全体を対象に ruff / pyright / pytest 収集 / coverage の件数を記録する(7 章の記録欄へ)
- [ ] 対象 10 ファイルとテストを `ruff format` と `ruff check --fix` で機械整形し、機械的な変更だけを別コミットにする
- [ ] import を置き換える
  - `api.models` → `meter_reading_mini.core.contract_models`
  - `application.` → `meter_reading_mini.core.`
- [ ] `build_scheduled_at()` と `normalize_to_utc()` を `core/timezone_settings.py` へ移す(D5)
- [ ] `build_current_datetime_provider` / `build_business_date_provider` が `datetime.now` を直接呼ぶ箇所を、`timeutil.utc_now()` に tz を明示した形へ置き換える
- [ ] 次のモジュールが pyright strict で 0 件になる: `contract_models`、`usage_delta_calculation`、`reading_signature`、`timezone_settings`、`schedule_settings`
- [ ] 業務例外を `core/errors.py` の `DomainError` 派生へそろえる。同期処理の拒否コードとの対応表を作る
- [ ] 次のテストを移植する: `test_api_models`、`test_usage_delta_calculation`、`test_reading_signature`、`test_timezone_settings`、`test_measurement_definition_schedule_service`
- [ ] `test_timezone_settings.py` のうち `core.startup_diagnostics` 等に依存する部分は削除または置換し、理由と件数を 7 章へ記録する
- [ ] 上記ファイルを `pyproject.toml` の ruff / pyright / pytest / coverage の対象外から外し、ローカルのゲートが通る

### 5.2 PR-1b `feature/phase1-sqlite-repositories`: 標準 sqlite3 Repository

- [ ] `connection.py` に `savepoint(conn, name)` を追加する
  - トランザクション外で呼ぶと `RuntimeError` になる
  - 例外時は `ROLLBACK TO` と `RELEASE` を行い、例外を再送出する
  - 正常時は `RELEASE` する
- [ ] スキーマを点検し、必要なら v1 を修正する(D8)。`db/schema.sql`、`db/migrations.py` の版別検査定義を同時に直す。点検項目は次のとおり。
  - [ ] 臨時検針枠(`ad_hoc`)を区別する列の要否(旧実装は固定の空配列)
  - [ ] `reading_transactions.result_source` の CHECK 制約の要否
  - [ ] 時刻列・Decimal 列の形式を保つ CHECK の要否
- [ ] `db/repositories.py` に次の Repository を実装する(`core/meter_reading_sync_repositories.py` の Protocol に合わせる)
  - 予定枠: Lookup / Generation / Lifecycle / Recomposition / TaskQuery
  - 測定定義: Lookup / Schedule / Query
  - その他: MeterMasterQuery / HistoricalMeasuredReadingLookup / ReadingPersistence(D3 を含む) / ReplacementEventUpsert / ReplacementReflection / ReplacementBoundaryLookup / Settings / Staff(新設)
- [ ] 実装規約を満たす
  - SQL の値はすべてプレースホルダで渡す
  - Decimal は有限の plain decimal 文字列で読み書きする
  - 時刻は D6 の形式にする
  - 現在時刻と業務日付は注入した時計から受け取り、`date.today()` と `datetime.now()` を持ち込まない
  - 低レベル DB 例外(`IntegrityError` 等)は Repository で `DomainError` 派生へ変換する
- [ ] `db/bootstrap_seeds.py` に設定の既定値を冪等に投入する処理を作る(D7)。投入する値は次のとおり。
  - 締め時刻 `07:00`
  - タイムゾーン(`app.timezone.v1`)
  - 周期ごとの既定スケジュール
  - 配信オフセット
- [ ] `tests/conftest.py` に、架空のサンプルマスタ入りフィクスチャと時刻固定フィクスチャを追加する
- [ ] Repository の統合テストを書く(ロジックは `:memory:`、WAL・永続化は `tmp_path`)
  - CHECK / UNIQUE / FOREIGN KEY 違反が `DomainError` 派生へ変換される
  - 古い入力の無視と、同時刻での `arrival_sequence` による採用順(D3)
  - 後続実測値が `scheduled_at, planned_reading_id` 順に取得できる
  - 期間検索が、ローカル暦の境界を UTC に変換した半開区間 `[start, end)` で行われ、SQL 内で TZ 補正しない
  - Staff の登録・無効化・一意制約

### 5.3 PR-1c `feature/phase1-service-integration`: Service 統合

- [ ] `MeterReadingSyncService` から `Session` を外し、`conn` と `transaction`、`savepoint` で作り直す(D2)
  - 業務拒否は SAVEPOINT 単位で戻し、拒否として集計する
  - 予期しない例外は全体を ROLLBACK して再送出する
  - 旧実装の `except Exception` による拒否扱いを廃止する
- [ ] 署名は PC の確定時に生成する(D4)。`device_id = sheet_id`、`updated_by = 確定担当者名`、`result_source = 'mobile_html'` を受け取れるようにする
- [ ] `ReadingWriteOrchestrationService` を、PC 直接入力とスマホ取込で共通の経路にする。結果保存・監査署名・交換境界・予定枠更新・後続再計算を同一トランザクションで整合させる
- [ ] `WorkPackageDownloadService` に次を実装する
  - 予定枠の補完生成(backfill)
  - 締め時刻経過後の前日以前の `open` → `auto_closed`
  - `auto_closed` 枠への後日入力を `completed` にして再計算する処理
  - 臨時検針枠(`ad_hoc`)を含むグループ化
- [ ] メーター交換を原子的に実行する
  - 旧メーターの `open` / `auto_closed` 枠を `superseded` にする
  - 新メーター枠を生成する
  - 割当を切り替える
  - 交換境界と交換記録を保存する
- [ ] `MeterMasterService` を Repository 経由に作り直す。使用中マスタは論理削除で保護する
- [ ] `StaffService` を新設する(D9)
- [ ] `app.py` の起動順に「予定枠補完・自動クローズ」を、DB オープン・マイグレーションの後、MainWindow 表示の前として組み込み、`test_app.py` を更新する
- [ ] 次のテストを移植し、ファイル DB(`tmp_path`)で実行する: `test_meter_master_service`、`test_meter_reading_sync_service`、`test_meter_reading_sync_service_batch_txn`、`test_reading_write_orchestration`、`test_work_package_download_service`
- [ ] ロールバック検証を追加する
  - 結果保存後に例外が起きた場合
  - 交換境界の更新中に例外が起きた場合
  - 後続再計算の途中で例外が起きた場合
  - COMMIT が失敗した場合
  - SAVEPOINT で一部だけ戻した後、ほかの結果が確定している場合
- [ ] 業務ルールの検証を追加する
  - Decimal の精度
  - ロールオーバーの有無(未設定の負の差分は拒否)
  - 新メーター初期値の有無(未指定のときだけ初回使用量 0)
  - 交換の前後
  - 過去の追加・訂正による連鎖再計算
  - `auto_closed` からの復活
  - `purpose` ごとの集計に必要なデータ保持
  - 締め時刻の境界とサマータイムのあるタイムゾーン
- [ ] 残りの `core/` 対象ファイルを検査対象へ戻し、pyright strict で 0 件になる

### 5.4 PR-1d `feature/phase1-gate-cleanup`: 検査対象外の解除と参考資産の削除

- [ ] `pyproject.toml` の ruff `extend-exclude`、pyright `ignore`、pytest `--ignore`、coverage `include` / `omit` に残る Phase 1 対象の設定をすべて消す。個別 skip の追加や閾値の引き下げは行わない
- [ ] 依存ルールのテストを追加する
  - `core/` と `db/` が PySide6 を import しない
  - 契約(`contract_models`、`errors`、Repository Protocol・入出力レコード)から Service・UI・DB 実装への依存がない
  - `db/` から Service・UI への依存がない
  - `timeutil` と `timezone_settings` 以外で `datetime.now`、`date.today`、SQL の `'localtime'` を使っていない
- [ ] 統合テストの成功を確認してから `db/reference_sqlalchemy/` を削除し、`sqlalchemy` と旧 import の参照が残っていないことを `grep` で確認する
- [ ] [移行ガイド](../../migration_package/README.md) の Step 2 / 3、README、正本 8 章の Phase 1 記述を実態に合わせる

### 5.5 完了記録 PR

- [ ] Windows / Ubuntu の必須チェック(`test (windows-latest)`・`test (ubuntu-latest)`)が成功していることを確認し、結果を 7 章へ記録する
- [ ] 確定した設計(D2〜D6、D8)を正本の 4・5 章へ統合する
- [ ] 本計画書を完了(凍結)として `docs/plan/implemented/` へ `git mv` し、移動後の相対リンクを修正する

## 6. 品質ゲート

PR ごとに `migration_package/` で実行する。Linux のヘッドレスでは `QT_QPA_PLATFORM=offscreen` を設定する。

```text
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest --cov
uv run coverage report --include="src/meter_reading_mini/core/*,src/meter_reading_mini/db/*" --fail-under=90
```

- PR-1a〜1c は、対象に戻したファイルを段階的に検査対象へ加え、各 PR 時点で全ゲートを成功させる。
- PR-1d の時点で、Phase 1 対象の除外設定が 0 件であることを完了条件とする。
- 失敗や未実施の検査を成功として報告せず、実行した検査と未実施・失敗の理由を区別して記録する。
- 作業は `main` へ直接 push せず、feature ブランチの PR で CI 成功後にマージする。コミットは Conventional Commits、内容は日本語とする。

## 7. 完了条件

- [ ] 既存の単体テスト一式(差分計算・オーケストレーション・同期・予定生成・スケジュール・マスタ)と、標準 `sqlite3` Repository の統合テストが全件成功する
- [ ] ruff / pyright(`core/`・`db/` strict、`ui/` standard)/ pytest が、除外設定なしで Windows と Ubuntu の CI で成功する
- [ ] `core/`・`db/` の行カバレッジが 90% 以上である
- [ ] `db/reference_sqlalchemy/` を削除し、旧 import と `sqlalchemy` の参照が 0 件である
- [ ] 文書(README・移行ガイド・正本)が実態と一致している

## 8. 留意事項

- **時刻が秒単位になる(D6 と D3):** 同じ秒に複数回更新があると `client_updated_at` だけでは順序を決められない。`arrival_sequence` で後着を採用する前提をテストで明示する。
  - 単一ユーザー運用を前提とし、同一秒の競合は `arrival_sequence` で解決する。マイクロ秒化はしない(D6 の形式は既存契約として維持する。スマホ側の時計ずれは精度を上げても解消しない)。
  - 更新の新旧を `client_updated_at` の文字列比較だけで判定しない。「同時刻なら `arrival_sequence`」の分岐を必ず通す実装にし、同一秒の複数更新を投入するテストを置く。- **`ad_hoc` の区別:** 5.2 の点検で、v1 への列追加が必要になる可能性が高い。追加する場合は `schema.sql`・`migrations.py`・版別検査定義・テストを同時に更新する。
- **旧テストの削減:** `startup_diagnostics` 依存分は移植しない。削除したテストと理由を完了記録に残す。
- **Phase 1 の SQL 移植:** `db/reference_sqlalchemy/` は ORM のセマンティクス(暗黙の flush、`onupdate`、型変換)に依存する。クエリ移植時は、ORM 側の暗黙の挙動を明示的な SQL と Python 側の処理へ置き換える。
- **実データの保護:** テストは `:memory:` または `tmp_path` を使う。動作確認は `METER_READING_MINI_DATA_DIR` に一時保存先を指定し、実データ領域を使わない。

## 9. 完了記録(Phase 1 完了時に追記)

- 全体の現状計測(5.1 の初回計測):
- 削除・置換したテストと理由:
- スキーマ点検の結果(`ad_hoc`・CHECK 制約の変更有無):
- 各 PR と CI の結果:
