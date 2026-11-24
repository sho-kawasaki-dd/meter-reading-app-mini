# v0.2.0 検針履歴閲覧・CSV 出力 実装計画

- 作成日: 2026-11-24
- ステータス: 計画中 (未着手)
- アプリ版数: 本改修で `0.2.0` に更新する。DB スキーマ版数は独立して管理し、本改修では v5 のまま変更しない
- ブランチ: `feature/v0.2.0-<内容>`
- 対象: リポジトリ直下 (`src/`・`tests/`・`docs/`・`README.md`)
- 仕様の正本: [ローカル検針管理アプリ開発計画書](ローカル検針管理アプリ開発計画書.md) 5.1・5.3・6 章
- 現状・実行手順: [README](../../README.md)

## 1. 目的・範囲

使用量集計 (5.3.6・5.3.7) とは別に、1 つの測定定義の検針履歴を期間を指定して時系列で閲覧し、CSV へ出力できるようにする。履歴は保存済みの読値・前回値・使用量・結果状態・監査情報を、計算や丸めを加えずにそのまま示す。主な用途は、交換をまたいだ読値の連続性の確認と、障害・訂正の調査である。

| 対象 | 対象外 |
| --- | --- |
| 読取専用の履歴 Service・契約・Repository 読取 | 履歴画面からの入力・訂正 (既存の「PC 直接入力・訂正」を使う) |
| 交換境界の 2 行展開と前回値の出所表示 | 複数の測定定義の同時表示、式の履歴 |
| 行ごとの不整合警告 (閲覧は継続) | 保存値の自動修復・再計算・丸め |
| 専用ダイアログ・CSV 出力 | スキーマ変更 (本機能では DB を変更しない) |
| `pyproject.toml`・`uv.lock` のアプリ版数を `0.2.0` に更新 | タグ付け・Release 作成・正式配布 |
| 改修後の Windows 検証用インストーラーのビルド・成果物検査 | 開発 PC でのインストール・既定保存先での起動 |

検証用成果物は正式な配布物ではなく、Release に添付しない。インストールを伴う実地確認は、実データのない Sandbox / VM で利用者が行う。タグ付け・Release は別途、利用者の承認と Phase 5c の手順に従う。

## 2. 決定事項

利用者確認 (2026-11-24) で確定した方針は以下のとおり。

| # | 論点 | 決定 |
| --- | --- | --- |
| D1 | 交換境界の表し方 | 2 行に展開する。「旧メーター最終」行 (メーター=旧、読値=`old_final_reading`、前回値=`previous_measured_reading` または初期値、使用量=`old_meter_usage_delta`) と「新メーター開始」行 (メーター=新、読値=`new_initial_reading`、使用量は空欄) |
| D2 | 表示する予定枠 | 未入力 (`open` / `auto_closed`)・非実測を含む全枠を表示する。`superseded` は既定で非表示とし、切替で表示する |
| D3 | 表示対象 | 測定定義を 1 つ選んで表示する |
| D4 | 監査列 | 記録日時・担当者 (`updated_by`)・取込元 (`result_source`)・備考を含める。`device_id`・署名・`client_updated_at` は含めない |
| D5 | 不整合データ | 業務エラーで表示を止めず、行に警告コードを付けて閲覧・出力を続ける |
| D6 | 入口 | ツールバーに「検針履歴」を追加し、専用のモーダルダイアログを開く |

### 2.1 D1 による交換境界の詳細規則

- 2 行とも `replacement_boundaries` (測定定義ごとの境界) を正本とする。`replacement_events` の旧読値列は参照しない。
- 行の位置は交換日時 `replaced_at` とする。同じ時点の予定枠より前に置き、「旧メーター最終 → 新メーター開始」の順に並べる。割当は半開区間 `[effective_from, effective_to)` のため、交換日時ちょうどの予定枠は新メーター側の枠となる。
- 期間判定には境界の `business_date` を使い、予定日時・記録日時から業務日を逆算しない。2 行は同じ業務日に属するため、期間の端で分断されない。
- 「旧メーター最終」行の前回値の出所は、`previous_measured_planned_reading_id` があれば `measured` (参照先の予定枠 ID を表示)、なく最初の割当に読値初期値があれば `definition_initial`、どちらもなければ `none` とする。
- 「新メーター開始」行は読値を新初期値とし、前回値・使用量は空欄とする。新初期値が未指定の場合は読値を空欄のまま `HIST_REPLACEMENT_INITIAL_MISSING` を付ける。
- 交換後の初回実測行の前回値の出所は、新初期値があれば `replacement_initial`、なければ `replacement_no_initial` (保存済み使用量 0 を維持し、基準不明を情報として示す) とする。
- 使用量列は「実測行の `usage_delta`」と「旧メーター最終行の `old_meter_usage_delta`」だけが値を持つ。通常の整合した `delta` 定義で `superseded` を非表示とし、確認待ち割当がない場合は、期間内の使用量合計が同期間・同定義の直接集計と一致することをテストする。確認待ちや不整合などで集計の採用条件が異なる場合は一致を保証せず、履歴の保存値と警告を示す。`raw_only` は使用量合計なしとする。

