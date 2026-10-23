# Phase 0 共通基盤・CI 整備計画

- 作成日: 2026-10-23
- ステータス: 作業計画(2026-10-23 レビューとベースライン実測を反映して改訂)
- 対象: `migration_package/` を基点とする Phase 0
- 正本: [ローカル検針管理アプリ開発計画書](ローカル検針管理アプリ開発計画書.md)
- GitHub: `sho-kawasaki-dd/meter-reading-app-mini`

## 1. 目的

同梱済みの共通基盤を再実装せず、ローカル品質ゲートと Windows / Ubuntu CI を整備する。保存先・多重起動防止・ログ・UI 表示設定を起動ライフサイクルで接続し、各 OS の必須チェックと GitHub の `main` 保護を実証する。

## 2. 現状と前提

- `config.py`、`db/connection.py`、`db/migrations.py`、`ui/single_instance.py`、`ui/display_settings.py` 等の基盤部品と単体テストは存在する。
- 起動オーケストレーション、標準 sqlite3 Repository、アプリ画面、CI workflow は未整備。
- ログ設定の既定保存先(`get_default_log_dir()` はパッケージ相対の `logs/`)と `AppPaths.log_dir` が一致していない。`get_default_log_dir()` の挙動は変更せず、起動処理が `AppPaths.log_dir` を必ず明示してログを構成する。
- `migration_package/uv.lock` を作成し、依存解決を固定した。以降 `uv sync --locked` を使用する。
- `migration_package/README.md` は Phase 0 完了済みと読める記述があるが、正本では CI と `main` 保護が未完了。両者を整合させる。
- GitHub リポジトリは作成済み。作業は `main` へ直接 push せず、`feature/phase0-foundation-ci` 等の feature ブランチから PR を作る。
- リポジトリの公開設定・プランで Ruleset / branch protection が使えるかは未確認(4.6 で最初に確認する)。

### 2.1 ベースライン実測(2026-10-23、Windows、`uv sync` 直後の一時コピー)

全体を対象にした現状は次のとおり。Phase 0 の共通基盤は概ね通過するが、Phase 1 で統合する未移植資産が全体ゲートを不通過にしている。

| ゲート | 全体の結果 | 主因 |
| --- | --- | --- |
| `ruff check` | 7681 件(W191 タブ 7290、E501 291 ほか) | Phase 1 資産(`core/*` の移植コード、`db/reference_sqlalchemy/`、移植テスト)のタブインデント・長行 |
| `ruff format --check` | 63 ファイル中 31 ファイルが対象 | 同上 |
| `pyright` | 2427 件(`db/reference_sqlalchemy` 1579、他の `src` 841、`tests` 7) | strict 配下の Unknown 系、`sqlalchemy`(20 件、未導入)、旧プロジェクト由来の `api` / `application` / `core` import(54 件)の未解決 |
| `pytest` | 10 ファイルの収集エラーで中断 | 旧プロジェクト由来の `api.*` / `application.*` import。`sqlalchemy` を追加しても解消しない |
| `coverage`(`core/*` + `db/*`) | 18 %(90 % 未達) | 未テストの Phase 1 資産が分母に入る |

Phase 0 資産のみに絞った場合の初回実測(下記 5 章のゲート対象の候補範囲)は次のとおり。

- `pytest`: 収集エラーのない 12 ファイルで 103 件成功・1 件 skip(`test_timeutil.py` の `time.tzset()` は Windows では実行不可の既存 OS 条件 skip)。
- `pyright`: 0 件。
- `ruff check`: 42 件、`ruff format --check`: 1 ファイル(`core/logging_config.py` のタブインデント等。4.1 で内訳を確認して機械整形する)。
- `coverage`: 初回実測は 855 文中 88 %。対象範囲を設定した後の再測定は 746 文中 86 %。`core/logging_config.py` と新規の起動処理は未テストのため、4.3 のテスト追加で 90 % に到達させる。到達しない場合も閾値は下げず、テストを追加する。

