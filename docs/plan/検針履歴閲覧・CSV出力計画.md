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
- 「旧メーター最終」行の期待する基準は、旧割当内の直前実測、旧割当を開始させた直前交換境界、最初の割当の読値初期値、なしの順に解決する。実測が基準なら、表示する前回値と参照元には境界の `previous_measured_reading` と `previous_measured_planned_reading_id` を使い、整合していれば出所を `measured` とする。保存された値と参照 ID が片方だけ、または期待する実測と値・ID のいずれかが不一致 (期待する実測があるのに両方欠落する場合を含む) なら `HIST_BASELINE_MISMATCH` とし、保存済みの項目だけを表示・出力する。期待値や期待する参照 ID で補完せず、出所は未確定とする。
- 直前実測がなく、境界に前回実測値・参照 ID がともに保存されていない正常な場合のみ、直前交換境界の新初期値の有無に応じて `replacement_initial` / `replacement_no_initial` (参照元は交換 ID)、または `definition_initial` (参照元は測定定義 ID) を用いる。基準がなければ `none` とする。連続交換では直前交換境界が優先され、その新初期値がなくても定義初期値へ戻らない。
- 「新メーター開始」行は読値を新初期値とし、前回値・使用量は空欄とする。新初期値が未指定の場合は読値を空欄のまま `HIST_REPLACEMENT_INITIAL_MISSING` を付ける。
- 交換後の初回実測行の前回値の出所は、3.2 の整合性判定を満たす場合に、新初期値があれば `replacement_initial`、なければ `replacement_no_initial` (保存済み使用量 0 を維持し、基準不明を情報として示す) とする。
- 使用量列は「実測行の `usage_delta`」と「旧メーター最終行の `old_meter_usage_delta`」だけが値を持つ。期間内使用量合計は `delta` かつ `decimal_string` の定義について、`superseded` を除く表示対象の保存済み使用量を `exact_add` で丸めずに加算する。確認待ち・警告行も保存値があれば含め、「保存済み使用量の単純合計」であることを画面に注記する。`raw_only` と力率は合計を `None` とし、「集計対象外」と表示する。通常の整合した `delta` 定義で `superseded` を非表示とし、確認待ち割当がない場合は、期間内の合計が同期間・同定義の直接集計と一致することをテストする。確認待ちや不整合などで集計の採用条件が異なる場合は一致を保証せず、履歴の保存値と警告を示す。

### 2.2 その他の前提 (計画として採用)

