# Phase 2 単一 HTML 検針シート・MR1 結果パーサ計画

- 作成日: 2026-10-27
- ステータス: 計画中
- 対象: リポジトリ直下を基点とする Phase 2(`src/`・`tests/`・`pyproject.toml`)
- 正本: [ローカル検針管理アプリ開発計画書](ローカル検針管理アプリ開発計画書.md) 5.1 章・5.2 章・8 章 Phase 2
- 前提: [Phase 1 計画](implemented/フェーズ1_sqlite3統合・コア業務ロジック移植計画.md) 完了済み
- GitHub: `sho-kawasaki-dd/meter-reading-app-mini`

## 1. 目的

PC からスマホ用の単一 HTML 検針シートを出力する機能を作る。スマホ側の入力・自動保存・途中バックアップ・結果出力も作る。結果は `MR1:` テキストまたは結果 JSON で戻し、Python パーサを通して `MeterReadingSyncService` で保存する。この往復を Playwright(Chromium、`file:` URL)で検証する。

あわせて [mobile_reading.py](../../src/meter_reading_mini/core/mobile_reading.py) の coverage 除外を解除し、パッケージ全体で 90% 以上を満たす。

## 2. 現状(2026-10-27 調査)

- [mobile_reading.py](../../src/meter_reading_mini/core/mobile_reading.py) にあるもの:
  - MR1 のエンコード / 抽出
  - 下書き形式の拒否
  - `validate_sheet_id` / `validate_note` / `safe_json_for_script`
- まだないもの: HTML 生成、結果ペイロードの検証、`MeterReadingSyncRequest` への変換。テストは [test_mobile_reading_envelope.py](../../tests/test_mobile_reading_envelope.py) だけ。
- `format_sheet_timestamps()` が `to_local()` を tz 指定なしで呼んでいる(Phase 1 D5 の「tz を必ず明示」に反する)。
- [work_package_download.py](../../src/meter_reading_mini/core/work_package_download.py) の `WorkPackageDownloadService`:
  - `open` の枠しか返さない。`PlannedReadingTask` にも `task_state` フィールドがない。
  - 当日パスは `release_date` 基準で、翌日 00:00 枠(配信オフセット -1)も含む。
- `list_meter_master_data()` と `list_measurement_definition_data()` は `WHERE is_active = 1` のマスタしか返さない。また DB スキーマ上 `location_name` は `physical_meters` にしか存在しないため、無効化済みマスタ(`is_active = 0`)の `auto_closed` 枠をシートに含める際は、対象枠が参照するマスタを無効化済みでも取得する必要がある。
- `list_measurement_definition_data()` の `last_reading` は「現在時点の最新実測(現行メーター)」で、予定枠ごとの前回値ではない。過去日のシートや交換直後の新メーターでは不正確になる。
- 前回値の正しい判定は `ReadingWriteOrchestrationService._get_calculation_context` にある(交換境界があれば前回実測を使わない)。
- 既存の使用量計算は通常の `Decimal` 演算を使い、計算精度の明示指定と業務数値の総桁数上限はない。既定精度 28 桁を超える計算では丸めが発生し得るため、BigInt による正確な演算との一致には Python 側の精度管理も必要になる。
- `ReadingWriteOrchestrationService.upsert_reading` / `SQLiteRepository.upsert_reading` には、シート出力後に交換・スケジュール再構成・定義無効化で `task_state = 'superseded'` となった予定枠への結果書込を拒否する検査がまだない。
- 移植元の参照実装: リポジトリ直下の `mobile_stocktake.py`(在庫アプリの棚卸シート、約 2,800 行の f-string テンプレート)。**Git 管理外(未追跡)** である。
  - IndexedDB のリビジョン管理・競合検出・途中バックアップ・コピー処理を含む。
  - CSS / JavaScript が `{{ }}` でエスケープされているため、移植時にエスケープを外す。
- CI は Windows / Ubuntu ともに Playwright Chromium を導入済み([ci.yml](../../.github/workflows/ci.yml))。
- [pyproject.toml](../../pyproject.toml) の coverage `omit` に `core/mobile_reading.py` が残っている。

## 3. 決定事項(2026-10-27 確認済み)

