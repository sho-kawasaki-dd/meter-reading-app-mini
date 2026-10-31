# Phase 3 メイン画面・マスタ管理・PC 直接検針・メーター交換 UI 計画

- 作成日: 2026-10-28
- ステータス: 計画中
- 対象: リポジトリ直下を基点とする Phase 3(`src/`・`tests/`・`pyproject.toml`)
- 正本: [ローカル検針管理アプリ開発計画書](./ローカル検針管理アプリ開発計画書.md) 3.1・5.1・6.1・8 章 Phase 3
- 前提: [Phase 2 計画](implemented/フェーズ2_単一HTML検針シート・MR1結果パーサ計画.md) 完了済み
- GitHub: `sho-kawasaki-dd/meter-reading-app-mini`

## 1. 目的

次の操作を UI だけで一通り行えるようにする。

- マスタ(物理メーター・測定定義・スケジュール・担当者)の登録
- 予定枠の生成・自動クローズ
- PC からの直接入力・訂正
- メーター交換の登録・訂正
- 設定(締め時刻・既定スケジュール・配信オフセット・表示設定)の変更

あわせて `__main__.py` を作り、`uv run python -m meter_reading_mini` で起動できるようにする。

## 2. 現状(2026-10-28 調査)

### 既にあるもの

| 機能 | 実装箇所 |
| --- | --- |
| メーター・担当者のマスタ操作 | [meter_master_service.py](../../src/meter_reading_mini/core/meter_master_service.py)、[staff_service.py](../../src/meter_reading_mini/core/staff_service.py) |
| 測定定義の登録(初期割当・初期履歴も 1 トランザクションで保存) | [measurement_definition_service.py](../../src/meter_reading_mini/core/measurement_definition_service.py) |
| スケジュール・配信オフセットの保存と予定枠の再構成 | [schedule_settings.py](../../src/meter_reading_mini/core/schedule_settings.py) |
| PC 直接入力の保存 | `ReadingWriteOrchestrationService.save_manual_reading`([reading_write_orchestration.py](../../src/meter_reading_mini/core/reading_write_orchestration.py)) |
| 交換の DB 反映(割当切替・境界作成・枠の `superseded` 化と後継枠生成) | `SQLiteRepository.reflect_replacement`([repositories.py](../../src/meter_reading_mini/db/repositories.py)) |

### 足りないもの

- **起動・画面**: [app.py](../../src/meter_reading_mini/app.py) は起動準備の後、画面を出さずに終了する。`__main__.py` もない。
- **一覧の取得**: MainWindow に必要な「業務日の全状態の枠 + 結果(`raw_reading`・力率の `structured_payload_json`) + マスタ名 + `warning_threshold`」の問い合わせがない。既存の取得処理はシート用で `open` と `auto_closed` しか返さない。
- **測定定義の一覧と取得項目**: `MeasurementDefinitionService` に管理画面用の一覧取得(`list_measurement_definitions`)がない。また `MeasurementDefinitionRecord` と `SQLiteRepository.get_measurement_definition` に `warning_threshold` と現在割当中の `meter_id` が含まれておらず、編集画面の初期表示やプレビューの閾値超過判定に足りない。
- **PC 用の交換 Service**: 交換は `MeterReadingSyncService.sync` の `replacement_items` 経由でしか実行できない。影響プレビュー・トランザクション・担当者確認・イベント ID 採番を持つ PC 用 Service が必要。
- **交換の訂正**: `reflect_replacement` は登録済みの交換に再実行しても境界の読値を更新しない。割当の検索条件が「旧メーターの割当が交換日時をまたぐこと」で、初回反映で旧割当が交換日時で終わるため、再実行では対象 0 件になる。訂正には `replacement_events` と `replacement_boundaries` の双方を直接更新する専用処理が必要(5.1)。同じ問題は `sync` 経由の再送にもある(別 issue、7 章)。
- **スケジュール・配信オフセットの履歴取得**: Repository には履歴一覧の取得があるが、`ScheduleSettingsService`・`MeasurementDefinitionScheduleService`・`ReleaseOffsetSettingsService` に履歴一覧を返す公開メソッドがない。また周期既定に戻す公開操作(NULL 履歴の保存)がない。
- **締め時刻の保存**: `CutoffSettingsService.save_cutoff_time` にトランザクションと入力形式の検証がない。
- **壁時計日時の検証・UTC 変換**: [timezone_settings.py](../../src/meter_reading_mini/core/timezone_settings.py) に、UI で入力した業務タイムゾーンの壁時計日時について存在しない時刻・重複する時刻(サマータイム)や未来日時を検証して UTC に変換する補助関数がない。
- **ID の扱い**: 作成要求が `meter_id`・`measurement_definition_id`・`staff_id` の入力を必須にしており、自動採番がない(`staff` テーブルのみ `INTEGER PRIMARY KEY` で整数 ID を要求する)。
- **場所名の一括変更**: 同じ `location_id` の全メーターへ場所名を反映する処理がない。
- **coverage ゲート**: CI の全体 coverage(`--fail-under=90`)は `ui/` と `app.py` も含む。UI テストを書いて基準を保つ。

