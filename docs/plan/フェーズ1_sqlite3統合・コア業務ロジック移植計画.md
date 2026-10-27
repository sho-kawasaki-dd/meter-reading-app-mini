# Phase 1 標準 sqlite3 統合・コア業務ロジック移植計画

- 作成日: 2026-10-25
- 更新日: 2026-10-27(着手前レビュー・予定枠補完設計を反映)
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
- 旧契約の `WorkPackageTaskGroups` には `ad_hoc_tasks` があるが、臨時検針枠(`ad_hoc`)は運用上不要なため廃止する。
- [usage_delta_calculation.py](../../migration_package/src/meter_reading_mini/core/usage_delta_calculation.py) にロールオーバー未設定時の負の差分拒否がなく、参考実装の後続再計算は直後 1 件のみ(`.limit(1)`)、交換境界・交換後再計算は倍率(`multiplier`)とロールオーバー補正が漏れている。

## 3. 決定事項(2026-10-25 確認済み、2026-10-27 追補)

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
| D10 | 臨時検針枠(`ad_hoc`) | 運用上不要なため廃止する。`WorkPackageTaskGroups.ad_hoc_tasks` を削除し、スキーマへの列追加は行わない |
| D11 | マスタの論理削除と保護 | マスタの物理削除は行わない。有効な測定定義に割当中または `open` 予定枠を持つ物理メーターの無効化は `MasterInUseError` で拒否し、無効化済みマスタ(`staff` 含む)の新規利用は `InactiveMasterError` で拒否する |
| D12 | 契約の独立性 | スケジュールのデータ型・Repository Protocol・入出力レコードを Service から分離する。`TYPE_CHECKING` 内を含め、契約から Service・UI・DB 実装を参照しない |
| D13 | トランザクションの所有者 | 公開業務操作の Service が境界を持つ。同期・直接入力・起動処理から呼ぶ共通処理と Repository はトランザクションを開始しない。設定保存と予定枠再構成も一体で確定する |
| D14 | 予定枠の補完 | 測定定義ごとの生成開始業務日から対象業務日まで、適用履歴に基づく期待枠と既存枠を照合し、不足分だけ追加する。最新生成日を完了の根拠にしない。14 日は内部の分割単位とし、起動・出力の成功前に対象日まで完了させる |
| D15 | マスタ更新の範囲 | 物理メーター・測定定義・初期割当の登録更新と論理削除・再有効化を Phase 1 の Service / Repository に含める。画面は Phase 3 とし、割当の交換は交換専用の業務操作を使う |
| D16 | 遡及再計算 | 実測値の追加・訂正に加え、実測と非実測の相互変更を対象にする。影響する交換境界も更新し、新メーターの初回基準は交換初期値を使う。再計算の業務条件違反は元の書込を含めて戻す |
| D17 | 署名前の入力契約 | スマホからの未署名入力と PC が補完する保存要求を分離する。確定担当者・シート ID・入力検証を経て、DB 保存と同じ時刻精度で署名を生成する。仮の署名で旧契約を通さない |

### 3.1 契約と更新操作の境界(D12、D15、D17)

- 既存の `contract_models.py` と `meter_reading_sync_repositories.py` を契約の配置先として再利用する。`schedule_settings.py` にあるスケジュールの enum・dataclass・型エイリアスと、設定関連の Protocol を契約側へ移し、Service と DB はそれを参照する。契約を重複定義しない。
- マスタの参照用 Protocol とは別に登録・更新・論理削除・再有効化・使用状況照会の契約を用意する。測定定義登録時は生成開始日・初期スケジュール履歴・初期割当を一緒に保存する。過去の割当を単純更新して交換処理を迂回しない。
- D11 の検証は、メーターの登録更新、測定定義への割当、交換先メーター指定、結果の担当者指定等の公開操作へ対応付ける。無効化済みマスタの既存履歴は参照可能とし、再有効化は明示的な操作にする。
- スマホ入力に PC 生成の `signature` や確定担当者名を要求しない。シート ID と PC で選択した有効な担当者を確定コンテキストとして渡し、保存要求の `device_id`・`updated_by`・`result_source`・署名を組み立てる。入力側の値を PC の監査情報として無条件に採用しない。
- 署名対象の日時は D6 と同じ UTC・秒単位へ正規化する。保存値から署名を再現できることを検証する。HTML / MR1 の生成・解析そのものは Phase 2 のままとする。