- 期間は業務日の開始日・終了日を入力し、終了日を含める。内部では半開区間 `[開始日, 終了日 + 1 日)` で `business_date` を検索する。未来日は選択できない。既定の開始日は「現在業務日の前月の同日 (前月に存在しない場合は前月末日) の翌日」とし、終了日は現在業務日とする (例: `2026-05-15` は `2026-04-16`、`2026-03-31` は `2026-03-01`、`2026-01-31` は `2026-01-01`)。
- 並び順は `(時点, 行種別順, 行 ID)` とする。時点は予定枠の `scheduled_at` または交換の `replaced_at`、行種別順は `replacement_old_final`、`replacement_new_initial`、`reading` の順、行 ID は交換行では交換 ID、検針行では予定枠 ID とする。交換行は同じ時点の予定枠より前に並び、交換行同士は旧最終、新開始の順とする。期間による絞り込みは `business_date` で行う。
- 検針行の前回値は保存済みの `last_reading` を表示し、3.2 の規則で出所を判定する。参照元が開始日前または終了日後の業務日に属していても、参照を保持する。前回値の出所と整合性検査のため、Repository は選択した定義の予定枠・交換境界・割当・確認待ち情報を全件読み取り、Service が全時系列で基準解決・警告判定を行った後、`business_date` で期間内の表示行に切り出す。`business_date` で SQL 絞り込みすると負の `day_offset` で時系列順の前回実測を取りこぼすため、期間条件による先行絞り込みはしない。
- 読値・前回値・倍率・使用量は保存済みの Decimal を plain decimal 文字列のまま表示・出力する。`decimal_places` による丸め・0 埋めはしない (桁数変更で過去値を変換しない 5.1 の規則と整合させる)。
- 通常の検針行の倍率・ロールオーバー値は、予定日時から一意に解決した、行のメーターと一致する割当 (`meter_assignments`) の値を表示する。交換行は予定日時で検索せず、境界に保存した旧・新メーター ID とそれぞれの割当開始日時 (`old_assignment_effective_from` / `new_assignment_effective_from`) から同じ測定定義の割当を引く。旧割当の終了と新割当の開始がそれぞれ交換日時に一致することを確認し、旧最終行には旧割当、新開始行には新割当の値を表示する。対応する割当が欠落・不整合の場合は該当行に `HIST_ASSIGNMENT_UNRESOLVED` を付け、他の割当の値で補わず計算条件を空欄にする。正常な交換差替えの旧枠は 3.2 の例外に従う。
- 力率 (`structured_payload_json`) は既存の表示形式 (% と位相) で表示し、数値読値・前回値・使用量は空欄で「集計対象外」とする。`raw_only` は保存された数値読値を表示し、前回値の基準整合性は検査しない。いずれも使用量は空欄とする。
- 時刻は業務タイムゾーンのローカル時刻で表示・出力する。取得・変換は `core/timeutil.py`・`core/timezone_settings.py` の既存ヘルパーを使う。
- 論理削除済みの測定定義・メーターも選択・表示できる (定義の選択欄に「無効化済みを含む」切替を設ける)。
- 履歴の取得は 1 回の読取スナップショット (既存の読取トランザクションの仕組み) で行い、枠・結果・境界・割当・確認待ちの整合を保つ。`AppContext` に `ReadingHistoryService` を追加し、読取トランザクション、現在業務日 Provider、タイムゾーン Provider を注入する。

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
| 警告 | `HIST_STATE_MISMATCH` | 状態・値・後継枠参照が整合しない (例: `completed` なのに結果がない、`open` / `auto_closed` なのに結果がある、交換差替えの後継枠が欠落・不整合、負の値、計算方式に応じた必須値がない、非実測結果に値がある、力率 JSON が不正)。正常な交換差替えでの非実測結果の保持は除く |
| 警告 | `HIST_BASELINE_MISMATCH` | `delta` かつ `decimal_string` の検針行で、保存済みの前回値が時系列から解決した基準と一致しない。または旧最終行で前回実測値・参照 ID が片欠け、ないし期待する基準と不一致 (2.1) |
| 警告 | `HIST_ASSIGNMENT_UNRESOLVED` | 予定日時から割当を一意に解決できない、または割当メーターと行のメーターが不一致。正常な交換差替えの旧枠で新メーターの割当が有効となる場合は除く。交換行では境界が指す割当の欠落・不整合を対象とする |
| 警告 | `HIST_ASSIGNMENT_PENDING` | 割当の計算条件が確認待ちである。確認待ちフラグまたは理由の欠落も警告として表示し、履歴閲覧を止めない |
| 警告 | `HIST_REPLACEMENT_FINAL_MISSING` | `delta` かつ `decimal_string` の交換境界で旧最終値がなく、旧最終行の使用量基準が欠ける |
| 警告 | `HIST_REPLACEMENT_INITIAL_MISSING` | `delta` かつ `decimal_string` の交換境界で新初期値がなく、交換後初回実測の基準が不明 |
| 情報 | `HIST_NO_BASELINE` | `delta` かつ `decimal_string` の実測行で前回基準がなく、保存済み使用量 0 は基準不明である (初回実測・交換後初回の両方) |
| 情報 | `HIST_ROLLOVER` | `delta` かつ `decimal_string` の読値で前回値より今回値が小さく、設定済みロールオーバー補正を含む |
| 情報 | `HIST_BASELINE_OUTSIDE_PERIOD` | 出所を確定できた前回基準の業務日が、開始日より前または終了日より後にある。実測・交換境界は参照元の `business_date`、定義初期値は初期割当開始の業務タイムゾーンでの日付を使う |
| 情報 | `HIST_INACTIVE_MASTER` | 測定定義が無効、または行の物理メーターが無効。該当する各行に付与する |
| 情報 | `HIST_SUPERSEDED` | 差替え済み (`superseded`) の枠である (表示切替時のみ) |

`superseded` は `open` / `auto_closed` とは区別する。正本 5.1 の規則に沿った交換差替えでは、旧枠の非実測結果・監査情報を表示し、これらが残っていることや、予定日時に新メーターの割当が有効なことだけでは警告しない。新割当の計算条件を旧枠に流用せず、旧枠の計算条件は空欄とする。表示時は `HIST_SUPERSEDED` を付け、表示の有無にかかわらず全 `superseded` 枠を後続行の前回基準の候補と使用量合計から除外する。後継枠の参照切れなど、実際の不整合は警告する。