### 2.2 その他の前提 (計画として採用)

- 期間は業務日の開始日・終了日を入力し、終了日を含める。内部では半開区間 `[開始日, 終了日 + 1 日)` で `business_date` を検索する。未来日は選択できず、既定は「現在業務日の 1 か月前の翌日〜現在業務日」とする。
- 並び順は後続再計算と同じ `scheduled_at, planned_reading_id` とし、交換行は 2.1 の位置に挿入する。期間による絞り込みは `business_date` で行う。
- 期間先頭行の前回値は保存済みの `last_reading` を表示し、期間外の参照も出所として示す。前回値の出所と整合性検査のため、Service は選択した定義の終了日までの履歴全体を読み取り、表示時に期間で切り出す。
- 読値・前回値・倍率・使用量は保存済みの Decimal を plain decimal 文字列のまま表示・出力する。`decimal_places` による丸め・0 埋めはしない (桁数変更で過去値を変換しない 5.1 の規則と整合させる)。
- 倍率・ロールオーバー値は、予定日時から解決した割当 (`meter_assignments`) の値を表示する。交換や条件訂正で行ごとに条件が変わることを確認できるようにする。
- 力率 (`structured_payload_json`) は既存の表示形式 (% と位相) で表示し、使用量は空欄で「集計対象外」とする。`raw_only` も使用量は空欄とする。
- 時刻は業務タイムゾーンのローカル時刻で表示・出力する。取得・変換は `core/timeutil.py`・`core/timezone_settings.py` の既存ヘルパーを使う。
- 論理削除済みの測定定義・メーターも選択・表示できる (定義の選択欄に「無効化済みを含む」切替を設ける)。
- 履歴の取得は 1 回の読取スナップショット (既存の読取トランザクションの仕組み) で行い、枠・結果・境界・割当・確認待ちの整合を保つ。

## 3. 行種別と警告コード

### 3.1 行種別 (`row_kind`)

| コード | 画面表示 | 内容 |
| --- | --- | --- |
| `reading` | 検針 | 予定枠 1 件 (結果なしの場合も含む) |
| `replacement_old_final` | 交換 (旧最終) | 交換境界の旧メーター最終値 |
| `replacement_new_initial` | 交換 (新開始) | 交換境界の新メーター初期値 |

### 3.2 警告・情報コード

使用量集計の `AggregationCode` とは目的が異なるため流用せず、履歴専用の列挙を `core/` の契約に定義する。CSV には固定コードを出力し、画面には日本語の説明を表示する。

| 種類 | コード | 条件 |
| --- | --- | --- |
| 警告 | `HIST_STATE_MISMATCH` | 状態と値が整合しない (例: `completed` かつ `measured` で `delta` なのに使用量がない、`completed` なのに結果がない、未完了なのに結果がある、負の値) |
| 警告 | `HIST_BASELINE_MISMATCH` | 保存済みの前回値が、時系列から解決した基準 (直前実測・交換初期値・定義初期値) と一致しない |
| 警告 | `HIST_ASSIGNMENT_UNRESOLVED` | 予定日時から割当を一意に解決できない |
| 警告 | `HIST_ASSIGNMENT_PENDING` | 割当の計算条件が確認待ちである |
| 警告 | `HIST_REPLACEMENT_FINAL_MISSING` | 交換境界の旧最終値がない |
| 警告 | `HIST_REPLACEMENT_INITIAL_MISSING` | 交換境界の新初期値がない |
| 情報 | `HIST_NO_BASELINE` | 交換後初回で新初期値がなく、使用量 0 は基準不明である |
| 情報 | `HIST_ROLLOVER` | 前回値より今回値が小さく、ロールオーバー補正を含む |
| 情報 | `HIST_BASELINE_OUTSIDE_PERIOD` | 前回基準が期間開始前にある |
| 情報 | `HIST_INACTIVE_MASTER` | 論理削除済みの測定定義・メーターを含む |
| 情報 | `HIST_SUPERSEDED` | 差替え済み (`superseded`) の枠である (表示切替時のみ) |

