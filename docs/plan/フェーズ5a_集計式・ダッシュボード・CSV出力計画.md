# Phase 5a 集計式・ダッシュボード・CSV 出力計画

- 作成日: 2026-11-06
- ステータス: 作業中
- 対象: リポジトリ直下を基点とする Phase 5a (`src/`・`tests/`・`docs/`)
- 正本: [ローカル検針管理アプリ開発計画書](ローカル検針管理アプリ開発計画書.md) 4・5・6.1・8 章 Phase 5
- 前提: [Phase 4 計画](implemented/フェーズ4_スマホ検針ダイアログ・差分プレビュー・一括確定計画.md) は完了済み
- 分割: PR-5a-1 スキーマ v4・集計式マスタ / PR-5a-2 集計計算 / PR-5a-3 ダッシュボード・CSV / PR-5a-4 完了記録
- 後続: 正本 8 章の Phase 5 を 5a (本計画)・5b (バックアップ・復元 UI)・5c (配布) に分ける。5b は本計画のスキーマ v4 と整合性検査に依存する。5b・5c は着手時に別の計画書を作る

## 1. 目的

確定済みの検針結果から、測定定義ごとの使用量と、複数の測定定義を組み合わせた使用量を集計する。表示はダッシュボード、出力は CSV / PNG で行う。

例: オーナー使用量 = 引込電力量計 − (ホテル電灯電力量計 + ホテル動力電力量計)

## 2. 現状と不足

- 測定定義ごとの `usage_delta` と交換境界の `old_meter_usage_delta` は保存済みだが、集計 Service と画面はない。
- 測定定義を組み合わせる集計式を表すマスタがない。
- `DashboardWindow` は正本 6.1 に定義済みだが、未実装のため MainWindow では表示していない。
- 業務整合性検査 (`db/integrity.py`) は集計式を対象にしていない。

## 3. 決定事項

| # | 論点 | 決定 |
| --- | --- | --- |
| D1 | 集計式の保持 | 集計式マスタ (スキーマ v4) を追加する。集計結果は保存せず、集計のたびに検針結果から計算する |
| D2 | 式の表現 | 測定定義 × Decimal 係数の線形和とする。差し引く項目は負の係数で表す。式の中で別の式は参照しない |
| D3 | 式の変更履歴 | スケジュール履歴と同じく適用開始日つきの版で保持する。過去日を適用開始日とする変更は拒否する |
| D4 | 項目にできる測定定義 | `calculation_method = 'delta'`、`value_schema = 'decimal_string'`、単位とカテゴリが式と一致、同じ周期グループの定義に限る。保存時と集計時の両方で検証する |
| D5 | 周期グループ | 日次 (`daily_1x`/`daily_2x`/`daily_3x`)・週次 (`weekly`)・月次 (`monthly`)・隔月 (`every_two_months`) の 4 つとし、異なるグループの混在を禁止する |
| D6 | `purpose` | 式自体に持たせ、各項目の `purpose` は問わない。ダッシュボードの初期選択は `operational` |
| D7 | 業務日 | 集計は予定枠の `business_date` で行う。`scheduled_at`・`recorded_at` から業務日を逆算しない。翌日 00:00 の検針は `day_offset = 1` のスケジュールで前日分に入る |
| D8 | 欠測・期間またぎ | 計算したうえで「参考値（欠測あり）」と表示する。期間をまたぐ使用量は按分しない |
| D9 | フラグ | 欠測系 (欠測あり・期間外から繰越)、警告 (結果がマイナス・期間不一致)、情報 (交換を含む) の 3 種類に分ける。参考値扱いになるのは欠測系だけとする |
| D10 | 月次・隔月 | 各値は「前回実測の業務日 ＜ 期間 ≦ 今回の業務日」の検針期間として表示する。項目同士で期間がずれる場合は警告「期間不一致」とする |
| D11 | 整合性検査 | 集計式の検査は、スキーマ v4 と同じ PR で `db/integrity.py` に追加する |

## 4. 設計方針

### 4.1 スキーマ v4

- `aggregation_formulas`: `formula_id`、`display_name`、`unit`、`category`、`cycle_group` (`daily`/`weekly`/`monthly`/`every_two_months`)、`purpose`、`is_active`
- `aggregation_formula_versions`: `formula_id`、`effective_from_date`、`created_at`。主キーは (`formula_id`, `effective_from_date`)
- `aggregation_formula_terms`: `formula_id`、`effective_from_date`、`measurement_definition_id`、`coefficient` (有限の plain decimal 文字列、0 は禁止)、`sort_order`。版内での測定定義の重複は UNIQUE で禁止する
- `db/schema.sql`、`db/migrations.py` の v3→v4 移行ステップ、`SCHEMA_VERSION = 4`、版別スキーマ検査を同時に更新する。移行・`PRAGMA user_version` 更新・検査は同一トランザクションで行う
- 集計式は論理削除で保護する。使用中の測定定義を無効化しても過去の版は残し、集計時に注記する

### 4.2 層構成

- 契約: 集計式・項目・集計結果・フラグのレコードを `core/contract_models.py` または独立した契約モジュールに置く。新しいモジュールを作る場合は `tests/test_contract_boundaries.py` を更新する
- Repository: 集計式の CRUD と、集計用の読取 (予定枠・結果・交換境界) を Protocol と sqlite3 実装に追加する
- Service: 集計式の登録・更新 (`core/aggregation_formulas.py`) と集計計算 (`core/reports.py`) を分ける。トランザクション境界は Service が持つ
- UI: `ui/widgets/dashboard.py` と集計式編集画面は Service だけを呼ぶ

### 4.3 使用量の計算