### 3.2 トランザクションと再計算の責務(D2、D13、D16)

- 一括確定、PC 直接入力、設定変更、マスタ更新、メーター交換、起動時準備の各公開業務操作が `transaction(conn)` を所有する。起動時準備は「既定設定投入 → 補完 → 自動クローズ」を統括する Service とし、`app.py` はそれを呼ぶ。
- 共通の書込・補完・再構成処理は呼出元のトランザクション内で動く。ネストした `transaction()` や、`conn.in_transaction` に応じて偶然境界が変わる実装にしない。Repository で計算 Service を呼ばず、Service が計算し Repository が取得・保存する。
- 設定保存は適用履歴の更新と影響する予定枠の再構成を同一トランザクションにする。一括確定では結果 1 件に伴う署名・境界・後続再計算までを同じ SAVEPOINT に含める。
- `measured` から `skipped` 等への変更でも後続を再計算する。例として `100 → 120 → 150` の中央を非実測に変えると、倍率 1 の後続使用量は `30` から `50` へ変わる。
- 交換前の遡及変更では `previous_measured_reading`・その参照先・`old_meter_usage_delta` も再評価する。交換をまたいで旧メーターの読値を新メーターの前回値にしない。新初回の訂正・非実測化では、次に初回となる実測へ交換初期値を適用する。
- 再計算で負の差分等が発生した場合、直接入力は業務操作全体を、一括確定は当該入力と全副作用を SAVEPOINT 単位で戻す。予期しない例外・COMMIT 失敗は全体を戻す。

### 3.3 予定枠補完の設計(D14)

#### 補完範囲と適用履歴

- 測定定義に予定生成開始業務日を保持する。新規登録時の既定は業務タイムゾーンでの当日とし、過去から運用する場合は明示指定する。開始日前は自動生成しない。メーター交換で開始日をリセットしない。
- 対象の測定定義ごとに、開始日から呼出側が指定した対象業務日までを包含して照合する。DB 全体または定義別の `MAX(business_date)` を補完開始位置・完了判定に使わない。
- スケジュールには適用開始業務日を持つ履歴を追加する。適用期間は次の版の開始日を終端とする半開区間で管理し、当該日に有効な周期・時刻・日付指定・`day_offset` を解決する。生成日時と配布日を再現できるよう、業務タイムゾーン・配信オフセットも適用履歴から解決できる形にする。
- 設定変更は適用開始日を明示し、変更前の期間へ最新設定を遡及適用しない。測定定義別の上書きと周期別既定値の優先関係を維持し、既定値を使う定義も過去に適用された設定を再現可能にする。既存の最新値用設定を残す場合も、履歴と同一トランザクションで整合させる。
- 物理メーターは生成する `scheduled_at` に有効な割当期間 `[effective_from, effective_to)` で解決する。履歴の不足・重複等で対象を特定できない場合、現在の設定・割当で推測せず、対象定義と期間を示す `DomainError` 派生例外で通知する。

#### 不足判定と既存データの保護

- Service が期待枠を算出し、Repository から取得した既存枠・交換差替え情報と照合して、本当に存在しない枠だけを追加する。元の予定枠 ID の有無だけで不足を判定しない。
- 論理的な枠(測定定義・業務日・検針時間)と、交換・再構成で差し替えられた実体の対応を契約で表す。既存の決定的な ID 生成を再利用しつつ、交換後の代替枠を重複生成しない。
- 通常の補完では既存枠・結果を更新しない。`completed`・`auto_closed` の状態を維持し、`superseded` を復活させない。設定変更に伴う意図的な枠変更は再構成処理の責務とする。
- 差替え済みの枠は後継枠または適用履歴上の廃止理由と照合する。必要な後継が失われる等の不整合は、旧枠の復活や無条件の追加で隠さず業務エラーにする。

