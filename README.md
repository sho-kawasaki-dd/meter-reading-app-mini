# 検針管理アプリ (Meter Reading mini)

施設内の電気・ガス・水道・ボイラー等の検針業務を管理する、ローカル完結型の PySide6 デスクトップアプリです。
PC で管理・集計し、現場では自己完結型の単一 HTML を Android の Chrome で開いて検針します。
HTML と結果は Quick Share 等で受け渡し、アプリ自身は外部通信や HTTP サーバーの起動を行いません。

備品在庫管理アプリ・業務日誌アプリとの将来統合を見据え、技術スタック・層構成・DB 接続規約・バックアップ基盤を共通化します。

## 開発状況と仕様の正本

現在はリポジトリ直下をプロジェクト基点として開発中です。
Phase 0 は完了しています。共通基盤・起動オーケストレーション (`app.py`)・品質ゲート設定・Windows / Ubuntu 用 CI workflow を整備し、GitHub の `main` 保護と必須チェックを設定しています。[本体 PR #1](https://github.com/sho-kawasaki-dd/meter-reading-app-mini/pull/1) で両 OS のチェック成功後に merge し、[失敗系検証 PR #2](https://github.com/sho-kawasaki-dd/meter-reading-app-mini/pull/2) で未成功チェックによる merge ブロックも確認済みです。
Phase 1 は完了しています。標準 sqlite3 Repository とコア業務ロジックを統合し、全品質ゲートが Windows / Ubuntu の必須 CI で成功しました。完了記録と作業計画書の凍結移動も済んでいます。メイン画面・起動用 `__main__.py`・スマホ検針の完成機能は後続フェーズの対象です。
Phase 2 は 2026-10-28 に完了しました。単一 HTML 検針シート、スマホ入力・IndexedDB 自動保存・途中経過 JSON、MR1 / 結果 JSON の出力から Python パーサ・同期までを実装し、全体・`core/`・`db/` の coverage 90% 以上と Windows / Ubuntu の必須 CI 成功を確認しています。[完了記録](docs/plan/implemented/フェーズ2_単一HTML検針シート・MR1結果パーサ計画.md) に詳細があります。Android 実機確認と PC 側の差分プレビュー・一括確定 UI は Phase 4 以降の対象であり、Phase 2 の完了には含めません。
以下の機能・操作・配布手順は、明記した既存基盤を除き開発計画上の仕様であり、利用可能な完成版の案内ではありません。

- **仕様の正本**: [ローカル検針管理アプリ開発計画書](docs/plan/ローカル検針管理アプリ開発計画書.md)
- **開発規約**: [Copilot 開発ガイドライン](.github/copilot-instructions.md)
- **旧構成の参考記録**: [旧企画書](docs/plan/archive/オフライン対応・Wi-Fi同期型_検針システム企画書_v4.md)

旧企画書は変更せず、仕様が異なる場合は開発計画書を正とします。旧企画書は旧サーバー・Android 構成の参考記録であり、その構成を現行アプリへ持ち込みません。

## 対象環境

- PC: Windows 10 以上、Linux (Python 環境を用意して実行)
- スマートフォン: Android / Chrome
- Python 3.13 以上 (`requires-python = ">=3.13"`)
- パッケージ管理: uv、ビルドバックエンド: hatchling
- 主要依存: PySide6、platformdirs、pydantic
- DB: SQLite (`sqlite3` 標準モジュール)
- PC 画面: 1366×768 以上、文字サイズ 150% でも操作可能な設計

運用は単一 PC・単一プロセスを前提とします。複数 PC からの同時 DB 編集、FastAPI / uvicorn、
mDNS 探索、ネイティブ Android アプリ、コードペアリング、端末ロックは対象外です。
実行時は完全オフラインで動作させますが、開発用依存や Chromium の初回取得にはネットワークが必要です。

## 主な機能 (実装予定)

- 物理メーター・測定定義・スケジュール・担当者の管理
- 日次・週次・月次・隔月の予定枠生成、未生成分の補完 (backfill)、締め時刻による自動クローズ
- 単一 HTML 検針シートの出力、結果テキスト / JSON の取込、差分プレビューと一括確定
- PC 直接入力・訂正
- ロールオーバー補正、メーター交換境界の計算、過去の訂正に伴う後続実測値の連鎖再計算
- 使用量集計、QtCharts によるグラフ、CSV / PNG 出力
- 手動バックアップ・検査付き復元、多重起動防止、フォント・文字サイズ設定
- Windows インストーラーによる配布

## 開発環境の準備

プロジェクト定義はリポジトリ直下の [pyproject.toml](pyproject.toml) にあります。
Python 3.13 と uv を用意し、リポジトリのルートから PowerShell で実行します。

```powershell
uv sync --locked
```

依存関係を追加・変更するときは `uv lock` で `uv.lock` を更新し、差分を確認してから
`uv sync --locked` でロックファイルどおりに同期します。通常の環境構築・CI では `uv sync --locked` を使用します。

## 検査・テスト

以下はリポジトリのルートで実行する共通の品質ゲートです。
検査対象は `pyproject.toml` に定義しています。Ruff・Pyright・pytest に対象外設定はありません。
coverage は `core/mobile_reading.py` を含むパッケージ全体を対象とし、全体・`core/`・`db/` の行 coverage それぞれ 90% 以上を必須にします。GitHub Actions では Windows / Ubuntu の両ジョブで各閾値を個別に検査します。

```powershell
uv run playwright install chromium
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest --cov
uv run coverage report --fail-under=90
uv run coverage report --include="*/meter_reading_mini/core/*" --fail-under=90
uv run coverage report --include="*/meter_reading_mini/db/*" --fail-under=90
```

- 型検査は `core/`・`db/` を strict、`ui/` を standard とします。
- Phase 0 で設定した coverage 対象の行カバレッジ目標は 90% 以上です。
- ブラウザテストは生成 HTML を `file:` URL で開き、HTTP サーバーを起動せず実施します。
- Playwright / Chromium 未準備を理由にブラウザテストをスキップしません。
- Ubuntu CI では `uv run playwright install --with-deps chromium` でシステム依存も準備します。

### Linux のヘッドレステスト

```sh
QT_QPA_PLATFORM=offscreen uv run pytest --cov
uv run coverage report --fail-under=90
uv run coverage report --include="*/meter_reading_mini/core/*" --fail-under=90
uv run coverage report --include="*/meter_reading_mini/db/*" --fail-under=90
```

通常の GUI 起動では `QT_QPA_PLATFORM=offscreen` を設定しません。
テスト DB は `:memory:` または `tmp_path` を使い、実データの保存先は使用しません。

## アプリの起動 (Phase 3 の画面・`__main__.py` 実装後)

起動オーケストレーション自体は `app.py` に実装済みですが、画面と起動用 `__main__.py` は未実装です。
そのため、現時点では次のコマンドでは起動できません。Phase 3 で画面・`__main__.py` を実装した後、
リポジトリのルートで実行する予定です。

```powershell
uv sync --locked
uv run python -m meter_reading_mini
```

Linux では Qt の OS 依存も必要です。Ubuntu 系の準備例:

```sh
sudo apt-get update
sudo apt-get install --yes libegl1 libgl1 libxkbcommon0 libdbus-1-3 libfontconfig1 libxcb-cursor0
uv sync --locked
uv run python -m meter_reading_mini
```

## 基本操作 (実装予定)

### 初期設定と PC 直接入力

1. 担当者、物理メーター、測定定義、周期別スケジュールを登録します。
2. 起動時・シート出力時に未生成の予定枠を補完します。締め時刻 (初期値 `07:00`) を過ぎると、
   前日以前の未消化枠が `auto_closed` になります。
3. 対象業務日・周期・状態・場所等で予定枠を選び、結果状態、測定値または力率、担当者、備考を入力します。
4. 過去枠への追加入力・訂正では後続の前回値・使用量も再計算します。
   `auto_closed` への後日入力も受け付け、確定後は `completed` に更新します。

設置場所 (`location_id`)、物理計器 (`meter_id`)、論理的な測定対象 (`measurement_definition_id`) は
分離して管理します。メーター交換後も場所を不変の集計キーとして履歴を継続します。
使用中マスタは論理削除で保護します。

### スマホ検針

1. PC から対象業務日の検針シートを出力します。既定は 1 日分の全枠で、特定の検針時間・周期にも絞れます。
2. HTML を Quick Share 等で Android へ渡し、Files から Chrome で開きます。
   CSS・JavaScript・対象データは HTML 内に含め、外部通信は行いません。
3. 検針時間のタブ、場所別表示、未入力フィルタを使い、実測・見送り・検針不能・計器不良を入力して確認します。
   同日先行枠の実測値は、後続枠の表示上の前回値・参考使用量に引き継ぎます。
4. **確認済みの正常な枠が 1 件以上あれば、未確認枠が残っていても結果を出力できます。**
   未確認枠は結果に含めません。全件確認が必要な在庫アプリの棚卸とは異なります。
5. `MR1:<Base64>.<CRC32>:END` 形式の結果テキスト、または結果 JSON を PC へ戻します。
   コピー・保存に失敗した場合は、結果テキスト表示から手動コピーします。
6. PC で結果を解析し、前回値・今回値・使用量・警告・上書き区分をプレビューします。
   確定担当者を選び、単一トランザクションで一括確定します。

入力の変更・再読込開始・解析失敗時には旧プレビューを破棄し、再プレビューまで確定できない設計とします。
スマホの参考計算だけを信用せず、PC 側でも再検証・再計算します。

#### 途中保存と回復

- IndexedDB に `sheet_id` ごとの途中状態を自動保存します。`localStorage` は使用しません。
- 「保存済み」は最新変更の保存トランザクション完了を示し、電源断後まで保証する表示ではありません。
- 空欄・入力エラーを含む途中状態も保持しますが、不正な枠は確認済みになりません。
- 保存失敗・既存データの読込失敗・別タブとの競合は警告し、自動上書き・自動マージしません。
- 途中経過 JSON を手動保存・読込できるようにします。同一シートであることを検証し、
  承認後に現在の入力全体を置き換えます。途中経過 JSON は PC 側の結果取込には使用できません。
- HTML・結果・途中経過 JSON は業務情報を含むため安全に保管し、不要になったら手動で削除します。
  結果出力・確定後も自動削除しません。

開発計画書にある Android の復元条件は、在庫アプリでの実機確認を基準にしたものです。
検針アプリ自身の実機確認済み実績ではありません。検針シートでも実機検証を行い、保証範囲を確定します。
別ブラウザ・プライベートモード・HTML 移動後の自動復元や、ブラウザデータ消去後の回復は保証しません。
アプリ更新によって配布済み HTML やその途中データが自動更新されることもありません。

### メーター交換と使用量計算

- 数値・倍率・閾値は Python では `decimal.Decimal`、DB / JSON では plain decimal 文字列とします。
- 通常の差分は `(今回値 - 前回値) × 倍率` です。ロールオーバー設定がある場合は桁あふれを補正し、
  設定のない負の差分は PC 側で異常値として拒否します。
- `raw_only` は差分を計算しません。
- 交換時は旧メーター最終値による使用量と新メーター初期値を記録します。
  新メーター初回は初期値を前回値とし、初期値がない場合に限り初回使用量を 0 とします。
- 旧メーターの未消化枠を `superseded` にし、新メーターの予定枠を生成します。

### 集計・CSV 出力

日次・月次・年次、カテゴリ・場所・測定定義で集計し、グラフと内訳を確認する設計です。
二重計上を防ぐため、検針目的の初期選択は `operational` とし、`billing`・`audit` と区別します。
時刻列は UTC 保存、業務日付・検針枠・締め判定はローカルタイムゾーンを基準とします。
CSV の日時はローカル時刻で出力し、文字列セルの数式注入を防止します。

## データ保存先

パス解決は [config.py](src/meter_reading_mini/config.py) に実装済みです。

- DB・バックアップ・表示設定・ロック: `%APPDATA%\meter-reading-mini`
- ログ: `%LOCALAPPDATA%\meter-reading-mini\Logs`
- DB 名: `meter_reading.db`、ロック: `meter_reading.lock`、表示設定: `ui_settings.json`

起動前に `METER_READING_MINI_DATA_DIR` を設定すると、DB 等は指定先、ログはその `logs` 配下になります。
Phase 3 でアプリを起動できるようになった後、一時的な動作確認では次のように通常データと分離します。

```powershell
$env:METER_READING_MINI_DATA_DIR = Join-Path $env:TEMP ("meter-reading-mini-" + [guid]::NewGuid().ToString('N'))
uv run python -m meter_reading_mini
```

Linux の指定例:

```sh
export METER_READING_MINI_DATA_DIR="$HOME/.local/share/meter-reading-mini-test"
uv run python -m meter_reading_mini
```

### バックアップ・復元 (実装予定)

`sqlite3.Connection.backup()` で手動バックアップを作成します。
復元元は読み取り専用で開き、版別スキーマ・SQLite 整合性・外部キーを検査します。
一時 DB で移行・Decimal 値・結果状態・交換境界の業務整合性を検証し、
現行 DB を `backups/pre-restore_日時.db` に保全してから置き換え、再起動します。
端末ごとの表示設定は復元対象に含めません。

## Windows 配布・ライセンス方針 (配布基盤の実装時に適用)

配布基盤は未実装です。備品在庫管理アプリと同じく PyInstaller と Inno Setup 6.7.3 を採用し、
次の運用を適用します。現時点ではインストーラーやビルドスクリプトがあることを意味しません。

- リリースタグは `v<版数>` / `v<版数>-rc<N>` とし、基本版数を `pyproject.toml` と一致させます。
- タグは `main` に含まれるコミットに付け、push 前に利用者の確認を得ます。
- Release は下書きで作成し、公開は手動とします。
- インストーラー、アプリと必要な依存の対応ソース、構成一覧、各ファイルの SHA256 を同じ Release に添付します。
- インストール前に SHA256 を照合します。失敗したビルドや過去の出力を今回の成果物として配布しません。
- 自作コードは MIT、Qt Charts を含むアプリ全体は GPLv3 で配布する方針です。
  ライセンス本文・第三者通知・対応ソース提供を配布開始までに整備します。
  Qt / PySide6 の全モジュールを一律に LGPL とみなしません。
- バイナリ提供中は対応ソースも保持し、GitHub 自動生成 ZIP や上流 URL の案内だけで代用しません。
- アンインストールではデータ・ログを保持する設計とします。完全削除はバックアップ後に利用者が手動で行います。
- インストール・更新・復元後の再起動・既定保存先の検証は、実データのない Sandbox / VM で行います。
  開発 PC では一時保存先を設定して exe を直接起動し、表示される DB パスを確認します。

## ディレクトリ構成と主なモジュール

```text
.
├── README.md
├── .github/
│   └── copilot-instructions.md
├── docs/
│   └── plan/
│       ├── archive/
│       │   └── オフライン対応・Wi-Fi同期型_検針システム企画書_v4.md
│       ├── implemented/
│       │   ├── フェーズ0_共通基盤・CI整備計画.md
│       │   ├── フェーズ1_sqlite3統合・コア業務ロジック移植計画.md
│       │   └── ルート配置移行計画.md
│       └── ローカル検針管理アプリ開発計画書.md
├── pyproject.toml
├── uv.lock
├── src/
│   └── meter_reading_mini/
│       ├── app.py
│       ├── config.py
│       ├── core/
│       ├── db/
│       └── ui/
└── tests/
```

以下の `core/`・`db/`・`ui/` は `src/meter_reading_mini/` 配下です。

| ファイル / ディレクトリ | 役割 |
| --- | --- |
| `src/meter_reading_mini/app.py` | 起動処理を統括し、DB と起動時準備を画面表示前に実行 |
| `src/meter_reading_mini/config.py` | アプリデータ・ログ等の保存先を解決 |
| 各 `__init__.py` | `meter_reading_mini`・`core`・`db`・`ui` を Python パッケージとして定義 |
| `core/contract_models.py` | Enum、DTO、Decimal 文字列等のデータ契約 |
| `core/errors.py` | 業務例外の基底型と共通例外 |
| `core/logging_config.py` | ローテーション付きログの設定 |
| `core/measurement_definition_service.py` | 測定定義の業務操作・入力検証 |
| `core/meter_master_service.py` | 物理メーターの業務操作・入力検証 |
| `core/meter_reading_sync_repositories.py` | Repository Protocol と入出力レコード |
| `core/meter_reading_sync.py` | 検針結果・交換イベントの保存処理 |
| `core/decimal_arithmetic.py` | Decimal の丸めなし加減算・乗算 |
| `core/mobile_reading.py` | MR1 結果テキスト / JSON の検証と同期要求への変換 |
| `core/mobile_reading_sheet.py` | 検針シートの枠・前回値・単一 HTML 生成 |
| `core/mobile_reading_assets/` | HTML に内包する検針画面の CSS / JavaScript |
| `core/previous_reading.py` | 予定枠ごとの前回値と交換境界の判定 |
| `core/reading_signature.py` | 監査用 SHA-256 署名の生成・検証 |
| `core/reading_write_orchestration.py` | 保存前の履歴照合、交換境界適用、後続値の再計算 |
| `core/schedule_settings.py` | 周期・検針枠等のスケジュール設定 |
| `core/staff_service.py` | 担当者の業務操作・入力検証 |
| `core/startup_preparation.py` | 起動時の既定値投入、予定枠補完、自動クローズ |
| `core/timeutil.py` | 現在時刻の取得、UTC とローカル時刻の変換 |
| `core/timezone_settings.py` | タイムゾーン・業務日付・予定日時の計算 |
| `core/usage_delta_calculation.py` | Decimal による丸めなし使用量差分・ロールオーバー計算 |
| `core/work_package_download.py` | 予定枠の補完生成と検針用データの構築 |
| `db/connection.py` | sqlite3 接続、PRAGMA 設定、トランザクション管理 |
| `db/schema.sql` | SQLite のテーブル・制約定義 |
| `db/migrations.py` | スキーマの移行と版別検査 |
| `db/repositories.py` | 標準 `sqlite3` による Repository 実装 |
| `db/bootstrap_seeds.py` | DB の既定設定の初期投入 |
| `db/backup.py` | SQLite バックアップ関連処理 |
| `ui/single_instance.py` | QLockFile による多重起動防止 |
| `ui/display_settings.py` | フォント・文字サイズ等の表示設定 |
| `ui/dialog_placement.py` | ダイアログの画面内配置 |
| `ui/error_handling.py` | 業務例外・予期しない例外の UI 通知 |
| `ui/csv_export.py` | CSV 等のファイル出力ダイアログ |
| `tests/` | コア業務ロジック、DB、UI 共通部品の自動テスト |

`core/` は純 Python の業務ロジック、`db/` は SQLite 永続化、`ui/` は PySide6 の UI 共通部品を配置します。`db/repositories.py` が標準 `sqlite3` Repository を実装しています。旧 SQLAlchemy 参考資産は統合テスト成功後に削除済みです。

## 開発フェーズ

| フェーズ | 状況 | 主な対象 |
| --- | --- | --- |
| Phase 0 | 完了 | リポジトリ骨格・共通基盤・品質ゲートの整備 |
| Phase 1 | 完了 | 標準 sqlite3 Repository と既存コア業務ロジックの統合 |
| Phase 2 | 完了 (2026-10-28) | 単一 HTML 検針シート・途中保存・MR1 / JSON パーサと DB 同期のブラウザ往復テスト |
| Phase 3 | 予定 | メイン画面・マスタ・PC 直接入力・交換 UI |
| Phase 4 | 予定 | スマホ検針ダイアログ・プレビュー・一括確定 |
| Phase 5 | 予定 | 集計・出力・バックアップ / 復元・インストーラー |

成果物と完了条件は開発計画書 8 章を参照してください。資産の同梱だけで各フェーズの完了とは扱いません。
Phase 2 は Playwright によるブラウザ検証まで完了しています。Android 実機での検証は未実施で、端末・Chrome の版・起動経路・HTML 移動時の復元可否を Phase 4 以降に確認します。

## commit / PR / merge 方針

備品在庫管理アプリと同じ方針を適用します。

- `main` は常にテスト成功状態を保ち、`feature/<フェーズ>-<内容>` ブランチで作業します。
- `main` へ直接 push せず、feature ブランチを push して PR を作成し、CI 成功後にマージします。
- 必須チェック `test (windows-latest)`・`test (ubuntu-latest)` と `main` のブランチ保護は設定済みです。
  workflow やジョブ名を変更するときは、必須チェック名も同時に更新します。
- コミットは Conventional Commits (`feat:`、`fix:`、`test:`、`docs:`、`refactor:` 等) に従い、
  メッセージ本文・UI テキスト・ドキュメント・コード内コメントは日本語を使います。
- 新機能・仕様変更は実装計画書を作成し、検証済みタスクにチェックを入れます。
  完了時に正本へ確定仕様を統合し、作業用計画書を `docs/plan/implemented/` へ移して凍結します
  (作業用計画書の文書基点は `docs/plan/`、開発計画書の正本も `docs/plan/` です)。
