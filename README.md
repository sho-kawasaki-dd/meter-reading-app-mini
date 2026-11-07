# 検針管理アプリ (Meter Reading mini)

施設内の電気・ガス・水道・ボイラー等の検針業務を管理する、ローカル完結型の PySide6 デスクトップアプリです。
PC で管理・集計し、現場では自己完結型の単一 HTML を Android の Chrome で開いて検針します。
HTML と結果は Quick Share 等で受け渡し、アプリ自身は外部通信や HTTP サーバーの起動を行いません。

備品在庫管理アプリ・業務日誌アプリとの将来統合を見据え、技術スタック・層構成・DB 接続規約・バックアップ基盤を共通化します。

## 開発状況と仕様の正本

現在はリポジトリ直下をプロジェクト基点として開発中です。
Phase 0 は完了しています。共通基盤・起動オーケストレーション (`app.py`)・品質ゲート設定・Windows / Ubuntu 用 CI workflow を整備し、GitHub の `main` 保護と必須チェックを設定しています。[本体 PR #1](https://github.com/sho-kawasaki-dd/meter-reading-app-mini/pull/1) で両 OS のチェック成功後に merge し、[失敗系検証 PR #2](https://github.com/sho-kawasaki-dd/meter-reading-app-mini/pull/2) で未成功チェックによる merge ブロックも確認済みです。
Phase 1 は完了しています。標準 sqlite3 Repository とコア業務ロジックを統合し、全品質ゲートが Windows / Ubuntu の必須 CI で成功しました。完了記録と作業計画書の凍結移動も済んでいます。
Phase 2 は 2026-10-28 に完了しました。単一 HTML 検針シート、スマホ入力・IndexedDB 自動保存・途中経過 JSON、MR1 / 結果 JSON の出力から Python パーサ・同期までを実装し、全体・`core/`・`db/` の coverage 90% 以上と Windows / Ubuntu の必須 CI 成功を確認しています。[完了記録](docs/plan/implemented/フェーズ2_単一HTML検針シート・MR1結果パーサ計画.md) に詳細があります。Android 実機確認と PC 側の差分プレビュー・一括確定 UI は Phase 4 以降の対象であり、Phase 2 の完了には含めません。
Phase 3 は 2026-11-01 に完了しました。起動、メイン画面、マスタ管理、割当期間の計算条件訂正、設定、PC 直接入力・訂正、メーター交換・履歴訂正を利用できます。Windows での手動通し確認と両 OS の必須 CI 成功を確認し、完了記録 PR #29 を merge しました。[完了記録](docs/plan/implemented/フェーズ3_メイン画面・マスタ・PC直接入力・交換UI計画.md) に詳細があります。
Phase 4 は 2026-11-04 に完了しました。実装・Windows ローカル品質ゲート・Android 実機確認を終え、PR #34・#35 で Windows / Ubuntu の必須 CI 成功後に完了記録を merge しています。moto g66j 5G (Android 16、Chrome 156.0.8078.79) で検針シートを確認し、Chrome 終了後・端末再起動後・HTML 移動後の入力復元と途中経過 JSON による回復を確認しました。MR1 / 結果 JSON の PC 取込と不正入力の拒否も確認済みです。詳細は[開発計画書](docs/plan/ローカル検針管理アプリ開発計画書.md)と[Phase 4 完了記録](docs/plan/implemented/フェーズ4_スマホ検針ダイアログ・差分プレビュー・一括確定計画.md)を参照してください。
Phase 5 は進行中です。5a のスキーマ v4、集計式の版管理・編集画面、期間集計、ダッシュボード、CSV / PNG 出力と復元用の整合性検査を実装し、自動テスト・自動操作確認を行いました。集計は保存済み業務日を使い、式の版別寄与・交換境界・欠測理由を扱います。5a は人手による通し確認と Windows / Ubuntu の必須 CI が未確認のため未完了です。5b はバックアップ・復元画面、候補検査、現行 DB の保全・置換と Python 実行での再起動を実装し、自動検証しました。5b の必須 CI は未確認で、実際の exe とインストール後の復元・再起動は 5c で検証します。5c の配布基盤も未実装です。これらの実装を Phase 5 全体の完了とは扱いません。

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

## 主な機能 (実装予定を含む)

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

## アプリの起動

リポジトリのルートで実行します。Phase 3 のメイン画面が起動します。現在利用できる機能は上記の開発状況を確認してください。

```powershell
uv sync --locked
uv run python -m meter_reading_mini
```

実データと分離して動作を確認するときは、一時フォルダを指定して起動します。

```powershell
$env:METER_READING_MINI_DATA_DIR = "<一時フォルダ>"
uv run python -m meter_reading_mini
```

手動確認用のサンプルデータを作る場合は、リポジトリルートで次を実行します。
既定では OS の一時フォルダーに新しい DB を作り、既存 DB がある保存先への上書きは拒否します。

```powershell
uv run python scripts/create_manual_test_data.py
```

作成先はコマンド出力に表示されます。表示された環境変数の設定と起動コマンドを使ってください。
担当者・複数場所のメーター・日次/週次の測定定義・直近の予定枠を作り、過去枠の既定 50% に PC 入力の実測値を登録します。残りは自動クローズ状態で残します。
実測の割合は `--measured-percent 80` のように 0〜100% で変更でき、予定枠の過去日数は `--days-back 30`、作成先は `--data-dir <フォルダー>` で指定できます。

### 手動確認用データの作成・削除 (PowerShell)

リポジトリルートで次を実行すると、毎回新しい一時フォルダーに確認用 DB を作成し、
その DB を使ってアプリを起動します。アプリ終了後は保存先の環境変数を元に戻します。

```powershell
$dataDir = Join-Path ([System.IO.Path]::GetTempPath()) ("meter-reading-phase4-" + [guid]::NewGuid().ToString('N'))

uv run python .\scripts\create_manual_test_data.py `
    --data-dir "$dataDir" `
    --days-back 14 `
    --measured-percent 50

if ($LASTEXITCODE -ne 0) {
    throw '手動確認用データの生成に失敗しました。'
}

$previousDataDir = $env:METER_READING_MINI_DATA_DIR
try {
    $env:METER_READING_MINI_DATA_DIR = $dataDir
    Write-Host "確認用データの保存先: $dataDir"
    uv run python -X faulthandler -m meter_reading_mini
    $exitCode = $LASTEXITCODE
    Write-Host "アプリ終了コード: $exitCode"
    if ($exitCode -ne 0) {
        throw "アプリが異常終了しました。終了コード: $exitCode"
    }
}
finally {
    $env:METER_READING_MINI_DATA_DIR = $previousDataDir
}
```

終了時の調査用に `faulthandler` を有効にし、正常終了時も含めて終了コードを表示します。
異常終了した場合は、終了コードと端末に出力された診断情報、および保存先の `logs/app.log` を確認してください。
ネイティブクラッシュの診断情報は端末に出力され、`logs/app.log` に残らない場合があります。

「スマホ検針」から HTML を同じ一時フォルダーへ出力し、確定担当者には「確認担当」を選びます。
MR1・結果 JSON・途中経過 JSON は画面操作で作成します。

確認後はアプリを終了し、必要な記録を退避してから、同じ PowerShell で次を実行します。
別の PowerShell を使う場合は、先に `$dataDir` へ上記で表示された保存先を設定してください。
削除対象は OS の一時フォルダー直下にある、この手順の命名規則に一致するフォルダーだけに制限します。
DB・ログ・出力した HTML / JSON など、対象フォルダー内の全ファイルを削除します。
スマホや別の場所へコピーしたファイルは削除されません。

```powershell
if ([string]::IsNullOrWhiteSpace($dataDir)) {
    throw '削除対象の $dataDir を設定してください。'
}

$target = Get-Item -LiteralPath $dataDir -Force -ErrorAction Stop
$pathSeparators = [char[]]@(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)
$tempRoot = (Get-Item -LiteralPath ([System.IO.Path]::GetTempPath()) -Force -ErrorAction Stop).FullName.TrimEnd($pathSeparators)
$targetParent = $target.Parent
if (
    -not $target.PSIsContainer -or
    ($target.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or
    $null -eq $targetParent -or
    $targetParent.FullName.TrimEnd($pathSeparators) -ne $tempRoot -or
    $target.Name -cnotmatch '^meter-reading-phase4-[0-9a-f]{32}$'
) {
    throw '安全のため削除を中止しました。作成時の一時フォルダーを指定してください。'
}

Write-Host "削除対象: $($target.FullName)"
Remove-Item -LiteralPath $target.FullName -Recurse -Force -Confirm -ErrorAction Stop
```

### Linux での起動

Linux では Qt の OS 依存も必要です。Ubuntu 系の準備例:

```sh
sudo apt-get update
sudo apt-get install --yes libegl1 libgl1 libxkbcommon0 libdbus-1-3 libfontconfig1 libxcb-cursor0
uv sync --locked
uv run python -m meter_reading_mini
```

## 基本操作

### マスタと設定

1. メイン画面の「マスタ管理」で、担当者と物理メーターを登録し、同じ場所の有効なメーターを初期割当として測定定義を登録します。場所名はメーターごとではなく場所単位で一括変更します。
2. 測定定義別のスケジュールを編集するか、「周期既定に戻す」を選択します。適用開始日と履歴を確認できます。
3. 「設定」で締め時刻、周期別スケジュール、検針時間ごとの配信オフセット、フォント・文字サイズを変更できます。業務タイムゾーンは表示のみです。各項目を個別に保存すると、表示設定は即時反映され、業務設定は予定枠の更新に連携します。
4. 計算条件を訂正する場合は「マスタ管理」の「計算条件の訂正」で対象の割当期間・担当者・訂正理由を指定し、影響を確認してから確定します。入力や対象が変わった場合は再プレビューが必要です。

### 初期設定と PC 直接入力

1. 担当者、物理メーター、測定定義、周期別スケジュールを登録します。
2. 起動時・シート出力時に未生成の予定枠を補完します。締め時刻 (初期値 `07:00`) を過ぎると、
   前日以前の未消化枠が `auto_closed` になります。
3. 対象業務日・周期・状態・場所等で予定枠を選び、「PC 直接入力・訂正」で結果状態、測定値または力率、記録日時、担当者、備考を入力します。力率が 100% のとき、位相は力率1に固定されます。
4. 入力に応じて表示される前回値・使用量・警告・検証結果を確認して保存します。プレビューは DB を変更せず、確定時にも入力と DB の状態を再検証します。記録日時は業務タイムゾーンで入力し、未来日時や夏時間の存在しない・重複する日時は指定できません。
5. 過去枠への追加入力・訂正では後続の前回値・使用量と交換境界も再計算します。
   `auto_closed` への後日入力も受け付け、確定後は `completed` に更新します。

設置場所 (`location_id`)、物理計器 (`meter_id`)、論理的な測定対象 (`measurement_definition_id`) は
分離して管理します。メーター交換後も場所を不変の集計キーとして履歴を継続します。
使用中マスタは論理削除で保護します。

### スマホ検針

1. PC のメイン画面で「スマホ検針」を開き、「シート出力」で対象業務日の HTML を保存します。既定は 1 日分の全枠で、検針時間・周期を複数選んで絞れます。出力時の担当者選択は不要です。
2. HTML を Quick Share 等で Android へ渡し、Files から Chrome で開きます。
   CSS・JavaScript・対象データは HTML 内に含め、外部通信は行いません。
3. 検針時間のタブ、場所別表示、未入力フィルタを使い、実測・見送り・検針不能・計器不良を入力して確認します。
   同日先行枠の実測値は、後続枠の表示上の前回値・参考使用量に引き継ぎます。
4. **確認済みの正常な枠が 1 件以上あれば、未確認枠が残っていても結果を出力できます。**
   未確認枠は結果に含めません。全件確認が必要な在庫アプリの棚卸とは異なります。
5. `MR1:<Base64>.<CRC32>:END` 形式の結果テキスト、または結果 JSON を PC へ戻します。
   コピー・保存に失敗した場合は、結果テキスト表示から手動コピーします。
6. PC の「結果取込・確定」で結果テキストを貼り付けるか、結果 JSON を読み込みます。貼付・ファイル読込後は自動でプレビューし、テキストを手編集した場合は「プレビュー」を押します。
7. 前回値・今回値・使用量・警告・採否区分を確認し、有効な確定担当者を選んで一括確定します。重複・古い入力・拒否された行がある場合は、除外理由を確認して採用可能な行だけを確定できます。

入力の変更・再読込開始・解析 / プレビュー / 確定の失敗時は旧プレビューを破棄し、再プレビューまで確定できません。スマホの参考計算だけを信用せず、確定時に PC 側で入力と DB の状態を再検証・再計算します。
シート生成処理後は保存失敗・キャンセル時も含めて一覧を再読込し、確定後も結果を反映します。再読込に失敗した場合はエラーを通知し、古い一覧を消去します。

#### シート対象・再検針と PC 入力との競合

シートは指定した業務日 1 日分です。現在業務日以前を指定できますが、過去の未完了枠をまとめて収録する方式ではありません。未来日は指定できません。

| 予定枠の状態 | 新しく出力するシートへの収録 |
| --- | --- |
| `open` (未消化) | 対象 |
| `auto_closed` (自動クローズ) | 対象。後日入力を確定すると `completed` になり、必要な後続使用量も再計算 |
| `completed` (完了) | 対象外 |
| `superseded` (差し替え済み) | 対象外。旧枠への取込も拒否 |

**予定枠の完了と、実測できたかどうかは別管理です。** 実測だけでなく、見送り・検針不能 (到達不能)・計器不良 (計器故障) の結果を保存した場合も、予定枠は `completed` になります。これらの枠は新規シートには再掲載されません。再検針して実測値が得られた場合は、PC 直接入力・訂正で更新できます。

一方、完了前に出力した HTML からその枠が自動で消えることはなく、PC の変更もスマホへ自動反映されません。そのシートから結果が戻った場合は、完了済みという理由だけでは拒否せず、同じ予定枠の保存済み結果と入力更新時刻 (`client_updated_at`) を比較します。

| スマホ結果の入力更新時刻と PC の保存済み結果との比較 | 取込時の扱い |
| --- | --- |
| 古い | 「古い入力」として除外し、保存済みの値・状態を維持 |
| 新しい | 業務検証を通れば「採用: 上書き」。確定時にスマホの値・状態へ置換 |
| 同時刻 (UTC 秒単位に正規化後) | 内容が異なれば後着のスマホ結果を採用 |
| 更新時刻・記録日時・結果状態・測定値・構造化入力・備考がすべて一致 | 重複として扱い、更新しない |

- この判定は、PC で実測値を登録した場合も、検針不能・計器不良を登録した場合も同じです。PC の結果を永久に優先するロックではありません。
- 例: PC で 10:00 に検針不能を保存した後、スマホの最終更新が 9:50 なら除外します。10:10 に実測値を入力・確認した結果なら、業務検証後に上書きできます (端末の時計が正しい場合)。
- 比較するのは取込時刻・HTML 出力時刻・記録日時ではなく、入力更新時刻です。スマホでは入力編集や確認操作でも更新されるため、端末の時計ずれに注意してください。
- PC 直接入力・訂正は業務検証を通れば常に採用し、更新時刻には「PC の現在時刻」と「保存済み更新時刻 + 1 秒」の遅い方を設定します。
- 取込時の使用量は PC の最新 DB を基に再計算します。プレビュー後に関連 DB 状態が変わった場合は確定を中止し、再プレビューを求めます。古い入力・重複・拒否が混在しても、採用行が 1 件以上あれば採用行のみ確定できます。

仕様の詳細は、[開発計画書](docs/plan/ローカル検針管理アプリ開発計画書.md)の 5.1「コア計算・データ整合性ルール」と 5.2.1「検針シート HTML の出力」を参照してください。

#### 途中保存と回復

- IndexedDB に `sheet_id` ごとの途中状態を自動保存します。`localStorage` は使用しません。
- 「保存済み」は最新変更の保存トランザクション完了を示し、電源断後まで保証する表示ではありません。
- 空欄・入力エラーを含む途中状態も保持しますが、不正な枠は確認済みになりません。
- 保存失敗・既存データの読込失敗・別タブとの競合は警告し、自動上書き・自動マージしません。
- 途中経過 JSON を手動保存・読込できるようにします。同一シートであることを検証し、
  承認後に現在の入力全体を置き換えます。途中経過 JSON は PC 側の結果取込には使用できません。
- HTML・結果・途中経過 JSON は業務情報を含むため安全に保管し、不要になったら手動で削除します。
  結果出力・確定後も自動削除しません。

検針アプリ自身の実機確認では、2026-11-04 に moto g66j 5G (Android 16、Chrome 156.0.8078.79) で、
Quick Share → Files → Chrome の経路を使い、Chrome 終了後・端末再起動後・端末内で HTML を別フォルダーへ移動した後の自動復元と、
途中経過 JSON による退避・回復を確認しました。この条件以外の端末・ブラウザ・プライベートモードや、
ブラウザデータ消去後の回復は保証しません。
アプリ更新によって配布済み HTML やその途中データが自動更新されることもありません。

### メーター交換と使用量計算

1. メイン画面の「メーター交換」で場所・旧メーター・交換日時を選び、交換先を既存メーターから選択するか、その場で新規登録します。新規メーターと交換は一緒に確定します。
2. 担当者と備考、各測定定義の旧最終読値・新初期読値を入力します。旧条件を確認し、新しい割当期間の倍率・ロールオーバー値を指定します。
3. 影響をプレビューし、未完了枠・非実測完了枠の差し替え件数、旧→新の条件、交換境界の使用量を確認して確定します。入力変更や検証失敗時は再プレビューが必要です。確定時にも同じ入力と DB の状態を再検証します。
4. 交換日時以降に実測済みの旧枠がある場合は交換できません。必要に応じて利用者が「PC 直接入力・訂正」で明示的に非実測へ訂正してから交換します。旧非実測結果は差し替え済みの旧枠に残り、新しい枠へコピーされません。
5. 「交換履歴・訂正」では担当者を選び、測定定義ごとの読値と備考を訂正できます。交換日時・旧メーター・新メーターは変更できません。割当期間の計算条件を訂正する場合は「マスタ管理」の「計算条件の訂正」を使います。
6. 同期済み交換イベントを同じ `replacement_event_id` で再送する場合も、訂正できるのは測定定義ごとの読値と備考です。交換日時・旧メーター・新メーターは変更できず、読値の訂正後は交換境界と後続使用量を再計算します。

- 数値・倍率・閾値は Python では `decimal.Decimal`、DB / JSON では plain decimal 文字列とします。
- 通常の差分は `(今回値 - 前回値) × 倍率` です。ロールオーバー設定がある場合は桁あふれを補正し、
  設定のない負の差分は PC 側で異常値として拒否します。
- 倍率とロールオーバー値は測定定義とメーターの割当期間ごとに保存し、予定日時に対応する条件で計算します。
  設定誤りは対象期間全体をプレビューし、担当者・理由付きで訂正します。
- `raw_only` は差分を計算しません。
- 交換時は旧メーター最終値による使用量と新メーター初期値を記録します。
  新メーター初回は初期値を前回値とし、初期値がない場合に限り初回使用量を 0 とします。
- 旧メーターの未完了枠と非実測完了枠を `superseded` にし、結果のない新メーターの後継枠を生成します。

### スキーマ版 1 からの計算条件移行

版 1 には過去の割当期間ごとの計算条件がないため、版 2 への移行時は各測定定義の現行の倍率・
ロールオーバー値を、その測定定義のすべての既存割当へ初期値としてコピーします。
これは過去の正しい条件を復元したものではありません。移行時に既存の読値・使用量は再計算しません。
移行前の DB はバックアップします。

過去の設定資料を確認し、異なる条件の期間は次の手順で訂正してください。

1. メイン画面の確認待ち警告から「計算条件の訂正」を開くか、「マスタ管理」で同名のタブを開き、対象の測定定義と割当期間を選択します。
2. 正しい倍率・ロールオーバー値、有効な担当者、訂正理由を入力して「影響をプレビュー」を押し、対象期間の結果・交換境界への影響と検証エラーを確認します。
3. エラーがなければ「確認した内容を確定」を押します。入力変更やプレビュー後の対象変更時は再プレビューが必要です。確定時にも同一トランザクション内で再検証され、条件・訂正履歴・使用量・交換境界が更新されます。読値や元の監査情報は変更されず、訂正履歴は同じ画面で確認できます。

### 集計・CSV / PNG 出力

1. 必要に応じて「マスタ管理」→「集計式」で名称・単位・カテゴリ・周期グループ・目的・表示桁数を指定し、測定定義と 0 以外の係数を追加します。負の係数も使用できます。保存前の確認で項目・適用開始日・過去への影響を確認します。式は登録しなくても測定定義ごとの集計を表示できます。
2. 既存式は版履歴から選んで編集するか、「この項目を使って新しい版を追加」で無効化・再有効化を含む版を追加します。名称・表示桁数と版は別々に保存し、両方を変更した場合は名称・表示桁数を先に保存します。未保存の入力を破棄する操作では確認します。初版だけは過去日から登録でき、1 版だけの間は開始日と項目を訂正できます。2 版目の追加後は初版を変更できません。式の単位・カテゴリ・周期グループ・目的は作成後に変更できず、式に参照された測定定義の単位・カテゴリも変更できません。
3. メイン画面の「使用量集計」で開始日・終了日、日次 / 月次 / 暦年 / 年度、目的・カテゴリ・場所を選び、「集計を取得」を押します。終了日は含む指定です。目的の初期値は `operational` (運用) で、`billing` (請求)・`audit` (監査) は別に表示します。
4. 「表示する系列」で測定定義または式を一つ選び、グラフと期間ごとの表を確認します。期間行を選ぶと、欠測・警告・情報、式版の適用範囲、前回基準・交換境界・各項目の寄与を確認できます。確認待ちの割当がある場合は「割当条件を訂正」から担当者・理由付きの訂正へ進みます。場所をまたぐ式は、場所で絞り込んでも全項目を使って計算します。
5. 「表示系列を CSV 出力」または「グラフを PNG 出力」で保存します。CSV は UTF-8 BOM 付きで表示値・無丸め値・根拠明細を出力し、文字列セルの数式注入を防ぎます。表計算ソフトに数値として取り込むと無丸め値の精度が失われる場合があるため、正確に確認するには文字列として取り込んでください。

グラフは浮動小数点を使った参考表示です。値のない期間では線をつながず、描画範囲外や精度不足で値・目盛りを区別できない場合は PNG を無効にします。正確な値は表または CSV を確認してください。表示値は設定された桁数で `ROUND_HALF_UP` により丸め、保存済みの値は変更しません。取得条件や元データが変わったとき、または取得に失敗したときは古い結果を破棄します。

スキーマ v4 は式と適用版を保持し、v1〜v3 の DB は起動時に移行前バックアップを作成して v4 へ移行します。時刻列は UTC 保存、集計・予定枠・締め判定はローカルタイムゾーンの業務日を基準とします。

## データ保存先

パス解決は [config.py](src/meter_reading_mini/config.py) に実装済みです。

- DB・バックアップ・表示設定・ロック: `%APPDATA%\meter-reading-mini`
- ログ: `%LOCALAPPDATA%\meter-reading-mini\Logs`
- DB 名: `meter_reading.db`、ロック: `meter_reading.lock`、表示設定: `ui_settings.json`

起動前に `METER_READING_MINI_DATA_DIR` を設定すると、DB 等は指定先、ログはその `logs` 配下になります。
一時的な動作確認では、次のように通常データと分離します。

```powershell
$env:METER_READING_MINI_DATA_DIR = Join-Path $env:TEMP ("meter-reading-mini-" + [guid]::NewGuid().ToString('N'))
uv run python -m meter_reading_mini
```

Linux の指定例:

```sh
export METER_READING_MINI_DATA_DIR="$HOME/.local/share/meter-reading-mini-test"
uv run python -m meter_reading_mini
```

### バックアップ・復元

1. メイン画面の「ファイル」→「バックアップ・復元」を開きます。
2. 「バックアップを保存」で保存先を選びます。既定名は業務タイムゾーンによる `meter_reading_YYYYMMDD_HHMMSS.db` です。既存ファイルは上書きできないため、別の名前を選んでください。
3. 復元する場合は復元元を指定し、「復元候補を検査」でスキーマ版・データ件数・警告を確認します。復元元の変更、参照のキャンセル、検査失敗、最終確認のキャンセル後は再検査が必要です。
4. 「内容を確認して復元」で現行 DB の上書き・自動保全・再起動を確認します。操作とタイマーを停止し、現行 DB を `backups/pre-restore_日時.db` に保全してから置換します。
5. 新プロセスの起動完了を確認して通知します。再起動を確認できない場合は保全バックアップの場所を案内します。アプリが起動していなければ手動で起動し、必要に応じて保全先を確認してください。

[バックアップ・復元 Service](src/meter_reading_mini/core/backup_restore_service.py) と
[復元基盤](src/meter_reading_mini/db/restore.py) は、`sqlite3.Connection.backup()` による
WAL 内のコミット済みデータを含むバックアップと、v1～v4 の復元候補の検査・移行を実装しています。
バックアップは専用一時領域で作成し、排他的に予約した保存先へ書き込みます。
保存先の差替えや予期しない関連ファイルを検出すると中止し、所有不明の関連ファイルを削除しません。
復元元に空でない WAL がある場合は元 DB と WAL を一時領域へコピーし、読取専用で検査します。
元 DB の `-shm` は作成・変更せず、空でないロールバックジャーナルがある候補は拒否します。
[業務整合性検査](src/meter_reading_mini/db/integrity.py) は Decimal 値・結果状態・
割当条件・期間の重複・訂正履歴・交換境界・集計式も検査します。

確定時は復元元と候補を再検証し、チェックポイント・接続終了と保全内容を確認して原子的に置換します。
新プロセスはロック取得・起動準備・画面表示とイベントループ開始後に確認を返し、
30 秒以内に確認できない場合は成功と扱いません。変更検出用 SHA-256 は送信者認証ではありません。
表示設定 `ui_settings.json` は復元対象外です。
Python 実行での自動検証は実施済みですが、実際の exe とインストール後の復元・再起動は 5c で検証します。

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
│       ├── restart.py
│       ├── core/
│       ├── db/
│       └── ui/
└── tests/
```

以下の `core/`・`db/`・`ui/` は `src/meter_reading_mini/` 配下です。

| ファイル / ディレクトリ | 役割 |
| --- | --- |
| `src/meter_reading_mini/app.py` | 起動・復元後の DB 終了とロック・再起動の順序を統括 |
| `src/meter_reading_mini/config.py` | アプリデータ・ログ等の保存先を解決 |
| `src/meter_reading_mini/restart.py` | 実行形態に応じた再起動と起動完了の確認 |
| 各 `__init__.py` | `meter_reading_mini`・`core`・`db`・`ui` を Python パッケージとして定義 |
| `core/contract_models.py` | Enum、DTO、Decimal 文字列等のデータ契約 |
| `core/aggregation_contracts.py` | 集計式・版・項目の契約と共通検証 |
| `core/aggregation_formulas.py` | 集計式の登録・版管理 |
| `core/backup_restore_contracts.py` | バックアップ・復元の契約 |
| `core/backup_restore_service.py` | バックアップ・復元の操作 |
| `core/errors.py` | 業務例外の基底型と共通例外 |
| `core/logging_config.py` | ローテーション付きログの設定 |
| `core/measurement_definition_service.py` | 測定定義の業務操作・入力検証 |
| `core/meter_master_service.py` | 物理メーターの業務操作・入力検証 |
| `core/meter_reading_sync_repositories.py` | Repository Protocol と入出力レコード |
| `core/meter_reading_sync.py` | 検針結果・交換イベントの保存処理 |
| `core/decimal_arithmetic.py` | Decimal の丸めなし加減算・乗算 |
| `core/mobile_reading.py` | MR1 結果テキスト / JSON の検証と同期要求への変換 |
| `core/mobile_reading_import.py` | スマホ結果の差分プレビュー・再検証・一括確定 |
| `core/mobile_reading_sheet.py` | 検針シートの枠・前回値・単一 HTML 生成 |
| `core/mobile_reading_assets/` | HTML に内包する検針画面の CSS / JavaScript |
| `core/previous_reading.py` | 予定枠ごとの前回値と交換境界の判定 |
| `core/reports.py` | 測定定義・式の期間集計と版別明細 |
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
| `db/schema_v3.sql` | スキーマ v3 の固定 DDL |
| `db/migrations.py` | スキーマの移行と版別検査 |
| `db/integrity.py` | 復元候補の業務整合性検査 |
| `db/repositories.py` | 標準 `sqlite3` による Repository 実装 |
| `db/bootstrap_seeds.py` | DB の既定設定の初期投入 |
| `db/backup.py` | SQLite バックアップ関連処理 |
| `db/restore.py` | 復元候補の検査・移行・保全・置換 |
| `ui/single_instance.py` | QLockFile による多重起動防止 |
| `ui/display_settings.py` | フォント・文字サイズ等の表示設定 |
| `ui/dialogs/` | スマホ検針・PC 直接入力・交換・マスタ管理・設定・バックアップ / 復元のダイアログ |
| `ui/widgets/` | スケジュール編集・マスタ管理・計算条件訂正・集計ダッシュボードの画面部品 |
| `ui/dialog_placement.py` | ダイアログの画面内配置 |
| `ui/error_handling.py` | 業務例外・予期しない例外の UI 通知 |
| `ui/csv_export.py` | CSV 等のファイル出力ダイアログ |
| `ui/aggregation_export.py` | 集計系列の CSV とグラフの PNG 出力 |
| `tests/` | コア業務ロジック、DB、UI 共通部品の自動テスト |

`core/` は純 Python の業務ロジック、`db/` は SQLite 永続化、`ui/` は PySide6 の UI 共通部品を配置します。`db/repositories.py` が標準 `sqlite3` Repository を実装しています。旧 SQLAlchemy 参考資産は統合テスト成功後に削除済みです。

## 開発フェーズ

| フェーズ | 状況 | 主な対象 |
| --- | --- | --- |
| Phase 0 | 完了 | リポジトリ骨格・共通基盤・品質ゲートの整備 |
| Phase 1 | 完了 | 標準 sqlite3 Repository と既存コア業務ロジックの統合 |
| Phase 2 | 完了 (2026-10-28) | 単一 HTML 検針シート・途中保存・MR1 / JSON パーサと DB 同期のブラウザ往復テスト |
| Phase 3 | 完了 (2026-11-01、両 OS 必須 CI 成功・PR #29 merge 済み) | メイン画面・マスタ・設定・PC 直接入力・交換 UI |
| Phase 4 | 完了 (2026-11-04、両 OS 必須 CI 成功・PR #34・#35 merge 済み) | スマホ検針ダイアログ・プレビュー・一括確定 |
| Phase 5 | 進行中 (5a の完了条件一部未確認、5b・5c 未完了) | 5a・5b・5c の全機能と検証。Release 下書きの実績確認後に全体完了 |
| Phase 5a | 実装・自動テスト・自動操作確認済み。人手による通し確認・必須 CI 未確認 | 集計式・版管理・期間集計・ダッシュボード・CSV / PNG |
| Phase 5b | 画面・復元・Python 再起動は実装・自動検証済み。必須 CI 未確認 | バックアップ・復元 UI、候補検査・移行、安全な置換・再起動。実際の exe・インストール後の検証は 5c |
| Phase 5c | 未着手 | Windows 配布、ライセンス・対応ソース、Release 下書き自動作成 |

成果物と完了条件は開発計画書 8 章を参照してください。資産の同梱だけで各フェーズの完了とは扱いません。
Phase 2 の完了条件は Playwright によるブラウザ検証までです。Android 実機確認は Phase 4 で実施し、端末・OS・Chrome・起動経路と確認結果を開発計画書に記録しています。

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
