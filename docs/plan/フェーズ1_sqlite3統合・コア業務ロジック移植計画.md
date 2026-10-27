# Phase 1 標準 sqlite3 統合・コア業務ロジック移植計画

- 作成日: 2026-10-25
- 更新日: 2026-10-27(PR-1e 検査対象外解除・参考資産削除)
- ステータス: 進行中(PR-1a〜1e の実装・ローカル品質ゲート完了、Windows / Ubuntu CI と完了記録未実施)
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
- 監査情報の語彙と署名の時刻表記が未確定である。
  - [ResultSource](../../migration_package/src/meter_reading_mini/core/meter_reading_sync_repositories.py) は `CLIENT` / `SYSTEM` / `MANUAL` で、`mobile_html` がない。
  - `reading_transactions.result_source` は NULL 許容で CHECK 制約がない。
  - [calculate_meter_reading_signature](../../migration_package/src/meter_reading_mini/core/reading_signature.py) は `device_id` が空だと例外になる。
  - 署名の `recorded_at` は `isoformat()` のままで、オフセット表記・マイクロ秒の有無で署名が変わる。
- `WorkPackageResponse.backfill_pending`(旧 14 日打切りの名残)が契約に残っている。
- [usage_delta_calculation.py](../../migration_package/src/meter_reading_mini/core/usage_delta_calculation.py) にロールオーバー未設定時の負の差分拒否がなく、参考実装の後続再計算は直後 1 件のみ(`.limit(1)`)、交換境界・交換後再計算は倍率(`multiplier`)とロールオーバー補正が漏れている。

## 3. 決定事項(2026-10-25 確認済み、2026-10-27 追補)

| # | 論点 | 決定 |
| --- | --- | --- |
| D1 | PR の分け方 | 5 段階の PR(1a〜1e)＋完了記録 PR。1a 契約・純粋ロジック、1b スキーマ・Repository、1c 予定枠(適用履歴・補完・自動クローズ・起動時準備)、1d 検針結果・マスタ(同期・再計算・交換・マスタ・担当者)、1e 検査対象外の解除。交換は新メーター枠の生成で 1c に依存するため 1d に置く |
| D2 | 一括確定のトランザクション | 全体を 1 トランザクションにし、その中で検針結果 1 件ごとに SAVEPOINT を置く。業務条件で拒否(`DomainError` 派生)した結果だけを戻し、残りは確定する。予期しない例外は握りつぶさず、全体を ROLLBACK して再送出する |
| D3 | 古い更新の無視 | スマホ取込で `client_updated_at` が古い入力は無視する。同時刻のときは `arrival_sequence` を進め、後から届いた入力を採用する。PC 直接入力・訂正への適用は D19 に従う |
| D4 | 監査署名 | PC の確定時に `reading_signature.py` で生成する。スマホ側署名の検証は行わない |
| D5 | 業務タイムゾーン | `meter_app_settings` の `app.timezone.v1`(既定 Asia/Tokyo)に統一する。`timeutil` には tz を必ず明示して渡す |
| D6 | DB の時刻文字列 | `timeutil` の `YYYY-MM-DD HH:MM:SS`(UTC、秒単位)に統一する |
| D7 | 初期データ | 設定の既定値のみ投入する。サンプルのマスタは架空のものをテスト用フィクスチャに限って用意し、実施設名のデータは移さない |
| D8 | スキーマ修正 | 未配布のため v1 を直接修正し、`SCHEMA_VERSION` は 1 のままにする |
| D9 | 担当者(`staff`) | Repository と Service を Phase 1 に含める |
| D10 | 臨時検針枠(`ad_hoc`) | 運用上不要なため廃止する。`WorkPackageTaskGroups.ad_hoc_tasks` を削除し、スキーマへの列追加は行わない |
| D11 | マスタの論理削除と保護 | マスタの物理削除は行わない。有効な測定定義に割当中または `open` 予定枠を持つ物理メーターの無効化は `MasterInUseError` で拒否し、無効化済みマスタ(`staff` 含む)の新規利用は `InactiveMasterError` で拒否する。`auto_closed` 枠の扱いと既存枠への後日入力は D23 で確定する |
| D12 | 契約の独立性 | スケジュールのデータ型・Repository Protocol・入出力レコードを Service から分離する。`TYPE_CHECKING` 内を含め、契約から Service・UI・DB 実装を参照しない |
| D13 | トランザクションの所有者 | 公開業務操作の Service が境界を持つ。同期・直接入力・起動処理から呼ぶ共通処理と Repository はトランザクションを開始しない。設定保存と予定枠再構成も一体で確定する |
| D14 | 予定枠の補完 | 測定定義ごとの生成開始業務日から対象業務日まで、適用履歴に基づく期待枠と既存枠を照合し、不足分だけ追加する。最新生成日を完了の根拠にしない。14 日は内部の分割単位とし、起動・出力の成功前に対象日まで完了させる |
| D15 | マスタ更新の範囲 | 物理メーター・測定定義・初期割当の登録更新と論理削除・再有効化を Phase 1 の Service / Repository に含める。測定定義の `reading_cycle`・`value_schema`・`calculation_method`・`location_id` は作成後の変更を不可とし、更新要求で変更された場合は `DomainError` 派生例外で拒否する(周期等を変更する場合は旧定義を無効化して新規定義を作成する)。画面は Phase 3 とし、割当の交換は交換専用の業務操作を使う |
| D16 | 遡及再計算 | 実測値の追加・訂正に加え、実測と非実測の相互変更を対象にする。影響する交換境界も更新し、新メーターの初回基準は交換初期値を使う(初期値未指定の交換境界も識別して旧メーターの実測値を新メーターの前回値にせず、新初回実測の訂正・再計算時に自分自身で境界消費済みと誤判定しない)。交換境界の `old_meter_usage_delta` でも倍率・ロールオーバー補正とロールオーバー未設定時の負の差分拒否(`DomainError` 派生)を適用する。再計算の業務条件違反は元の書込を含めて戻す |
| D17 | 署名前の入力契約 | スマホからの未署名入力と PC が補完する保存要求を分離する。確定担当者・シート ID・入力検証を経て、DB 保存と同じ時刻精度で署名を生成する。仮の署名で旧契約を通さない |
| D18 | PC 直接入力の監査情報 | `ResultSource` は `mobile_html` と `manual` の 2 値にし、`CLIENT`・`SYSTEM` は削除する。PC 直接入力・訂正は `result_source = 'manual'`、`device_id = 'pc-direct'`(固定値)、`updated_by = 入力担当者名` とし、PC 保存でも署名を生成する。`reading_transactions.result_source` は `NOT NULL CHECK (result_source IN ('mobile_html','manual'))` にする。後続の再計算で `result_source`・署名・`updated_by` を書き換えない |
| D19 | D3 の適用範囲 | 「古い入力を無視する」比較はスマホ取込だけに適用する。PC 直接入力・訂正は明示的な操作として常に採用し、保存する `client_updated_at` は「注入した時計の現在時刻」と「既存値 + 1 秒」の遅い方とする。スマホの時計が進んでいても、後から古い MR1 を再取込して PC の訂正を上書きしない |
| D20 | 同一内容の再取込 | 既存結果と `client_updated_at` および `recorded_at`(ともに D6 の秒単位 UTC 正規化後)が同じで、結果状態・測定値・正規化後の構造化入力・備考もすべて一致する入力は何も更新せず、`UpsertDisposition.IGNORED_DUPLICATE` / `SyncItemIgnoreCode.DUPLICATE_SUBMISSION` として「重複」に集計する(`arrival_sequence`・署名・再計算も変えない)。内容(`recorded_at` を含む)が異なる場合だけ D3 の同時刻規則で `arrival_sequence` を進める |
| D21 | 署名の時刻表記 | 署名に使う `recorded_at` は D6 と同じ `YYYY-MM-DD HH:MM:SS`(UTC、秒単位)とし、`isoformat()` 表記を廃止する。署名の元文字列は `planned_reading_id\|result_status\|payload\|recorded_at\|device_id` とし、保存値から再現できることをテストで検証する。署名は予定枠・結果状態・測定値・記録日時・`device_id` の改ざん検出に限り、`updated_by`・`note`・`usage_delta` は対象外、送信者認証でもない |
| D22 | 業務タイムゾーンの変更 | 運用開始後の変更は禁止する。`planned_readings` が 1 件でもあるとき、`app.timezone.v1` の変更は `DomainError` 派生例外で拒否する(初回投入と、同じ値の再保存は許可する)。タイムゾーンは適用履歴の対象外とする |
| D23 | 使用中メーターの判定と後日入力 | 物理メーターの無効化を拒否する条件は「有効な測定定義に割当中」または「`open` 予定枠を持つ」とし、`auto_closed` は含めない(入力漏れの枠が残る旧メーターを永久に無効化できなくなるため)。メーター交換は旧メーターを自動で無効化せず、旧メーターの無効化は交換後にマスタ操作で別に行う。無効化済みメーターの既存枠(`auto_closed` を含む)への後日入力・訂正は許可し、`completed` 化と再計算を通常どおり行う。`InactiveMasterError` の対象は、新規の割当・交換先指定・新規枠の生成に限る |
| D24 | 起動時準備の失敗 | 準備トランザクションを ROLLBACK してログへ記録し、`QApplication` が有効な状態で汎用エラー(注入可能な通知関数、既定は `show_unexpected_error`)で通知して `STARTUP_FAILURE` を返す。MainWindow は表示せず、DB に準備の途中結果を残さない |
| D25 | 測定定義の無効化 | 測定定義の無効化は、現在業務日以降の `open` 枠を同一トランザクションで `superseded` にする(廃止理由 `definition_deactivated` を `close_reason` に記録)。`completed`・`auto_closed` 枠と結果は変更しない。補完は有効な測定定義だけを対象にする。再有効化では枠を復活させず、生成開始業務日を `max(既存値, 現在業務日)` へ更新して、無効期間を遡って補完しない |