警告の判定に使う基準の解決は、既存の `PreviousReadingSource`・前回値解決 (`core/previous_reading.py`) の規則と同じ優先順位 (未消費の交換境界 → 同じ割当内の直前実測 → 最初の割当の読値初期値) で行い、別規則を作らない。使用量の再計算は行わず、保存値をそのまま表示する。

## 4. 設計

### 4.1 層構成

```text
ui/dialogs/reading_history_dialog.py   (PySide6、表示・条件入力・CSV 保存)
ui/models/reading_history_model.py     (QAbstractTableModel)
ui/reading_history_export.py           (CSV 行生成・保存)
        ↓ Service のみ呼び出す
core/reading_history.py                (ReadingHistoryService、純 Python)
core/reading_history_contracts.py      (行・警告・条件の契約)
        ↓ Protocol
core/meter_reading_sync_repositories.py (ReadingHistoryRepository Protocol と入出力レコード)
        ↓ 実装
db/repositories.py                     (標準 sqlite3 の読取クエリ)
```

- `core/`・`db/` は PySide6 を import しない。契約は Service・UI・DB 実装へ依存しない。
- Repository の読取レコードは集計用の `AggregationReadingRecord` を拡張せず、履歴専用の `ReadingHistoryReadingRecord` を追加する (記録日時・担当者・取込元・備考・メーター名・製造番号を含む)。集計クエリと性能・意味を分離する。
- 交換境界は既存の `AggregationBoundaryRecord` 相当の項目で足りるため、共通化できる場合は既存の読取を再利用する。
- DB は変更しない。インデックス `idx_planned_readings_def_sched` で定義ごとの時系列読取を行う。

### 4.2 Service 契約 (案)

```python
@dataclass(frozen=True, slots=True)
class ReadingHistoryQuery:
    measurement_definition_id: str
    start_date: date          # 含む
    end_date: date            # 含む (内部で翌日の半開区間へ変換)
    include_superseded: bool = False

class ReadingHistoryService:
    def list_definitions(self, *, include_inactive: bool) -> tuple[ReadingHistoryDefinitionOption, ...]: ...
    def get_history(self, query: ReadingHistoryQuery) -> ReadingHistory: ...
```

- `ReadingHistory` は定義情報 (名称・場所・単位・カテゴリ・目的・計算方式・無効化状態)、期間、行の tuple、期間内使用量合計 (値のある行の Decimal 合計、値がなければ `None`)、警告件数を持つ。
- 未来日・開始日 > 終了日・存在しない定義は `ValidationError` とする。
- 例外は握りつぶさない。行単位の不整合は警告コードとし、Repository の低レベル例外は既存の `DomainError` 派生へ変換する。

### 4.3 CSV

- 既存の集計 CSV と同じく UTF-8 BOM・固定列・一時ファイルへの書込完了後の置換・成功通知とし、`ui/csv_export.py` の共通処理を使う。
- 文字列セルが `=`・`+`・`-`・`@`・タブ・CR で始まる場合は `'` を付ける。数値列 (読値・前回値・倍率・ロールオーバー値・使用量) は対象外とする。
- ファイル名は `meter_reading_history_<測定定義名を安全化>_YYYYMMDD-YYYYMMDD_<出力時刻>.csv` とし、出力時刻は既存の `local_timestamp_for_filename` を使う。
- 列 (固定順):

| 列 | 内容 |
| --- | --- |
| 測定定義ID / 測定定義名 / 場所ID / 場所名 / 単位 | 全行同一。CSV 単体で対象を識別できるようにする |
| 行種別 | `reading` / `replacement_old_final` / `replacement_new_initial` |
| 業務日 / 検針時間 / 予定日時 | 予定枠の値。交換行は交換日時を予定日時列に出力し、検針時間は空欄 |
| 枠状態 / 結果状態 | `task_state` / `result_status` (交換行は空欄) |
| メーターID / メーター名 / 製造番号 | 行の物理メーター |
| 読値 / 前回値 / 前回値の出所 / 前回値の参照元 | 出所は `PreviousReadingSource` の値。参照元は予定枠 ID または交換 ID |
| 倍率 / ロールオーバー値 | 解決した割当の値 |
| 使用量 | 保存値 (無丸め) |
| 力率 | 構造化入力の表示値 |
| 記録日時 / 担当者 / 取込元 / 備考 | D4 の監査列。交換行は交換イベントの担当者・備考 |
| 交換ID / 予定枠ID | 追跡用 |
| 警告・情報コード | `;` 区切りの固定コード |

- 表示中の条件・結果をそのまま出力する。条件変更・取得失敗時は出力対象を破棄し、出力ボタンを無効化する。