#### 4.1 再測定(2026-10-23、Windows、Python 3.13.12、uv 0.11.32)

`uv lock` と `uv sync --locked` の後、対象範囲を `pyproject.toml` に設定して全体と Phase 0 の両方を再実行した。

| ゲート | 全体(除外設定前) | Phase 0(除外設定後) |
| --- | --- | --- |
| `ruff check` | 7681 件 | 0 件 |
| `ruff format --check` | 63 ファイル中 31 ファイルが未整形 | 32 ファイルすべて整形済み |
| `pyright` | 2427 件 | 0 件 |
| `pytest --cov` | 旧移植テスト 10 ファイルの収集エラー | 103 passed、1 skipped |
| coverage | pytest の収集失敗で全体測定不可(別途 `core/*` + `db/*` は 18 %) | 746 文中 86 % |

Phase 0 の lint 42 件と未整形 1 ファイルは `core/logging_config.py` のタブインデント・import 順・末尾改行であり、Ruff の機械整形で解消した。Phase 0 に回帰・環境起因の失敗はない。全体の lint / 型検査 / テスト収集エラーは旧 import、`sqlalchemy` 依存および未移植資産に起因する Phase 1 課題であり、coverage 90 % 未達は Phase 0 起動・ログテストを追加する 4.3 の課題として扱う。Windows で既存の OS 条件 skip が 1 件あるが、新しい skip は追加しない。

## 3. スコープ

### 対象

- `uv.lock` の作成・管理と再現可能な同期
- Phase 0 の検査対象(5 章)の定義と、`pyproject.toml` への一元化(ローカルと CI で同一のコマンドを使うため)
- パス、ロック、ログ、DB migration、UI 表示設定を接続する最小の起動オーケストレーション
- Windows / Ubuntu の同一品質ゲートを実行する GitHub Actions
- README、移行ガイド、正本の Phase 0 状態・コマンド・検査対象の整合
- GitHub Ruleset または branch protection の設定と実 PR による確認

### 対象外

- MainWindow、仮ウィンドウ、業務画面、マスタ管理、`__main__.py`(画面実装前のため Phase 3 で追加)
- 予定枠の backfill、自動クローズ、業務データの seed
- Phase 1 の sqlite3 Repository 移植、既存移植コードの一括修正(lint・型・import の修正を含む)
- Phase 1 資産のための暫定依存(`sqlalchemy` 等)の追加
- Phase 2 以降のスマホ検針、集計、配布
- 旧企画書の変更

起動オーケストレーションは `app.py` の関数として順序と後始末をテストできるものとし、画面未実装の状態で業務アプリとして起動完了を装う仮 UI や `__main__.py` は作らない。

## 4. 実装タスク

### 4.1 feature ブランチ・ベースライン・検査対象の確定

- [x] `main` の状態を確認してから `feature/phase0-foundation-ci` を作成する。作業中の既存変更は保持する。
- [x] `migration_package/` で `uv lock` を実行して `uv.lock` を生成し、`uv sync --locked` で検証して Git 管理する。以降のローカル・CI 手順は `uv sync --locked` に統一する。
- [x] 2.1 の実測を再現し、Windows のベースラインを記録する(全体と Phase 0 資産のみの両方)。
- [x] 失敗を「機械整形」「Phase 0 の回帰・環境問題」「Phase 1 移植課題」に分類する。
  - 機械整形: Phase 0 資産の `ruff format` / `ruff check --fix` で直るもの(`core/logging_config.py` 等)。挙動を変えない純粋な整形コミットとして機能変更から分離し、Phase 0 に含める。`ruff format` で直らない違反(E501 等)は個別に直し、`ignore` は追加しない。
  - Phase 1 移植課題: `api` / `application` / `core` 旧 import、`sqlalchemy` 依存、Phase 1 資産のタブ・長行・strict 型エラー。Phase 0 では修正せず、5 章の検査対象外として明示的に記録する。