| # | 論点 | 決定 |
| --- | --- | --- |
| D1 | シートに含める枠とマスタ | 対象 `business_date` の `open` と `auto_closed` の枠。`completed` と `superseded` は含めない。`release_date` は条件に使わない(後日入力に対応するため `auto_closed` を含める)。対象 `slots` が参照する測定定義・物理メーター(`meter_name`・`meter_serial` を含む)・場所名(`location_name`)は、マスタが論理削除済み(`is_active = 0`)であっても欠落なくシートデータに含める |
| D2 | 枠ごとの初期前回値 | PC 側で枠ごとに `scheduled_at, planned_reading_id` より前の同じ測定定義・メーターの実測値を求め、読値と参照元の `scheduled_at`・`planned_reading_id` をシートに埋め込む。交換境界があれば `new_initial_reading` を使い、未指定なら「初回使用量 0」を示す印を付ける。`_get_calculation_context` と同じ判定を契約外の core モジュールに共通化して使う(契約モジュールの独立性を維持する) |
| D3 | 資産の配置 | `core/mobile_reading_assets/`(`sheet.html`、`sheet.css`、`sheet.js`)。`importlib.resources` で読み込み、1 ファイルにまとめる。データは `<script type="application/json">` に `safe_json_for_script` で埋め込む。PyInstaller へのデータ指定は Phase 5 で行う |
| D4 | 参照実装と削除方針 | `mobile_stocktake.py` から IndexedDB リビジョン管理・競合検出・途中バックアップ・コピー処理の設計を移植する。**ファイルは未追跡のためコミットしない。Phase 2 の実装完了時(完了記録 PR)にファイルを削除する**(5.5 参照) |
| D5 | ピルタブの構成と初期選択 | 日次枠(`daily_1x` / `daily_2x` / `daily_3x`)は `reading_session` ごとの時間タブ(`[08:00]` 等)に振り分け、非日次枠(`weekly` / `monthly` / `every_two_months`)は `[週次・月次・隔月]` タブにまとめる(`[すべて]` は全枠)。初期選択は未消化の枠が残る最も早い日次検針枠とし、日次枠がすべて消化済みで非日次の未消化枠があれば `[週次・月次・隔月]`、全部消化済みなら `[すべて]` とする |
| D6 | 発行済みシートの記録と状態照合 | シート自体は DB に記録しない(スキーマ変更なし)。PC 側の検証は `sheet_id` の形式と、各予定枠の存在・状態の照合で行う。シート出力後に交換・スケジュール再構成・定義無効化等で `task_state = 'superseded'` となった予定枠への結果保存は `DomainError` 派生例外で拒否する(`open` / `auto_closed` / `completed` は許容) |
| D7 | PR の分け方 | 4 PR(2a〜2d)と完了記録 PR |
| D8 | 時刻 | `format_sheet_timestamps` は業務タイムゾーン(`tzinfo`)を必須引数で受け取る。シートの出力日時は UTC 文字列(`generated_at`)と業務タイムゾーン基準のローカル表示文字列(`generated_at_local`)の両方を埋め込み、ブラウザの OS タイムゾーンに依存せず表示できるようにする |
| D9 | Python / JavaScript の正確な小数計算 | 数値は文字列で持つ。JavaScript は BigInt の固定小数点演算(加減算・乗算・比較)を使い、`Number` / `parseFloat` を業務計算に使わない。Python は演算対象の桁数・小数桁数から必要な精度を確保した局所的な Decimal コンテキストを使い、減算・ロールオーバー補正・乗算の途中を含めて丸めなしに計算する。既存の入力範囲を狭める桁数上限は追加しない。加減算は両辺の小数桁数の最大、乗算は両辺の小数桁数の和を結果のスケールとし、有限の plain decimal 文字列として Python / JavaScript の結果を一致させる。共通の使用量計算へ適用し、PC 直接入力・スマホ同期・交換境界・後続再計算で同じ規則を使う |
| D10 | ファイル名と周期区分 | `cycle_groups` の語彙は `"daily"`(`daily_1x` / `daily_2x` / `daily_3x` を束ねる)・`"weekly"`・`"monthly"`・`"every_two_months"` の 4 区分とする。<br>全枠: `meter_reading_YYYYMMDD_HHMMSS.html`<br>単一検針枠: `meter_reading_YYYYMMDD_0800_HHMMSS.html`<br>単一周期区分のみで絞り込み: `meter_reading_YYYYMMDD_weekly_HHMMSS.html` 等<br>複数条件: `meter_reading_YYYYMMDD_filtered_HHMMSS.html`<br>日付部は対象業務日、時刻部は出力時のローカル時刻 |
| D11 | プレビュー計算 | 確定前の差分プレビュー(正本 5.2.3)用の Service は Phase 4 で作る。Phase 2 はパース → 保存要求の構築 → `sync` の往復までとする |
| D12 | 同日先行枠からの動的引き継ぎ | 同じ `measurement_definition_id` かつ同じ `meter_id` の、DB 由来の初期前回実測とシート内の確認済みで正常な同日先行実測を候補とし、対象枠より前で `scheduled_at, planned_reading_id` が最大の候補を採用する。シートに含まれない `completed` 枠の実測を、より古いシート内の入力で上書きしない。別 `meter_id` へは引き継がない。実測候補がなければシート出力時の交換初期値または初回使用量 0 の扱いに戻す。シート内実測を引き継ぐ場合は `previous_source` が `replacement_no_initial` や `none` でも通常の差分計算に切り替える。先行枠の変更・確認解除時は依存する後続枠の前回値・参考使用量を再計算し、確認状態(`confirmed`)も連動して解除する |