#### 処理分割・完了・締め

- 14 日は内部の走査・照合単位に限定する。生成件数ではなく日付で進め、週次・月次・隔月で生成が 0 件の期間でも進行する。全期間の要求を一度にメモリへ保持しない。
- 起動時とシート出力前の準備は、1 回の公開業務操作で対象日まで補完を完了する。途中で打ち切った状態を成功として返さない。`backfill_pending` の旧仕様・テストはこの契約へ置換する。
- 「補完 → 自動クローズ」を同一トランザクションで行う。締め判定には出力対象日ではなく注入した時計の現在業務日時を使い、補完した過去枠も通常の締め対象にする。
- シート用データの取得は補完・締め処理後に行う。Phase 2 以降の HTML 出力は DB 処理の成功後とし、補完失敗時に不完全なシートを出力しない。
- まずは期間全体の差分照合で実装する。走査済み日付等のキャッシュは初期実装に追加せず、必要性を実測してから検討する。

### 3.4 スキーマ変更と開発用 DB(D8)

- 生成開始業務日と適用履歴を追加するため、v1 の DDL・版別検査定義・スキーマのテストを一緒に更新する。履歴の一意性・参照整合性は DB 制約、期間の適用可否は Service 検証でも保護する。
- 変更前の v1 DB は同じ `user_version` でもスキーマ検査で拒否される。未配布の開発用 DB を自動削除・黙示変換せず、新しい一時保存先で作成する手順を記録する。保全が必要な DB は別途保管し、実データ領域を使わない。
- 本章は未実装の設計であり、正本への統合は 5.5 の実装完了確認後に行う。

## 4. スコープ

### 対象

- `core/` 10 ファイルの import 修正・整形・strict 型付け
- `db/repositories.py`(Protocol の標準 sqlite3 実装)と `db/bootstrap_seeds.py`
- `connection.py` への SAVEPOINT 補助の追加
- `staff` の Repository と Service
- 物理メーター・測定定義・初期割当の更新契約と Service
- スケジュール契約の分離、生成開始業務日と適用履歴のスキーマ・Repository・Service
- 設定変更と予定枠再構成の原子的な実行、未署名入力と PC 保存要求の分離
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

- [ ] `migration_package/` で `uv sync --locked` を実行し、全体を対象に ruff / pyright / pytest 収集 / coverage の件数を記録する(9 章の記録欄へ)
- [ ] 対象 10 ファイルとテストを `ruff format` と `ruff check --fix` で機械整形し、機械的な変更だけを別コミットにする
- [ ] import を置き換える
  - `api.models` → `meter_reading_mini.core.contract_models`
  - `application.` → `meter_reading_mini.core.`
- [ ] `meter_reading_sync_repositories.py` から `core.database`(`MeasurementDefinitionORM`)への型依存を外し、純粋な契約型へ置き換える
- [ ] 3.1 に従いスケジュール型・設定関連 Protocol を契約側へ移し、`TYPE_CHECKING` 内も含めて Service への依存を除去する。契約の依存方向テストをこの PR で追加する
- [ ] 物理メーター・測定定義・割当・Staff の更新要求と結果レコード、使用状況照会、生成開始日・適用履歴・論理枠と差替え実体の照合に必要な Protocol を定義する
- [ ] スマホの未署名入力と PC 確定コンテキスト・保存要求を分け、署名や担当者のダミー値なしで検証できる契約テストを追加する(D17)
- [ ] 契約と設定の語彙・不要機能を整理する
  - `ResultSource` に `MOBILE_HTML = "mobile_html"` を追加する
  - `WorkPackageTaskGroups` から廃止対象の `ad_hoc_tasks` を削除する(D10)
  - `schedule_settings.py` から対象外の `ActiveDeviceBindingService` 関連を削除する