### 3.1 契約と更新操作の境界(D12、D15、D17)

- 既存の `contract_models.py` と `meter_reading_sync_repositories.py` を契約の配置先として再利用する。`schedule_settings.py` にあるスケジュールの enum・dataclass・型エイリアスと、設定関連の Protocol を契約側へ移し、Service と DB はそれを参照する。契約を重複定義しない。
- マスタの参照用 Protocol とは別に登録・更新・論理削除・再有効化・使用状況照会の契約を用意する。測定定義登録時は生成開始日・初期スケジュール履歴・初期割当を一緒に保存する。測定定義の更新では `reading_cycle`・`value_schema`・`calculation_method`・`location_id` を作成後変更不可とし、過去の割当も単純更新して交換処理を迂回しない。
- `HistoricalMeasuredReadingLookupRepository` と `ReplacementBoundaryLookupRepository` は `meter_id` と順序境界(`before_scheduled_at`、`before_planned_reading_id`、`excluding_planned_reading_id`)を受け取り、`new_initial_reading is None`(初期値未指定)の交換境界も識別して返せる契約にする。交換境界の探索は `new_meter_id = meter_id` かつ `replaced_at <= target_scheduled_at` の直近境界を対象とし、その境界が未消費か(境界の `replaced_at` 以降かつ対象枠より前に同一 `meter_id` の実測値が存在しないか)で新初回実測を判定する。後続再計算は Repository 内で計算せず、「後続実測枠一覧の取得」と「計算済み結果の保存」の契約に分離する。
- `PlannedReadingCloseReason` は `cutoff_elapsed`・`meter_replaced`・`schedule_recomposed`・`definition_deactivated` の 4 値とする。同一内容の再取込(D20)を区別するため、`UpsertDisposition.IGNORED_DUPLICATE` と `SyncItemIgnoreCode.DUPLICATE_SUBMISSION` を追加する。
- D11 の検証は、メーターの登録更新、測定定義への割当、交換先メーター指定、結果の担当者指定等の公開操作へ対応付ける。無効化済みマスタの既存履歴は参照可能とし、再有効化は明示的な操作にする。無効化済みメーターの既存枠への後日入力・訂正は許可する(D23)。測定定義の無効化は現在業務日以降の `open` 枠を `superseded` にする操作を含む(D25)。
- スマホ入力に PC 生成の `signature` や確定担当者名を要求しない。シート ID と PC で選択した有効な担当者を確定コンテキストとして渡し、保存要求の `device_id`・`updated_by`・`result_source`・署名を組み立てる。入力側の値を PC の監査情報として無条件に採用しない。PC 直接入力は `device_id = 'pc-direct'`・`result_source = 'manual'` を Service が付与する(D18)。
- 署名対象の日時は D21 の形式(UTC・秒単位の `YYYY-MM-DD HH:MM:SS`)へ正規化する。保存値から署名を再現できることを検証する。HTML / MR1 の生成・解析そのものは Phase 2 のままとする。
- 契約から廃止する項目: `WorkPackageTaskGroups.ad_hoc_tasks`(D10)、`WorkPackageResponse.backfill_pending`(3.3)、`ResultSource.CLIENT` / `SYSTEM`(D18)、旧 HTTP API / 主端末制専用の `ApiRequestErrorCode`・`ApiRequestErrorResponse`、および `SyncItemRejectCode` の旧クライアント署名検証用コード(`MISSING_DEVICE_ID` / `SIGNATURE_CANONICALIZATION_FAILED` / `INVALID_SIGNATURE`、D4・D17)。

### 3.2 トランザクションと再計算の責務(D2、D13、D16、D19、D20、D24)

- 一括確定、PC 直接入力、設定変更、マスタ更新(測定定義の無効化を含む)、メーター交換、起動時準備の各公開業務操作が `transaction(conn)` を所有する。起動時準備は「既定設定投入 → 補完 → 自動クローズ」を統括する Service とし、`app.py` はそれを呼ぶ。失敗時の扱いは D24 に従う。
- 共通の書込・補完・再構成処理は呼出元のトランザクション内で動く。ネストした `transaction()` や、`conn.in_transaction` に応じて偶然境界が変わる実装にしない。Repository で計算 Service を呼ばず、Service が計算し Repository が取得・保存する。
- 設定保存は適用履歴の更新と影響する予定枠の再構成を同一トランザクションにする。測定定義別スケジュール(`MeasurementDefinitionScheduleService`)だけでなく、周期別既定スケジュール(`ScheduleSettingsService`: 該当周期で `schedule_json IS NULL` の有効測定定義すべて)と配信オフセット(`ReleaseOffsetSettingsService`: 該当 `reading_session` の適用開始日以降の `open` 枠の `release_date`)も同一トランザクションで再構成する。一括確定では結果 1 件に伴う署名・境界・後続再計算までを同じ SAVEPOINT に含める。
- `measured` から `skipped` 等への変更でも後続を再計算する。例として `100 → 120 → 150` の中央を非実測に変えると、倍率 1 の後続使用量は `30` から `50` へ変わる。
- 交換前の遡及変更では `previous_measured_reading`・その参照先・`old_meter_usage_delta` も再評価する。交換境界の `old_meter_usage_delta` 計算でも `UsageDeltaCalculationService` の規則(倍率・ロールオーバー補正、および `rollover_value is None` での負の差分拒否)を適用する。交換をまたいで旧メーターの読値を新メーターの前回値にしない(初期値未指定の交換でも新初回の前回値は `None`・使用量は `0` とする)。新初回の訂正・非実測化では、自分自身で境界消費済みと誤判定せず、次に初回となる実測へ交換初期値を適用する。
- メーター交換時は `planned_reading.scheduled_at >= replaced_at`(UTC 正規化後、割当期間 `[effective_from, effective_to)` と同一基準)で差替え対象枠を判定する。外部キー制約(`PRAGMA foreign_keys = ON`)を満たすため、先に新メーター枠を INSERT(旧枠の `scheduled_at` と `release_date` を引き継いで明示設定)してから、旧枠を `close_reason = 'meter_replaced'`・`superseded_by_planned_reading_id = 新枠ID` で `superseded` に UPDATE する。
- 再計算で負の差分等が発生した場合、直接入力は業務操作全体を、一括確定は当該入力と全副作用を SAVEPOINT 単位で戻す。予期しない例外・COMMIT 失敗は全体を戻す。
- PC 直接入力・訂正は D3 の「古い入力の無視」を適用せず、`client_updated_at` を D19 の規則で決める。スマホ取込では、同一内容の再取込を重複として何も更新せず(D20)、内容が異なる同時刻の入力だけ `arrival_sequence` を進める。