## 4. データ形式(v1)

**シートデータ**(HTML に埋め込む)

```json
{"kind":"mrm-meter-reading-sheet","v":1,"sheet_id":"<32hex>",
 "business_date":"2026-10-27","generated_at":"2026-10-27 11:00:00",
 "generated_at_local":"2026-10-27 20:00:00",
 "filter":{"reading_sessions":["08:00"]|null,
   "cycle_groups":["daily"|"weekly"|"monthly"|"every_two_months"]|null},
 "locations":[{"location_id","location_name"}],
 "meters":[{"meter_id","location_id","location_name","meter_name","category","meter_serial"|null}],
 "definitions":[{"measurement_definition_id","location_id","display_name","category","unit",
   "value_schema","multiplier","decimal_places","warning_threshold","rollover_value",
   "calculation_method"}],
 "slots":[{"planned_reading_id","measurement_definition_id","meter_id","reading_session",
   "scheduled_at","reading_cycle","task_state",
   "initial_last_reading":"123.4"|null,
   "initial_previous_scheduled_at":"2026-10-27 05:00:00"|null,
   "initial_previous_planned_reading_id":"<planned_reading_id>"|null,
   "previous_source":"measured"|"replacement_initial"|"replacement_no_initial"|"none"}]}
```

- `previous_source = "measured"` の場合、`initial_last_reading` と参照元の `initial_previous_scheduled_at`・`initial_previous_planned_reading_id` は必須とする。参照元は同じ測定定義・メーターに属し、対象枠より前でなければならない。
- `initial_previous_scheduled_at` は `scheduled_at` と同じ UTC 秒単位の形式を使う。`previous_source` が実測以外の場合は、参照元の 2 項目をともに `null` とする。`replacement_initial` は交換初期値を持ち、`replacement_no_initial` / `none` の `initial_last_reading` は `null` とする。

**下書き(IndexedDB と途中経過 JSON で共通)**

- 形式: `{"kind":"mrm-meter-reading-draft","v":1, sheet_id, business_date, generated_at, filter, slots:[...]}`
- 各枠に持たせる項目:
  - `planned_reading_id`、`measurement_definition_id`、`meter_id`、`initial_last_reading`、`previous_source`(照合用)
  - `initial_previous_scheduled_at`、`initial_previous_planned_reading_id`(初期前回実測の参照元照合用)
  - `result_status`、`raw`、`pf_percent`、`pf_phase`、`note`
  - `confirmed`、`bad_input`、`recorded_at`、`client_updated_at`
- IndexedDB:
  - DB `mrm_meter_reading`、版数 1、ストア `drafts`、キー `sheet_id`
  - 1 レコードは `{sheet_id, storage_revision, draft}`