- [ ] `UsageDeltaCalculationService.calculate` で、`rollover_value is None` かつ `raw_reading < last_reading` のときに `DomainError` 派生例外を送出するよう修正する
- [ ] `build_scheduled_at()` と `normalize_to_utc()` を `core/timezone_settings.py` へ移す(D5)
- [ ] `build_current_datetime_provider` / `build_business_date_provider` は `timeutil.utc_now()` で UTC 現在時刻を取得し、設定された業務タイムゾーンへ明示的に変換する。`utc_now()` 自体に tz 引数は追加しない
- [ ] 次のモジュールが pyright strict で 0 件になる: `contract_models`、`meter_reading_sync_repositories`、`usage_delta_calculation`、`reading_signature`、`timezone_settings`、`schedule_settings`
- [ ] 業務例外を `core/errors.py` の `DomainError` 派生へそろえる。同期処理の拒否コードとの対応表を作る
- [ ] 次のテストを移植する: `test_api_models`、`test_usage_delta_calculation`、`test_reading_signature`、`test_timezone_settings`、`test_measurement_definition_schedule_service`
- [ ] `test_timezone_settings.py` の `core.startup_diagnostics` 依存分、および `test_measurement_definition_schedule_service.py` の `ActiveDeviceBindingService` 依存分を削除または置換し、理由と件数を 9 章へ記録する
- [ ] 上記ファイルを `pyproject.toml` の ruff / pyright / pytest / coverage の対象外から外し、ローカルのゲートが通る

### 5.2 PR-1b `feature/phase1-sqlite-repositories`: 標準 sqlite3 Repository

- [ ] `connection.py` に `savepoint(conn, name)` を追加する
  - トランザクション外で呼ぶと `RuntimeError` になる
  - 例外時は `ROLLBACK TO` と `RELEASE` を行い、例外を再送出する
  - 正常時は `RELEASE` する
- [ ] 3.3・3.4 に従い、生成開始業務日・適用履歴と必要な差替え対応を v1 へ追加する(D8、D14)。`db/schema.sql`、`db/migrations.py` の版別検査定義・スキーマテストを同時に直す。追加の点検項目は次のとおり。
  - [ ] `reading_transactions.result_source` の CHECK 制約の要否
  - [ ] 時刻列・Decimal 列の形式を保つ CHECK の要否
- [ ] `db/repositories.py` に次の Repository を実装する(`core/meter_reading_sync_repositories.py` の Protocol に合わせる)
  - 予定枠: Lookup / Generation / Lifecycle / Recomposition / TaskQuery。期間内の既存枠・差替え対応の取得と不足枠保存を含む
  - 測定定義: Lookup / Schedule / Query に加え、登録更新・論理削除・再有効化、生成開始日とスケジュール適用履歴の保存取得
  - 物理メーター・割当: MeterMasterQuery に加え、登録更新・論理削除・再有効化・使用状況照会、初期割当と有効期間に基づく履歴照会
  - その他: HistoricalMeasuredReadingLookup / ReadingPersistence(D3 を含む) / ReplacementEventUpsert / ReplacementReflection / ReplacementBoundaryLookup / Settings(`meter_app_settings` 対象、適用履歴を含む) / Staff(新設)
- [ ] 実装規約を満たす
  - SQL の値はすべてプレースホルダで渡す
  - Decimal は有限の plain decimal 文字列で読み書きする
  - 時刻は D6 の形式にする。`client_updated_at` の新旧比較も D6 の秒単位 UTC に正規化した値同士で行う
  - 現在時刻と業務日付は注入した時計から受け取り、`date.today()` と `datetime.now()` を持ち込まない
  - 低レベル DB 例外(`IntegrityError` 等)は Repository で `DomainError` 派生へ変換する
- [ ] `db/bootstrap_seeds.py` に `meter_app_settings` へ設定の既定値を冪等に投入する処理を作る(D7)。投入する値は次のとおり。
  - 締め時刻 `07:00`
  - タイムゾーン(`app.timezone.v1`)
  - 周期ごとの既定スケジュール
  - 配信オフセット
