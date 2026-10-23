# Phase 0 共通基盤・CI 整備計画

- 作成日: 2026-10-23
- ステータス: 作業計画
- 対象: `migration_package/` を基点とする Phase 0
- 正本: [ローカル検針管理アプリ開発計画書](../../../docs/ローカル検針管理アプリ開発計画書.md)
- GitHub: `sho-kawasaki-dd/meter-reading-app-mini`

## 1. 目的

同梱済みの共通基盤を再実装せず、ローカル品質ゲートと Windows / Ubuntu CI を整備する。保存先・多重起動防止・ログ・UI 表示設定を起動ライフサイクルで接続し、各 OS の必須チェックと GitHub の `main` 保護を実証する。

## 2. 現状と前提

- `config.py`、`db/connection.py`、`db/migrations.py`、`ui/single_instance.py`、`ui/display_settings.py` 等の基盤部品と単体テストは存在する。
- 起動オーケストレーション、標準 sqlite3 Repository、アプリ画面、CI workflow は未整備。
- ログ設定の既定保存先と `AppPaths.log_dir` が一致していない。起動時には `AppPaths.log_dir` を明示してログを構成する。
- `migration_package/uv.lock` は未作成。依存解決を固定し、以降 `uv sync --locked` を使用する。
- `migration_package/README.md` は Phase 0 完了済みと読める記述があるが、正本では CI と `main` 保護が未完了。両者を整合させる。
- GitHub リポジトリは作成済み。作業は `main` へ直接 push せず、`feature/phase0-foundation-ci` 等の feature ブランチから PR を作る。

## 3. スコープ

### 対象

- `uv.lock` の作成・管理と再現可能な同期
- パス、ロック、ログ、DB migration、UI 表示設定を接続する最小の起動オーケストレーション
- Windows / Ubuntu の同一品質ゲートを実行する GitHub Actions
- README、移行ガイド、正本の Phase 0 状態・コマンドの整合
- GitHub Ruleset または branch protection の設定と実 PR による確認

### 対象外

- MainWindow、仮ウィンドウ、業務画面、マスタ管理
- 予定枠の backfill、自動クローズ、業務データの seed
- Phase 1 の sqlite3 Repository 移植、既存移植コードの一括修正
- Phase 2 以降のスマホ検針、集計、配布
- 旧企画書の変更

起動オーケストレーションは順序と後始末をテストできるものとし、画面未実装の状態で業務アプリとして起動完了を装う仮 UI は作らない。

## 4. 実装タスク

### 4.1 feature ブランチと現状ベースライン

- [ ] `main` の状態を確認してから `feature/phase0-foundation-ci` を作成する。作業中の既存変更は保持する。
- [ ] `migration_package/` で初回 `uv sync` を行い、生成された `uv.lock` を Git 管理する。
- [ ] 依存確定後、以降のローカル・CI手順を `uv sync --locked` に統一する。
- [ ] Windows で ruff check、ruff format --check、pyright、全 pytest＋coverage を実行し、失敗を記録する。
- [ ] 失敗を Phase 0 の回帰・環境問題・Phase 1 移植課題に分類する。失敗を隠す skip、検査対象除外、品質設定緩和は行わない。

### 4.2 起動・ログ統合

- [ ] 小さな `app.py` 等に起動ライフサイクルを追加し、既存部品を再利用する。
- [ ] 順序を「データ・ログディレクトリ準備 → `SingleInstanceLock` 取得 → ログ初期化 → DB 接続・migration → `QApplication` 作成 → 表示設定読込・適用」とする。
- [ ] `configure_logging(log_dir=paths.log_dir)` を使い、`METER_READING_MINI_DATA_DIR` 指定時もログが指定領域の `logs/` に保存されるようにする。
- [ ] ロック取得に失敗した場合はログ・DB・UI の初期化へ進まない。
- [ ] 起動途中に例外が発生した場合は、開いた DB 接続と取得済みロックを必ず解放する。
- [ ] 起動処理を依存注入等でテスト可能にし、MainWindow や仮ウィンドウを追加しない。

### 4.3 起動・ログのテスト

- [ ] 既存の `test_config.py`、`test_single_instance.py`、`test_display_settings.py`、`test_migrations.py` 等を再利用する。
- [ ] 起動イベントの順序、ロック競合時の短絡、例外時のリソース解放を確認する。
- [ ] 一時データディレクトリで DB の migration とログ出力先を確認する。
- [ ] ログハンドラーの重複設定を防ぎ、既定のログ保存先がアプリの `AppPaths.log_dir` に統一されることをテストする。
- [ ] Qt を使うテストは既存 fixture を使い、Linux CI では offscreen 実行する。