## 3. 決定事項(2026-10-28 確認済み)

| # | 論点 | 決定 |
| --- | --- | --- |
| D1 | SettingsDialog | Phase 3 に全項目を入れる(締め時刻・周期別の既定スケジュール・配信オフセット・表示設定)。正本 8 章の Phase 3 を更新する。業務タイムゾーンは表示のみとする(予定枠が 1 件でもあれば変更できないため) |
| D2 | スケジュール編集 | 全形式を編集できる(日次の検針時間・週次の曜日・月次の指定・隔月の奇偶月)。適用開始日は現在業務日以降。適用履歴を表示し「周期既定に戻す」を付ける。編集部品は MasterDialog と SettingsDialog で共用する |
| D3 | 予定枠の補完・自動クローズのタイミング | 自動: 起動時、マスタ・スケジュール・設定の保存後、対象日の切替時。手動: 「予定枠を更新」ボタン |
| D4 | 対象業務日 | 現在業務日以前のみ選べる。未来日は選べない |
| D5 | ID の採番 | `meter_id`・`location_id`・`measurement_definition_id`・`replacement_event_id` は Service が自動採番する。採番関数は注入可能(既定は `uuid4().hex`)。`staff` は共通スキーマの `INTEGER PRIMARY KEY` に合わせ、DB の整数自動採番(文字列化して返却)を使い、同名担当者の重複は業務エラーにする。UI は名前を主に表示し、ID は詳細欄に出す |
| D6 | 設置場所 | 既存の場所から選ぶか新規入力する。場所名の変更は同じ `location_id` の全メーターへ 1 トランザクションで反映する。メーターの `location_id` は作成後に変更できない |
| D7 | 交換日時以降(`scheduled_at >= replaced_at`)に旧メーターの `completed` 枠がある場合 | 業務エラーで拒否する(先に訂正または非実測化してもらう)。旧メーター割当の有効期間も `effective_from < replaced_at` を必須とし、幅 0 の割当を作らない |
| D8 | 交換先メーター | 交換ダイアログ内で新規登録できる。メーター登録と交換を 1 トランザクションで確定する |
| D9 | 交換の訂正 | 旧最終読値・新初期読値・備考のみ訂正できる(有効な担当者の選択を必須とする)。交換日時・旧メーター・新メーターは変更不可。`replacement_events` と `replacement_boundaries` の両方を更新し、境界と後続実測値を同じトランザクションで再計算する。取消は行わない |
| D10 | 担当者の選択 | 各ダイアログで選ぶ。直前の選択をアプリ起動中だけ初期値として覚える(ファイルには保存しない) |
| P1 | 業務日・締め時刻の境界検出 | 60 秒ごとのタイマーで業務日と締め時刻の境界を確認し、D3 の補完・自動クローズを実行する(時計は注入する) |
| P2 | 直接入力・交換の日時入力 | 直接入力の初期値は既存結果があればその記録日時、なければ予定日時と現在時刻の早い方(交換日時の初期値は現在時刻)。未来の日時は拒否する。日時は業務タイムゾーンの壁時計で入力し、`core/timezone_settings.py` の補助関数で UTC に変換する。存在しない時刻・重複する時刻(サマータイム)は業務エラーにする |
| P3 | ツールバー | Phase 4・5 の機能(スマホ検針・集計ダッシュボード)の項目は、実装されるまで出さない |
| P4 | 数値入力 | `QDoubleSpinBox` を使わない。`QLineEdit` の文字列を core の Decimal 検証に通す |

