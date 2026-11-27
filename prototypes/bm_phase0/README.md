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

## 品質検査・配布の対象範囲

試作コードはアプリの wheel、対応ソースの収録一覧、Pyright、pytest、coverage の対象外です。`packaging/source-files.txt` へ試作ファイルを追加せず、wheel の対象は `src/meter_reading_mini` のままとします。リポジトリ全体を対象とする Ruff の検査・整形確認は試作コードにも適用します。