- [ ] `tests/conftest.py` に、架空のサンプルマスタ入りフィクスチャと時刻固定フィクスチャを追加する
- [ ] Repository の統合テストを書く(ロジックは `:memory:`、WAL・永続化は `tmp_path`)
  - CHECK / UNIQUE / FOREIGN KEY 違反が `DomainError` 派生へ変換される
  - 古い入力の無視と、同時刻での `arrival_sequence` による採用順(D3)
  - 後続実測値が `scheduled_at, planned_reading_id` 順に取得でき、Service による連鎖再計算の結果を保存できる。交換境界の取得更新も検証する
  - 生成開始日・適用開始日・割当期間に基づく取得、途中欠落と交換後の代替枠の照合
  - 期間検索が、ローカル暦の境界を UTC に変換した半開区間 `[start, end)` で行われ、SQL 内で TZ 補正しない
  - Staff の登録・無効化・一意制約

### 5.3 PR-1c `feature/phase1-service-integration`: Service 統合

- [ ] `MeterReadingSyncService` から `Session` を外し、`conn` と `transaction`、`savepoint` で作り直す(D2)
  - 業務拒否は SAVEPOINT 単位で戻し、拒否として集計する
  - 予期しない例外は全体を ROLLBACK して再送出する
  - 旧実装の `except Exception` による拒否扱いを廃止する
- [ ] 署名は PC の確定時に生成する(D4)。スマホ側署名の検証を外し、`device_id = sheet_id`、`updated_by = 確定担当者名`、`result_source = 'mobile_html'` を受け取って確定時に署名を生成・記録できるようにする
  - 未署名入力を検証し、有効な担当者とシート ID から保存要求を構築する。時刻を D6 へ正規化してから署名を生成し、保存値から再現できることを検証する
- [ ] `ReadingWriteOrchestrationService` を、PC 直接入力とスマホ取込で共通の経路にする。結果保存・監査署名・交換境界・予定枠更新・後続すべての実測値の連鎖再計算(`UsageDeltaCalculationService` に一元化)を同一トランザクションで整合させる
  - 実測と非実測の相互変更、交換前の訂正による境界更新、新初回の訂正・非実測化を 3.2 に従って処理する。負の差分等の業務拒否では元の書込と副作用を一緒に戻す
- [ ] 公開業務操作と共通処理を分け、3.2 のトランザクション所有者を実装する。`MeasurementDefinitionScheduleService` 等の設定保存は適用履歴更新・予定枠再構成まで原子的に実行する
- [ ] `WorkPackageDownloadService` に次を実装する
  - 3.3 に従う予定枠の補完生成(backfill)。最新生成日方式を廃止し、内部で 14 日ずつ照合して対象日まで完了する
  - 締め時刻経過後の前日以前の `open` → `auto_closed`
  - `auto_closed` 枠への後日入力を `completed` にして再計算する処理
  - 周期区分(`daily` / `weekly` / `monthly` / `every_two_months`)によるグループ化(D10: `ad_hoc` は廃止)
- [ ] メーター交換を原子的に実行する
  - 旧メーターの `open` / `auto_closed` 枠を `superseded` にする
  - 新メーター枠を生成する
  - 割当を切り替える
  - 交換境界(`old_meter_usage_delta` への倍率・ロールオーバー適用を含む)と交換記録を保存し、遅延交換時の完了済み新メーター実測値も倍率・ロールオーバー込みで再計算する