## 4. 構成

- **組み立て処理(新規 `app_context.py`、パッケージ直下)**
  - 接続・Repository・Service を組み立て、UI には Service 群だけを渡す。
  - `ui/` からは `meter_reading_mini.db` も `sqlite3` も import せず、`app_context.py` からも `ui/` を import しない。依存境界テストに追加する。
- **新規 Service(`core/`)**

| Service | 役割 |
| --- | --- |
| `PlannedReadingMaintenanceService` | 補完と自動クローズを 1 トランザクションで実行する。起動時準備からも使う |
| `ReadingOverviewQueryService` | 一覧・件数の集計・警告の判定 |
| `ReadingPreviewService` | 直接入力の使用量プレビュー。DB を変更しない。Phase 4 の差分プレビューでも再利用する |
| `MeterReplacementService` | 交換の影響プレビュー・登録・訂正・履歴 |

- **新規 UI**

| ファイル | 内容 |
| --- | --- |
| `ui/main_window.py` | メイン画面 |
| `ui/models/reading_overview_model.py` | 一覧のテーブルモデル |
| `ui/dialogs/master_dialog.py` | マスタ管理 |
| `ui/dialogs/direct_reading_dialog.py` | PC 直接入力・訂正 |
| `ui/dialogs/meter_replacement_dialog.py` | メーター交換 |
| `ui/dialogs/settings_dialog.py` | 設定 |
| `ui/widgets/schedule_editor.py` | スケジュール編集部品 |
| `ui/widgets/decimal_line_edit.py` | Decimal 文字列の入力欄 |
| `ui/staff_selection_memory.py` | 直前の担当者の記憶 |

## 5. 実装タスク

各タスクは完了を検証してから `[x]` にする。ファイルが存在するだけでは完了としない。

### 5.1 PR-3a `feature/phase3-core-services`: core / db の追加(UI なし)

- [ ] 補完と自動クローズを `PlannedReadingMaintenanceService.refresh()` にまとめる。
  - 現在業務日まで補完し、自動クローズまでを 1 トランザクションで行う。
  - `StartupPreparationService` も内部でこれを使う。
  - 未完了の補完を成功として返さない。
  - 失敗時に全体がロールバックすることをテストする。
- [ ] 測定定義の取得項目拡張と管理用一覧取得を追加する。
  - `MeasurementDefinitionRecord` と `SQLiteRepository.get_measurement_definition()` に `warning_threshold` と現在割当中の `meter_id` を含める。
  - `MeasurementDefinitionManagementRepository` と `MeasurementDefinitionService` に `list_measurement_definitions(*, show_inactive: bool = False)` を追加する。
- [ ] 壁時計日時の検証・UTC 変換補助を [timezone_settings.py](../../src/meter_reading_mini/core/timezone_settings.py) に追加する(P2)。
  - 業務タイムゾーンの壁時計日時を受け取り、サマータイムの存在しない時刻・重複する時刻、および未来日時を業務エラーとして拒否した上で UTC 秒単位へ変換する。
  - DST 境界を含む単体テストを追加する。
- [ ] 一覧用の読取 Repository を追加する。
  - 業務日を指定し、全状態の枠と結果(`raw_reading` に加え力率の `structured_payload_json` を含む)・マスタ名(`location_name` は `physical_meters` を結合して取得)・`warning_threshold` を返す。論理削除済みのマスタも含める。
  - 並び順は `scheduled_at, planned_reading_id` とする。
- [ ] `ReadingOverviewQueryService` を作る。
  - 周期・状態・カテゴリ・場所・キーワードの絞り込みを純関数にする。
  - 絞り込み後の状態ごとの件数と警告件数を返す。警告は `result_status == 'measured'` かつ `warning_threshold is not None` かつ `usage_delta is not None` かつ `usage_delta > warning_threshold`(Decimal で比較)とする。
  - 未来日は業務エラーにする(D4)。