### 3.3 予定枠補完の設計(D14、D22、D25)

#### 補完範囲と適用履歴

- 測定定義に予定生成開始業務日を保持する。新規登録時の既定は業務タイムゾーンでの当日とし、過去から運用する場合は明示指定する。開始日前は自動生成しない。メーター交換で開始日をリセットしない。補完の対象は有効な測定定義に限り、無効化・再有効化の扱いは D25 に従う。
- 対象の測定定義ごとに、開始日から呼出側が指定した対象業務日までを包含して照合する。DB 全体または定義別の `MAX(business_date)` を補完開始位置・完了判定に使わない。
- スケジュールには適用開始業務日を持つ履歴を追加する。適用期間は次の版の開始日を終端とする半開区間で管理し、当該日に有効な周期・時刻・日付指定・`day_offset`・配信オフセットを解決する。
  - 不足の判定は論理キー(測定定義・業務日・検針時間)で行い、適用履歴は新しく作る枠の `scheduled_at` と `release_date` の算出にだけ使う。
  - 業務タイムゾーンは D22 により運用開始後は変更されないため、適用履歴の対象外とする。締め時刻は現在値だけを使う。
- 設定変更は適用開始日(`effective_from_date`、既定は現在業務日)を明示し、変更前の期間へ最新設定を遡及適用しない。現在業務日より前の日付指定は `DomainError` 派生例外で拒否する(初期シード `'0001-01-01'` と測定定義登録時の `generation_start_date` 初期履歴投入を除く)。同一 `effective_from_date` での再保存は該当履歴行を上書き(UPSERT)する。測定定義別の上書きと周期別既定値の優先関係を維持し、既定値を使う定義も過去に適用された設定を再現可能にする。周期別既定値と配信オフセットは履歴テーブルへ移し、`meter_app_settings` に最新値を重複して持たない(`meter_app_settings` に残すのは締め時刻と業務タイムゾーン)。
- 物理メーターは生成する `scheduled_at` に有効な割当期間 `[effective_from, effective_to)` で解決する。履歴の不足・重複等で対象を特定できない場合、現在の設定・割当で推測せず、対象定義と期間を示す `DomainError` 派生例外で通知する。

#### 不足判定と既存データの保護

- Service が期待枠を算出し、Repository から取得した既存枠・交換差替え情報と照合して、本当に存在しない枠だけを追加する。元の予定枠 ID の有無だけで不足を判定しない。
- 論理的な枠(測定定義・業務日・検針時間)と、交換・再構成で差し替えられた実体の対応を契約で表す。既存の決定的な ID 生成を再利用しつつ、交換後の代替枠を重複生成しない。
- 通常の補完では既存枠・結果を更新しない。`completed`・`auto_closed` の状態を維持し、`superseded` を復活させない。設定変更に伴う意図的な枠変更は再構成処理の責務とする。再構成処理では、初期 ID 固定ではなく論理枠(測定定義・業務日・検針時間)の現在有効な後継枠(`superseded_by_planned_reading_id` の終端にある `open` 枠、または `schedule_recomposed` で廃止された枠)を対象とし、メーター交換済みの旧枠(`close_reason = 'meter_replaced'`)や測定定義無効化枠(`definition_deactivated`)、`auto_closed` 枠を `open` に書き戻さない。
- 差替え済みの枠は後継枠または適用履歴上の廃止理由と照合する。必要な後継が失われる等の不整合は、旧枠の復活や無条件の追加で隠さず業務エラーにする。測定定義の無効化による `superseded`(`close_reason = 'definition_deactivated'`、D25)およびスケジュール再構成による廃止(`close_reason = 'schedule_recomposed'`)は廃止理由として扱い、後継枠がなくても不整合としない。

#### 処理分割・完了・締め

- 14 日は内部の走査・照合単位に限定する。生成件数ではなく日付で進め、週次・月次・隔月で生成が 0 件の期間でも進行する。全期間の要求を一度にメモリへ保持しない。
- 起動時とシート出力前の準備は、1 回の公開業務操作で対象日まで補完を完了する。途中で打ち切った状態を成功として返さない。`backfill_pending` の旧仕様・テストはこの契約へ置換する。
- 「補完 → 自動クローズ」を同一トランザクションで行う。締め判定には出力対象日ではなく注入した時計の現在業務日時を使い、補完した過去枠も通常の締め対象にする。
- シート用データの取得は補完・締め処理後に行う。Phase 2 以降の HTML 出力は DB 処理の成功後とし、補完失敗時に不完全なシートを出力しない。
- まずは期間全体の差分照合で実装する。走査済み日付等のキャッシュは初期実装に追加せず、必要性を実測してから検討する。

### 3.4 スキーマ変更と開発用 DB(D8、D18、D22)

- 生成開始業務日と適用履歴を追加するため、v1 の DDL・版別検査定義・スキーマのテストを一緒に更新する。履歴の一意性・参照整合性は DB 制約、期間の適用可否は Service 検証でも保護する。
- DDL の方針(列名・型の最終形は PR-1b で確定する)は次のとおり。
  - `measurement_definitions` に `generation_start_date TEXT NOT NULL` を追加する。
  - `measurement_definition_schedules` は主キーを `(measurement_definition_id, effective_from_date)` に変え、`schedule_json`(NULL の版は「周期別既定に従う」)・`created_at` を持つ履歴にする。
  - 周期別既定の履歴 `reading_cycle_schedule_defaults(reading_cycle, effective_from_date, schedule_json, created_at)` と、検針時間ごとの配信オフセットの履歴 `reading_session_release_offsets(reading_session, effective_from_date, offset_days, created_at)` を新設する。主キーは適用開始日を含める。
  - `bootstrap_seeds` は周期別既定・配信オフセットの初版を `effective_from_date = '0001-01-01'` で冪等に投入する。`meter_app_settings` へ投入するのは締め時刻とタイムゾーンだけにする。
  - `reading_transactions.result_source` は `NOT NULL CHECK (result_source IN ('mobile_html','manual'))` にする(D18)。
  - `planned_readings` の `close_reason` に `CHECK (close_reason IS NULL OR close_reason IN ('cutoff_elapsed','meter_replaced','schedule_recomposed','definition_deactivated'))` を設け、交換等で差し替えられた後継枠を示す `superseded_by_planned_reading_id TEXT REFERENCES planned_readings(planned_reading_id)` を追加して実体との対応(3.3)を保持する。
- 変更前の v1 DB は同じ `user_version` でもスキーマ検査で拒否される。未配布の開発用 DB を自動削除・黙示変換せず、新しい一時保存先で作成する手順を記録する。保全が必要な DB は別途保管し、実データ領域を使わない。
- 本章は未実装の設計であり、正本への統合は 5.6 の実装完了確認後に行う。

## 4. スコープ

### 対象