**結果(MR1 テキストと JSON ファイルで同じ内容)**

```json
{"kind":"mrm-meter-reading-result","v":1,"sheet_id","business_date","generated_at","filter",
 "entries":[{"planned_reading_id","result_status","raw_reading"|null,
   "structured_payload":{"percent","phase"}|null,"recorded_at","client_updated_at","note"|null}]}
```

- `entries` には確認済みで正常な枠だけを入れる(1 件以上)。
- トップレベルの `kind`・`v`・`sheet_id`・`entries` は必須とし、`business_date`・`generated_at`・`filter` はシートヘッダ値(省略可)を受け入れるが、未知キーは拒否する。
- `result_status` に `replaced` は使わない。
- 時刻はオフセット付き ISO 8601 とし、PC 側で UTC 秒単位に正規化する。
- `kind` が結果 / 下書きのどちらでもない JSON は、PC 側で拒否する。

## 5. 実装タスク

各タスクは、完了を検証してから `[x]` にする。ファイルが存在するだけでは完了としない。

### 5.1 PR-2a `feature/phase2-sheet-generation-parser`: シート生成と結果パーサ(Python)

- [x] `PlannedReadingTaskQueryRepository` に、業務日と状態(`open` / `auto_closed`)を指定してシート用の予定枠(`task_state: PlannedReadingTaskState` を含むレコード)を取得するメソッドを追加し、sqlite3 で実装する。ORDER BY は既存の一覧取得と同じにする。あわせて、対象枠が参照する測定定義・物理メーター・場所名(`location_name`)を、マスタが論理削除済み(`is_active = 0`)の場合も含めて取得できるようにする(D1)
- [x] D2 の前回値判定を契約モジュール(`contract_models.py`、`errors.py`、`meter_reading_sync_repositories.py`)以外の core モジュールに共通の関数(または Service)として切り出し、オーケストレーションとシート生成の両方で使う。次のケースを単体テストで確認する:
  - 実測あり
  - 交換初期値あり
  - 交換初期値なし
  - 実測なし
  - 同日の先行枠が `completed`(読値と参照元の予定日時・枠 ID がそろうこと)
  - 実測以外の前回値では参照元の 2 項目がともに `null` となること
- [x] D9 に従い、共通の使用量計算を局所的な Decimal コンテキストで丸めなしに計算する方式へ変更する
  - 必要精度は演算対象の整数部・小数部の桁数と桁上がりから算出し、減算・ロールオーバー補正・乗算の途中結果も正確に保持する。グローバルな精度は変更しない
  - 加減算・乗算のスケールを D9 に統一し、保存時も有限の plain decimal 文字列とする
  - 28 桁境界とそれを超える値、大きな倍率、末尾ゼロ、小数桁数の異なる減算、ロールオーバー補正を単体テストで確認する。呼出元の Decimal 精度を変えても同じ結果になることを確認する
  - PC 直接入力・スマホ同期・交換境界・後続再計算の既存テストを実行し、通常桁数での既存結果とトランザクション整合性を維持する
- [x] D6 に従い、`task_state == 'superseded'` の予定枠に対する結果保存(`ReadingWriteOrchestrationService.upsert_reading`)を `DomainError` 派生例外で拒否し、スマホ同期(`MeterReadingSyncService.sync`)では当該 SAVEPOINT だけ戻して `rejected_reading_items` に記録することを単体・結合テストで確認する
- [x] `MobileReadingSheetService.create_sheet(target_business_date, *, reading_sessions, cycle_groups)` を作る
  - 補完 → 自動クローズ → 枠取得 → 前回値の算出を 1 つのトランザクションにまとめ、Service がトランザクションを持つ
  - 未来日の指定と対象 0 件は `DomainError` 派生例外で拒否する
  - 時計と `sheet_id` 生成(`secrets.token_hex(16)`)は外から注入できるようにする
  - 戻り値: `MobileReadingSheet(sheet_id, html, filename, slot_count)`
