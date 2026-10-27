# Meter Reading mini (検針管理アプリ) 開発ガイドライン

## プロジェクト概要・仕様の正本

- 基本設計・業務ルールの正本は [ローカル検針管理アプリ開発計画書](../docs/plan/ローカル検針管理アプリ開発計画書.md) です。旧企画書と異なる場合は開発計画書を優先します。
- [旧企画書](../migration_package/docs/plan/オフライン対応・Wi-Fi同期型_検針システム企画書_v4.md) は旧サーバー・Android 構成の参考記録として変更しません。旧アーキテクチャを新構成へ持ち込みません。
- 現状と実行手順は [README](../README.md)、移植手順は [移行ガイド](../migration_package/README.md) を参照します。同梱資産を完成済みアプリと扱いません。
- 現在のプロジェクト基点は `migration_package/` です。以下の `src/`・`tests/`・`pyproject.toml` 等は、特記しない限りこの基点からの相対パスです。開発計画書・作業用計画書はリポジトリ直下の `docs/plan/` にあります。`migration_package/docs/plan/` には旧企画書を残しています。
- 正式なルート配置へ移行するときは import・設定・文書リンク・コマンドの実行位置を一緒に更新します。文書整備だけでソースを移動しません。
- UI テキスト・ドキュメント・コミットメッセージ・コード内コメントは日本語を使用します。

## アーキテクチャと依存関係

- 業務処理の呼び出しは `ui/ (PySide6) → core/ の Service (純 Python) → db/ (sqlite3)` の一方向です。
- `core/`・`db/` は PySide6 を import しません。UI は Service 経由で操作し、SQL・Repository を直接扱いません。
- Service は `core/meter_reading_sync_repositories.py` の Protocol を介して標準 sqlite3 Repository を使用します。
- `core/contract_models.py`・`core/errors.py`・Repository Protocol / 入出力レコードは独立した契約です。`db/` から契約の参照は許可しますが、Service・UI への依存は禁止します。契約から Service・UI・DB 実装への依存も禁止します。
- 開発計画書の概念上の `core/models.py` に対応する既存ファイルは `core/contract_models.py` です。名称違いを理由に契約を重複定義しません。
- 既存サービス・時刻補助・契約・共通 UI 部品を再利用します。将来の在庫・業務日誌との統合を妨げる密結合を避けます。
- `db/` は標準 sqlite3 Repository を使用します。新規依存として SQLAlchemy / Alembic、FastAPI / uvicorn / zeroconf 等を追加しません。
- 旧 import・ORM の commit / rollback は標準 sqlite3 実装と Service のトランザクションへ移行済みです。純粋な日時補助は `core/timezone_settings.py` に置きます。

## データベース・トランザクション

- `sqlite3.connect(path, autocommit=True)` を使用し、接続時に `foreign_keys = ON`、`journal_mode = WAL`、`busy_timeout = 5000` を設定します。読取専用接続は既存ヘルパーに従います。
- Service がトランザクション境界を持ち、1 業務操作を 1 トランザクションにします。`transaction(conn)` による `BEGIN IMMEDIATE`・`COMMIT`・`ROLLBACK` に統一し、暗黙の `conn.commit()`・`with conn:` は使いません。
- 結果保存・監査署名・交換境界・予定枠更新・後続再計算は、対応する業務操作と同一トランザクションで整合させます。
- Service の事前検証と DB の CHECK / UNIQUE / FOREIGN KEY を併用します。低レベル DB 例外は Repository で適切な `DomainError` 派生例外へ変換します。
- SQL の値はプレースホルダで渡し、利用者入力を f-string 等で SQL へ埋め込みません。
- スキーマ変更時は `db/schema.sql`、`db/migrations.py` の移行ステップ・`SCHEMA_VERSION`・版別検査定義を同時に更新します。
- 各版の移行・`PRAGMA user_version` 更新・版別スキーマ検査を同一トランザクションで実施します。アプリ版数とスキーマ版数は独立管理します。
- 将来統合に備え検針設定は `meter_app_settings` とし、`staff` は共通スキーマと整合させます。

## 数値・時刻・検針整合性

