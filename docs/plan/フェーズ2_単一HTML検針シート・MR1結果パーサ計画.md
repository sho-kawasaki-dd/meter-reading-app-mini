# Phase 2 単一 HTML 検針シート・MR1 結果パーサ計画

- 作成日: 2026-10-27
- ステータス: 計画中
- 対象: リポジトリ直下を基点とする Phase 2(`src/`・`tests/`・`pyproject.toml`)
- 正本: [ローカル検針管理アプリ開発計画書](../ローカル検針管理アプリ開発計画書.md) 5.2 章・8 章 Phase 2
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
  - `open` の枠しか返さない。
  - 当日パスは `release_date` 基準で、翌日 00:00 枠(配信オフセット -1)も含む。
- `list_measurement_definition_data()` の `last_reading` は「現在時点の最新実測(現行メーター)」で、予定枠ごとの前回値ではない。過去日のシートや交換直後の新メーターでは不正確になる。
- 前回値の正しい判定は `ReadingWriteOrchestrationService._get_calculation_context` にある(交換境界があれば前回実測を使わない)。
- 移植元の参照実装: リポジトリ直下の `mobile_stocktake.py`(在庫アプリの棚卸シート、約 2,800 行の f-string テンプレート)。**Git 管理外(未追跡)** である。
  - IndexedDB のリビジョン管理・競合検出・途中バックアップ・コピー処理を含む。
  - CSS / JavaScript が `{{ }}` でエスケープされているため、移植時にエスケープを外す。
- CI は Windows / Ubuntu ともに Playwright Chromium を導入済み([ci.yml](../../.github/workflows/ci.yml))。
- [pyproject.toml](../../pyproject.toml) の coverage `omit` に `core/mobile_reading.py` が残っている。

## 3. 決定事項(2026-10-27 確認済み: D1〜D7 / 提案: D8〜D11)

| # | 論点 | 決定 |
| --- | --- | --- |
| D1 | シートに含める枠 | 対象 `business_date` の `open` と `auto_closed` の枠。`completed` と `superseded` は含めない。`release_date` は条件に使わない(後日入力に対応するため `auto_closed` を含める) |
| D2 | 枠ごとの初期前回値 | PC 側で枠ごとに `scheduled_at, planned_reading_id` より前の同じ測定定義・メーターの実測値を求め、シートに埋め込む。交換境界があれば `new_initial_reading` を使い、未指定なら「初回使用量 0」を示す印を付ける。`_get_calculation_context` と同じ判定を共通化して使う |
| D3 | 資産の配置 | `core/mobile_reading_assets/`(`sheet.html`、`sheet.css`、`sheet.js`)。`importlib.resources` で読み込み、1 ファイルにまとめる。データは `<script type="application/json">` に `safe_json_for_script` で埋め込む。PyInstaller へのデータ指定は Phase 5 で行う |
| D4 | 参照実装と削除方針 | `mobile_stocktake.py` から IndexedDB リビジョン管理・競合検出・途中バックアップ・コピー処理の設計を移植する。**ファイルは未追跡のためコミットしない。Phase 2 の実装完了時(完了記録 PR)にファイルを削除する**(5.5 参照) |
| D5 | ピルタブの初期選択 | 未消化の枠が残る最も早い検針枠。全部消化済みなら [すべて] |
| D6 | 発行済みシートの記録 | DB には記録しない(スキーマ変更なし)。PC 側の検証は `sheet_id` の形式と、各予定枠の存在・状態の照合で行う |
| D7 | PR の分け方 | 4 PR(2a〜2d)と完了記録 PR |
| D8 | 時刻(提案) | `format_sheet_timestamps` は業務タイムゾーンを引数で受け取る。シートの出力日時は UTC 文字列で埋め込み、表示はローカル時刻にする |
| D9 | JavaScript の小数計算(提案) | 数値は文字列で持つ。BigInt を使った固定小数点演算(加減算・乗算・比較)を `sheet.js` に実装し、`Number` / `parseFloat` を業務計算に使わない |
| D10 | ファイル名(提案) | 全枠: `meter_reading_YYYYMMDD_HHMMSS.html`<br>単一検針枠: `meter_reading_YYYYMMDD_0800_HHMMSS.html`<br>周期だけで絞り込み: `_weekly_` 等<br>複数条件: `_filtered_`<br>日付部は対象業務日、時刻部は出力時のローカル時刻 |
| D11 | プレビュー計算(提案) | 確定前の差分プレビュー(正本 5.2.3)用の Service は Phase 4 で作る。Phase 2 はパース → 保存要求の構築 → `sync` の往復までとする |

D8〜D11 は提案であり、確認後に確定する。

## 4. データ形式(v1)

**シートデータ**(HTML に埋め込む)