- [x] `render_sheet_html(sheet_data)` を作り、D3 の資産をまとめて 1 つの HTML にする。外部 URL・`localStorage`・`</script>` の混入がないことをテストする
- [x] `format_sheet_timestamps` を tz 必須の引数に変える(D8)
- [x] `parse_mobile_reading_result(raw_text) -> MobileReadingResult` を作る。検証内容:
  - `kind` と `v`(トップレベルの未知キーは拒否し、`business_date`・`generated_at`・`filter` は任意で保持する)
  - `sheet_id`
  - `entries` が空でないこと、`planned_reading_id` が重複しないこと
  - `MeterReadingSubmission` として正しいこと
  - `replaced` を拒否すること
  - 備考が `validate_note` を通ること
  - 下書き形式と未対応の版数を拒否すること
- [x] `MobileReadingResult.to_sync_request()` と、`ReadingConfirmationContext` を使った保存を、`:memory:` DB の結合テストで確認する。確認する点:
  - `device_id = sheet_id`
  - `result_source = 'mobile_html'`
  - 署名がある
  - `auto_closed` の枠が `completed` になる
  - `superseded` の枠が拒否される
- [x] `test_mobile_reading_envelope.py` を `tests/test_mobile_reading.py` に統合する。既存の MR1 テストは結果 `kind` 付きのペイロードに合わせて直す
- [x] [test_contract_boundaries.py](../../tests/test_contract_boundaries.py) に `core/mobile_reading_assets/` の資産読込と静的検査を追加し、契約モジュールの独立性、`PySide6` 非依存、`datetime.now` 不使用に加えて、資産内に外部 URL(`http://`、`https://`)や `localStorage` が含まれないことを確認する

### 5.2 PR-2b `feature/phase2-sheet-ui-input`: 画面と入力(HTML/JS)

- [ ] `sheet.html`、`sheet.css`、`sheet.js` を作る。`<noscript>` と冒頭の表示で「Chrome で開いてください」と案内する
- [ ] 検針時間・周期のピルタブを作る。日次枠は `reading_session` ごとの時間タブ、非日次枠(`weekly` / `monthly` / `every_two_months`)は `[週次・月次・隔月]` タブに振り分け、消化件数バッジを付けて上部に固定する。初期選択は D5 に従う
- [ ] `location_id` ごとのセクションを作る(Sticky 見出し・開閉・完了バッジ)。並び順はカテゴリ固定順(`electricity` → `gas` → `water` → その他)、その次に `display_name` 昇順。`location_id` 未設定は末尾の「場所未設定」にまとめる。場所ドロップダウンと「未入力のみ」トグルを付ける
- [ ] カードの表示と入力欄を作る
  - 測定定義名(`display_name`)・単位・物理メーター情報(`meter_name`・`meter_serial`)・前回値の表示
  - 状態 4 種(`measured` / `skipped` / `unreachable` / `meter_fault`)
  - 数値(非負の plain decimal 文字列、`decimal_places` 以下の小数桁数、および `rollover_value` 設定時は `0 <= raw_reading < rollover_value` で検証。`rollover_value` 同値は不可)
  - 力率(`%` は 0〜100 の非負 plain decimal、位相 `lag` / `lead` / `unity` を選択。`100%` のときは `unity` に固定し、`100%` 未満のときは `unity` を選択不可とする。`100%` から `100%` 未満へ変更した際も `unity` のままでは確認済みにできない)
  - 備考(500 文字以内)
  - 確認ボタン、「次へ」で同じ場所の次の未入力へ移動
- [ ] D9 の BigInt 小数で参考使用量を計算する
  - `raw_only` のとき
  - 加減算のスケールは両辺の小数桁数の最大とし、途中結果を丸めない
  - 倍率を掛けるとき(スケールは差分と `multiplier` の小数桁数の和)
  - ロールオーバー補正
  - ロールオーバー未設定で負の差分になるときの警告
  - 閾値超過の警告
  - 交換初期値がない初回(`replacement_no_initial`)および前回実測なし(`none`)は 0
  - 警告は現場の実数記録を妨げない