- [ ] `ReadingPreviewService.preview_manual_reading()` を作る。
  - 前回値・交換境界の判定は既存の共通化済みロジック([previous_reading.py](../../src/meter_reading_mini/core/previous_reading.py))を使う。
  - 次を返す: 前回値・出所・使用量・ロールオーバー発生・閾値超過・負の差分エラー。
  - DB を変更しないことをテストで確認する。
  - 確認するケース: 交換の前後、初期値の有無、`auto_closed` からの復活、訂正。
- [ ] 自動採番(D5)を入れる。
  - メーター・測定定義・担当者の作成要求から ID の入力をなくす。
  - `meter_id`・新しい場所の `location_id`・`measurement_definition_id`・`replacement_event_id` は注入可能な採番関数(既定 `uuid4().hex`)で Service が採番し、既存 ID と重複した場合は業務エラーにする。
  - `staff` は DB の `INTEGER PRIMARY KEY` で正の整数 ID を自動採番して文字列化し、同名担当者の重複登録は業務エラーにする。
  - 既存呼出し・既存テストを新しい作成要求へ更新する。
- [ ] `MeterMasterService.rename_location(location_id, name)` と `list_locations()` を作る(D6)。1 トランザクションで同じ場所の全メーターへ反映する。
- [ ] スケジュール・配信オフセットの履歴取得とリセットを追加する(D2)。
  - `MeasurementDefinitionScheduleService.reset_to_default()` を作る。NULL 履歴を保存し、同じトランザクションで予定枠を再構成する。
  - `ScheduleSettingsService`・`MeasurementDefinitionScheduleService`・`ReleaseOffsetSettingsService` に適用履歴一覧を取得する公開メソッドを追加する。
- [ ] `CutoffSettingsService.save_cutoff_time` に `transaction_factory` を持たせて 1 トランザクションで保存し、`HH:MM` の形式(分単位)を検証する。`app.py` 等の既存生成箇所も更新する。
- [ ] `MeterReplacementService.preview_replacement()` と `register_replacement()` を作る(D7・D8)。
  - 事前検証:
    - 担当者が有効であること
    - 旧メーターに、交換日時をまたぐ有効な割当(`effective_from < replaced_at` かつ `effective_to IS NULL OR effective_to > replaced_at`)があること
    - 新メーターが有効・同じ場所・未割当・旧メーターと別であること
    - 交換日時が未来でないこと(壁時計変換補助を使用)
    - 交換日時以降(`scheduled_at >= replaced_at`)に旧メーターの `completed` 枠がないこと(D7)
  - `preview_replacement()` は DB を変更せず、差し替えられる `open` / `auto_closed` 枠の件数と、旧メーターに紐づく各測定定義の境界使用量(ロールオーバー発生・負の差分エラーを含む)を返す。
  - `register_replacement()` は新メーターの任意登録・`upsert_replacement`・`reflect_replacement`・`recalculate_after_replacement` を 1 トランザクションで行う。
  - ロールオーバー未設定で負の差分になる場合は拒否する。
  - 確認すること: 割当の切替、枠の `superseded` 化、後継枠の生成。
  - 再計算の途中や COMMIT で失敗したとき、全体がロールバックすることをテストする。
- [ ] `MeterReplacementService.correct_replacement()` を作る(D9)。
  - 有効な担当者を確認し、`replacement_events`(旧最終読値・新初期読値・備考・`updated_by`・`client_updated_at`・`arrival_sequence`)と `replacement_boundaries`(旧最終読値・新初期読値・`updated_at`)の両方を更新する専用の Repository 処理を追加する。`reflect_replacement` は再実行しない。
  - `client_updated_at` は「現在時刻」と「既存値 + 1 秒」の遅い方にする。
  - 同一トランザクションで `recalculate_after_replacement` を実行し、境界の使用量と新メーターの後続実測値の再計算を単体・結合テストで確認する。
- [ ] 交換履歴の一覧(場所別、ローカル時刻で表示)の取得処理を追加する。
- [ ] `uv run ruff check`・`uv run ruff format --check`・`uv run pyright` を実行する。
- [ ] `uv run pytest --cov` と `coverage report` で、全体・`core/`・`db/` がそれぞれ 90% 以上であることを確認する。