`delta` かつ `decimal_string` の前回基準は、既存の `PreviousReadingSource`・前回値解決 (`core/previous_reading.py`) の規則と同じ優先順位 (未消費の交換境界 → 同じ割当内の直前実測 → 最初の割当の読値初期値) で解決する。旧メーター最終行の基準は 2.1 の順序で解決する。実測の検針行は保存済みの `last_reading` を維持し、値または NULL が解決した基準と一致した場合のみ、その出所・参照元を示す。不一致時は `HIST_BASELINE_MISMATCH` を付け、画面では「出所未確定」、CSV では出所・参照元ともに空欄とする。推定した基準は画面の警告詳細に分け、保存値の参照元として扱わない。基準がないと確認できた `none` とは区別する。実測行に基準がなく、既存ルールによる保存済み使用量が 0 の場合は `HIST_NO_BASELINE` を付与する。

正常な `delta` / `decimal_string` 実測行は数値の `raw_reading` と `usage_delta` を持ち、`last_reading` は基準の有無に応じて値または NULL となる。正常な `raw_only` 実測行は数値の `raw_reading` を持ち、`last_reading`・`usage_delta`・構造化値は NULL となる。正常な力率実測行は `raw_reading`・`last_reading`・`usage_delta` が NULL で、妥当な `structured_payload_json` を持つ。これらの方式別正常形を外れた状態・値の組合せは `HIST_STATE_MISMATCH` とする。`raw_only` と力率は前回基準・ロールオーバー・基準不明の判定対象外とする。`raw_only` の交換読値は保存・表示するが、使用量がないこと自体を不整合としない。力率の交換読値は既存仕様で入力できないため、欠損警告の対象外とする。力率 JSON の形式不正は `HIST_STATE_MISMATCH` として行表示を継続する。DB 値の型・文字列表現が壊れていて Repository が解析できない場合は `ValidationError` とし取得を中断するが、読み取れた値の業務上の不整合は行警告とする。使用量の再計算は行わず、保存値をそのまま表示する。

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
- Repository の読取レコードは集計用の `AggregationReadingRecord` を拡張せず、履歴専用の `ReadingHistoryReadingRecord` を追加する (記録日時・担当者・取込元・備考・メーター名・製造番号・メーター有効状態を含む)。集計クエリと性能・意味を分離する。
- 交換境界にも履歴専用の `ReadingHistoryBoundaryRecord` を用いる。`replacement_boundaries` の値に加え、対応する `replacement_events` の `updated_by`・`note` と旧・新メーターの `meter_name`・`meter_serial`・`is_active` を取得する。交換行の「記録日時」と「取込元」は空欄とし、交換日時は予定日時列、担当者と備考は交換イベントの値を用いる (`replacement_events` に `created_at` はない)。
- 新しい `reading_history_contracts.py` は契約モジュールとして扱い、`PreviousReadingSource` は契約モジュール (`contract_models.py`) に配置する。既存呼出しとの互換性のため `previous_reading.py` から再公開する。契約境界テストの対象ファイル・モジュール一覧も更新する。
- DB は変更しない。予定枠はインデックス `idx_planned_readings_def_sched` で定義ごとの時系列全件を読み、期間条件で先行絞り込みしない。

### 4.2 Service 契約 (案)

```python
@dataclass(frozen=True, slots=True)
class ReadingHistoryQuery:
    measurement_definition_id: str
    start_date: date  # 含む
    end_date: date  # 含む (内部で翌日の半開区間へ変換)
    include_superseded: bool = False


class ReadingHistoryService:
    def list_definitions(
        self, *, include_inactive: bool
    ) -> tuple[ReadingHistoryDefinitionOption, ...]: ...
    def get_history(self, query: ReadingHistoryQuery) -> ReadingHistory: ...
```