- `core/` 10 ファイルの import 修正・整形・strict 型付け
- `db/repositories.py`(Protocol の標準 sqlite3 実装)と `db/bootstrap_seeds.py`
- `connection.py` への SAVEPOINT 補助の追加
- `staff` の Repository と Service
- 物理メーター・測定定義・初期割当の更新契約と Service
- スケジュール契約の分離、生成開始業務日と適用履歴のスキーマ・Repository・Service
- 設定変更と予定枠再構成の原子的な実行、未署名入力と PC 保存要求の分離
- 監査情報の語彙(`ResultSource`、`device_id = 'pc-direct'`)と署名時刻表記の確定、業務タイムゾーン変更の禁止、測定定義の無効化に伴う `open` 枠の廃止
- 旧テストの移植と Repository 統合テスト
- 検査対象外設定の解除と `db/reference_sqlalchemy/` の削除
- 予定枠の補完・自動クローズの Service 実装と、`app.py` の起動順への組み込み(失敗時の扱いを含む)
- 関連文書の更新

### 対象外

- UI 全般(Phase 3 / 4)、`__main__.py`
- HTML シート生成と Playwright テスト(Phase 2)
- 集計・CSV・`db/integrity.py`・復元(Phase 5)
- 新規依存の追加(SQLAlchemy、Alembic、FastAPI 等)

## 5. 実装タスク

各タスクは、完了を検証してから `[x]` にする。ファイルが存在するだけでは完了としない。

### 5.1 PR-1a `feature/phase1-contracts-pure-logic`: 契約と純粋ロジック

- [x] `migration_package/` で `uv sync --locked` を実行し、全体を対象に ruff / pyright / pytest 収集 / coverage の件数を記録する(9 章の記録欄へ)
- [x] 対象 10 ファイルとテストを `ruff format` と `ruff check --fix` で機械整形し、機械的な変更だけを別コミットにする
- [x] import を置き換える
  - `api.models` → `meter_reading_mini.core.contract_models`
  - `application.` → `meter_reading_mini.core.`
- [x] `meter_reading_sync_repositories.py` から `core.database`(`MeasurementDefinitionORM`)への型依存を外し、純粋な契約型へ置き換える
- [x] 3.1 に従いスケジュール型・設定関連 Protocol を契約側へ移し、`TYPE_CHECKING` 内も含めて Service への依存を除去する。契約の依存方向テストをこの PR で追加する
- [x] 物理メーター・測定定義・割当・Staff の更新要求と結果レコード、使用状況照会、生成開始日・適用履歴・論理枠と差替え実体の照合に必要な Protocol を定義する
  - 測定定義の更新要求では `reading_cycle`・`value_schema`・`calculation_method`・`location_id` を作成後変更不可とする(D15)
  - `HistoricalMeasuredReadingLookupRepository`(前回実測取得・後続実測一覧取得)および `ReplacementBoundaryLookupRepository` に `meter_id` と順序境界引数(`before_scheduled_at`、`before_planned_reading_id`、`excluding_planned_reading_id`)を追加し、`new_initial_reading is None` の交換境界も返せる契約にする。後続再計算は Repository 内の計算メソッドを廃止し、後続実測枠の取得と計算済み結果の保存に分離する(D16、3.1)
- [x] スマホの未署名入力と PC 確定コンテキスト・保存要求を分け、署名や担当者のダミー値なしで検証できる契約テストを追加する(D17)
- [x] 契約と設定の語彙・不要機能を整理する
  - `ResultSource` を `MOBILE_HTML = "mobile_html"` と `MANUAL = "manual"` の 2 値にし、`CLIENT`・`SYSTEM` を削除する(D18)
  - PC 直接入力の `device_id` 固定値 `pc-direct` を契約側の定数として定義する(D18)
  - `PlannedReadingCloseReason` に `METER_REPLACED = "meter_replaced"`、`SCHEDULE_RECOMPOSED = "schedule_recomposed"`、`DEFINITION_DEACTIVATED = "definition_deactivated"` を追加する(3.1、D25)
  - `UpsertDisposition` に `IGNORED_DUPLICATE = "ignored_duplicate"`、`SyncItemIgnoreCode` に `DUPLICATE_SUBMISSION = "duplicate_submission"` を追加する(D20)
  - `WorkPackageTaskGroups` から廃止対象の `ad_hoc_tasks` を削除する(D10)
  - `WorkPackageResponse` から `backfill_pending` を削除する(3.3)
  - `contract_models.py` から旧 HTTP API / 主端末制専用の `ApiRequestErrorCode`・`ApiRequestErrorResponse` と、旧クライアント署名検証用の `SyncItemRejectCode` 項目を削除する(D4、D17)
  - `schedule_settings.py` から対象外の `ActiveDeviceBindingService` 関連を削除する
- [x] `UsageDeltaCalculationService.calculate` で、`rollover_value is None` かつ `raw_reading < last_reading` のときに `DomainError` 派生例外を送出するよう修正する
- [x] 署名の時刻表記を D21 に合わせる。`canonicalize_signature_recorded_at` を `YYYY-MM-DD HH:MM:SS`(UTC・秒単位)へ変更し、タイムゾーンなし・秒未満の入力の扱いをテストで固定する
- [x] `build_scheduled_at()` と `normalize_to_utc()` を `core/timezone_settings.py` へ移す(D5)
- [x] `build_current_datetime_provider` / `build_business_date_provider` は `timeutil.utc_now()` で UTC 現在時刻を取得し、設定された業務タイムゾーンへ明示的に変換する。`utc_now()` 自体に tz 引数は追加しない
- [x] 次のモジュールが pyright strict で 0 件になる: `contract_models`、`meter_reading_sync_repositories`、`usage_delta_calculation`、`reading_signature`、`timezone_settings`、`schedule_settings`
- [x] 業務例外を `core/errors.py` の `DomainError` 派生へそろえる。同期処理の拒否コードとの対応表を作る。D22(業務タイムゾーン変更の拒否)・3.3(適用履歴・割当の不足と重複)で必要な例外を追加する
- [x] 次のテストを移植する: `test_api_models`、`test_usage_delta_calculation`、`test_reading_signature`、`test_timezone_settings`(純粋関数の部分: 各プロバイダ、`normalize_to_utc`、`build_scheduled_at`)
- [x] `test_timezone_settings.py` のうち、設定 Repository・`core.bootstrap`・`create_database_schema` に依存する分は PR-1b へ、`core.startup_diagnostics` 依存分は削除する。移植・移送・削除の件数と理由を 9 章へ記録する
- [x] `test_measurement_definition_schedule_service.py` は適用履歴化(D14)で書き直すため PR-1c へ送る。`ActiveDeviceBindingService` 依存分の削除と件数は PR-1c で記録し、PR-1a では対象外のままにする
- [x] 上記の移植対象を `pyproject.toml` の ruff / pyright / pytest / coverage の対象外から外し、ローカルのゲートが通る。ただし `schedule_settings.py` はテストを PR-1c で移すため、ruff / pyright の対象には戻し、coverage の対象は PR-1c で戻す(テストのないファイルで 90% を割らないため)

### 5.2 PR-1b `feature/phase1-sqlite-repositories`: 標準 sqlite3 Repository

- [x] `connection.py` に `savepoint(conn, name)` を追加する
  - トランザクション外で呼ぶと `RuntimeError` になる
  - `name` は呼出側が固定で指定する識別子とし、SQL へ埋め込む前に `[A-Za-z_][A-Za-z0-9_]*` に一致しなければ `ValueError` にする(SAVEPOINT 名はプレースホルダで渡せないため)
  - 例外時は `ROLLBACK TO` と `RELEASE` を行い、例外を再送出する
  - 正常時は `RELEASE` する