### 5.2 PR-3b `feature/phase3-main-window`: 起動と MainWindow

- [ ] `app_context.py` で Service を組み立てる。
- [ ] `run_app` が起動準備の後に MainWindow を表示し、イベントループを回すようにする。
  - 起動順は既存の通り(ロック → ログ → DB → QApplication → 起動時準備 → 表示)。
  - ログ初期化後に `install_excepthook(resolved_paths.log_path)` を接続する。
  - 起動時準備に失敗したら MainWindow を表示しないことを維持する。
  - 既存の `tests/test_app.py` と両立できるよう、MainWindow の生成・イベントループ実行は注入可能にする。
- [ ] `__main__.py` を作り、`sys.exit(run_app())` を接続する。
- [ ] MainWindow の画面要素を作る。
  - 対象日の選択: カレンダー・前日/翌日・今日。翌日は現在業務日で無効にする(D4)。
  - 周期タブ、状態フィルタ、カテゴリ・場所・キーワードの絞り込み
  - `QTableView` と `QAbstractTableModel` による一覧(`raw_reading` の数値文字列と、力率の `structured_payload_json` の `%`・位相の両方を整形表示する)
  - 行の色: `open` は強調、警告は黄色、`auto_closed` はグレー。
  - ステータスバー: 状態ごとの件数・警告件数
- [ ] 補完・自動クローズの実行契機を作る(D3・P1)。
  - 自動: 対象日の切替時、ダイアログ保存後の通知、60 秒タイマーでの境界検出
  - 手動: 「予定枠を更新」ボタン
  - 業務エラーは `show_domain_error` で、予期しない例外は `run_guarded` で表示する。
- [ ] 行選択と操作の連動を作る。
  - 行を選ぶと「PC 直接入力・訂正」を有効にする。`superseded` の行では無効にする。
  - ツールバーに「マスタ管理」「メーター交換」「設定」を置く(P3)。
- [ ] 依存境界テストを追加する: `ui/` が `db`・`sqlite3` を import しないこと、`app_context.py` が `ui/` を import しないこと、`ui/`・`app.py`・`app_context.py` でも `core/timeutil.py` 以外が `datetime.now()` / `date.today()` を直接呼ばないこと。
- [ ] pytest-qt でテストする(`tmp_path` のファイル DB と固定した時計を使う)。
  - 表示・絞り込み・日付の制限・タイマーによる自動クローズ・更新ボタン
- [ ] `METER_READING_MINI_DATA_DIR` に一時フォルダを指定して起動し、DB のパスが一時フォルダ側になることを確認する。

### 5.3 PR-3c `feature/phase3-master-settings`: MasterDialog と SettingsDialog

- [ ] `ScheduleEditor` 部品を作る(D2)。
  - 周期に応じて入力の形を切り替える。日次は 1x/2x/3x の件数を満たすことを検証する。
  - 適用開始日は現在業務日以降にする。
  - 適用履歴を表示する。
- [ ] 物理メーター タブを作る。
  - 一覧(無効も表示する切替)、登録、編集
  - 場所は既存から選ぶか新規入力する。場所名は一括で変更する(D6)。
  - 無効化・再有効化。使用中のメーターを無効化しようとしたら `MasterInUseError` を表示する。
- [ ] 測定定義・スケジュール タブを作る。
  - 一覧(無効も表示する切替)、登録、編集(現在割当メーター・警告閾値を含む全項目の表示と更新)。
  - 登録時の初期メーターは必須で、同じ場所の有効なメーターから選ぶ。
  - 生成開始日の初期値は現在業務日。過去日を明示的に指定することもできる(保存後の D3 補完・自動クローズと連携して過去日からの枠生成・締めが行われることを確認する)。
  - 変更できない項目(場所・周期・値形式・計算方式)は編集時に無効化する。
  - 倍率・閾値・ロールオーバー値は Decimal 文字列で入力する(P4)。
  - 無効化・再有効化の前に、何が起きるかを確認ダイアログで示す。
  - スケジュールの編集と「周期既定に戻す」