- `ReadingHistory` は定義情報 (名称・場所・単位・カテゴリ・目的・計算方式・無効化状態)、期間、行の tuple、期間内使用量合計 (`delta` / `decimal_string` の場合に `superseded` を除いた保存済み使用量を `exact_add` で加算し、値がなければ `None`)、警告件数を持つ。確認待ち・警告のある行の保存値も合計に含める。`raw_only`・力率は合計を `None` とする。
- 未来日・開始日 > 終了日・存在しない定義は `ValidationError` とする。
- 例外は握りつぶさない。条件不正と DB 値の型・形式破損は `ValidationError` 等の既存 `DomainError` 派生で通知する。読み取れた業務データの状態・値・参照・割当の不整合は警告コードにし、閲覧を継続する。

### 4.3 CSV

- 既存の集計 CSV と同じく UTF-8 BOM・固定列・一時ファイルへの書込完了後の置換・成功通知とし、`ui/csv_export.py` の共通処理を使う。
- 文字列セルが `=`・`+`・`-`・`@`・タブ・CR で始まる場合は `'` を付ける。数値列は読値・前回値・倍率・ロールオーバー値・使用量に限定し、それ以外の文字列列と力率表示列は数式注入対策の対象とする。
- ファイル名は `meter_reading_history_<測定定義名を安全化>_YYYYMMDD-YYYYMMDD_<出力時刻>.csv` とし、出力時刻は既存の `local_timestamp_for_filename` を使う。測定定義名は Windows 禁止文字 `\ / : * ? " < > |` と制御文字を `_` に置換し、連続する空白・`_` を畳み、前後の空白・ドットを除去して 40 文字以内にする。空になった場合や、拡張子の有無によらず先頭要素が Windows 予約名 (`CON`・`PRN`・`AUX`・`NUL`・`COM1`〜`COM9`・`LPT1`〜`LPT9`) の場合は測定定義 ID を同じ規則で安全化して使う。
- 列 (固定順):

| 列 | 内容 |
| --- | --- |
| 測定定義ID / 測定定義名 / 場所ID / 場所名 / 単位 | 全行同一。CSV 単体で対象を識別できるようにする |
| 行種別 | `reading` / `replacement_old_final` / `replacement_new_initial` |
| 業務日 / 検針時間 / 予定日時 | 予定枠の値。交換行は交換日時を予定日時列に出力し、検針時間は空欄 |
| 枠状態 / 結果状態 | `task_state` / `result_status` (交換行は空欄) |
| メーターID / メーター名 / 製造番号 | 行の物理メーター |
| 読値 / 前回値 / 前回値の出所 / 前回値の参照元 | 確定した出所は `PreviousReadingSource` の値。`measured` は予定枠 ID、`replacement_initial` / `replacement_no_initial` は交換 ID、`definition_initial` は測定定義 ID、`none` は参照元なし。出所未確定時は出所・参照元ともに空欄とするが、旧最終行の保存済み参照 ID は監査用に参照元列へそのまま出力し、警告コードを併記する |
| 倍率 / ロールオーバー値 | 解決した割当の値 |
| 使用量 | 保存値 (無丸め) |
| 力率 | 構造化入力の表示値。力率行の読値・前回値は空欄 |
| 記録日時 / 担当者 / 取込元 / 備考 | D4 の監査列。検針行は結果の監査値。交換行は記録日時・取込元を空欄、交換イベントの担当者・備考を出力 |
| 交換ID / 予定枠ID | 追跡用 |
| 警告・情報コード | `;` 区切りの固定コード |

- 表示中の条件・結果をそのまま出力する。CSV 数値は無丸めである一方、表計算ソフトで数値として開くと精度が失われる場合がある旨を画面に表示する。条件変更・取得失敗時は出力対象を破棄し、出力ボタンを無効化する。

### 4.4 画面 (`ReadingHistoryDialog`)

- 条件: 場所・カテゴリによる絞り込み → 測定定義 (1 件)、「無効化済みを含む」、開始日・終了日 (未来日不可)、「差替え済みの枠を表示」、「表示」ボタン。
- 一覧: 3.1 の行種別ごとに色・アイコンで交換行を区別し、警告行を強調する。警告のある行にはツールチップで日本語の説明を表示する。下部に件数・期間内使用量合計・警告件数を表示し、確認待ち・警告行も合計に含む場合は「保存済み使用量の単純合計」である旨を注記する。
- 確認待ち割当が複数ある場合に対応する候補コンボと「計算条件の訂正」ボタンを設け、選択した割当の理由を表示する (既存の集計画面と同じ導線を再利用する)。
- 場所・カテゴリ・無効化済み表示の変更後も選択中の測定定義が候補に残る場合は選択を維持する。候補から外れた場合は選択をクリアする。条件変更時はいずれの場合も古い一覧と出力対象を破棄する。
- メイン画面のモーダル表示中に業務日境界タイマーによる再読込があった場合、および履歴画面から開いた「計算条件の訂正」を保存した場合は、履歴ダイアログを無効化し、最大業務日を更新する。訂正後は利用者が履歴を再取得する。
- 予期しない例外は既存の `run_guarded` でログに記録し、汎用エラーダイアログで通知する。