- `location_id` (集計場所)、`meter_id` (物理計器)、`measurement_definition_id` (論理測定対象) を分離します。交換で場所の集計キーを変えません。
- 測定値・倍率・閾値・ロールオーバー値・使用量は `decimal.Decimal` で計算し、DB / JSON は有限の plain decimal 文字列で保持します。業務数値に `float` を使いません。ブラウザも浮動小数点による丸めを業務計算へ持ち込まない実装にします。
- `raw_only` は `usage_delta = None`、`delta` / `decimal_string` は前回実測との差分に倍率を乗じます。
- 今回値が前回値未満なら、設定済み `rollover_value` で補正します。未設定の負の差分は PC 側で拒否します。スマホ側の警告は現場の実数記録自体を妨げません。
- 起動時・シート出力時に未生成予定枠を backfill します。締め時刻 (初期値 `07:00`) 経過後、前日以前の `open` を `auto_closed` にします。
- `auto_closed` への後日入力は現場入力を優先し、`completed` にして再計算します。
- 過去の追加・訂正は同一測定定義の後続実測値を `scheduled_at, planned_reading_id` 順に連鎖再計算します。PC 直接入力とスマホ取込で同じオーケストレーションを使用します。
- 交換時は旧メーター最終使用量と新メーター初期値を交換境界に保持します。新初回実測の前回値は初期値とし、初期値未指定の場合のみ初回使用量を 0 にします。
- 交換対象の `open` / `auto_closed` 枠を `superseded` にし、新メーター枠の生成・割当変更・交換記録を原子的に実行します。
- 予定枠状態 (`task_state`) と結果状態 (`result_status`) を混同しません。使用中マスタは論理削除で保護します。
- 集計は `purpose` を区別し、初期選択を `operational` とします。`billing` / `audit` を無条件に合算して二重計上しません。
- DB の時刻列は UTC 文字列で保持し、既存契約の形式を維持します。表示・CSV はローカル時刻、予定生成・締め・集計はローカルの `business_date` と `reading_session` を基準とします。
- 現在時刻取得・変換は `core/timeutil.py`、業務タイムゾーン・予定日時計算は `core/timezone_settings.py` 等の既存ヘルパーへ集約します。他の箇所で `datetime.now()` や SQLite の `'localtime'` に依存しません。
- 時刻列で期間検索する場合はローカル暦の境界を UTC に変換して半開区間 `[start, end)` を使い、SQL 内でタイムゾーン補正しません。

## スマホ検針・取込の安全規約

- 詳細は開発計画書 5.2・6・8 章に従います。HTML は CSS・JavaScript・データを内包し、外部通信・CDN・HTTP サーバー・`localStorage` に依存させません。
- 出力既定は対象業務日 1 日の全枠とし、検針時間・周期での絞り込みを提供します。`sheet_id` は 32 文字の小文字 16 進数です。
- 検針時間ピルタブ、場所別グルーピング・絞り込み、未入力フィルタ、次の入力への導線を維持します。同日先行枠の入力を後続枠の前回値・参考使用量へ反映します。
- IndexedDB (`mrm_meter_reading`、版数 1、`drafts`) にシート単位で自動保存します。最新変更の書込トランザクション完了前に「保存済み」と表示しません。
- 不正・途中入力も保持しますが、業務条件を満たさない枠は確認済みにしません。測定値・状態・備考の変更で確認を解除します。
- 保存失敗・既存データ読込失敗・別タブ競合を明示し、入力を維持します。確認できない既存データの自動上書きや競合の自動マージは禁止します。
- 途中経過 JSON は同一シート・対象条件等を検証し、利用者の確認後に全体置換します。IndexedDB が利用できなくても JSON 表示・手動コピー / 貼付による退避・回復を提供します。
- 結果は確認済み正常枠が 1 件以上あれば部分出力を許可します。未確認枠を含めず、全件確認必須の棚卸ルールを流用しません。
- 結果テキストは UTF-8 JSON → 標準 Base64 → Base64 文字列の CRC-32 (小文字 16 進 8 桁) による `MR1:<Base64>.<CRC32>:END` です。結果 JSON とのパーサ互換性を維持します。
- 前後のメール本文・折返し・行頭引用 `>` を許容し、同一結果ブロックの重複は 1 件化します。異なる結果ブロックが複数あれば自動選択せず拒否します。
- 途中経過 JSON は PC の結果取込で拒否します。CRC-32 は破損検出、SHA-256 署名は監査用途であり、送信者認証とみなしません。
- 入力編集・ファイル / クリップボード再読込開始・解析 / プレビュー失敗時は旧プレビューと確定データを破棄します。正常プレビューした入力そのものだけを再検証後に確定します。
- プレビュー中は DB を変更しません。有効な確定担当者選択後、PC 側で再計算し単一トランザクションで確定します。`device_id = sheet_id`、`updated_by = 確定担当者名`、`result_source = 'mobile_html'` と監査署名を記録します。
- コピー・保存失敗時は結果テキストの手動コピーを案内します。ダウンロード要求だけで保存成功を断定しません。業務情報を含む HTML・JSON・ログを Git / Release に含めません。
- 在庫アプリの Android 実機確認を検針アプリの実績と記載しません。検針シートを実機検証し、端末・Chrome・起動経路・移動時の復元可否を記録します。