- [x] 3.3・3.4 に従い、生成開始業務日・適用履歴(測定定義別・周期別既定・配信オフセット)と必要な差替え対応(`planned_readings.superseded_by_planned_reading_id` および `close_reason` の 4 値 CHECK 制約)を v1 へ追加し、`reading_transactions.result_source` を `NOT NULL CHECK (result_source IN ('mobile_html','manual'))` にする(D8、D14、D18)。`db/schema.sql` を更新し、`db/migrations.py` が DDL から生成する版別検査定義と `test_migrations.py` で検証する。`SCHEMA_VERSION = 1` は維持する。
  - [x] SQLite の CHECK だけでは暦として有効な日付や有限 plain decimal の正規形を完全に検査できないため、形式 CHECK は追加しない。Repository で UTC 秒精度・厳密な日付表記・有限 plain decimal を検証し、状態・列挙値・参照整合性は DB 制約で保護する
- [x] `db/repositories.py` に次の Repository を実装する(`core/meter_reading_sync_repositories.py` の Protocol に合わせる)
  - 予定枠: Lookup / Generation / Lifecycle / Recomposition / TaskQuery。期間内の既存枠・差替え対応の取得と不足枠保存、測定定義の無効化に伴う `open` 枠の廃止(`superseded` と `close_reason`、D25)を含む
  - 測定定義: Lookup / Schedule / Query に加え、登録更新・論理削除・再有効化、生成開始日とスケジュール適用履歴の保存取得
  - 物理メーター・割当: MeterMasterQuery に加え、登録更新・論理削除・再有効化・使用状況照会(割当中の有効な測定定義の有無と `open` 枠の有無。`auto_closed` は含めない、D23)、初期割当と有効期間に基づく履歴照会
  - その他: HistoricalMeasuredReadingLookup(`meter_id` と順序境界および必要に応じた `after_or_at_scheduled_at` による前回実測取得・後続実測一覧取得) / ReadingPersistence(D3 を含む。保存結果として新規・更新・古い入力の無視・同一内容の重複(`UpsertDisposition.IGNORED_DUPLICATE`、D20)を区別して返し、後続再計算用の更新保存を提供する) / ReplacementEventUpsert / ReplacementReflection / ReplacementBoundaryLookup(`meter_id` と対象枠の順序境界を受け取り、`replaced_at <= target_scheduled_at` の直近境界について `new_initial_reading is None` の場合も含めて未消費境界を識別して返す) / Settings(`meter_app_settings` と適用履歴。`planned_readings` の有無の照会を含む、D22) / Staff(新設)
- [x] 実装規約を満たす
  - SQL の値はすべてプレースホルダで渡す
  - Decimal は有限の plain decimal 文字列で読み書きする
  - 時刻は D6 の形式にする。`client_updated_at` の新旧比較および D20 の重複判定(`client_updated_at`・`recorded_at`)も D6 の秒単位 UTC に正規化した値同士で行う
  - 現在時刻と業務日付は注入した時計から受け取り、`date.today()` と `datetime.now()` を持ち込まない
  - 低レベル DB 例外(`IntegrityError` 等)は Repository で `DomainError` 派生へ変換する
- [x] `db/bootstrap_seeds.py` に既定値を冪等に投入する処理を作る(D7)。投入先と値は次のとおり。
  - `meter_app_settings`: 締め時刻 `07:00`、タイムゾーン(`app.timezone.v1`)
  - 周期別既定スケジュールと配信オフセットの履歴: 初版を `effective_from_date = '0001-01-01'` で投入する
- [x] `tests/conftest.py` に、架空のサンプルマスタ入りフィクスチャと時刻固定フィクスチャを追加する
- [x] PR-1a から送られた `test_timezone_settings.py` の残り(設定 Repository・既定値投入に依存する分)を新構成で移植する
- [x] Repository の統合テストを書く(ロジックは `:memory:`、WAL・永続化は `tmp_path`)
  - CHECK / UNIQUE / FOREIGN KEY 違反が `DomainError` 派生へ変換される(`result_source`・`close_reason` の不正値を含む)
  - 古い入力の無視と、同時刻での `arrival_sequence` による採用順(D3)。同一内容(`recorded_at` を含む)の入力は `IGNORED_DUPLICATE` として返り、`arrival_sequence` が進まない(D20)
  - 前回実測取得・交換境界取得が `meter_id` と順序境界で正しく絞り込まれ、初期値未指定の交換後に旧メーターの読値を引かないこと、新初回実測自身の再計算で境界が失われないこと(D16)
  - 後続実測値が `scheduled_at, planned_reading_id` 順に取得でき、Service による連鎖再計算の結果を保存できる。交換境界の取得更新も検証する
  - 生成開始日・適用開始日・割当期間に基づく取得、途中欠落と交換後の代替枠の照合
  - 期間検索 API は PR-1b の Repository Protocol に定義されていないため新設しない。実装済みの日時順序検索は D6 の UTC 秒精度文字列を使い、SQL 内で TZ 補正しない
  - Staff の登録・無効化・一意制約
  - 物理メーターの使用状況照会が、割当中・`open` 枠ありのときだけ使用中を返し、`auto_closed` 枠だけの場合は使用中にならない(D23)
  - 測定定義の無効化による `open` 枠の廃止が、`completed`・`auto_closed` 枠を変更しない(D25)

### 5.3 PR-1c `feature/phase1-schedule-generation`: 予定枠の適用履歴・補完・起動時準備

- [x] `MeasurementDefinitionScheduleService`・`ScheduleSettingsService`(周期別既定)・`ReleaseOffsetSettingsService`(配信オフセット)を適用履歴化し、設定保存は適用履歴更新・影響する予定枠の再構成まで原子的に実行する(D13、D14、3.2、3.3)
  - 周期別既定の変更は `schedule_json IS NULL` の該当周期の有効測定定義すべて、配信オフセットの変更は該当 `reading_session` の適用開始日以降の `open` 枠の `release_date` を再構成する
  - 設定保存時の `effective_from_date` は既定を現在業務日とし、現在業務日より前の過去日指定は `DomainError` 派生例外で拒否する。同一 `effective_from_date` の再保存は UPSERT で上書きする
  - 再構成処理は論理枠ごとの現在有効な後継枠(交換後の代替枠を含む)のうち `open` 枠および `schedule_recomposed` 枠のみを対象とし、メーター交換済みの旧枠(`close_reason = 'meter_replaced'`)や測定定義無効化枠(`definition_deactivated`)、`auto_closed` 枠を `open` に戻さない
  - `ActiveDeviceBindingService` 依存分を削除し、件数と理由を 9 章へ記録する
- [x] 業務タイムゾーンの保存で、`planned_readings` が 1 件でもあるときの変更を `DomainError` 派生例外で拒否する。初回投入と同じ値の再保存は許可する(D22)
- [x] `WorkPackageDownloadService` に次を実装する
  - 3.3 に従う予定枠の補完生成(backfill)。最新生成日方式を廃止し、内部で 14 日ずつ照合して対象日まで完了する。有効な測定定義だけを対象にする(D25)
  - 締め時刻経過後の前日以前の `open` → `auto_closed`
  - 周期区分(`daily` / `weekly` / `monthly` / `every_two_months`)によるグループ化(D10: `ad_hoc` は廃止)
  - `backfill_pending` の旧仕様を新しい完了契約へ置換する
- [x] 公開業務操作と共通処理を分け、3.2 のトランザクション所有者を実装する(D13)。補完・再構成の共通処理は呼出元のトランザクション内で動く
- [x] 「`bootstrap_seeds` による既定設定投入 → 予定枠補完・自動クローズ」を統括する起動時準備 Service を作り、`app.py` から「DB オープン・マイグレーション → `_initialize_ui`(`QApplication` 確保・表示設定適用) → 起動時準備 Service → (Phase 3 で MainWindow 表示)」の順で呼ぶ。準備全体を単一トランザクションとする
  - 準備が失敗したときは D24 に従い、ROLLBACK の上で `_initialize_ui` により有効化された `QApplication` 下で汎用エラー通知(注入可能なエラー通知関数、既定はログ記録を含む `show_unexpected_error`)を呼び、`STARTUP_FAILURE`・MainWindow 非表示とする。`test_app.py` を更新し、モーダル待ちを発生させずにエラー通知と DB へ途中結果が残らないことを検証する