```json
{"kind":"mrm-meter-reading-sheet","v":1,"sheet_id":"<32hex>",
 "business_date":"2026-10-27","generated_at":"2026-10-27 11:00:00",
 "filter":{"reading_sessions":["08:00"]|null,"cycle_groups":["daily"]|null},
 "locations":[{"location_id","location_name"}],
 "definitions":[{"measurement_definition_id","location_id","display_name","category","unit",
   "value_schema","multiplier","decimal_places","warning_threshold","rollover_value",
   "calculation_method"}],
 "slots":[{"planned_reading_id","measurement_definition_id","meter_id","reading_session",
   "scheduled_at","reading_cycle","task_state",
   "initial_last_reading":"123.4"|null,
   "previous_source":"measured"|"replacement_initial"|"replacement_no_initial"|"none"}]}
```

**下書き(IndexedDB と途中経過 JSON で共通)**

- 形式: `{"kind":"mrm-meter-reading-draft","v":1, sheet_id, business_date, generated_at, filter, slots:[...]}`
- 各枠に持たせる項目:
  - `planned_reading_id`、`measurement_definition_id`、`initial_last_reading`(照合用)
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
- `result_status` に `replaced` は使わない。
- 時刻はオフセット付き ISO 8601 とし、PC 側で UTC 秒単位に正規化する。
- `kind` が結果 / 下書きのどちらでもない JSON は、PC 側で拒否する。

## 5. 実装タスク

各タスクは、完了を検証してから `[x]` にする。ファイルが存在するだけでは完了としない。

### 5.1 PR-2a `feature/phase2-sheet-generation-parser`: シート生成と結果パーサ(Python)

- [ ] `PlannedReadingTaskQueryRepository` に、業務日と状態(`open` / `auto_closed`)を指定して枠を取得するメソッドを追加し、sqlite3 で実装する。ORDER BY は既存の一覧取得と同じにする
- [ ] D2 の前回値判定を共通の関数(または Service)に切り出し、オーケストレーションとシート生成の両方で使う。次のケースを単体テストで確認する:
  - 実測あり
  - 交換初期値あり
  - 交換初期値なし
  - 実測なし
  - 同日の先行枠が `completed`
- [ ] `MobileReadingSheetService.create_sheet(target_business_date, *, reading_sessions, cycle_groups)` を作る
  - 補完 → 自動クローズ → 枠取得 → 前回値の算出を 1 つのトランザクションにまとめ、Service がトランザクションを持つ
  - 未来日の指定と対象 0 件は `DomainError` 派生例外で拒否する
  - 時計と `sheet_id` 生成(`secrets.token_hex(16)`)は外から注入できるようにする
  - 戻り値: `MobileReadingSheet(sheet_id, html, filename, slot_count)`
- [ ] `render_sheet_html(sheet_data)` を作り、D3 の資産をまとめて 1 つの HTML にする。外部 URL・`localStorage`・`</script>` の混入がないことをテストする
- [ ] `format_sheet_timestamps` を tz 必須の引数に変える(D8)
- [ ] `parse_mobile_reading_result(raw_text) -> MobileReadingResult` を作る。検証内容:
  - `kind` と `v`
  - `sheet_id`
  - `entries` が空でないこと、`planned_reading_id` が重複しないこと
  - `MeterReadingSubmission` として正しいこと
  - `replaced` を拒否すること
  - 備考が `validate_note` を通ること
  - 下書き形式と未対応の版数を拒否すること
- [ ] `MobileReadingResult.to_sync_request()` と、`ReadingConfirmationContext` を使った保存を、`:memory:` DB の結合テストで確認する。確認する点:
  - `device_id = sheet_id`
  - `result_source = 'mobile_html'`
  - 署名がある
  - `auto_closed` の枠が `completed` になる
- [ ] [test_mobile_reading_envelope.py](../../tests/test_mobile_reading_envelope.py) を `tests/test_mobile_reading.py` に統合する。既存の MR1 テストは結果 `kind` 付きのペイロードに合わせて直す
- [ ] 依存境界テストに `mobile_reading.py` と資産の読込を含め、PySide6 に依存しないこと、`datetime.now` を使わないことを確認する

### 5.2 PR-2b `feature/phase2-sheet-ui-input`: 画面と入力(HTML/JS)

- [ ] `sheet.html`、`sheet.css`、`sheet.js` を作る。`<noscript>` と冒頭の表示で「Chrome で開いてください」と案内する
- [ ] 検針時間・周期のピルタブを作る。消化件数バッジを付け、上部に固定し、初期選択は D5 に従う
- [ ] `location_id` ごとのセクションを作る(Sticky 見出し・開閉・完了バッジ)。並び順はカテゴリ固定順(`electricity` → `gas` → `water` → その他)、その次に `display_name` 昇順。`location_id` 未設定は末尾の「場所未設定」にまとめる。場所ドロップダウンと「未入力のみ」トグルを付ける
- [ ] カードの入力欄を作る
  - 状態 4 種(`measured` / `skipped` / `unreachable` / `meter_fault`)
  - 数値(`decimal_places` と `rollover_value` の上限で検証)
  - 力率(`%` は 0〜100、位相 `lag` / `lead` / `unity` を選択。100% のときは `unity` に固定)
  - 備考(500 文字以内)
  - 確認ボタン、「次へ」で同じ場所の次の未入力へ移動