## 例外・ログ・CSV

- 業務例外は `core/errors.py` の `DomainError` 派生として定義します。UI は業務エラーを表示し、予期しない例外はログ記録と汎用エラーダイアログで通知します。例外を握りつぶしません。
- ログは障害調査用途に限定し、業務記録の正本は DB とします。
- CSV は `=`・`+`・`-`・`@`・タブ・CR で始まる文字列セルに `'` を付けます。数値列は対象外とします。

## 起動・保存先・バックアップ

- 起動順は「`QLockFile` 多重起動チェック → ログ初期化 → DB オープン・マイグレーション → 予定枠補完・自動クローズ → MainWindow 表示」とし、DB を開く前にロックを取得します。
- 保存先は `config.py` に集約します。既定のアプリ名は `meter-reading-mini`、DB は `meter_reading.db`、ロックは `meter_reading.lock` です。
- 動作確認は `METER_READING_MINI_DATA_DIR` に一時保存先を指定し、実データと分離します。指定時のログはその `logs/` 配下です。
- バックアップは `sqlite3.Connection.backup()` を使います。復元は開発計画書 7 章の順序 (読取専用検査 → 一時コピー・移行 → 業務整合性検査 → 現行 DB 自動保全 → 置換・再起動) を守ります。
- 復元では Decimal 値・状態と値の対応・交換境界も検証します。表示設定 `ui_settings.json` は DB 復元の対象外です。

## コマンド体系・品質ゲート

開発・テスト・静的検査は `uv` 経由で、現在は `migration_package/` で実行します。

- 初回同期: `uv sync`。現状は `uv.lock` 未同梱です。確定後はロックファイルを管理し `uv sync --locked` に統一します。
- 整形・静的検査: `uv run ruff check` / `uv run ruff format --check`
- 型検査: `uv run pyright` (`core/`・`db/` strict、`ui/` standard)
- ブラウザ準備: `uv run playwright install chromium` (Linux CI: `uv run playwright install --with-deps chromium`)
- テスト: `uv run pytest --cov`。Linux ヘッドレス環境では `QT_QPA_PLATFORM=offscreen` を設定します。
- カバレッジ: `uv run coverage report --include="src/meter_reading_mini/core/*,src/meter_reading_mini/db/*" --fail-under=90`
- 起動処理実装後: `uv run python -m meter_reading_mini`。現時点では起動モジュールは未整備です。

旧 import 等の未移植部分による失敗を、成功・完了と報告しません。実行した検査と未実施・失敗理由を区別します。

## 実装・テスト方針