- [x] 検査対象外は `pyproject.toml` に宣言し(ruff `extend-exclude`、pyright `ignore`、pytest `--ignore`、`[tool.coverage.report]` の `include` / `omit`)、各設定に「Phase 1 で解除」のコメントを付ける。`[tool.coverage.run] source = ["meter_reading_mini"]` により未 import の Phase 1 ファイルが分母へ混入しないよう、coverage は `src/meter_reading_mini/` 配下の Phase 0 対象モジュールのみを明示し、`tests/*` と `core/mobile_reading.py` を除外する。個別の skip、`noqa` の一括付与、閾値の引き下げは行わない。
- [x] 検査対象外の一覧(ファイル名と理由)を正本の Phase 0 検査対象として転記する(4.5)。

### 4.2 起動・ログ統合

- [x] 小さな `src/meter_reading_mini/app.py` に起動ライフサイクルを追加し、既存部品を再利用する。依存(`QApplication` 生成、DB 接続、ロック、ログ設定)を引数で注入できる形にする。
- [x] 順序を「データ・ログディレクトリ準備(`ensure_dirs(paths)`) → `SingleInstanceLock` 取得 → ログ初期化 → DB 接続・migration → `QApplication` 作成 → 表示設定読込・適用」とする。
- [x] DB 接続・migration は `open_database(paths.db_path, paths.backup_dir, backup_timestamp=local_timestamp_for_filename)` を呼び出し、`core/timeutil.py` の `local_timestamp_for_filename` を渡す。
- [x] `configure_logging(log_dir=paths.log_dir)` を使い、`METER_READING_MINI_DATA_DIR` 指定時もログが `<data_dir>/logs/app.log` に保存されるようにする。
- [x] ロック取得に失敗した場合はログ・DB・UI の初期化へ進まない。
- [x] 起動途中に例外が発生した場合は、開いた DB 接続と取得済みロックを必ず解放する。
- [x] `QApplication` の既定ファクトリは既存の `QApplication.instance()` があれば再利用し(テスト環境 `pytest-qt` 等との競合防止)、`app.py` 自身が新規生成した場合のみ終了時に破棄する。
- [x] MainWindow、仮ウィンドウ、`__main__.py` を追加しない。

起動処理の仕様は次のとおり。

| 項目 | 仕様 |
| --- | --- |
| 戻り値 | `int` の終了コード。成功 0、起動失敗 1、ロック競合(多重起動)2 |
| ロック競合時 | ログ初期化前のため `stderr` に 1 行出力して終了する。UI・ログ・DB は初期化しない |
| 失敗の記録 | ログ初期化後は `logger.exception`、それ以前は `stderr` |
| 解放順序 | 取得の逆順(`QApplication` 破棄(自前生成時のみ) → DB close → ロック解放)。`try/finally` または `ExitStack` で保証する |
| イベントループ | Phase 0 ではループに入らず、初期化完了(表示設定の適用まで)で終了コードを返す |
| coverage | `app.py` は 5 章の coverage 対象に明示的に含める |

### 4.3 起動・ログのテスト

- [ ] 既存の `test_config.py`、`test_single_instance.py`、`test_display_settings.py`、`test_migrations.py` 等を再利用する。
- [ ] 起動イベントの順序、ロック競合時の短絡(終了コード 2、ログ・DB・UI 未初期化)、例外時のリソース解放(DB → ロックの順)を確認する。
- [ ] 一時データディレクトリで DB の migration とログ出力先(`<data_dir>/logs/app.log`)を確認する。
- [ ] `configure_logging` について、同一パスの再呼び出しでハンドラーが増えないこと、`basicConfig` 由来の StreamHandler が増えないこと、別パスで再呼び出しした場合の挙動(既存の `RotatingFileHandler` を `close()` して新しいパスのハンドラーへ切り替える等)を仕様として固定してテストする。root logger のハンドラーは fixture で退避・復元し、Windows で `tmp_path` の `app.log` がファイルロックされないようテスト中に追加された `RotatingFileHandler` を確実に `close()` する。
- [ ] 起動処理が `AppPaths.log_dir` を明示して渡すことをテストする(`get_default_log_dir()` の既定値は変更しない)。
- [ ] Qt を使うテストは既存 fixture を使い、Linux CI では offscreen 実行する。
- [ ] 新規の skip を追加しない。既存の OS 条件 skip(`test_timeutil.py` の `tzset`、Windows のみ skip)は 4.1 で一覧に記録し、Ubuntu で実行されることを確認する。