- 値の元は `reading_transactions.usage_delta` と `replacement_boundaries.old_meter_usage_delta` とする
- 測定定義ごとに `business_date` 単位で合計し、日・月・年へ足し上げる。期間の検索は `business_date` の文字列範囲で行う
- 式の値は Σ(係数 × 項目値) とする。業務日ごとに有効な版を適用する
- 計算はすべて Decimal で行い、途中で丸めない。CSV・表示の桁は測定定義の `decimal_places` に合わせ、表示時にだけ丸める
- 対象は `task_state = 'completed'` かつ `result_status = 'measured'` の結果とする。`superseded` の枠は集計しない

### 4.4 フラグ

| 種類 | フラグ | 条件 | 式結果の扱い |
| --- | --- | --- | --- |
| 欠測系 | 欠測あり | 期間内に `open`・`auto_closed`、または非実測の枠がある | 参考値（欠測あり） |
| 欠測系 | 期間外から繰越 | 直前の実測が期間開始前で、使用量が期間をまたぐ | 参考値（欠測あり） |
| 警告 | 結果がマイナス | 式の結果 < 0 | 値を表示し警告 |
| 警告 | 期間不一致 | 月次・隔月で項目同士の検針期間が異なる | 値を表示し警告 |
| 情報 | 交換を含む | 期間内に交換境界がある | 注記のみ |

交換日の使用量は旧メーター分と新メーター分を合算した値とし、欠測扱いにしない。

### 4.5 画面・出力

- `DashboardWindow`: 集計対象として測定定義と集計式の両方を選べる。期間 (日次・月次・年次)、`purpose`、カテゴリ・場所の絞り込み、推移グラフ (`QtCharts`)、内訳表を表示する
- 集計式の編集: `MasterDialog` に「集計式」タブを追加する。版ごとに項目と係数を編集し、保存前に条件違反を表示する
- CSV: 式の結果、項目ごとの値と係数、フラグ (欠測系・警告・情報の別の列)、適用した式の版を出力する。数式インジェクション対策は文字列セルに適用し、数値列には適用しない
- PNG: 表示中のグラフを出力する。保存の成否は書込完了で判定する
- MainWindow から `DashboardWindow` を開く導線を追加し、正本 6.1 の「集計機能は実装されるまで表示しない」を更新する

## 5. タスク

### PR-5a-1 スキーマ v4・集計式マスタ・整合性検査

- [ ] `db/schema.sql` に集計式の 3 テーブルを追加し、`SCHEMA_VERSION = 4` とする
- [ ] `db/migrations.py` に v3→v4 の移行ステップと版別スキーマ検査を追加する
- [ ] 集計式の契約・Repository Protocol・sqlite3 実装を追加し、DB の制約違反を `DomainError` 派生例外へ変換する
- [ ] 集計式 Service で項目条件 (D4・D5) と版の規則 (D3) を検証する
- [ ] `db/integrity.py` に、係数が有限の Decimal かつ 0 でないこと、参照先が存在すること、版内に重複がないこと、周期グループが一致することの検査を追加する
- [ ] v1〜v3 の DB ファイルが v4 へ移行でき、検査に合格することを `tmp_path` で検証する

### PR-5a-2 集計計算

- [ ] `core/reports.py` に測定定義ごとの集計と式の集計を実装する
- [ ] 4.4 のフラグ判定を実装する
- [ ] 月次・隔月の検針期間表示と期間不一致の判定を実装する
- [ ] 6 章のテストを追加し、`core/`・`db/` の行 coverage 90% 以上を維持する

### PR-5a-3 ダッシュボード・CSV・PNG

- [ ] `MasterDialog` に集計式タブを追加する
- [ ] `ui/widgets/dashboard.py` を実装し、`AppContext` と MainWindow に組み込む
- [ ] CSV・PNG 出力を実装する
- [ ] 一時保存先 (`METER_READING_MINI_DATA_DIR`) で手動の通し確認を行い、結果を記録する

### PR-5a-4 完了記録

- [ ] 確定した集計ルールを正本の 4・5・6 章へ統合する
- [ ] 正本 8 章の Phase 5 を 5a・5b・5c に分け、Phase 5a を完了にし、README を同期する
- [ ] 本計画を凍結し、`docs/plan/implemented/` へ `git mv` してリンクを修正する

## 6. テスト

- [ ] 00:00 の検針 (`day_offset = 1`) が前日分に入る。締め時刻・自動クローズ・サマータイムの境界でも正しく扱われる
- [ ] 日次 1 回と日次 3 回の測定定義を組み合わせた式
- [ ] 欠測、`auto_closed` 後の入力による復活、過去分の訂正後の再集計
- [ ] 交換日の旧メーター分が算入され、交換だけでは「参考値（欠測あり）」にならない
- [ ] 交換と欠測が同時にある場合は「参考値（欠測あり）」になり、交換の注記も残る
- [ ] 式の版の切替日をまたぐ期間
- [ ] 周期グループ・単位・カテゴリの不一致、`raw_only`、力率の定義を拒否する
- [ ] マイナス結果、月次の期間不一致
- [ ] `purpose` ごとに集計し、二重計上しない
- [ ] Decimal の精度 (大きな桁数・負の係数・小数係数)
- [ ] CSV の数式インジェクション対策とフラグ列

## 7. 完了条件

- 6 章のテストが成功し、Ruff・整形検査・Pyright が成功する
- 全体・`core/`・`db/` の行 coverage がそれぞれ 90% 以上である
- Windows / Ubuntu の必須 CI が成功し、完了記録 PR を merge する

## 8. 対象外

- バックアップ・復元 UI と復元手順 (Phase 5b)
- PyInstaller・Inno Setup による配布 (Phase 5c)
- 式の中で別の式を参照すること、期間をまたぐ使用量の按分