## 5. タスク

各 PR は必須 CI 成功を確認してからマージする。先行 PR の成果を取り込んだ後に次の PR を進める。

### PR-1 core 契約・Service・Repository

ブランチ: `feature/v0.2.0-history-core`

- [ ] `core/reading_history_contracts.py` に条件・行・行種別・警告コード・結果の契約を定義する
- [ ] `ReadingHistoryRepository` Protocol と `ReadingHistoryReadingRecord` を `core/meter_reading_sync_repositories.py` に追加する
- [ ] `db/repositories.py` に選択定義の予定枠・結果・メーター情報と交換境界・イベント監査情報・旧新メーター情報の全件読取を実装する (プレースホルダ使用)。期間で SQL 絞り込みせず、時系列順に取得する
- [ ] `core/reading_history.py` に `ReadingHistoryService` を実装する (時系列の合成、2.1 の交換行展開、前回値の出所解決、割当解決、警告判定、期間の切り出し、合計)
- [ ] `PreviousReadingSource` を `core/contract_models.py` へ移し、`core/previous_reading.py` から再公開する。新契約を `tests/test_contract_boundaries.py` の `CONTRACT_FILES` / `CONTRACT_MODULES` に追加する
- [ ] `AppContext` に `ReadingHistoryService` を追加し、読取トランザクション・現在業務日 Provider・タイムゾーン Provider を注入する
- [ ] 層依存・契約独立性・PySide6 非依存・現在時刻取得の禁止を既存のアーキテクチャテストで満たすことを確認する
- [ ] 新規ソース・テストを `packaging/source-files.txt` の許可リストへ追加し、関連する配布検査を実行する
- [ ] テスト (`:memory:`、時刻固定) を追加する
  - [ ] 交換なしの通常履歴、未入力・非実測・`auto_closed` の行、`superseded` の表示切替
  - [ ] 計器故障結果を保持した交換差替えの旧枠に、状態・割当の誤警告が出ないこと。後継枠の参照切れには警告を付け、表示切替で後続行の前回基準や期間合計が変わらないこと
  - [ ] 交換の 2 行展開と位置 (交換日時ちょうどの予定枠より前)、新初期値あり / なし、最初の割当で実測前の交換 (定義初期値基準)、同一定義の複数回交換、および実測を挟まない連続交換 (新初期値あり / なし)
  - [ ] 旧・新の倍率とロールオーバー値が異なるとき、交換 2 行にそれぞれの条件を表示し、交換日時ちょうどの検針行には新割当の条件を表示すること。境界が指す割当が不整合なら該当行の計算条件を空欄にして警告すること
  - [ ] 交換後初回実測の出所 (`replacement_initial` / `replacement_no_initial`)、開始日前の基準に対する `HIST_BASELINE_OUTSIDE_PERIOD`
  - [ ] 保存済みの前回値 `100` と解決した基準 `120` が不一致なら、保存値を維持し、推定した枠 ID を出所にしないこと。旧最終行では境界の保存済み前回値・参照 ID を上書きしないこと
  - [ ] 期間端: 終了日を含むこと、交換日が開始日・終了日に一致する場合、未来日・開始日 > 終了日の拒否
  - [ ] 期間内使用量合計が、同期間・同定義の使用量集計 (直接集計) の合計と一致すること (交換あり・ロールオーバーあり)
  - [ ] Decimal の無丸め表示 (桁数変更後の過去値)、ロールオーバー補正の情報コード
  - [ ] 不整合データ (状態と値の不整合・使用量欠落・前回値不一致・境界参照の片欠け / 不一致・割当欠落 / 重複・確認待ち・場所不一致・力率 JSON 破損) は警告として表示継続し、DB 値の型 / 形式破損は `ValidationError` になること
  - [ ] 力率・`raw_only` の基準警告対象外と使用量空欄、論理削除済み定義 / メーターの行別表示
  - [ ] 負の `day_offset` で時系列上の直前実測が終了日の翌業務日に属する場合も、その参照を維持して `HIST_BASELINE_OUTSIDE_PERIOD` を付けること
  - [ ] 警告・確認待ち行を含む単純合計と `exact_add` の精度を検証すること
  - [ ] 既定期間の月末・年末・閏年 (例: `2026-03-31` → `2026-03-01`、`2024-03-31` → `2024-03-01`) を検証すること
  - [ ] 業務タイムゾーン・夏時間を含むローカル時刻表示と業務日の境界