- [ ] 担当者 タブを作る(登録・名前の変更・無効化・再有効化)。
- [ ] SettingsDialog を作る(D1)。
  - 締め時刻
  - 周期別の既定スケジュール(適用開始日付き)
  - 検針時間ごとの配信オフセット(適用開始日・適用履歴付き)
  - 表示設定(フォント・文字サイズ。`ui_settings.json` に保存し、すぐ反映する)
  - 業務タイムゾーン(表示のみ)
- [ ] 保存後に MainWindow へ通知し、補完・自動クローズを自動で実行する(D3)。
- [ ] pytest-qt で、各タブの正常な保存と業務エラーの表示をテストする。

### 5.4 PR-3d `feature/phase3-direct-reading-replacement`: 直接入力と交換

- [ ] DirectReadingDialog を作る。
  - 予定枠の情報を表示する。結果状態は `measured`/`skipped`/`unreachable`/`meter_fault` から選ぶ。
  - 測定値は Decimal 文字列で入力する(P4)。力率は % と位相で入力し、100% のときは位相を `unity` に固定する。
  - 記録日時(P2)、担当者(D10)、備考
  - 使用量プレビューは入力のたびに更新する。DB は変更しない。
  - 確定は `save_manual_reading` で行う。
  - 既存結果の訂正、`auto_closed` からの復活も扱う。
  - 後続の実測値が再計算されることを UI 経由のテストで確認する。
- [ ] MeterReplacementDialog を作る。
  - 場所・旧メーター・新メーターを選ぶ。新メーターはこの画面で新規登録もできる(D8)。
  - 交換日時(P2 の壁時計変換補助で検証)、旧最終読値、新初期読値、担当者(D10)、備考
  - 確定前に `MeterReplacementService.preview_replacement()` で影響を表示する: 差し替えられる枠の件数、旧メーターに紐づく各測定定義の境界での使用量。
  - 確定後に MainWindow を更新する。
- [ ] 交換履歴の表示と訂正(D9)を作る。訂正時も担当者(D10)を選び、訂正できない項目(交換日時・旧メーター・新メーター)は無効化する。
- [ ] pytest-qt でテストする。
  - 交換の登録 → `superseded` 化 → 新しい枠への直接入力 → 初期値が前回値として使われること
  - D7 の拒否
  - 訂正による再計算

### 5.5 完了記録 PR `feature/phase3-completion`

- [ ] 1366×768・文字サイズ 150% の環境で、全ダイアログが画面内に収まり、すべての操作に届くことを手動で確認し、記録する。
- [ ] 一時フォルダを指定した起動で、通しの手順を手動確認する: マスタ登録 → 予定枠の生成 → 締め時刻の経過による自動クローズ → 直接入力 → 交換 → 訂正。
- [ ] 正本へ反映する。
  - D1〜D10・P1〜P4 を 5.1・6.1 章へ統合する。
  - 8 章の Phase 3 に SettingsDialog を追加し、完了日を書く。
- [ ] README を更新する。起動手順の「未実装」の記述を外し、Phase 3 を完了にする。
- [ ] 両 OS の必須 CI が成功した後に merge し、この計画書を `git mv` で `docs/plan/implemented/` へ移す。相対リンクも直す。

## 6. 完了条件

- UI からマスタ登録・予定枠の生成と自動クローズ・PC 直接入力・メーター交換と訂正・設定の変更が一通りでき、5 章の検証で確認済みであること。
- 品質ゲートがすべて通ること: ruff、pyright(`core/`・`db/` は strict)、`QT_QPA_PLATFORM=offscreen uv run pytest --cov`。
- coverage が全体・`core/`・`db/` のそれぞれで 90% 以上であること。新しい skip の追加や閾値の引き下げはしないこと。

## 7. 計画外の既知の問題

`MeterReadingSyncService.sync` の交換経路でも、登録済みの交換イベントを読値・備考を変えて再送すると、`replacement_events` だけが更新され `replacement_boundaries` が更新されない(2 章の `reflect_replacement` と同じ原因)。Phase 3 の PC 訂正は専用処理(5.1)を使うため影響しない。別 issue として管理し、Phase 3 の完了条件には含めない。