- [x] 次のテストを移植し、ファイル DB(`tmp_path`)で実行する: `test_measurement_definition_schedule_service`、`test_work_package_download_service`
- [x] `schedule_settings.py` を coverage の対象へ戻す(PR-1a から持ち越し)
- [x] ロールバック検証を追加する
  - 設定保存後・適用履歴更新後・予定枠再構成中に例外が起きた場合
  - 複数の補完区間を処理した後、途中で例外が起きた場合
  - 起動時準備の COMMIT が失敗した場合
- [x] 業務ルールの検証を追加する
  - 締め時刻の境界とサマータイムのあるタイムゾーン
  - 業務タイムゾーンの変更が、運用開始後は拒否され、運用開始前と同じ値の再保存は許可される(D22)
- [x] 予定枠補完の受入テストを追加する。旧 14 日打切り・`backfill_pending` 前提のテストは置換理由を記録する
  - 他定義の当日枠がある状態で新定義を追加しても、開始日から枠が生成される
  - 中間の日付・同日の一部時間帯の欠落だけを補完し、開始日前は生成しない
  - 14 日を超える長期未起動でも 1 回の業務操作で対象日まで完了する
  - 月次・隔月のみで生成 0 件の区間があっても処理が停滞しない
  - 再実行しても枠数・既存状態・結果が変わらない
  - 設定変更前の期間へ新しい時間帯を追加せず、適用履歴・割当の不足や重複を明示的に拒否する
  - 交換前後の割当を使い分け、旧枠の復活・代替枠の重複がない
  - 補完した過去枠の締めは現在業務日時で判定し、出力対象日に左右されない
  - 無効な測定定義は補完の対象にならない(D25)
- [x] 受け入れテストのために、PR-1d で実装する交換・無効化の結果を表すテスト用データ(`superseded` 枠・`close_reason`)をフィクスチャで用意する

### 5.4 PR-1d `feature/phase1-readings-masters`: 検針結果・マスタ・交換

- [x] `MeterReadingSyncService` から `Session` を外し、`conn` と `transaction`、`savepoint` で作り直す(D2)
  - 業務拒否は SAVEPOINT 単位で戻し、拒否として集計する
  - 予期しない例外は全体を ROLLBACK して再送出する
  - 旧実装の `except Exception` による拒否扱いを廃止する
  - 同一内容の再取込を重複として集計し、何も更新しない(D20)
- [x] 署名は PC の確定時に生成する(D4)。スマホ側署名の検証を外し、`device_id = sheet_id`、`updated_by = 確定担当者名`、`result_source = 'mobile_html'` を受け取って確定時に署名を生成・記録できるようにする
  - 未署名入力を検証し、有効な担当者とシート ID から保存要求を構築する。時刻を D21 の形式へ正規化してから署名を生成し、保存値から再現できることを検証する
- [x] `ReadingWriteOrchestrationService` を、PC 直接入力とスマホ取込で共通の経路にする。結果保存・監査署名・交換境界・予定枠更新・後続すべての実測値の連鎖再計算(`UsageDeltaCalculationService` に一元化)を同一トランザクションで整合させる
  - 実測と非実測の相互変更、交換前の訂正による境界更新、新初回の訂正・非実測化を 3.2 に従って処理する。負の差分等の業務拒否では元の書込と副作用を一緒に戻す
  - PC 直接入力・訂正は `device_id = 'pc-direct'`・`result_source = 'manual'` で署名を生成し、D3 の「古い入力の無視」を適用せず、`client_updated_at` を D19 の規則で決める
  - `auto_closed` 枠への後日入力を `completed` にして再計算する処理(無効化済みメーターの既存枠を含む、D23)
- [x] メーター交換を原子的に実行する
  - `planned_reading.scheduled_at >= replaced_at`(UTC 正規化後)で対象枠を判定し、先に新メーター枠を INSERT する(旧枠の `scheduled_at` と `release_date` を引き継いで明示設定する)
  - 新メーター枠の INSERT 後に、旧メーターの `open` / `auto_closed` 枠を `close_reason = 'meter_replaced'`・`superseded_by_planned_reading_id = 新枠ID` で `superseded` に UPDATE する
  - 割当を切り替える(旧メーターは自動で無効化しない、D23)
  - 交換境界(`old_meter_usage_delta` への倍率・ロールオーバー適用およびロールオーバー未設定時の負の差分拒否を含む)と交換記録を保存し、遅延交換時の完了済み新メーター実測値も倍率・ロールオーバー込みで再計算する
- [x] `MeterMasterService` を Repository 経由に作り直す。物理削除は行わず、有効な測定定義に割当中または `open` 予定枠を持つ物理メーターの無効化は `MasterInUseError` で拒否し(`auto_closed` は拒否の対象にしない)、無効化済みマスタの新規利用(割当・交換先指定・新規枠生成)は `InactiveMasterError` で拒否する(D11、D23)
- [x] 測定定義・初期割当の登録更新と論理削除・再有効化を Service 経由で提供する(D15)。登録時の生成開始日・初期履歴・割当保存を原子的に行い、無効化済みマスタの新規利用を各操作で拒否する
  - 測定定義の更新で `reading_cycle`・`value_schema`・`calculation_method`・`location_id` の変更が指定された場合は `DomainError` 派生例外で拒否する(D15)
  - 測定定義の無効化は、現在業務日以降の `open` 枠を同一トランザクションで `superseded`(`close_reason = 'definition_deactivated'`)にする。`completed`・`auto_closed` 枠と結果は変更しない(D25)
  - 再有効化は枠を復活させず、生成開始業務日を `max(既存値, 現在業務日)` へ更新する(D25)
- [x] `StaffService` を新設する(D9、D11: 無効化済み担当者の指定は `InactiveMasterError` で拒否)
- [x] 次のテストを移植し、ファイル DB(`tmp_path`)で実行する: `test_meter_master_service`、`test_meter_reading_sync_service`、`test_meter_reading_sync_service_batch_txn`、`test_reading_write_orchestration`
- [x] ロールバック検証を追加する
  - 結果保存後に例外が起きた場合
  - 交換境界の更新中に例外が起きた場合
  - 後続再計算の途中で例外が起きた場合
  - 測定定義の無効化で `open` 枠の廃止中に例外が起きた場合(D25)
  - COMMIT が失敗した場合
  - SAVEPOINT で一部だけ戻した後、ほかの結果が確定している場合
- [x] 業務ルールの検証を追加する
  - Decimal の精度
  - ロールオーバーの有無(未設定の負の差分は拒否)
  - 新メーター初期値の有無(未指定のときだけ初回使用量 0)
  - 交換の前後
  - 過去の追加・訂正による連鎖再計算
  - `100 → 120 → 150` の中央を非実測に訂正すると、倍率 1 の後続使用量が `50` になる
  - 交換前実測の訂正で交換境界も更新され、新メーターの前回値に旧メーター値が混入しない
  - 新初回実測の訂正・非実測化で交換初期値の適用先が正しく決まる
  - `auto_closed` からの復活(無効化済みメーターの既存枠への後日入力を含む、D23)
  - `purpose` ごとの集計に必要なデータ保持
- [x] 監査情報・同期規則の検証を追加する(D18〜D21)
  - PC 直接入力・訂正が `device_id = 'pc-direct'`・`result_source = 'manual'` で署名され、後続再計算で `result_source`・署名・`updated_by` が変わらない(D18)
  - スマホの時計が進んだ状態で PC 訂正し、その後に古い MR1 を再取込しても PC の訂正が残る(D19)
  - 同一内容の再取込は何も更新せず重複として集計され、内容が異なる同時刻の入力だけ `arrival_sequence` が進む(D20)
  - 署名が `YYYY-MM-DD HH:MM:SS`(UTC・秒単位)で生成され、保存値から再現できる(D21)