- [ ] 同日先行枠の値を引き継ぐ(D12)
  - 同じ `measurement_definition_id` かつ同じ `meter_id` の DB 由来の初期前回実測と、確認済みで正常な同日先行実測を比較し、対象枠より前で `scheduled_at, planned_reading_id` が最大の候補を前回値とする(交換前後の別 `meter_id` 間では引き継がない)
  - DB 由来の候補には `initial_previous_scheduled_at`・`initial_previous_planned_reading_id` を使い、より古いシート内の入力では置き換えない
  - 引き継ぎ中は、シート出力時の `previous_source` が `replacement_no_initial` や `none`(初回使用量 0)であっても、先行枠の実測値を前回値とする通常の差分計算に切り替える
  - 先行枠が未入力・非実測・未確認ならその枠を候補から外し、DB 由来の初期前回実測と残る確認済み実測から再選択する。実測候補がない場合だけ、シート出力時の交換初期値または初回使用量 0 の扱いに戻す
  - 先行枠の測定値・状態・備考が変更されて確認が解除された場合、それを引き継いでいた後続枠の前回値・参考使用量を再計算するとともに、後続枠の確認状態(`confirmed`)も連動して解除する
- [ ] 測定値・状態・備考を変えたら確認を解除する。業務条件を満たさない枠は確認済みにしない
- [ ] Playwright の基盤を `tests/conftest.py` に置く
  - Chromium をセッション単位で起動する
  - `file:` 以外のリクエストを遮断して記録する
  - `pageerror` と `console.error` を集める
  - `localStorage` にアクセスしたら検出する
- [ ] `tests/test_mobile_reading_browser.py` でブラウザ操作を確認する
  - タブの切替、グループ化、絞り込み
  - 同日先行枠からの引き継ぎ
  - 08:00 が未入力、14:00 が `completed` で読値 200、22:00 が未入力のシートで、08:00 に 150、22:00 に 250 を確認入力しても、22:00 の前回値は 200、参考使用量は倍率 1 で 50 となること
  - 同じ予定日時の候補は `planned_reading_id` 順で判定し、引き継いだ先行枠の確認解除時も DB 由来の候補を含めて再選択されること
  - 数値と力率の入力
  - 警告表示
  - スマホ縦(360×640)・スマホ横(640×360)・PC(1366×768)で全操作に到達できること

### 5.3 PR-2c `feature/phase2-indexeddb-draft`: 自動保存と途中経過 JSON

- [ ] `mobile_stocktake.py` の設計を参照して IndexedDB 層を実装する(コードは `sheet.js` へ書き直し、参照元ファイルは変更・コミットしない)
  - 接続、`onversionchange` と `close` の処理、タイムアウト
  - `storage_revision` を使った楽観的な書込
  - 保存キュー。別タブとの競合検出用の `storage_revision` とは別に、同一タブの最新編集世代と保存完了世代を管理し、各書込に保存対象の編集世代を対応付ける
  - 保存状態の表示(読込中・保存中・保存済み・保存失敗)。「保存済み」は最新編集世代に対応する書込トランザクションの `complete` を受け、保存完了世代が最新編集世代と一致した場合だけ表示する。古い世代の完了では「保存済み」にせず、未保存の最新変更があれば「保存中」を維持する。最新変更の保存失敗・競合を古い書込の完了で消さない
- [ ] 読込を確認できない、またはレコードが壊れている場合は、初期状態で自動上書きしない。再試行の手段と、明示的に置き換える手段を用意する
- [ ] 別タブとの競合(リビジョン不一致)では、自動マージ・自動上書きをせず、利用者に選ばせる
- [ ] 途中経過 JSON を作る
  - ファイル保存と、JSON 表示による手動コピー
  - 貼り付け・ファイルからの読込
  - 読込時に `sheet_id`・業務日・出力日時・条件・枠構成・初期前回値・初期前回実測の参照元日時と枠 ID の一致を確認し、利用者の確認後に全体を置き換える。IndexedDB からの復元でも同じ照合を行う
  - 置き換え後は引き継ぎ・参考使用量・警告を再計算する
  - 結果 JSON と MR1 は下書きとして拒否する