### 4.4 Windows / Ubuntu CI

- [ ] リポジトリ直下に `.github/workflows/ci.yml` を追加する。
- [ ] `main` 向け `pull_request` と `push` をトリガーにする。
- [ ] `permissions: contents: read`、`concurrency`(同一 ref の古い実行をキャンセル)、`strategy.fail-fast: false` を設定する。
- [ ] job に `name: test (${{ matrix.os }})` を明示し、matrix を `windows-latest` / `ubuntu-latest` にして、必須チェック名 `test (windows-latest)`、`test (ubuntu-latest)` と一致させる。workflow 名・job id・job 名を変更するときは、GitHub の必須チェック名も同時に更新する。
- [ ] action は公式をメジャータグ、サードパーティ(`astral-sh/setup-uv` 等)をコミット SHA で固定する。
- [ ] uv と品質ゲートのコマンドは `migration_package/` を作業ディレクトリとして実行する(job の `defaults.run.working-directory` を `migration_package` に設定する)。workflow 自体はリポジトリ直下に置く。
- [ ] Python 3.13、uv、`uv sync --locked` を使用する。`setup-uv` のキャッシュは `cache-dependency-glob: migration_package/uv.lock` を指定する。
- [ ] 各 OS で 5 章のコマンドを実行する。検査対象は `pyproject.toml` で定義済みのため、コマンド自体に絞り込み引数を付けない。
- [ ] Playwright Chromium を pytest 実行より前にインストールする。Ubuntu は `uv run playwright install --with-deps chromium` を使って Chromium と Qt offscreen 実行に必要なシステム依存をまとめて導入し、pytest 実行時に `QT_QPA_PLATFORM=offscreen` を設定する(万一 `ubuntu-latest` で `libegl1` / `libgl1` / `libxkbcommon0` 等の Qt 共有ライブラリが不足する場合は apt で明示導入する)。現状ブラウザテストは存在せず(Phase 2 で追加)、Phase 0 では「両 OS でブラウザ準備が成功すること」を確認する。
- [ ] 改行コード(Windows の CRLF)で `ruff format --check` が失敗しないかを初回 CI で確認し、失敗した場合のみ `.gitattributes` を追加する。
- [ ] ブラウザテストの skip、検査対象の無断除外、品質ゲートの緩和をしない。

### 4.5 文書の整合

- [ ] ルート README の開発状況・コマンドと Phase 0 の実装状況を正本に合わせる(品質ゲートの `coverage report` コマンドも `pyproject.toml` 一元化に合わせて引数なしの `uv run coverage report --fail-under=90` に揃え、`python -m meter_reading_mini` は Phase 3 の画面・`__main__.py` 実装後に有効になる旨を明記する)。
- [ ] `migration_package/README.md` の「Phase 0 完了済み」を、「基盤部品は同梱済み、CI・起動統合・GitHub 保護は未完了」のように、資産の存在とフェーズ完了を区別する記述へ直す。完了締め PR(4.6 手順 6)で「完了」へ更新する。
- [ ] `uv.lock` の管理開始後は、README の同期コマンドを `uv sync --locked` にする。lock の生成・更新手順(`uv lock`)も記録する。
- [ ] 正本の Phase 0 に、検査対象と検査対象外(ファイル名・理由)を記載する。正本の Phase 1 完了条件に「Phase 0 で設けた検査対象外をすべて解除し、全テスト・全ゲートが成功する」を追加する。
- [ ] 正本の Phase 0 完了チェックは、該当する実装・CI・GitHub 確認が完了してから、4.6 手順 6 の完了締め PR で更新する。