- [x] マスタ保護の検証を追加する(D11、D15、D23、D25)
  - 交換前に `auto_closed` 枠が残っていても、旧メーターを無効化できる
  - 無効化した旧メーターの `auto_closed` 枠に後日入力すると `completed` になり、交換境界と後続が再計算される
  - 割当中または `open` 枠のあるメーターの無効化は `MasterInUseError` で拒否される
  - 無効化したメーターを交換先・割当に指定すると `InactiveMasterError` で拒否される
  - 測定定義の更新で `reading_cycle`・`value_schema`・`calculation_method`・`location_id` の変更が `DomainError` 派生例外で拒否される(D15)
  - 測定定義の無効化で現在業務日以降の `open` 枠だけが `superseded`(`definition_deactivated`)になり、`completed`・`auto_closed` 枠と結果は変わらない。再有効化後に枠が復活せず、無効期間の補完もされない
- [x] マスタ登録更新・再有効化、未署名入力から PC 署名保存までの Service 統合テストを追加する
- [x] 残りの `core/` 対象ファイルを検査対象へ戻し、pyright strict で 0 件になる

### 5.5 PR-1e `feature/phase1-gate-cleanup`: 検査対象外の解除と参考資産の削除

- [x] `pyproject.toml` の ruff `extend-exclude`、pyright `ignore`、pytest `--ignore`、coverage の `include` に残る Phase 1 対象の設定をすべて消す。個別 skip の追加や閾値の引き下げは行わない
  - coverage の `include` はファイルの列挙をやめ、パッケージ全体を対象にする
  - coverage の `omit` の `core/mobile_reading.py` は Phase 2 の対象なので残し、Phase 2 の計画でこの除外を解除すると明記する
- [x] PR-1a から適用した契約の依存検査を維持し、全対象へ依存ルールのテストを拡張する
  - `core/` と `db/` が PySide6 を import しない
  - 契約(`contract_models`、`errors`、Repository Protocol・入出力レコード)から Service・UI・DB 実装への依存がない
  - `db/` から Service・UI への依存がない
  - `timeutil` 以外で `datetime.now`、`date.today`、SQL の `'localtime'` を使っていない
- [x] 統合テストの成功を確認してから `db/reference_sqlalchemy/` を削除し、`sqlalchemy` と旧 import の参照が残っていないことを `grep` で確認する
- [x] [移行ガイド](../../migration_package/README.md) の Step 2 / 3、README、正本 8 章の Phase 1 記述を実態に合わせる

### 5.6 完了記録 PR

- [x] Windows / Ubuntu の必須チェック(`test (windows-latest)`・`test (ubuntu-latest)`)が成功していることを確認し、結果を 9 章へ記録する
- [x] 確定した設計(D2〜D6、D8、D10〜D25、3.1〜3.4)を正本の 3・4・5・6 章へ統合する。生成開始日・適用履歴の DDL と業務ルール、トランザクション所有者、署名前入力の境界、署名の元文字列と保証範囲(D21)、`result_source`・`device_id` の語彙(D18)を含める。正本 6 章の「未消化予定枠を持つ物理メーター」は「`open` 予定枠」へ直し、`auto_closed` を拒否の対象にしないこと(D23)と測定定義の無効化時の枠の扱い(D25)を追記する
- [ ] 本計画書を完了(凍結)として `docs/plan/implemented/` へ `git mv` し、移動後の相対リンクを修正する

## 6. 品質ゲート

PR ごとに `migration_package/` で実行する。Linux のヘッドレスでは `QT_QPA_PLATFORM=offscreen` を設定する。

```text
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest --cov
uv run coverage report --fail-under=90
```

- `uv run coverage report --fail-under=90` は CI([.github/workflows/ci.yml](../../.github/workflows/ci.yml))と同じく `pyproject.toml` の対象設定に従い、あわせて `core/`・`db/` でも行カバレッジ 90% 以上を満たすことを確認する。

- PR-1a〜1d は、対象に戻したファイルを段階的に検査対象へ加え、各 PR 時点で全ゲートを成功させる。
- PR-1e の時点で、Phase 1 対象の除外設定が 0 件であることを完了条件とする(`core/mobile_reading.py` の coverage `omit` は Phase 2 まで残す)。
- 失敗や未実施の検査を成功として報告せず、実行した検査と未実施・失敗の理由を区別して記録する。
- 作業は `main` へ直接 push せず、feature ブランチの PR で CI 成功後にマージする。コミットは Conventional Commits、内容は日本語とする。

## 7. 完了条件

- [ ] 既存の単体テスト一式(差分計算・オーケストレーション・同期・予定生成・スケジュール・マスタ)と、標準 `sqlite3` Repository の統合テストが全件成功する
- [ ] ruff / pyright(`core/`・`db/` strict、`ui/` standard)/ pytest が、除外設定なしで Windows と Ubuntu の CI で成功する
- [ ] `core/`・`db/` の行カバレッジが 90% 以上である
- [ ] 契約の独立性、設定変更の原子性、マスタ更新契約、未署名入力からの PC 署名保存が検証されている
- [ ] 生成開始日と適用履歴による不足補完が対象日まで完了し、欠落・長期未起動・設定変更・交換・再実行の受入テストが成功する
- [ ] 実測と非実測の相互変更・交換境界を含む遡及再計算と、拒否時の全副作用のロールバックが検証されている
- [ ] 監査情報・同期規則(D18〜D21)、業務タイムゾーン変更の禁止(D22)、マスタ保護と測定定義の無効化(D23、D25)、起動時準備の失敗時の扱い(D24)がテストで検証されている
- [ ] `db/reference_sqlalchemy/` を削除し、旧 import と `sqlalchemy` の参照が 0 件である
- [ ] 文書(README・移行ガイド・正本)が実態と一致している

## 8. 留意事項

- **時刻が秒単位になる(D6 と D3):** 同じ秒に複数回更新があると `client_updated_at` だけでは順序を決められない。`arrival_sequence` で後着を採用する前提をテストで明示する。
  - 単一ユーザー運用を前提とし、同一秒の競合は `arrival_sequence` で解決する。マイクロ秒化はしない(D6 の形式は既存契約として維持する。スマホ側の時計ずれは精度を上げても解消しない)。
  - 更新の新旧を `client_updated_at` の文字列比較やミリ秒付きの生の `datetime` 比較だけで判定しない。D6 の秒単位 UTC に正規化した上で比較し、「同時刻なら `arrival_sequence`」の分岐を必ず通す実装にして、同一秒の複数更新を投入するテストを置く。
  - この比較はスマホ取込だけに適用する。PC 直接入力・訂正は D19 に従い常に採用する。