- [ ] ブラウザテストを追加する
  - 自動保存とリロード後の再開(入力途中・不正な入力を含む)
  - `indexedDB.open` の失敗と書込失敗を注入したときの保存失敗表示と、入力が残ること
  - 2 ページで同時に保存したときの競合
  - 同一タブで変更 A の書込完了を遅延させ、その間に変更 B を入力する。A の完了時は「保存中」のまま、B の完了時だけ「保存済み」となり、リロード後に B が復元されること
  - B の保存失敗・競合の通知を A の完了で消さず、B の入力内容と JSON による退避手段が残ること
  - 途中経過 JSON のファイル往復・テキスト往復
  - キャンセル・壊れた JSON・別シートの拒否
  - `window.indexedDB` がない環境での退避と回復

### 5.4 PR-2d `feature/phase2-result-roundtrip`: 結果出力・往復・品質ゲート

- [ ] 結果出力を作る
  - 確認済みの正常な枠が 1 件以上あれば出力できる。未確認の枠は含めない
  - JavaScript で MR1 テキストを作る: UTF-8 → 標準 Base64 → Base64 文字列の CRC-32(小文字 16 進 8 桁)
  - 結果 JSON をダウンロードできる
  - クリップボードへのコピーが失敗したら、テキストを全選択して表示し、手動コピーを案内する
  - ダウンロードしただけでは「保存成功」と表示しない
- [ ] 往復テストを作る
  1. JavaScript が出力した MR1 と JSON を取得する
  2. `parse_mobile_reading_result` で読む
  3. `MeterReadingSyncService.sync` で `tmp_path` のファイル DB に保存する
  4. Decimal の文字列・力率・備考(絵文字を含む)・`auto_closed` の復活・後続再計算が DB に反映されたことを確認する
  - D12 の途中に `completed` 枠があるケースでも、ブラウザの参考使用量と PC 側の確定使用量が一致することを確認する
  - D9 の共通テストケース(28 桁境界とそれを超える値、大きな倍率、末尾ゼロ、小数桁数の異なる減算、ロールオーバー補正)で、JavaScript の参考使用量・Python の計算結果・DB 保存値が plain decimal 文字列として一致することを確認する
  - あわせて、メールの引用・折り返しの許容、同じブロックの重複を 1 件にすること、異なるブロックの拒否、下書きの拒否を確認する
- [ ] [pyproject.toml](../../pyproject.toml) の coverage `omit` から `core/mobile_reading.py` を外す。6 章の全体・`core/` 単独・`db/` 単独の 3 つの閾値チェックがすべて成功することを確認する。個別 skip の追加や閾値の引き下げは行わない
- [ ] [ci.yml](../../.github/workflows/ci.yml) に `core/` 単独・`db/` 単独の coverage 閾値チェックを追加し、Windows / Ubuntu ともに全体・各層それぞれ 90% 以上を必須にする。片方の層が 90% 未満の場合は、全体が 90% 以上でも CI を失敗させる
- [ ] 外部通信がないこと、`localStorage` を使っていないこと、JavaScript エラーがないことを、全ブラウザテスト共通の後処理で確認する

### 5.5 完了記録 PR

- [ ] Windows / Ubuntu の必須チェック(`test (windows-latest)`・`test (ubuntu-latest)`)が成功していることを 9 章へ記録する
- [ ] D1〜D12 と 4 章のデータ形式を正本 5.2 章へ統合し、D9 の共通計算規則を正本 5.1 章にも反映する。正本 8 章の Phase 2 記述・品質ゲートと README の Phase 状況も更新する
- [ ] 実機確認(端末、Chrome の版、起動経路、ファイルを移動したときの復元可否)は Phase 4 以降の作業として正本に明記する。Phase 2 の完了扱いにはしない。在庫アプリの実機確認を検針アプリの実績と記載しない
- [ ] **参照実装 `mobile_stocktake.py` を削除する**(D4)
  - Phase 2 の実装(2a〜2d)がすべて main にマージされ、品質ゲートが成功したことを確認してから削除する
  - 未追跡ファイルなので `git rm` ではなくファイルを直接削除する。`git status` に残っていないことを確認する
  - 削除後は、リポジトリ内の文書・コードに `mobile_stocktake` への参照が残っていないことを `grep` で確認する(本計画書の記述を除く)
  - 実装中に誤ってコミットしないよう、PR ごとに `git status` / `git diff --cached --stat` でステージ内容を確認する