- [ ] D9 の BigInt 小数で参考使用量を計算する
  - `raw_only` のとき
  - 倍率を掛けるとき
  - ロールオーバー補正
  - ロールオーバー未設定で負の差分になるときの警告
  - 閾値超過の警告
  - 交換初期値がない初回は 0
  - 警告は現場の実数記録を妨げない
- [ ] 同日先行枠の値を引き継ぐ
  - 同じ測定定義の先行枠が `measured` で確認済みの正常値なら、後続枠の前回値と参考使用量を更新する
  - 未入力や非実測なら、さらに前の枠またはシート出力時の前回値に戻す
- [ ] 測定値・状態・備考を変えたら確認を解除する。業務条件を満たさない枠は確認済みにしない
- [ ] Playwright の基盤を `tests/conftest.py` に置く
  - Chromium をセッション単位で起動する
  - `file:` 以外のリクエストを遮断して記録する
  - `pageerror` と `console.error` を集める
  - `localStorage` にアクセスしたら検出する
- [ ] `tests/test_mobile_reading_browser.py` でブラウザ操作を確認する
  - タブの切替、グループ化、絞り込み
  - 同日先行枠からの引き継ぎ
  - 数値と力率の入力
  - 警告表示
  - スマホ縦(360×640)・スマホ横(640×360)・PC(1366×768)で全操作に到達できること

### 5.3 PR-2c `feature/phase2-indexeddb-draft`: 自動保存と途中経過 JSON

- [ ] `mobile_stocktake.py` の設計を参照して IndexedDB 層を実装する(コードは `sheet.js` へ書き直し、参照元ファイルは変更・コミットしない)
  - 接続、`onversionchange` と `close` の処理、タイムアウト
  - `storage_revision` を使った楽観的な書込
  - 保存キュー
  - 保存状態の表示(読込中・保存中・保存済み・保存失敗)。「保存済み」は書込トランザクションの `complete` を受けてから表示する
- [ ] 読込を確認できない、またはレコードが壊れている場合は、初期状態で自動上書きしない。再試行の手段と、明示的に置き換える手段を用意する
- [ ] 別タブとの競合(リビジョン不一致)では、自動マージ・自動上書きをせず、利用者に選ばせる
- [ ] 途中経過 JSON を作る
  - ファイル保存と、JSON 表示による手動コピー
  - 貼り付け・ファイルからの読込
  - 読込時に `sheet_id`・業務日・出力日時・条件・枠構成・初期前回値の一致を確認し、利用者の確認後に全体を置き換える
  - 置き換え後は引き継ぎ・参考使用量・警告を再計算する
  - 結果 JSON と MR1 は下書きとして拒否する
- [ ] ブラウザテストを追加する
  - 自動保存とリロード後の再開(入力途中・不正な入力を含む)
  - `indexedDB.open` の失敗と書込失敗を注入したときの保存失敗表示と、入力が残ること
  - 2 ページで同時に保存したときの競合
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
  - あわせて、メールの引用・折り返しの許容、同じブロックの重複を 1 件にすること、異なるブロックの拒否、下書きの拒否を確認する
- [ ] [pyproject.toml](../../pyproject.toml) の coverage `omit` から `core/mobile_reading.py` を外す。`uv run coverage report --fail-under=90` が通り、`core/`・`db/` もそれぞれ 90% 以上であることを確認する。個別 skip の追加や閾値の引き下げは行わない
- [ ] 外部通信がないこと、`localStorage` を使っていないこと、JavaScript エラーがないことを、全ブラウザテスト共通の後処理で確認する

### 5.5 完了記録 PR

- [ ] Windows / Ubuntu の必須チェック(`test (windows-latest)`・`test (ubuntu-latest)`)が成功していることを 9 章へ記録する
- [ ] D1〜D11 と 4 章のデータ形式を正本 5.2 章へ統合する。正本 8 章の Phase 2 記述と README の Phase 状況も更新する
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
```

- Chromium がないことを理由にテストをスキップしない。
- 失敗や未実施の検査を成功として報告せず、実行した検査と未実施・失敗の理由を区別して記録する。
- 作業は `main` へ直接 push せず、feature ブランチの PR で CI 成功後にマージする。コミットは Conventional Commits、内容は日本語とする。

## 7. 完了条件

- [ ] 正本 8 章の Phase 2 完了条件にある全項目がブラウザテストで成功する
- [ ] JavaScript の出力 → Python パーサ → DB 保存の往復が成功する
- [ ] coverage 除外が 0 件で、パッケージ全体・`core/`・`db/` がいずれも 90% 以上
- [ ] Windows / Ubuntu の CI が成功する
- [ ] `mobile_stocktake.py` が削除され、リポジトリ内に参照が残っていない
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