### 4.6 GitHub の保護設定と PR 検証

実施順序は次のとおり。順序を入れ替えない。

1. [ ] 前提確認: リポジトリの公開設定・プランで Ruleset または branch protection が使えることを確認する。使えない場合は公開設定・プランの変更要否をユーザーに確認し、解決するまで Phase 0 を完了扱いにしない。
2. [ ] Phase 0 本体の PR(`feature/phase0-foundation-ci`)で CI を実行し、両 OS が成功し、GitHub 上のチェック名が `test (windows-latest)` / `test (ubuntu-latest)` であることを確認する。
3. [ ] リポジトリ管理者が `main` に Ruleset または branch protection を設定する。
   - PR 経由を必須にする(必須承認数は単独開発のため 0)。
   - `test (windows-latest)` と `test (ubuntu-latest)` の成功を必須にする。
   - force push とブランチ削除を禁止する。
   - 通常運用を迂回する bypass を設けない。
   - 設定ミスで締め出された場合の復旧手順(Ruleset を `Disabled` にする)をメモしておく。
4. [ ] 失敗系の検証: 使い捨てブランチ(例: `verify/phase0-required-check-fail`)で意図的に失敗するテストを含む PR を作成し、チェック未成功時に merge がブロックされることを確認して記録する。この PR は merge せず close する。
5. [ ] 成功系の検証: Phase 0 本体の PR で両 OS のチェック成功後に merge できることを確認し、merge する。
6. [ ] 完了締め PR の作成・merge(`main` 保護有効化後は直接 push できないため別 PR で実施): 完了記録用ブランチ(例: `docs/phase0-completion-record`)を切り、以下を反映した PR を作成して両 OS の必須チェック成功後に merge する。workflow の追加やローカル成功だけで GitHub 側の完了とはみなさない。
   - 設定内容、失敗系検証 PR 番号、本体 PR 番号、チェック結果(URL またはスクリーンショット)を 8 章に記録する。
   - 正本の Phase 0 完了チェックと `migration_package/README.md`・ルート `README.md` の完了状態を更新する。
   - 本計画書を凍結して `docs/plan/implemented/` へ `git mv` し、正本への相対リンクを更新する。

## 5. 品質ゲートと Phase 0 検査対象

`migration_package/` から実行する。ローカルと CI で同一のコマンドを使い、検査対象の絞り込みは `pyproject.toml` の設定で行う(コマンドに引数を付けない)。

```powershell
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pyright
uv run playwright install chromium
uv run pytest --cov
uv run coverage report --fail-under=90
```

Ubuntu CI では Chromium 準備に `uv run playwright install --with-deps chromium` を使用し、pytest の環境変数に `QT_QPA_PLATFORM=offscreen` を設定する。CI の uv・品質ゲートの各コマンドは `migration_package/` を作業ディレクトリとして実行し、Windows / Ubuntu の両方で同じ品質ゲートを実行する。

### 5.1 検査対象(正本の「Phase 0 の検査対象の明記」に対応)