- [ ] 本計画書を完了(凍結)として `docs/plan/implemented/` へ `git mv` し、移動後の相対リンクを修正する

## 6. 品質ゲート

PR ごとにリポジトリ直下で実行する。Linux のヘッドレスでは `QT_QPA_PLATFORM=offscreen` を設定する。

```text
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pyright
uv run playwright install chromium
uv run pytest --cov
uv run coverage report --fail-under=90
uv run coverage report --include="*/meter_reading_mini/core/*" --fail-under=90
uv run coverage report --include="*/meter_reading_mini/db/*" --fail-under=90
```

- coverage は同じ計測結果に対して全体・`core/` 単独・`db/` 単独をそれぞれ判定する。全体の合計だけで層別の閾値を満たした扱いにしない。PR-2d で両 OS の CI にも層別チェックを追加する。
- Chromium がないことを理由にテストをスキップしない。
- 失敗や未実施の検査を成功として報告せず、実行した検査と未実施・失敗の理由を区別して記録する。
- 作業は `main` へ直接 push せず、feature ブランチの PR で CI 成功後にマージする。コミットは Conventional Commits、内容は日本語とする。

## 7. 完了条件

- [ ] 正本 8 章の Phase 2 完了条件にある全項目がブラウザテストで成功する
- [ ] JavaScript の出力 → Python パーサ → DB 保存の往復が成功する
- [ ] 途中に `completed` 枠がある場合の前回値選択、28 桁を超える丸めなし計算、最新編集世代に対応した保存表示の回帰テストが成功する
- [ ] coverage 除外が 0 件で、パッケージ全体・`core/`・`db/` がいずれも 90% 以上
- [ ] Windows / Ubuntu の CI が成功する
- [ ] `mobile_stocktake.py` が削除され、リポジトリ内に参照が残っていない(本計画書の履歴記述を除く)
- [ ] 文書(README・正本)が実態と一致している

## 8. 留意事項

- **file: URL の扱い:** Chromium の `file:` オリジンでは、同じブラウザコンテキストの 2 ページが IndexedDB を共有する。競合テストはこの前提で作る。
- **時計の固定:** ブラウザテストは `page.clock` で時刻を固定する。Python 側は時計を注入する。
- **テストデータ:** 架空のマスタだけを使い、出力した HTML・JSON は `tmp_path` に置く。業務情報を含む HTML・JSON・ログを Git / Release に含めない。
- **参照実装の扱い:** `mobile_stocktake.py` は在庫アプリ用の実装で、検針アプリの仕様(部分出力、前回値の引き継ぎ、小数計算)とは異なる。棚卸の「全件確認必須」ルールを流用しない。
- **配布前の扱い:** 作成済みの HTML は、アプリを更新しても移行しない(正本 5.2.2)。データ形式を変えるときは `v` を上げ、パーサは旧版をはっきり拒否する。
- **PyInstaller:** 資産ファイルの同梱指定は Phase 5 の範囲。Phase 2 では `importlib.resources` で読めることだけを保証する。

## 9. 完了記録

(PR ごとに追記し、Phase 2 完了時に集約する)

- PR-2a(2026-10-27、`feature/phase2-sheet-generation-parser`): Ruff check / format、Pyright(0 errors)、pytest 368 passed / 1 skipped、`core/`・`db/` coverage 95%(`--fail-under=90` 達成)を確認した。
  - 追加: `core/decimal_arithmetic.py`(D9 の丸めなし演算)、`core/previous_reading.py`(D2 の前回値判定、オーケストレーションと共用)、`core/mobile_reading_sheet.py`(`MobileReadingSheetService`)、`core/mobile_reading_assets/`(2b で差し替える最小の骨組み)。
  - `SupersededPlannedReadingError` を `core/errors.py` に追加し、`ReadingWriteOrchestrationService.upsert_reading` と `SQLiteRepository.upsert_reading` で拒否する。
  - `pyproject.toml` の coverage `omit` の解除は 5.4 の項目のため、この PR では行っていない。`mobile_reading.py` の単体 coverage は 99%。
