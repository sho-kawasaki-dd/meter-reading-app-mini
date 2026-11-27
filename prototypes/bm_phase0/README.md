# BM-Phase 0 技術検証用試作

このディレクトリは、業務日誌アプリの PDF 生成手段と気象庁の日別値ページの取得・解析を検証するための試作置き場です。試作コードと関連資料をここに配置します。

## 実行方法

試作コードはこのディレクトリ以下に置き、必要な Python パッケージはプロジェクトの依存関係へ追加せず、`uv run --with <パッケージ名> python <試作スクリプト>` のように一時的に指定して実行します。各試作で必要なパッケージや具体的な実行コマンドは、スクリプトの追加時にここへ追記します。

PDF・生成 HTML・ダウンロードしたフォント・一時ファイルなどの生成物は `out/` 以下へ出力してください。この出力先は `.gitignore` で除外しています。気象庁から取得して記録する試料ページは `jma_samples/` に置き、生成物とは区別します。

### PDF 生成手段の比較

以下は架空の業務日誌・プログラム生成写真・実行時取得フォントを使います。データ生成と WeasyPrint の検証は次のとおりです。

```powershell
uv run --with pillow --with fonttools --with weasyprint --with pypdf python prototypes/bm_phase0/pdf_spike.py prepare
uv run --with pillow --with fonttools --with weasyprint --with pypdf python prototypes/bm_phase0/pdf_spike.py render --engine weasyprint
```

Qt WebEngine はプロジェクトの PySide6 を使い、PDF 検査用パッケージだけ一時指定します。

```powershell
uv run --with pillow --with fonttools --with pypdf python prototypes/bm_phase0/pdf_spike.py render --engine qtwebengine
```

`out/pdf/` に日次・上限超過日次・31 日月次の HTML/PDF、Noto Sans JP と SIL Open Font License 1.1、試料サイズ・OS 別の検査結果 JSON を出力します。HTML と PDF は Git に含めません。

#### WeasyPrint の分割境界の確認

上記の `prepare` で試料を用意した後、既存の超長日次と、写真・表行を境界に置く追加試料を PDF にして確認します。Windows では上記と同様に MSYS2 UCRT64 の Pango を PATH に追加します。

```powershell
uv run --locked --with weasyprint==70.0 --with pymupdf --with pillow python prototypes/bm_phase0/verify_weasyprint_pagination.py
```

画像の描画矩形・画素と表行の文字を確認し、1 ページに収まらない表行のはみ出し・欠落も再現します。CSS の改ページ回避だけでは巨大な行の保護を保証できません。この検証は WeasyPrint の試作のみを対象とし、Qt WebEngine の検証や製品側での入力制限・検出の実装を代替しません。

### Windows 開発 PC での試作 exe 比較

以下は開発 PC 上での `onedir` 試作であり、未導入の Sandbox/VM や GitHub Actions での検証の代わりにはなりません。事前に上記の `prepare` を実行し、WeasyPrint 用には MSYS2 UCRT64 の Pango とその依存 DLL を用意します。計測時は Python 3.13.12、PyInstaller 6.22.3、WeasyPrint 70.0、PySide6 6.12.0 を使用しました。試作ごとに新しい出力先を指定し、既存の成果物に上書きしないでください。

```powershell
$root = (Get-Location).Path
$qt = Join-Path $root 'prototypes\bm_phase0\out\build-qt'
$weasy = Join-Path $root 'prototypes\bm_phase0\out\build-weasy'
if ((Test-Path $qt) -or (Test-Path $weasy)) { throw '新しい出力先を指定してください。' }
New-Item -ItemType Directory -Path $qt, $weasy | Out-Null

uv run --with pypdf python -m PyInstaller --noconfirm --onedir --name pdf-qt `
  --distpath "$qt\dist" --workpath "$qt\build" --specpath $qt `
  --exclude-module weasyprint --exclude-module PIL --exclude-module fontTools `
  prototypes\bm_phase0\pdf_spike.py
if ($LASTEXITCODE -ne 0) { throw 'Qt のビルドに失敗しました。' }

$oldPath = $env:PATH
try {
  $env:PATH = "C:\msys64\ucrt64\bin;$oldPath"
  $dllArgs = @()
  foreach ($name in @('libgobject-2.0-0', 'libpango-1.0-0', 'libharfbuzz-0',
      'libharfbuzz-subset-0', 'libfontconfig-1', 'libpangoft2-1.0-0')) {
    $dllArgs += '--add-binary', "C:\msys64\ucrt64\bin\$name.dll;."
  }
  uv run --with weasyprint --with pypdf python -m PyInstaller --noconfirm --onedir `
    --name pdf-weasy --distpath "$weasy\dist" --workpath "$weasy\build" `
    --specpath $weasy --exclude-module PySide6 --collect-all weasyprint `
    @dllArgs prototypes\bm_phase0\pdf_spike.py
  if ($LASTEXITCODE -ne 0) { throw 'WeasyPrint のビルドに失敗しました。' }
} finally {
  $env:PATH = $oldPath
}
```

配布一式のサイズは試料のコピー前に `Get-ChildItem <dist 内の試作フォルダ> -Recurse -File | Measure-Object Length -Sum` で測ります。実行時は `out/pdf/` の 3 個の HTML を各試作フォルダの `_internal\out\pdf\` にコピーし、`pdf-qt.exe render --engine qtwebengine`、`pdf-weasy.exe render --engine weasyprint` をそれぞれ実行します。PATH をシステムディレクトリに限定しても、開発 PC にあるフォントや設定への依存を排除したことにはなりません。

GitHub Actions の `BM Phase 0 PDF spike` は必須 CI と別の試作用ワークフローです。対象ブランチでワークフローまたは試作コードを更新した場合に起動し、各方式の配布一式・架空の HTML・検査結果を 7 日間の成果物として保存します。この成果物は製品の配布物ではありません。

## 品質検査・配布の対象範囲

試作コードはアプリの wheel、対応ソースの収録一覧、Pyright、pytest、coverage の対象外です。`packaging/source-files.txt` へ試作ファイルを追加せず、wheel の対象は `src/meter_reading_mini` のままとします。リポジトリ全体を対象とする Ruff の検査・整形確認は試作コードにも適用します。