- [ ] `MeterMasterService` を Repository 経由に作り直す。物理削除は行わず、有効な測定定義に割当中または `open` 予定枠を持つ物理メーターの無効化は `MasterInUseError` で拒否し、無効化済みマスタの新規利用は `InactiveMasterError` で拒否する(D11)
- [ ] 測定定義・初期割当の登録更新と論理削除・再有効化を Service 経由で提供する(D15)。登録時の生成開始日・初期履歴・割当保存を原子的に行い、無効化済みマスタの新規利用を各操作で拒否する
- [ ] `StaffService` を新設する(D9、D11: 無効化済み担当者の指定は `InactiveMasterError` で拒否)
- [ ] 「`bootstrap_seeds` による既定設定投入 → 予定枠補完・自動クローズ」を統括する起動時準備 Service を作り、`app.py` から DB オープン・マイグレーション後、MainWindow 表示前に呼ぶ。準備全体を単一トランザクションとし、`test_app.py` を更新する
- [ ] 次のテストを移植し、ファイル DB(`tmp_path`)で実行する: `test_meter_master_service`、`test_meter_reading_sync_service`、`test_meter_reading_sync_service_batch_txn`、`test_reading_write_orchestration`、`test_work_package_download_service`
- [ ] ロールバック検証を追加する
  - 結果保存後に例外が起きた場合
  - 交換境界の更新中に例外が起きた場合
  - 後続再計算の途中で例外が起きた場合
  - 設定保存後・適用履歴更新後・予定枠再構成中に例外が起きた場合
  - 複数の補完区間を処理した後、途中で例外が起きた場合
  - COMMIT が失敗した場合
  - SAVEPOINT で一部だけ戻した後、ほかの結果が確定している場合
- [ ] 業務ルールの検証を追加する
  - Decimal の精度
  - ロールオーバーの有無(未設定の負の差分は拒否)
  - 新メーター初期値の有無(未指定のときだけ初回使用量 0)
  - 交換の前後
  - 過去の追加・訂正による連鎖再計算
  - `100 → 120 → 150` の中央を非実測に訂正すると、倍率 1 の後続使用量が `50` になる
  - 交換前実測の訂正で交換境界も更新され、新メーターの前回値に旧メーター値が混入しない
  - 新初回実測の訂正・非実測化で交換初期値の適用先が正しく決まる
  - `auto_closed` からの復活
  - `purpose` ごとの集計に必要なデータ保持
  - 締め時刻の境界とサマータイムのあるタイムゾーン
- [ ] 予定枠補完の受入テストを追加する。旧 14 日打切り・`backfill_pending` 前提のテストは置換理由を記録する
  - 他定義の当日枠がある状態で新定義を追加しても、開始日から枠が生成される
  - 中間の日付・同日の一部時間帯の欠落だけを補完し、開始日前は生成しない
  - 14 日を超える長期未起動でも 1 回の業務操作で対象日まで完了する
  - 月次・隔月のみで生成 0 件の区間があっても処理が停滞しない
  - 再実行しても枠数・既存状態・結果が変わらない
  - 設定変更前の期間へ新しい時間帯を追加せず、適用履歴・割当の不足や重複を明示的に拒否する
  - 交換前後の割当を使い分け、旧枠の復活・代替枠の重複がない
  - 補完した過去枠の締めは現在業務日時で判定し、出力対象日に左右されない
- [ ] マスタ登録更新・再有効化、未署名入力から PC 署名保存までの Service 統合テストを追加する
- [ ] 残りの `core/` 対象ファイルを検査対象へ戻し、pyright strict で 0 件になる

### 5.4 PR-1d `feature/phase1-gate-cleanup`: 検査対象外の解除と参考資産の削除

- [ ] `pyproject.toml` の ruff `extend-exclude`、pyright `ignore`、pytest `--ignore`、coverage `include` / `omit` に残る Phase 1 対象の設定をすべて消す。個別 skip の追加や閾値の引き下げは行わない
- [ ] PR-1a から適用した契約の依存検査を維持し、全対象へ依存ルールのテストを拡張する
  - `core/` と `db/` が PySide6 を import しない
  - 契約(`contract_models`、`errors`、Repository Protocol・入出力レコード)から Service・UI・DB 実装への依存がない
  - `db/` から Service・UI への依存がない
  - `timeutil` と `timezone_settings` 以外で `datetime.now`、`date.today`、SQL の `'localtime'` を使っていない