- **旧テストの削減:** `startup_diagnostics` 依存分および廃止対象の `ActiveDeviceBindingService` / `ApiRequestErrorResponse` / `ad_hoc_tasks` / `backfill_pending` / クライアント送信署名検証(D4・D17 により PC 確定時署名生成へ置換)の依存分は移植しない。削除・置換したテストと理由を 9 章の完了記録に残す。
- **Phase 1 の SQL 移植:** `db/reference_sqlalchemy/` は ORM のセマンティクス(暗黙の flush、`onupdate`、型変換)に依存する。クエリ移植時は、ORM 側の暗黙の挙動を明示的な SQL と Python 側の処理へ置き換える。
- **実データの保護:** テストは `:memory:` または `tmp_path` を使う。動作確認は `METER_READING_MINI_DATA_DIR` に一時保存先を指定し、実データ領域を使わない。
- **変更前の開発用 v1 DB:** D8 による再定義後は既存 DB の読込成功を前提にしない。新規の一時保存先を指定して検証し、既存 DB は必要に応じて保全する。自動削除・検査の緩和でスキーマ不一致を回避しない。
- **レビュー指摘の対応:** 契約分離は D12・5.1、設定変更と共通処理の境界は D13・3.2、補完方式は D14・3.3、マスタ更新は D15、遡及再計算は D16、署名前入力は D17 に対応する。監査情報・同期規則は D18〜D21、業務タイムゾーンは D22、マスタ保護は D23・D25、起動時準備の失敗は D24 に対応する。これらの追加対象も段階的な品質ゲートへ含める。
- **署名の保証範囲:** SHA-256 署名は予定枠・結果状態・測定値・記録日時・`device_id` の改ざん検出に使う監査用途で、`updated_by`・`note`・`usage_delta` は対象外、送信者認証でもない。Phase 5 の復元検査で署名を再計算するため、元文字列の形式(D21)を正本へ統合する。
- **PR の依存関係:** PR-1a から 1e は順に進める。交換(1d)は新メーター枠の生成(1c)に、補完(1c)は Repository(1b)と契約(1a)に依存する。PR-1a で持ち越すテスト(`test_timezone_settings` の設定 Repository 依存分は 1b、`test_measurement_definition_schedule_service` は 1c)は、各 PR の完了時に 9 章へ移送先と件数を記録する。

## 9. 完了記録(PR ごとに追記、Phase 1 完了時に集約)

- 全体の現状計測(5.1 の初回計測): 初回は Ruff 0 件、Pyright 0 件、pytest 111 件収集(110 成功・1 skipped)、coverage 90%。PR-1a 完了時は Ruff / format / Pyright の全検査成功(Pyright 0 件)、pytest 159 件収集(158 成功・1 skipped)、設定対象 coverage 91%。PR-1b 完了時は Ruff check / format、全体 Pyright が成功し、pytest 179 件収集(178 成功・1 skipped)、設定対象 coverage 92%。Repository は 92%、`bootstrap_seeds.py` は 100%。`uv run coverage report --include="src/meter_reading_mini/core/*,src/meter_reading_mini/db/*" --fail-under=90` は 48% で未達。未移植の Service と `db/reference_sqlalchemy/` が含まれるため、Phase 1 全体の残課題として扱い、PR-1b 完了とは区別する。テスト中に既存 `test_contract_boundaries` の ResourceWarning が 1 件発生。
- PR 分割(1a〜1e)と、テスト移植先の実績: PR-1a は `feature/phase1-contracts-pure-logic` で実装済み(実装差分は未コミット)。機械整形のみを `8b61855`(19 ファイル)へ分離してコミット。`test_timezone_settings.py` の Repository 依存 2 テストを PR-1b へ移送。`test_measurement_definition_schedule_service.py` の 13 テスト関数は適用履歴化に合わせ PR-1c で書き直す。
- 削除・置換したテストと理由: `test_timezone_settings.py` の `core.startup_diagnostics` 依存 3 テストを対象外仕様に従い削除。純粋関数の既存 2 テストは新 API に合わせて更新し、UTC 正規化・業務タイムゾーン・現在時刻 provider 等のテストを追加。PR-1b へ移した 2 テストは新 Repository / bootstrap に合わせて再実装する。
- スキーマ点検の結果(CHECK 制約の変更有無): `close_reason` の 4 値、`result_source` の 2 値、`reading_cycle` / `reading_session`、offset の整数、FK を DDL で保護。日付・UTC 時刻・Decimal の形式は Repository 境界で検証し、SQLite の CHECK は追加しない。`SCHEMA_SPECS` は `schema.sql` から生成され、スキーマ検査で変更前 v1 DB を拒否する。
- 生成開始日・適用履歴・差替え対応のスキーマと、変更前 v1 開発用 DB の取扱い: 生成開始日、測定定義・周期別既定・配信オフセットの適用履歴、差替え後継 ID を v1 DDL に追加。テスト用 `:memory:` と `tmp_path` の WAL DB で検証し、既存実データ DB の自動削除・変換は行わない。
- 予定枠補完の受入テスト結果と、長期未起動時の処理時間・対象規模:
- PR-1c の実装・検証結果: スケジュール・周期別既定・配信オフセットを適用履歴化し、UPSERT と予定枠再構成を各 Service の単一トランザクションに統合。補完は測定定義ごとの生成開始日から対象業務日までを 14 日区切りで 1 回の業務操作内に完了し、最新生成日を完了根拠にしない。既存の論理枠・交換後継を照合し、completed / auto_closed / meter_replaced / definition_deactivated を復活させない。WorkPackage の補完・現在業務日時に基づく自動クローズ・取得、起動時の既定値投入・補完・自動クローズは同一トランザクション。`ActiveDeviceBindingService` の参照は実装・テストで 0 件(予定日時の割当を Repository で解決するため Service 依存を置かない)。再構成の廃止・生成件数をログへ記録し、旧最新生成日取得 API を削除。
- PR-1c の受入・品質ゲート: file DB(`tmp_path`)で 21 日の補完(14 日 + 7 日)、定義ごとの生成開始日、中間日・同日時間帯の欠落、日次・週次・月次・隔月グループ、期間別スケジュール・割当・オフセット、交換後継、締め境界・DST、設定・複数区間・起動時処理・COMMIT の rollback を検証。計測した補完対象規模は 21 日であり、実運用規模 DB の性能計測は未実施。全 240 件中 239 passed / 1 skipped(Windows の `time.tzset()` 制限)、Ruff check / format・Pyright 0 件、設定対象 coverage 94%、`schedule_settings.py` 99%。既存 `test_contract_boundaries` の ResourceWarning 1 件は継続。
- 旧補完テストの置換理由: 14 日超で段階的に次回呼出しを要求する `backfill_pending` 契約を廃止し、1 回の業務操作で全対象日を完了する設計へ変更。旧 SQLAlchemy import を含む fake テストを削除し、file DB を用いた適用履歴・トランザクション統合テストへ置換。
- 契約分離・トランザクション境界・再計算・署名前入力の検証結果: PR-1a では契約境界テスト 4 件、署名前モバイル入力と契約語彙、純粋な差分計算・署名時刻・タイムゾーン処理を検証。PR-1b では SAVEPOINT の局所 rollback、Repository の後続更新保存・交換境界を検証。Service の連鎖再計算は PR-1d で検証する。
- 監査情報・同期規則・マスタ保護・起動時準備失敗(D18〜D25)の検証結果: PR-1b では `result_source` / `close_reason` 制約、stale/tie/duplicate、担当者一意制約、`auto_closed` の使用状況除外、測定定義無効化時の `open` 枠廃止と `completed` / `auto_closed` 維持を検証。PR-1c ではタイムゾーン変更拒否、締め境界、起動準備の処理例外・COMMIT 失敗時の rollback とエラー通知を検証した。
- 各 PR と CI の結果: PR-1a〜1e の実装差分はローカル品質ゲートで確認済み。GitHub PR / CI の実行・マージは未実施。
- PR-1e の実装・ローカル検査結果: 参考実装削除前に全テストを実行し 272 passed / 1 skipped を確認後、`db/reference_sqlalchemy/` を削除。Ruff の除外、Pyright の ignore、coverage のファイル列挙を解除し、pytest に `--ignore` 設定がないことを確認した。契約境界テストを `core/`・`db/` 全体の依存規則へ拡張。最終確認は Ruff check / format 成功、Pyright 0 errors、pytest 276 passed / 1 skipped(Windows の `time.tzset()` 制限)、パッケージ全体 coverage 94%、`core/`・`db/` coverage 94%。`migration_package/src/` と `uv.lock` に SQLAlchemy および旧 import は残っていない。Windows / Ubuntu の GitHub Actions も完了。