- [ ] `uv run ruff check` / `uv run ruff format --check` / `uv run pyright` / 対象テストと、全体・`core/`・`db/`・`core/` + `db/` の各カバレッジ 90% 以上を確認する
  - [ ] `uv run coverage report --fail-under=90`
  - [ ] `uv run coverage report --include="*/meter_reading_mini/core/*" --fail-under=90`
  - [ ] `uv run coverage report --include="*/meter_reading_mini/db/*" --fail-under=90`
  - [ ] `uv run coverage report --include="src/meter_reading_mini/core/*,src/meter_reading_mini/db/*" --fail-under=90`

### PR-2 UI・CSV

ブランチ: `feature/v0.2.0-history-ui`

- [ ] `ui/models/reading_history_model.py` (表示文字列・色・ツールチップ)
- [ ] `ui/reading_history_export.py` (4.3 の固定列・列別数式注入対策・UTF-8 BOM・ファイル名安全化)
- [ ] `ui/dialogs/reading_history_dialog.py` (4.4 の条件入力・表示・破棄・CSV 保存)
- [ ] `MainWindow` のツールバーに「検針履歴」を追加し、書込系操作後および業務日境界タイマー再読込時の無効化に接続する。選択中の予定枠があればその測定定義を初期選択する
- [ ] 新規ソース・テストを `packaging/source-files.txt` の許可リストへ追加し、関連する配布検査を実行する
- [ ] テスト (`QT_QPA_PLATFORM=offscreen`、`METER_READING_MINI_DATA_DIR` は `tmp_path`)
  - [ ] CSV の列順・BOM・列別数式注入対策・ファイル名安全化・交換 2 行の監査列・警告コードの出力
  - [ ] 前回値が不一致の検針行では、画面と CSV に保存値を表示し、画面では「出所未確定」と警告詳細を表示、CSV では出所・参照元を空欄にすること。旧最終行に保存済み参照 ID があれば CSV に残すこと
  - [ ] 条件変更・取得失敗で一覧と出力対象を破棄すること、候補に残る測定定義の選択維持、未来日を選べないこと
  - [ ] 履歴画面からの計算条件訂正後に一覧・出力対象を無効化し、再取得を求めること
  - [ ] 書込失敗時に成功を通知しないこと
- [ ] 品質ゲート一式と Windows / Ubuntu の必須 CI 成功を確認する
  - [ ] PR-1 と同じ全体・`core/`・`db/`・`core/` + `db/` の各カバレッジ 90% 以上を確認する

### PR-3 版数・文書

ブランチ: `feature/v0.2.0-version-docs`

- [ ] `pyproject.toml` を `0.2.0` に更新し、`uv lock` で `uv.lock` を更新する。差分と `uv lock --check` の結果を確認する
- [ ] アプリ版数を参照するテスト・表示・配布設定・文書を確認し、必要な箇所を更新する。スキーマ v5 と `SCHEMA_VERSION` は変更しない
- [ ] 本計画書はこの PR では移動せず、現行パスを `packaging/source-files.txt` に登録し、`tests/test_distribution_build.py` の検証を実行する
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
- [ ] ビルド対象コミット・検証結果・未実施事項を記録した後、本書を完了 (凍結) として `docs/plan/implemented/` へ一度だけ `git mv` する。相対リンク修正と `packaging/source-files.txt` のパス更新を同じ変更に含め、`tests/test_distribution_build.py` を再実行する。検証記録のマージ確認を先に行う必要がある場合は、移動・凍結と関連変更を後続の文書 PR にまとめる
- [ ] 完了・凍結を含む PR の Windows / Ubuntu 必須 CI 成功と `main` へのマージを確認する

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