| 区分 | 対象 |
| --- | --- |
| ruff / pyright / pytest の対象 | `config.py`、`app.py`(新規)、`core/errors.py`、`core/timeutil.py`、`core/logging_config.py`、`core/mobile_reading.py`、`db/connection.py`、`db/migrations.py`、`db/backup.py`、`ui/*`、および収集可能な 12 テストファイルと `tests/conftest.py` |
| coverage 90 % の対象 | `src/meter_reading_mini/` 配下の Phase 0 対象モジュール(`config.py`、`app.py`、`core/errors.py`、`core/timeutil.py`、`core/logging_config.py`、`db/connection.py`、`db/migrations.py`、`db/backup.py`、`ui/*`)。`tests/*` と `core/mobile_reading.py`(Phase 2 資産、実測 80 %)は含めない |
| 検査対象外(Phase 1 で解除) | `core/contract_models.py`、`meter_master_service.py`、`meter_reading_sync.py`、`meter_reading_sync_repositories.py`、`reading_signature.py`、`reading_write_orchestration.py`、`schedule_settings.py`、`timezone_settings.py`、`usage_delta_calculation.py`、`work_package_download.py`、`db/reference_sqlalchemy/**`、および対応する 10 テスト(`test_api_models.py`、`test_measurement_definition_schedule_service.py`、`test_meter_master_service.py`、`test_meter_reading_sync_service.py`、`test_meter_reading_sync_service_batch_txn.py`、`test_reading_signature.py`、`test_reading_write_orchestration.py`、`test_timezone_settings.py`、`test_usage_delta_calculation.py`、`test_work_package_download_service.py`) |

- 検査対象外の理由は、旧プロジェクト由来の import(`api.*` / `application.*` / `core.*`)と `sqlalchemy` 依存で、標準 `sqlite3` への統合が Phase 1 の完了条件であるため。
- 対象一覧は 4.1 のベースラインで再確認し、差異があれば本表と `pyproject.toml` を同時に更新する。
- 閾値(90 %)は下げない。不足する場合はテストを追加する。

## 6. 完了条件

- [ ] `uv.lock` が追加され、`uv sync --locked` が Windows / Ubuntu で成功する。
- [ ] 起動順・ロック失敗時の短絡・例外時の後始末・DB migration・ログ保存先・ログハンドラーの重複防止と `close()` がテストされる。
- [ ] 5.1 の検査対象について、Windows と Ubuntu の全品質ゲートが成功する。
- [ ] 検査対象外が `pyproject.toml`・正本・README に同じ内容で記録され、Phase 1 の完了条件に解除が含まれている。
- [ ] README・移行ガイド・正本の記述が実装状態と一致する。
- [ ] GitHub の `main` 保護設定が確認され、実 PR の未成功時ブロックと成功後の通過が実証され、完了締め PR(4.6 手順 6)で 8 章への記録・正本チェック更新・計画書凍結移動が完了している。
- [ ] すべての条件を確認した後に限り、正本の Phase 0 完了チェックを更新する。

## 7. 運用上の注意

- `main` へ直接 push しない。変更は feature ブランチと PR で提出する。
- Phase 1 の移植課題を CI 成功のために Phase 0 の変更へ混ぜない。検査対象外に回した範囲は、5.1 と `pyproject.toml` に記録し、Phase 1 で解除する。
- 機械整形コミットは機能変更から分離し、差分が整形のみであることをレビューで確認できるようにする。
- 実データ領域を使わず、`METER_READING_MINI_DATA_DIR` またはテスト用一時ディレクトリを使う。
- Phase 0 本体 PR の merge 後は、完了締め PR(4.6 手順 6)で 8 章の記入・正本更新とあわせて本計画書を凍結し、`docs/plan/implemented/` へ `git mv` する。
- 移動時は正本への相対リンクを `../ローカル検針管理アプリ開発計画書.md` に更新し、完了記録として以後は変更しない。

## 8. 完了記録(Phase 0 完了時に追記)

| 項目 | 記録 |
| --- | --- |
| 公開設定・プラン、Ruleset / branch protection の種類 | |
| 必須チェック名・bypass 設定・force push / 削除禁止の確認日 | |
| 失敗系検証 PR(番号・ブロック確認) | |
| 成功系検証 PR(本体 PR 番号・両 OS 成功・merge) | |
| 完了締め PR(番号・記録反映・計画書凍結移動) | |
| 初回 CI 実行 URL(Windows / Ubuntu) | |