- [ ] 統合テストの成功を確認してから `db/reference_sqlalchemy/` を削除し、`sqlalchemy` と旧 import の参照が残っていないことを `grep` で確認する
- [ ] [移行ガイド](../../migration_package/README.md) の Step 2 / 3、README、正本 8 章の Phase 1 記述を実態に合わせる

### 5.5 完了記録 PR

- [ ] Windows / Ubuntu の必須チェック(`test (windows-latest)`・`test (ubuntu-latest)`)が成功していることを確認し、結果を 9 章へ記録する
- [ ] 確定した設計(D2〜D6、D8、D10〜D17、3.1〜3.4)を正本の 3・4・5・6 章へ統合する。生成開始日・適用履歴の DDL と業務ルール、トランザクション所有者、署名前入力の境界を含める
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
- [ ] 契約の独立性、設定変更の原子性、マスタ更新契約、未署名入力からの PC 署名保存が検証されている
- [ ] 生成開始日と適用履歴による不足補完が対象日まで完了し、欠落・長期未起動・設定変更・交換・再実行の受入テストが成功する
- [ ] 実測と非実測の相互変更・交換境界を含む遡及再計算と、拒否時の全副作用のロールバックが検証されている
- [ ] `db/reference_sqlalchemy/` を削除し、旧 import と `sqlalchemy` の参照が 0 件である
- [ ] 文書(README・移行ガイド・正本)が実態と一致している

## 8. 留意事項

- **時刻が秒単位になる(D6 と D3):** 同じ秒に複数回更新があると `client_updated_at` だけでは順序を決められない。`arrival_sequence` で後着を採用する前提をテストで明示する。
  - 単一ユーザー運用を前提とし、同一秒の競合は `arrival_sequence` で解決する。マイクロ秒化はしない(D6 の形式は既存契約として維持する。スマホ側の時計ずれは精度を上げても解消しない)。
  - 更新の新旧を `client_updated_at` の文字列比較やミリ秒付きの生の `datetime` 比較だけで判定しない。D6 の秒単位 UTC に正規化した上で比較し、「同時刻なら `arrival_sequence`」の分岐を必ず通す実装にして、同一秒の複数更新を投入するテストを置く。
- **旧テストの削減:** `startup_diagnostics` 依存分および廃止対象の `ActiveDeviceBindingService` / `ad_hoc_tasks` 依存分は移植しない。削除・置換したテストと理由を 9 章の完了記録に残す。
- **Phase 1 の SQL 移植:** `db/reference_sqlalchemy/` は ORM のセマンティクス(暗黙の flush、`onupdate`、型変換)に依存する。クエリ移植時は、ORM 側の暗黙の挙動を明示的な SQL と Python 側の処理へ置き換える。
- **実データの保護:** テストは `:memory:` または `tmp_path` を使う。動作確認は `METER_READING_MINI_DATA_DIR` に一時保存先を指定し、実データ領域を使わない。
- **変更前の開発用 v1 DB:** D8 による再定義後は既存 DB の読込成功を前提にしない。新規の一時保存先を指定して検証し、既存 DB は必要に応じて保全する。自動削除・検査の緩和でスキーマ不一致を回避しない。
- **レビュー指摘の対応:** 契約分離は D12・5.1、設定変更と共通処理の境界は D13・3.2、補完方式は D14・3.3、マスタ更新は D15、遡及再計算は D16、署名前入力は D17 に対応する。これらの追加対象も段階的な品質ゲートへ含める。

## 9. 完了記録(Phase 1 完了時に追記)

- 全体の現状計測(5.1 の初回計測):
- 削除・置換したテストと理由:
- スキーマ点検の結果(CHECK 制約の変更有無):
- 生成開始日・適用履歴・差替え対応のスキーマと、変更前 v1 開発用 DB の取扱い:
- 予定枠補完の受入テスト結果と、長期未起動時の処理時間・対象規模:
- 契約分離・トランザクション境界・再計算・署名前入力の検証結果:
- 各 PR と CI の結果:
