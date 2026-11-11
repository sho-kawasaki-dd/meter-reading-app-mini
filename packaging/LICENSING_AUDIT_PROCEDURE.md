# 第三者ソフトウェア・対応ソース監査手順

この手順は、検証用 Windows x64 配布物のバイナリ、対応ソース、ライセンス通知、
再配布条件を照合するための作業手順です。法的助言ではありません。
判断が不明な場合は推測で完了扱いにせず、配布を止めて権利者または専門家に確認してください。

現在の監査状態は [`dependency-sources/manifest.json`](./dependency-sources/manifest.json)
の `audit_status: "pending"` です。監査の完了、Sandbox / VM 検証、配布承認は別の判定です。
通常の配布用ビルドを可能にする目的で、証拠が揃う前に状態だけを変更してはいけません。

## 監査の対象と完了条件

この手順では、次の対象について、配布バイナリからその由来と配布条件まで追跡します。

| 対象 | 確認すること |
|---|---|
| libffi | 配布 DLL を作ったソースコミット、パッチ、ビルド元 |
| pydantic-core | 配布 wheel / 拡張バイナリと Rust ソース・ビルド条件の対応 |
| Rust | 実際のビルド対象依存、標準ライブラリ、著作権表示・通知 |
| Qt / PySide6 | 同梱 DLL・拡張・プラグインと対応ソース・第三者通知 |
| Microsoft 製コンポーネント | 同梱ファイルの由来と、適用される再配布条件 |

各対象について、少なくとも以下を満たしたときに技術的な監査完了候補とします。

- 配布物内の実ファイルを特定し、バージョン・SHA256・入手元を記録した。
- バイナリと対応ソース、コミット、パッチ、ビルド条件の関係を根拠資料で説明できる。
- 実際に配布するファイルに必要なライセンス本文・著作権表示・第三者通知を確認した。
- 対応ソース一式と、再ビルドに必要なスクリプト・設定・パッチが揃っている。
- 不明点、除外した依存、再配布条件の判断と根拠を記録した。

版数が同じであること、候補ソースを取得したこと、SHA256 が取得元アーカイブと一致したことだけでは、
バイナリとソースの対応を確認したことにはなりません。SHA256 はファイル同一性の確認に使い、
発行者の認証やライセンス適合性の証明とは扱いません。

## 1. 作業を準備する

### 1.1 監査対象を固定する

リポジトリルートの PowerShell で、監査するコミットと作業状態を記録します。

```powershell
git status --short
git rev-parse HEAD
uv --version
uv run --locked python --version
```

作業ツリーに変更がある場合は、その差分を監査記録に含めます。コミット ID だけで対象ソースを
特定できない状態のまま監査を進めないでください。

次の情報も記録します。

- Windows のエディション・バージョン・x64 であること
- Python と python-build-standalone の版
- uv、Rust / Cargo、Inno Setup の版
- `pyproject.toml`・`uv.lock`・PyInstaller spec の対象コミット
- 使用した配布ビルドの識別子と生成日時

### 1.2 依存ソースと検証用成果物を用意する

README の配布検証手順に従い、ソース候補の取得・ハッシュ検証と検証用ビルドを行います。

```powershell
uv sync --locked
uv run --locked python scripts\fetch_dependency_sources.py
uv run --locked python scripts\build_distribution.py --verification-only
```

ビルド出力は毎回新しいディレクトリにします。既存の成果物を今回の監査対象として使い回さず、
生成された構成一覧、`SHA256SUMS.txt`、対応ソース ZIP、検証用インストーラーを一緒に保管します。
この検証用成果物は配布禁止です。

### 1.3 バイナリ一覧を作る

検証用出力の `app` ディレクトリを `packaging/bundle-files.txt` と照合します。
各 DLL・`.pyd`・プラグインについて、ファイル名、相対パス、SHA256、バージョン情報、
署名情報、想定するコンポーネントを記録します。

```powershell
Get-ChildItem -LiteralPath '検証用出力の絶対パス' -Recurse -File |
    Where-Object { $_.Extension -in '.dll', '.pyd' } |
    ForEach-Object {
        [pscustomobject]@{
            Path = $_.FullName
            SHA256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            Company = $_.VersionInfo.CompanyName
            Product = $_.VersionInfo.ProductName
            FileVersion = $_.VersionInfo.FileVersion
            SignatureStatus = (Get-AuthenticodeSignature -LiteralPath $_.FullName).Status
        }
    } | Format-Table -AutoSize
```

バージョン情報や署名がないファイルもあり得ます。その場合は空欄や状態をそのまま記録し、
他の証拠で出所を確認します。署名の状態だけで再配布可否を判断しません。

## 2. libffi の実ビルドを追跡する

[`manifest.json`](./dependency-sources/manifest.json) に登録された `libffi-candidate` は候補です。
登録済みコミットを実際のビルド元とみなさず、以下の順で確認します。

1. 配布物の `libffi-8.dll` のパスと SHA256 を記録する。
2. 固定した python-build-standalone の配布物・ビルド定義から、その DLL の取得元を追う。
3. ビルド定義で libffi をビルドしているか、別の既成バイナリを取り込んでいるかを確認する。
4. 実際に使われたリポジトリ URL、完全なコミット ID、ソースアーカイブの SHA256 を特定する。
5. 適用パッチ、ビルドスクリプト、対象アーキテクチャ、コンパイラー、設定を記録する。
6. 記録済み候補コミットと照合し、一致しない場合は実際のソース候補・通知・許可リストを更新する。

ビルドログや上流の固定参照で特定できず、候補コミットとの対応を立証できない場合は未解決とします。
文字列・ファイル名・版数が似ていることからコミットを推定してはいけません。
確認可能なソースから自前ビルドへ切り替える場合は、置き換え後の DLL を新たに棚卸しし、
アプリの起動と関連機能を再検証します。

## 3. pydantic-core と Rust を追跡する

### 3.1 wheel と拡張バイナリを特定する

1. `uv.lock` と生成済み構成一覧から pydantic-core の版と使用 wheel を記録する。
2. wheel の入手元と SHA256 を記録する。
3. wheel 内の `_pydantic_core` 拡張と、検証用配布物内の拡張を特定する。
4. wheel 内の拡張と配布物の拡張の SHA256 を比較する。
5. wheel を作ったリリースタグ・コミット・CI 設定・ビルドログを調べる。
6. 同梱した sdist / crate のソースが、そのビルドに対応することを確認する。

インストール済み拡張の位置は次で確認できます。

```powershell
uv run --locked python -c "import pydantic_core._pydantic_core as m; print(m.__file__)"
```

同じ版の sdist が存在するだけでは wheel のビルド元の証明になりません。
上流のビルド記録で対応が追えない場合は、その不足を記録して上流へ確認するか、
把握可能な条件で自前ビルドした wheel を使う案を検討します。

### 3.2 実際に使われる Cargo 依存を確認する

`Cargo.lock` に記録された crate 全件を「Windows バイナリに組み込まれる依存」とは扱いません。
監査対象の pydantic-core ソースを別の作業ディレクトリに展開し、実際のビルド条件を確認します。

- target triple と Rust toolchain の完全な版
- cargo / maturin の版と実行コマンド
- default features と明示的に有効化した features
- Windows 対象の通常依存・ビルド依存・proc-macro
- テスト専用または Windows 以外に限定された依存
- 上流ビルドに適用されたパッチ・環境変数・コンパイラー設定

ビルド条件が確認できたら、その条件を明記して Cargo の依存グラフを出力します。例:

```powershell
cargo tree --locked --target x86_64-pc-windows-msvc -e normal,build
```

このコマンドは依存関係を調べる補助資料です。上流 wheel が同じコマンド・設定で作られたことは、
上流のビルド定義やログで別途確認します。除外した crate は、除外理由と target / feature 条件を記録します。

### 3.3 Rust 標準ライブラリと通知を確認する

1. wheel のビルドに使われた Rust toolchain の版を確定する。
2. その toolchain に対応する標準ライブラリのソースとライセンス資料を取得する。
3. 標準ライブラリのライセンス・著作権・第三者コード通知を調べる。
4. 実際に使われた crate のライセンス原文・著作権表示・必要な帰属表示を確認する。
5. `Rust-third-party-notices.txt` の各項目を実際の依存と照合し、不足・誤記を修正する。

現在の通知候補にある `r-efi` の `AUTHORS` や `wit-bindgen-rt` の上流コミット資料は、
候補を照合するための証拠です。個別候補の確認だけで、Rust 全体の通知監査を完了にしません。
上流 Rust プロジェクトの一般的な `main` ブランチではなく、対象 toolchain の版に結び付いた資料を使います。

## 4. Qt・PySide6 の対応ソースと通知を追跡する

### 4.1 配布物をソース系統に割り当てる

`bundle-files.txt` に記載された全 Qt 関連 DLL、`.pyd`、プラグインを確認し、次のソース系統へ割り当てます。

| 配布物の例 | 確認するソース |
|---|---|
| Qt Core / Gui / Widgets / OpenGL と各プラグイン | qtbase |
| Qt Svg と SVG 関連プラグイン | qtsvg |
| Qt Charts | qtcharts |
| PySide6 / shiboken6 | pyside-setup |

実際の割当てはファイル名だけで決めず、wheel の内容・公式ビルド資料・依存情報で裏付けます。
不要な Qt DLL やプラグインが紛れ込んでいないことも確認します。

### 4.2 ソース・ビルド条件と通知を照合する

1. `PySide6`、`PySide6_Addons`、`PySide6_Essentials`、`shiboken6` の wheel とハッシュを記録する。
2. wheel に含まれる各 Qt バイナリ・プラグインの入手元、版、対象ソースを特定する。
3. 対応する qtbase / qtsvg / qtcharts / pyside-setup の正確なソース版とコミットを確認する。
4. ビルドに適用されたパッチ・構成オプション・第三者ライブラリ・SBOM の有無を調べる。
5. ソース内の attribution 情報と、`Qt-third-party-notices.txt` の部品・版・ライセンス・表示内容を突合する。
6. 配布する Qt モジュールごとのライセンス選択と必要なソース提供・表示を確認する。

候補ソースの版が一致するだけで対応確認済みとは扱いません。wheel のビルド情報や公式 SBOM 等で
追跡できない箇所は未解決として記録し、必要なら Qt / PySide の配布元へ確認します。
Qt Charts を含むアプリのライセンス方針は [`DISTRIBUTION_LICENSE.txt`](./DISTRIBUTION_LICENSE.txt)
と [`THIRD_PARTY_NOTICES.txt`](./THIRD_PARTY_NOTICES.txt) に整合させます。

## 5. Microsoft 製コンポーネントの再配布条件を確認する

対象ファイルを `bundle-files.txt` から列挙します。現行一覧には MSVC runtime DLL、UCRT、
Windows API-set DLL などが含まれます。ファイルごとに次を記録してください。

1. ファイル名、バージョン情報、SHA256、署名の状態、取得元。
2. そのファイルをアプリに同梱する必要があるか、Windows が提供するものか。
3. Visual Studio / MSVC のどの版のライセンス条項と REDIST 一覧が適用されるか。
4. 個別 DLL または再頒布パッケージとして配布する際の条件・制約・必要な表示。
5. 開発ツールや再配布対象外ファイルが成果物に混入していないこと。

確認には Microsoft の一次資料を使います。

- [Visual C++ ファイルの再配布](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170)
- [Universal CRT の配置](https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment?view=msvc-170)
- 該当する Visual Studio 版のソフトウェアライセンス条項と REDIST 一覧

UCRT や .NET Framework が Windows のコンポーネントとして提供されることと、
成果物に DLL を同梱して再配布することは別の判断です。また、GPL 上の「System Libraries」に
該当する可能性と、Microsoft の再配布条件への適合も別々に判定してください。
不明なファイルは「OS 標準だから対象外」と推定せず、依存解析とクリーンな Windows 環境での起動確認を行います。

## 6. 対応ソース ZIP と通知を再検査する

各対象の根拠確認後、ソースと通知を更新し、次を確認します。

1. 実際に配布するソース・パッチ・ビルドスクリプトが [`source-files.txt`](./source-files.txt)
   に含まれている。
2. 実行ファイル・DLL・プラグインが [`bundle-files.txt`](./bundle-files.txt) と一致している。
3. ライセンス本文と通知が [`licenses/`](./licenses)、
   [`THIRD_PARTY_NOTICES.txt`](./THIRD_PARTY_NOTICES.txt) にあり、名称・版・対応物が一致している。
4. 依存ソース一覧の URL・版・SHA256・ライセンス・監査注記が実際の証拠と一致している。
5. ソースアーカイブを新しい隔離ディレクトリに展開し、記載された手順で再ビルドできる。
6. 再ビルドに必要なソース・パッチ・設定が欠けていた場合は追加し、アーカイブを再生成する。

ソース一式だけを取得したことや、アプリ本体だけを再ビルドできたことを、
依存バイナリの対応ソース確認の代わりにしてはいけません。再ビルドは実データのない
Sandbox / VM で実施し、ネットワークが必要だった箇所・取得物・コマンド・結果を記録します。

配布ビルドの再実行:

```powershell
uv run --locked python scripts\fetch_dependency_sources.py
uv run --locked python scripts\build_distribution.py --verification-only
```

生成物の内容、構成一覧、SHA256 を確認し、失敗した場合は古い出力を流用しません。
ビルド出力が検査を通ったことは、法的な判断の代替にはなりません。

## 7. 監査記録を作成する

監査対象ごとに、次の記録を残します。長いログや成果物は識別可能な保管先を記載し、
秘密情報・実データ・利用者情報を記録や配布ソースに含めません。

| 記録項目 | 内容 |
|---|---|
| 対象 | コンポーネント名・配布ファイルの相対パス |
| 成果物 | ファイル版、SHA256、ビルド ID |
| 取得元 | URL、wheel / アーカイブ名、取得日 |
| 対応ソース | リポジトリ、完全なコミット ID、ソース SHA256 |
| ビルド条件 | OS / target、toolchain、features、パッチ、コマンド |
| ライセンス | ライセンス識別子、原文・通知ファイル、必要な表示 |
| 再配布条件 | 一次資料、該当条項、判定と確認者 |
| 根拠 | URL、SBOM、ビルドログ、メタデータ、保存場所 |
| 未解決事項 | 不明点、照会先、配布停止が必要か |
| 結果 | 確認済み / 未解決 / 対象外と理由、確認日・担当者 |

対象外とする場合も理由と根拠を記載します。ライセンス解釈や再配布権限の判断が未解決なら、
技術的な調査が終わっていても監査を完了にしません。

## 8. 状態を更新する前の最終確認

- [ ] 作業対象コミット・配布物 ID・環境が記録されている。
- [ ] libffi の実際のソースコミット・パッチ・バイナリとの対応を確認した。
- [ ] pydantic-core 拡張バイナリと wheel / Rust ソースの対応を確認した。
- [ ] 実際の Rust 依存・標準ライブラリ・通知を確認した。
- [ ] Qt / PySide6 の全同梱バイナリ・プラグインと対応ソース・通知を確認した。
- [ ] Microsoft 製同梱ファイルの由来・再配布条件をファイル単位で確認した。
- [ ] ライセンス本文・第三者通知・ソース許可リスト・構成一覧を一致させた。
- [ ] 対応ソース ZIP を隔離環境で展開し、必要なソースと手順を確認した。
- [ ] すべての不明点が解決し、監査記録に根拠・確認者・日付がある。

未完了の項目がある間は `audit_status` を `pending` に保ち、検証用成果物を配布しません。
全項目を確認した後に限り、監査記録と配布関連資料をレビューし、
`manifest.json` の状態・注記を更新できます。Sandbox / VM のインストール・更新・削除等の
実地確認は別チェックなので、この監査完了だけで `sandbox_validation_status` を変更しません。
配布用ビルドと Release は、Phase 5 計画書の該当手順・利用者承認にも従います。