### 4.4 Windows / Ubuntu CI

- [ ] リポジトリ直下に `.github/workflows/ci.yml` を追加する。
- [ ] `main` 向け `pull_request` と `push` をトリガーにする。
- [ ] matrix の job 名を必須チェックと一致する `test (windows-latest)`、`test (ubuntu-latest)` に固定する。
- [ ] Python 3.13、uv、`uv sync --locked` を使用する。
- [ ] 各 OS で次を実行する。
  - `uv run ruff check`
  - `uv run ruff format --check`
  - `uv run pyright`
  - `uv run pytest --cov`
  - `uv run coverage report --include="src/meter_reading_mini/core/*,src/meter_reading_mini/db/*" --fail-under=90`
- [ ] Playwright Chromium をインストールする。Ubuntu は `uv run playwright install --with-deps chromium` と `QT_QPA_PLATFORM=offscreen` を設定する。
- [ ] ブラウザテストの skip、既存テストの除外、品質ゲートの緩和をしない。

### 4.5 文書の整合

- [ ] ルート README の開発状況・コマンドと Phase 0 の実装状況を正本に合わせる。
- [ ] `migration_package/README.md` の「Phase 0 完了済み」を、「基盤部品は同梱済み、CI・起動統合・GitHub 保護は未完了」のように、資産の存在とフェーズ完了を区別する記述へ直す。
- [ ] `uv.lock` の管理開始後は、README の同期コマンドを `uv sync --locked` にする。初回 lock 生成手順も必要に応じて記録する。
- [ ] 正本の Phase 0 完了チェックは、該当する実装・CI・GitHub確認が完了してから更新する。

### 4.6 GitHub の保護設定と PR 検証

- [ ] リポジトリ管理者が `main` に Ruleset または branch protection を設定する。
- [ ] PR 経由を必須とし、`test (windows-latest)` と `test (ubuntu-latest)` の成功を必須にする。
- [ ] force push とブランチ削除を禁止し、通常運用を迂回する bypass を設けない。
- [ ] feature ブランチから実 PR を作成し、チェック未成功時は merge がブロックされ、両 OS 成功後に merge 可能であることを確認する。
- [ ] 設定・PR・チェック結果を記録する。workflow の追加やローカル成功だけで GitHub 側の完了とはみなさない。

## 5. 品質ゲート

`migration_package/` から実行する。

```powershell
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pyright
uv run playwright install chromium
uv run pytest --cov
uv run coverage report --include="src/meter_reading_mini/core/*,src/meter_reading_mini/db/*" --fail-under=90
```

Ubuntu CI では Chromium 準備に `uv run playwright install --with-deps chromium` を使用し、pytest の環境変数に `QT_QPA_PLATFORM=offscreen` を設定する。CI では Windows / Ubuntu の両方で同じ品質ゲートを実行する。

## 6. 完了条件

- [ ] `uv.lock` が追加され、`uv sync --locked` が Windows / Ubuntu で成功する。
- [ ] 起動順・ロック失敗時の短絡・例外時の後始末・DB migration・ログ保存先がテストされる。
- [ ] Windows と Ubuntu の全品質ゲートが成功する。Phase 1 由来の失敗が残る場合は Phase 0 を完了扱いにしない。
- [ ] README・移行ガイド・正本の記述が実装状態と一致する。
- [ ] GitHub の `main` 保護設定が確認され、実 PR の未成功時ブロックと成功後の通過が実証される。
- [ ] すべての条件を確認した後に限り、正本の Phase 0 完了チェックを更新する。

## 7. 運用上の注意

- `main` へ直接 push しない。変更は feature ブランチと PR で提出する。
- Phase 1 の移植課題を CI 成功のために Phase 0 の変更へ混ぜない。全テスト必須の条件によりブロックされる場合は、失敗と依存関係を記録し、適切な Phase 1 作業として扱う。
- 実データ領域を使わず、`METER_READING_MINI_DATA_DIR` またはテスト用一時ディレクトリを使う。
- Phase 0 完了後、本計画書を凍結して `migration_package/docs/plan/implemented/` へ移動する。
- 移動時は正本への相対リンクを更新し、完了記録として以後は変更しない。