- `core/`・`db/` の行カバレッジ 90% 以上を維持します。変更をカバーする最小の検査から実行します。
- ロジックは `:memory:`、WAL・永続化・移行・バックアップ / 復元は `tmp_path` 上のファイル DB で検証します。実データ領域を使いません。
- 層の依存方向・PySide6 非依存・契約の独立性・時刻ヘルパー以外の現在時刻取得禁止をテストで検査します。
- 時刻を固定・注入して、ローカル業務日付・締め境界・サマータイム・予定枠順序を検証します。共通フィクスチャは `tests/conftest.py` へ置きます。
- 正常保存に加え、結果保存後・交換境界更新中・後続再計算中の例外や COMMIT 失敗でも全体がロールバックすることを検証します。
- Decimal 精度・ロールオーバー・初期値有無・交換前後・遡及訂正・自動クローズからの復活・`purpose` ごとの集計を検証します。
- Playwright は生成 HTML を `file:` URL で開きます。Chromium 不足をスキップせず、MR1 / JSON を JavaScript → Python パーサ → DB 保存まで往復検証します。
- IndexedDB の再開・途中入力・障害・非同期競合、途中経過 JSON の往復・別シート / 破損拒否、部分結果出力、旧プレビュー無効化をテストします。
- 外部通信・`localStorage` 依存・JavaScript エラーがないこと、スマホ縦横と PC の全操作到達性を検証します。

## commit / PR / merge 方針

- 備品在庫管理アプリと同じく、`main` は常にテスト成功状態を保ち、`feature/<フェーズ>-<内容>` ブランチで作業して PR でマージします。
- `main` へ直接 push しません。feature ブランチを push して PR を作り、必須 CI 成功後にマージします。
- 必須チェックは `test (windows-latest)`・`test (ubuntu-latest)` で、CI workflow は `.github/workflows/ci.yml` に整備済みです。`main` の保護設定と未成功チェック時のマージブロックは Phase 0 で確認済みです。
- CI 導入時は両ジョブとリポジトリルールをそろえ、ジョブ名変更時には必須チェック名も更新します。
- `main` 上に誤コミットした場合は `git switch -c <ブランチ名>` で保全します。ローカル `main` を戻す必要がある場合のみ、作業ツリーと `origin/main` を確認して `git branch -f main origin/main` を使い、未保全の作業・コミットを失わせません。
- Conventional Commits (`feat:`、`fix:`、`test:`、`docs:`、`refactor:` 等) に従い、内容は日本語で記述します。

## 実装計画書の運用

- 新機能・仕様変更を伴う改修はリポジトリ直下の `docs/plan/` に作業用計画書を作り、目的・要件・段階的な具体タスク・完了確認用チェックボックスを記載します。
- 各タスクは完了を検証してからチェックを入れます。資産が存在するだけで完了扱いにしません。
- 完了時は確定した設計・業務ルールを正本の該当章へ統合し、作業用計画書を完了 (凍結) として `docs/plan/implemented/` へ `git mv` します。移動後の相対リンクも修正します。
- 完了済み計画書は記録として変更しません。検針アプリのフェーズ計画は開発計画書 8 章であり、在庫アプリの Phase 8 完了記録や 9.2 節を存在するものとして参照しません。

## パッケージング・リリース

- 配布基盤は未実装です。整備時は開発計画書の Phase 5 と下記の在庫アプリ共通方針を満たし、確定手順を正本・README に反映します。
- Windows 配布は PyInstaller と Inno Setup 6.7.3 を使用します。ビルドごとに新しい出力先を作り、成功時に確定した成果物だけを配布します。
- タグは `v<版数>` / `v<版数>-rc<N>` (N は正の整数) とし、基本版数を `pyproject.toml` に一致させます。`main` に含まれるコミットに付け、push 前に利用者の確認を得ます。
- Release は下書きで作成し、公開は手動です。インストーラー・アプリと必要な依存の対応ソース・構成一覧・各ファイルの SHA256 を同じ Release に添付します。
- 自作コードは MIT、Qt Charts を含むアプリ全体は GPLv3 で配布する方針とし、配布前に本文・第三者通知・対応ソースの案内を整備します。Qt / PySide6 を一律に LGPL とみなしません。
- バイナリ提供中は対応ソースも保持し、自動生成 ZIP・上流 URL の案内だけで代用しません。DB・バックアップ・ログ・個人情報は配布しません。
- 手動の配布検証は実データのない Sandbox / VM で行います。開発 PC では一時保存先を指定して exe を直接起動し DB パスを確認します。既定保存先を使うインストール・初回起動・復元後再起動を行いません。
- 同版再インストール・異版更新・起動中の更新 / 削除検出・データ保持・オフラインの法的通知参照・対応ソースからの再ビルドを確認します。