### 4.4 画面 (`ReadingHistoryDialog`)

- 条件: 場所・カテゴリによる絞り込み → 測定定義 (1 件)、「無効化済みを含む」、開始日・終了日 (未来日不可)、「差替え済みの枠を表示」、「表示」ボタン。
- 一覧: 3.1 の行種別ごとに色・アイコンで交換行を区別し、警告行を強調する。警告のある行にはツールチップで日本語の説明を表示する。下部に件数・期間内使用量合計・警告件数を表示する。
- 確認待ち割当がある場合は理由と「計算条件の訂正」への導線を示す (既存の集計画面と同じ導線を再利用する)。
- 条件を変更したら、古い一覧と出力対象を破棄する。メイン画面で入力・交換・訂正・取込・復元があった場合に再表示を求めるため、集計画面と同じ無効化の仕組みに接続する。
- 予期しない例外は既存の `run_guarded` でログに記録し、汎用エラーダイアログで通知する。

## 5. タスク

各 PR は必須 CI 成功を確認してからマージする。先行 PR の成果を取り込んだ後に次の PR を進める。

### PR-1 core 契約・Service・Repository

ブランチ: `feature/v0.2.0-history-core`

- [ ] `core/reading_history_contracts.py` に条件・行・行種別・警告コード・結果の契約を定義する
- [ ] `ReadingHistoryRepository` Protocol と `ReadingHistoryReadingRecord` を `core/meter_reading_sync_repositories.py` に追加する
- [ ] `db/repositories.py` に定義単位・終了日までの予定枠＋結果＋メーター名の読取と、交換境界＋交換イベント監査情報の読取を実装する (プレースホルダ使用)
- [ ] `core/reading_history.py` に `ReadingHistoryService` を実装する (時系列の合成、2.1 の交換行展開、前回値の出所解決、割当解決、警告判定、期間の切り出し、合計)
- [ ] 層依存・PySide6 非依存・現在時刻取得の禁止を既存のアーキテクチャテストで満たすことを確認する
- [ ] 新規ソース・テストを `packaging/source-files.txt` の許可リストへ追加し、関連する配布検査を実行する
- [ ] テスト (`:memory:`、時刻固定) を追加する
  - [ ] 交換なしの通常履歴、未入力・非実測・`auto_closed` の行、`superseded` の表示切替
  - [ ] 交換の 2 行展開と位置 (交換日時ちょうどの予定枠より前)、新初期値あり / なし、最初の割当で実測前の交換 (定義初期値基準)、同一定義の複数回交換
  - [ ] 交換後初回実測の出所 (`replacement_initial` / `replacement_no_initial`)、期間先頭の `HIST_BASELINE_OUTSIDE_PERIOD`
  - [ ] 期間端: 終了日を含むこと、交換日が開始日・終了日に一致する場合、未来日・開始日 > 終了日の拒否
  - [ ] 期間内使用量合計が、同期間・同定義の使用量集計 (直接集計) の合計と一致すること (交換あり・ロールオーバーあり)
  - [ ] Decimal の無丸め表示 (桁数変更後の過去値)、ロールオーバー補正の情報コード
  - [ ] 不整合データ (使用量欠落・前回値不一致・割当欠落 / 重複・確認待ち) で例外にならず警告になること
  - [ ] 力率・`raw_only` の使用量空欄、論理削除済みマスタの表示
  - [ ] 業務タイムゾーン・夏時間を含むローカル時刻表示と業務日の境界
- [ ] `uv run ruff check` / `uv run ruff format --check` / `uv run pyright` / 対象テスト / カバレッジ 90% 以上を確認する

### PR-2 UI・CSV

ブランチ: `feature/v0.2.0-history-ui`

- [ ] `ui/models/reading_history_model.py` (表示文字列・色・ツールチップ)
- [ ] `ui/reading_history_export.py` (4.3 の固定列・数式注入対策・UTF-8 BOM)
- [ ] `ui/dialogs/reading_history_dialog.py` (4.4 の条件入力・表示・破棄・CSV 保存)
- [ ] `MainWindow` のツールバーに「検針履歴」を追加し、書込系操作後の無効化に接続する
- [ ] 新規ソース・テストを `packaging/source-files.txt` の許可リストへ追加し、関連する配布検査を実行する
- [ ] テスト (`QT_QPA_PLATFORM=offscreen`、`METER_READING_MINI_DATA_DIR` は `tmp_path`)
  - [ ] CSV の列順・BOM・数式注入対策 (数値列は対象外)・交換 2 行の出力・警告コードの出力
  - [ ] 条件変更・取得失敗で一覧と出力対象を破棄すること、未来日を選べないこと
  - [ ] 書込失敗時に成功を通知しないこと
- [ ] 品質ゲート一式と Windows / Ubuntu の必須 CI 成功を確認する

### PR-3 版数・文書

ブランチ: `feature/v0.2.0-version-docs`

- [ ] `pyproject.toml` を `0.2.0` に更新し、`uv lock` で `uv.lock` を更新する。差分と `uv lock --check` の結果を確認する
- [ ] アプリ版数を参照するテスト・表示・配布設定・文書を確認し、必要な箇所を更新する。スキーマ v5 と `SCHEMA_VERSION` は変更しない
- [ ] 開発計画書に「5.3.8 検針履歴の閲覧・出力」を追加し、2 章・3 章の確定事項を統合する
- [ ] 開発計画書 5.3.1 のツールバー一覧と 6.1 の画面一覧に「検針履歴」を追加する
- [ ] README の機能一覧・操作手順・版数と、必要な Phase 5 計画の版数前提を更新する。正式配布・実地確認を完了済みと記載しない
- [ ] 関連テストと配布ビルドのテストを実行し、Windows / Ubuntu の必須 CI 成功を確認する

### PR-4 検証用インストーラー・完了記録

ブランチ: `feature/v0.2.0-verification-installer`

PR-1〜3 の必須 CI 成功と `main` へのマージを確認した後、その時点の `main` と同じソース・ロックファイルを用いて v0.2.0 をビルドし、対象コミットを記録する。ビルド対象に変更が生じた場合は対象コミットを明示して再検証する。

- [ ] Windows x64 の既存手順に従い、ロック済み依存・Inno Setup 6.7.3 等の前提とソース・同梱資産の許可リストを確認する
- [ ] 新しい出力先に開発用 exe をビルドし、`METER_READING_MINI_DATA_DIR` に実データと分離した一時保存先を指定して `scripts/verify_windows_exe.ps1` で直接起動する。起動・DB パス・スキーマ v5 と既存確認項目を検証する
- [ ] 合成データを使って exe で交換を含む定義の履歴表示・CSV 出力を確認する。検証後は実行した項目と結果を記録する
- [ ] `uv run --locked python scripts\build_distribution.py --verification-only` で検証用インストーラーを新規ビルドする。今回成功した PyInstaller 出力のみを入力とし、過去の出力や中断マーカーのある出力は使用しない
- [ ] インストーラーの版数 `0.2.0`、ファイル名・設定での「検証用」表示、ビルド入力・同梱一覧からの業務データ非混入、`SHA256SUMS.txt` の全件一致を確認する。画面表示の実地確認は別途 Sandbox / VM で行う
- [ ] ビルド対象コミット、成果物の場所・SHA256、実施した検査と未実施事項を 7 章に記録する。利用者へ成果物と Sandbox / VM の確認手順を伝える
- [ ] 実データのない Sandbox / VM でのインストール・初回起動、v0.1.1 からの更新とデータ保持、履歴表示・CSV 出力、アンインストール後のデータ保持を利用者に案内し、確認結果を受領した場合のみ記録する
- [ ] Windows / Ubuntu の必須 CI を確認し、完了条件を満たした後、本書を完了 (凍結) として `docs/plan/implemented/` へ `git mv` し、相対リンクを修正する。マージ確認が後続作業となる場合は、その結果を確認してから別途凍結する

## 6. 完了条件

- 機能・版数・文書の PR が Windows / Ubuntu の必須 CI に成功し、PR 経由で `main` にマージ済みであること
- 実データのない一時保存先で、交換を含む定義の履歴表示と CSV 出力を確認したこと
- v0.2.0 の検証用インストーラーをビルドし、対象コミット・版数・業務データ非混入・SHA256 を検証・記録したこと
- Sandbox / VM での実地確認の実施状況を明記すること。未実施の場合は未実施と記録し、ビルド成功を実地確認の成功とみなさない
- 本書の完了・凍結を含む変更についても必須 CI 成功と PR マージを確認すること

## 7. 検証記録

各 PR の実施時に、対象コミット、実行した検査・結果、未実施項目と理由を記録する。現時点では実装・ビルド・実地確認とも未実施。

## 8. 注意事項

- 検証用成果物は社外・第三者へ配布せず、Release に添付しない。正式配布に必要な法的監査と Sandbox / VM 検証は Phase 5c の未完了事項として扱う。
- 開発 PC ではインストーラーを実行せず、既定保存先を使うインストール・初回起動・復元後再起動を行わない。
- Inno Setup 6.7.3 等の前提を満たせずビルドできない場合は、未実施と理由を記録し、導入方法を利用者と相談する。
