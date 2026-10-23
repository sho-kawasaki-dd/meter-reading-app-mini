# **プロジェクト企画書：オフライン対応・Wi-Fi同期型 検針業務システム (v2)**

## **1\. プロジェクト概要**

本プロジェクトは、施設内の電気・ガス・水道メーターの検針業務をデジタル化し、ヒューマンエラーの削減と、\*\*「料金試算の根拠となる精緻なデータ集計」\*\*の自動化を目的とする。

通信環境の不安定な現場での利用を前提とし、「完全オフラインでの入力・異常検知」と「特定ネットワーク下での任意タイミング1ボタン同期」を両立させるローカルファーストなアーキテクチャを採用する。

## **2\. システム・アーキテクチャと技術スタック**

フロントエンド（入力端末）とバックエンド（管理PC）をAPI契約によって完全に分離する。

### **管理PC側（バックエンド 兼 集計・管理UI）**

* **言語/UI**: Python 3.x / FastAPI + Jinja2 + htmx + Alpine.js + PicoCSS  
* **APIサーバー**: FastAPI（検針 API と管理 UI を同一プロセス上で提供）  
* **Web管理基盤**: FastAPI + Jinja2（本番運用でも継続利用する共通管理基盤。DB テーブル閲覧（読み取り専用）、設定閲覧・編集、planned reading 一覧、ログ閲覧を提供する。加えて、マスターデータ（物理メーター、measurement definition、meter assignment）の CRUD 管理画面を提供し、本番運用中のメーター追加や測定対象変更をコード変更なしで行える）  
* **現場専用運用ページ**: FastAPI + Jinja2（朝の確認導線、例外処理、臨時検針作成、締め確認など、現場固有の業務フローに応じて後から追加する専用ページ群）  
* **データベース・ORM**: SQLite3 \+ SQLAlchemy 2.0  
* **ネットワーク検知**: zeroconf (mDNSによるサービスブロードキャスト)。mDNS は Android 側の手動 `base_url` 入力を置き換えるものではなく、接続先候補を見つける補助機能として扱う。service type は `_meter-reading._tcp.local.` を初期案とする。

### **現場端末側（フロントエンド）**

* **OS/言語**: Android / Kotlin  
* **UIフレームワーク**: Jetpack Compose  
* **ローカルDB**: Room (未送信 backlog と表示用キャッシュの永続化用)  
* **ネットワーク検知**: NsdManager (mDNS探索)  
* **HTTP通信**: Ktor Client

### **プロジェクトフォルダ構成**

本リポジトリは、Python バックエンドアプリと Android フロントエンドアプリを同一プロジェクト配下で管理する。各アプリケーションのルートフォルダは以下のとおりとする。

* **Python バックエンドアプリのルート**: `./pc_app/`  
   FastAPI、SQLite/SQLAlchemy を用いる管理 PC 側アプリケーション本体を配置する。FastAPI 上には検針 API（`/api/*`）に加えて、共通管理機能を担う Web 管理基盤と、必要に応じて現場専用の運用ページを Jinja2 テンプレートで提供する。ローカル開発用の Python 仮想環境 `.venv/` も `./pc_app/` 配下に存在し、依存関係管理と実行環境はこのディレクトリを基準に扱う。
* **Android フロントエンドアプリのルート**: `./android_app/`  
   Kotlin / Jetpack Compose による現場端末向けアプリケーション本体を配置する。
* **企画・契約ドキュメント**: `./docs/`  
   API 契約の補助資料、企画書、JSON Schema スナップショットなどを配置する。

```text
./
|- android_app/        # Android フロントエンドアプリのルート
|  |- app/
|  |- gradle/
|  |- build.gradle.kts
|  `- settings.gradle.kts
|- docs/               # 企画書・API 契約関連ドキュメント
`- pc_app/             # Python バックエンドアプリのルート
    |- .venv/           # Python 仮想環境
    |- data/
    |- src/
    |- pyproject.toml
    `- requirements.txt
```

## **3\. 業務フロー（ユースケース）**

1. **準備（朝/同期候補セッション）**:  
   管理PCとAndroid端末を同一Wi-Fiに接続。朝 7:00 ごろを目安に、まず Android から PC へアクセスして前日分の検針結果およびメーター交換イベントをアップロードする。アップロードは前日結果の反映と確認を目的とした独立ステップとして扱い、端末側は成功応答を受け取るまで未送信フラグを保持する。  
2. **当日予定受信（朝/PC→Android ダウンロード）**:  
   前日分アップロード後、必要な確認を経て Android は本日の検針対象となる物理メーター一覧 `meters`、論理的な測定対象一覧 `measurement_definitions`、および PC 側で事前生成した「当日の検針予定枠一覧（business\_date, reading\_session, planned\_reading\_id, measurement\_definition\_id を含む work package）」をダウンロードする。ここで `meter_id` は交換対象となる物理デバイス、`measurement_definition_id` は「どの値をどの単位・計算方式・頻度で読むか」を表す論理的な測定対象を意味し、planned reading はこの measurement definition 単位で生成する。work package はフラットな planned reading 配列ではなく、`daily_tasks`、`weekly_tasks`、`monthly_tasks`、`every_two_months_tasks`、`ad_hoc_tasks` をキーに持つ辞書構造として配信し、Android 側は受信したグループ単位で UI ページを構成する。これにより、クライアント側で `reading_cycle` を見て配列を自力で再分類する責務を避ける。HTTP API 上も UX 上も、アップロードとダウンロードは別操作として扱い、単一の巨大な同期エンドポイントには統合しない。PC 側が数日停止していた場合でも、planned reading は PC 操作の有無ではなく business\_date と measurement definition ごとの reading\_cycle に基づいて欠損なく再構築し、未配布だった過去日分の予定枠も Android へ配信できるようにする。これにより、現場で手書き記録した検針値を Android 上で後追い入力する復旧フローを許容する。朝の同期候補セッションを基本導線としつつ、同期操作自体は日中の任意タイミングで手動実行できるものとする。  
3. **検針（日中・夜間/オフライン対応）**:  
   担当者は Android 端末上で `daily_tasks`、`weekly_tasks`、`monthly_tasks`、`every_two_months_tasks`、`ad_hoc_tasks` の各ページから planned reading ごとのタスク一覧を確認し、各タスクに対して measurement definition で定義された形式の値を記録する。単一の Decimal で表せる測定対象は**「生の指針（Raw Reading）」**を、power factor のように複合表現が必要な測定対象は measurement payload を入力し、検針不能・未実施などの場合は result\_status を記録する。reading\_session は PC から配信された予定枠を UI 上で表示するために利用し、通常は Android 側で自由入力しない。タスクの分類は API レスポンス時点で完了しているため、Android 側は受信したグループをそのまま表示・進捗管理すればよい。各ページ内での並び替えや絞り込みは PC 側ロジックを複製するのではなく、Android 側の汎用ビューア機能として提供する。  
      端末内の独立したバリデーションロジックは measurement definition に紐づく last\_reading、warning\_threshold、value\_schema を参照し、入力形式チェックと参考値表示用のローカル差分評価を行う。ここで算出した差分は UI 補助情報であり、PC 側へ送信する正規データには含めない。欠測時も planned reading 自体は残り、単なる未送信ではなく状態付きデータとして保持される。skipped や unreachable の場合も recorded\_at は「その予定枠に対して現場で結果を確定した時刻」として記録するが、このレコード自体は差分計算の基準値にはしない。メーター交換が発生した場合は、通常の検針結果とは別に交換イベントを記録し、同一 measurement definition に紐づく残り予定枠の meter assignment を新メーター前提へ差し替える。  
4. **前日分同期・集計（朝/Android→PCバッチ送信）**:  
   次回の朝の同期候補セッションで、Android は未送信 backlog に含まれる planned\_reading\_id をキーにした結果データと、必要に応じてメーター交換イベントを**バッチ送信**する。UX 上の基本導線は「前日分アップロード」だが、API は複数 business\_date にまたがる未送信データを受け付ける。アップロードはリトライ可能な独立エンドポイントで受け付け、端末側は成功応答を確認するまで未送信フラグを保持する。Android のローカル DB は未送信バックログの一次保管を目的とし、PC 側で `accepted` または `ignored` と判定された item は端末内バックログから速やかに消し込む。  
      **PC側（FastAPI/SQLAlchemy）にて、PC のシステム時計が設定された締め時刻（初期値例: 毎朝 7:00）を過ぎた時点で、Android からのアップロード有無に関わらず、前日以前で結果未着の planned reading を自律的に照合して auto\_closed として強制クローズする。締め時刻は PC 側の設定値として保持し、運用中に変更可能とする。保存時には recorded\_at を UTC へ正規化しつつ、PC が事前に確定していた business\_date と reading\_session を参照して業務集計へ反映する。result\_status が skipped や unreachable のレコードは usage\_delta=null のまま保存し、後続の measured レコードが到着した時点で、同一連続区間に属する直近の measured を recorded\_at 順に探索して差分計算を行う。auto\_closed 後であっても、後着の現場入力が届いた場合は system による強制クローズより現場入力を優先して採用し、必要な usage\_delta と集計を再計算する。メーター交換イベントが含まれる場合は、delta かつ decimal\_string の measurement definition ごとに replacement boundary fact を保存し、旧メーターの残り予定枠を replaced または superseded 扱いにし、新メーター向けの予定枠へ差し替える。新メーター初回 measured は replacement event の new\_initial\_reading を last\_reading として優先利用し、値がない場合だけ usage\_delta=0 とする。交換イベントが遅延到着した場合も、open / auto\_closed の予定枠は巻き戻して再照合し、completed 済み reading は task\_state を completed のまま維持して reading transaction と replacement boundary fact を再評価する。**

※ 朝の同期候補セッションは、前日分アップロードと当日予定ダウンロードを別々の UX 操作として提供する。

## **4\. 管理対象ロケーション（測定箇所）一覧**

※物理的なメーター機器（シリアル番号等）とは分離し、以下の「論理的な設置場所」をベースに集計を行う。実際の予定枠生成は location 単位ではなく、各 location 配下にぶら下がる measurement_definition 単位で行い、同一箇所に「積算電力量」「電流」「電圧」「温度」のような複数の測定対象を並立して持てるようにする。

* **電気（7箇所）**: 引込電力量計、ホテル棟分岐盤電力量計、ホテル棟低圧動力盤電力量計、防災動力盤電力量計、カフェ動力盤電力量計、ホテル厨房電灯盤、ホテル厨房動力盤  
* **ガス（3箇所）**: 本社棟、カフェ、ホテル  
* **水道（2箇所）**: 引込量水器、ホテル量水器  
* その他の計測機器を追加していく可能性あり（差し引き計算を必要としないものもある）

## **5\. 料金計算基盤としての堅牢性設計（コア・ルール）**

本システムは課金・料金試算の基礎データとなるため、以下の厳密なデータ管理ルールを敷く。

1. **浮動小数点（Float）の排除**:  
   丸め誤差を防ぐため、JSON通信時は小数を「文字列（String）」として扱う。Python側では decimal.Decimal として処理し、SQLiteへの保存時はSQLAlchemyのカスタム型を用いて TEXT として格納する。  
2. **物理メーター・論理ロケーション・measurement definition の三層分離**:  
   メーター交換時や測定対象追加時のデータ破綻を防ぐため、`location_id`（集計場所）、`meter_id`（物理デバイス）、`measurement_definition_id`（論理的な測定対象）を分けて管理する。月次集計は常に location\_id と measurement definition の意味づけを基準に行い、物理デバイス交換は meter assignment の差し替えとして扱う。これにより、同一 location に複数の測定対象を定義しても planned reading は独立した予定枠として生成できる。  
   さらに、同一物理メーターに対して異なる目的（日次運用確認と月次費用按分根拠など）で検針する場合に集計時の二重計上を防ぐため、measurement definition に `purpose` を持たせる。`purpose` は検針の業務上の目的を示す固定語彙（初期語彙: `operational` / `billing` / `audit`）であり、集計クエリで用途別にフィルタする際の安全弁として機能する。delta 計算ロジック自体は `measurement_definition_id` 単位で既に分離されているため、`purpose` は計算には関与せず、集計・表示のフィルタ専用として扱う。  
3. **ロールオーバー（カンスト）対応**:  
   メーターが1周した際のマイナス値算出を防ぐため、マスター側に rollover\_value（例: 100000.0）を保持し、バックエンドの計算ロジックで自動補正する。  
4. **新設メーターの初期値処理**:  
   交換直後の新しいメーター（前回検針データが存在しない状態）から最初の measured を受けた場合、バックエンドは replacement event に new\_initial\_reading があればそれを last\_reading として差分計算に使い、new\_initial\_reading がない場合だけ「今回の使用量（usage\_delta）＝ 0」として記録する。これにより、交換時に控えた初期指針を初回計算へ明示的に反映しつつ、値が欠けるケースでは異常な使用量の跳ね上がりを防ぐ。
5. **時刻の正規化と業務日付の分離**:  
   API 契約では recorded\_at を offset 付き ISO8601 として受け付け、通信経路上では現地時刻の意味を保持する。一方、SQLite へ保存する際は UTC に正規化した recorded\_at を canonical な時刻として格納する。さらに、午前0時をまたぐ検針でも日次・月次集計を破綻させないため、検針トランザクションテーブルに business\_date（業務日付）と reading\_session（どの検針サイクルかを示すスロット）を追加し、集計はこれらを基準に行う。  
6. **計算ポリシーと値スキーマの明示**:  
   差分計算が必要ない計測機器や、単一 Decimal では表現できない測定対象を将来追加できるよう、measurement definition に calculation\_method と value\_schema を持たせる。標準的な積算メーターは前回値との差分を計算する方式を採用し、差分不要の機器は生値をそのまま保存・表示する方式に切り替えられる設計とする。value\_schema は少なくとも decimal\_string と power\_factor\_percent\_phase を表現できるようにし、API 契約と Android UI の双方で入力形式の判定に用いる。  
7. **検針頻度の明示**:  
   1日3回、1日1回、週1回（`weekly`）、月1回（`monthly`）、隔月1回（`every_two_months`）など、measurement definition ごとに異なる検針頻度を扱えるよう、measurement definition に reading\_cycle カラムを持たせる。これにより、Android への対象提示、未検針チェック、業務日付ごとの期待件数判定を「どの値を読む予定か」という単位で切り替えられるようにする。`reading_cycle` は頻度の種類だけを表す固定語彙とし、日次の検針時刻、週次の検針曜日、月次の検針ルール、隔月の基準月・検針ルール・reading\_session といった具体的な予定生成ルールは measurement definition ごとではなく Web 管理基盤から変更できる `app_settings` の運用設定として保持する。日次設定は reading\_cycle が示す回数分の reading\_session 時刻を保持する（`daily_1x` は 1 個、`daily_2x` は 2 個、`daily_3x` は 3 個）。週次設定は曜日指定を基本とし、月次・隔月設定は少なくとも `day_of_month`、`nth_weekday`、`month_edge`（月初/月末）の選択肢を持てるようにする。`day_of_month` で指定した日が存在しない月は、その月の末日に丸めて planned reading を生成する。
8. **固定語彙フィールドの扱い**:  
   category、calculation\_method、value\_schema、reading\_cycle、reading\_session、result\_status、purpose のような固定語彙フィールドは、SQLite では String として柔軟に保持し、DB レベルでは Enum 制約をかけない。一方、API 契約では Pydantic の StrEnum を用いて許容値を明示し、想定外の値は FastAPI の入力検証で排除する。これにより SQLite との相性問題を避けつつ、Python 実装では語彙を一元管理しながら、API 境界では文字列 enum として十分な型安全性を確保する。reading\_session の初期語彙は 24 時間表記の時刻文字列を採用し、当面は 00:00、06:00、08:00、14:00、22:00 を候補とする。`daily_1x` / `daily_2x` / `daily_3x` の検針時刻、および `weekly` / `monthly` / `every_two_months` の具体的な生成条件は固定語彙そのものへ埋め込まず、PC 側 `app_settings` に保持した運用設定から解決する。purpose の初期語彙は `operational`（日常運用確認）、`billing`（費用按分根拠）、`audit`（監査・検証用途）とし、デフォルト値は `operational` とする。
9. **予定枠先行配布と欠測状態の明示**:  
   欠落データで集計が破綻しないよう、PC は業務開始時に planned\_reading\_id を持つ検針予定枠を事前配布する。予定枠の粒度はメーター単位ではなく measurement definition 単位とし、Android は各予定枠に対して必ず結果レコードを作成し、測定できた場合は value\_schema に応じた値を、測定できなかった場合は result\_status を記録する。skipped や unreachable は「測れていないが予定枠は消化した」状態として保持し、raw\_reading と usage\_delta は自動補完しない。これにより「送られてこなかったため存在しないデータ」と「予定されていたが未実施・不能だったデータ」を明確に区別できる。
10. **メーター交換イベントの明示**:  
   メーター交換は通常の検針結果とは別のドメインイベントとして扱う。交換時には location\_id を不変の集計キーとして維持しつつ、old\_meter\_id と new\_meter\_id、交換時刻、旧メーター最終読値、新メーター初期読値を記録する。さらに、calculation\_method=delta かつ value\_schema=decimal\_string の measurement definition については、replacement\_event 単位ではなく measurement definition 単位の replacement boundary fact を保持し、直前旧メーター実測と old\_final\_reading の差分を old\_meter\_usage\_delta として保存する。同日中に交換が起きた場合は、旧メーターに紐づく未消化の予定枠を replaced または superseded 扱いにし、新メーター用の予定枠へ差し替える。新メーターの初回検針は new\_initial\_reading があればそれを last\_reading として使い、ない場合だけ usage\_delta=0 として扱う。
11. **UI 分割を前提にした work package 構造化**:  
   日次・週次・月次・隔月の検針では現場導線や確認項目が大きく異なるため、PC→Android の work package は planned reading をフラット配列で返さず、`daily_tasks`、`weekly_tasks`、`monthly_tasks`、`every_two_months_tasks`、`ad_hoc_tasks` をキーに持つ辞書構造で返す。個々の PlannedReadingTask は引き続き reading\_cycle を保持するが、クライアントはその値を見て再分類せず、API が返したグループを信頼して UI を分割する。これにより Thin Client 方針を維持しつつ、将来「日次タスクのみ先に同期する」といった段階同期にも対応しやすくする。
12. **クライアント更新時刻による upsert 優先順位の明示**:  
   Android から返却される検針結果およびメーター交換イベントには `client_updated_at` を含め、`planned_reading_id` と `replacement_event_id` を自然キーとした upsert の優先順位判定に用いる。サーバーは同一キーの既存レコードがない場合は insert し、既存レコードがある場合は `client_updated_at` がより新しい入力を優先して上書きする。`client_updated_at` が同値の場合は、サーバーが後から受信した入力を採用する。これにより、再送は冪等に扱いつつ、現場での訂正入力はクライアント側の更新順序を基本に、同時刻競合時は後着優先で反映できる。
13. **予定枠ライフサイクルと自動クローズの分離**:  
   planned reading の open / completed / auto\_closed / superseded といったライフサイクル状態は、検針結果の result\_status とは別に PC 側で管理する。auto\_closed は「PC のシステム時計が設定された締め時刻を越えた時点で、現場結果が存在しないまま残っていた」という planned reading の状態であり、Android が送信する result\_status の語彙へ混在させない。これにより、現場で入力された skipped / unreachable と、PC 側が業務日を閉じるために行う system 処理を監査上区別できる。
14. **遅延アップロードと遅延交換イベントの優先順位**:  
   Android 端末が故障や通信不良により複数日アップロードできなかった場合でも、後着の現場入力は system による auto\_closed より優先して採用する。`client_updated_at` は client 起源データ同士の競合解決にのみ用い、system による強制クローズとは別レイヤで扱う。メーター交換イベントが遅延到着した場合は、open / auto\_closed の planned reading を superseded に巻き戻して新メーター向け planned reading を再生成し、completed 済み reading については task\_state を completed のまま維持したまま reading transaction と replacement boundary fact を再評価する。
15. **予定枠生成の連続性保証**:  
   planned reading の生成責務は「PC がその日に操作されたかどうか」ではなく、measurement definition ごとの reading\_cycle と business\_date カレンダーに基づいて PC 側が担う。PC が数日停止していた、あるいは担当者が UI を操作していなかった場合でも、次回起動時または同期時には欠けている business\_date 分の planned reading を backfill 生成し、過去日分を含む予定枠として Android へ配信できるようにする。これにより、現場で紙に控えた検針値を Android へ後追い入力し、planned\_reading\_id を基準に正規の結果として返却できる。
16. **署名シードと端末識別子の固定化**:  
   `signature` は単なる任意文字列ではなく、`planned_reading_id`、`result_status`、値 payload、`recorded_at`、`device_id` を固定順で連結したシード文字列を `SHA-256` 等でハッシュ化して生成する。`device_id` は Android 端末上で初回起動時に UUID v4 を生成し、DataStore に永続化した stable per-install 識別子とする。再インストール後は別 ID として扱い、操作者や表示名を表す `updated_by` とは分離する。Android 側では `site_label` と `device_label` を DataStore の別キーで保持し、両方を trim 後に必須・各 32 文字以内としたうえで、保存時に `${site_label}-${device_label}` 形式の snapshot を `updated_by` として採番する。設定変更後も既存 backlog の `updated_by` は自動更新しない。複合 payload は署名計算前にキー順と空白を固定した canonical JSON 文字列へ正規化する。
17. **主端末制と端末切替運用**:  
   本システムは通常運用では「1 台の主 Android 端末」を前提とし、PC 側は `app_settings` に保持した `active_device_id` を用いて work package download と backlog upload の相手を 1 台へ束ねる。`active_device_id` は Android が初回起動時に生成して保持する `device_id` と同じ stable per-install 識別子を指す。`active_device_id` が未設定の初期状態では、最初に同期成功した端末を主端末として自動登録する。登録済みの主端末と異なる `device_id` からのアクセスは通常運用では拒否し、`device_not_active` のような固定理由を返して Android 側で再試行不能な状態として判定できるようにする。  
   ただし、端末故障・再インストール・一時代替機への切替に対応するため、PC 側管理画面から `active_device_id` の確認、解除、切替を行えるようにする。これにより通常時は単一端末運用を維持しつつ、故障時には代替端末へ短時間で移行できるようにする。

## **6\. API契約（データモデル定義）**

API は batch-first を原則とし、朝の同期候補セッションでは Android→PC の前日分結果・交換イベント返却と、PC→Android の当日分 work package 配信を別々に扱う。HTTP API だけでなく UX 上も、前日分アップロードと当日予定ダウンロードを分離する。API 契約では `meter_id` を物理デバイス識別子、`measurement_definition_id` を論理的な測定対象識別子として明示的に分け、planned reading は後者を基準に生成する。主端末制を適用する場合、work package download 系 GET では Android は `X-Device-Id` request header に `device_id` を必ず載せ、PC 側は `app_settings` 上の `active_device_id` と照合する。GET は request body を持たないため端末識別子は header で渡す。POST /api/readings でも Android は `X-Device-Id` request header を必ず載せるが、`device_id` は署名シードと item 単位監査に必要であるため、従来どおり各 `MeterReadingSubmission` の body にも保持する。

* **行き（PC \-\> Android / 例: GET /api/work-packages/today, Header: X-Device-Id）**: WorkPackageResponse  
   * meters: MeterMasterData[]  
   * measurement\_definitions: MeasurementDefinitionData[]  
   * work\_package: WorkPackageTaskGroups  
* **WorkPackageTaskGroups**  
   * daily\_tasks: PlannedReadingTask[]  
   * weekly\_tasks: PlannedReadingTask[]  
   * monthly\_tasks: PlannedReadingTask[]
   * every_two_months_tasks: PlannedReadingTask[]  
   * ad\_hoc\_tasks: PlannedReadingTask[]  
* **MeterMasterData**  
   * meter\_id, location\_id, location\_name, meter\_name, category (StrEnum: electricity/gas/water/boiler/others), meter\_serial (optional)  
* **MeasurementDefinitionData**  
   * measurement\_definition\_id, location\_id, meter\_id (current assignment snapshot or null), display\_name, category (StrEnum: electricity/gas/water/boiler/others), unit, value\_schema (StrEnum: decimal\_string/power\_factor\_percent\_phase), last\_reading (String/Decimal or null), multiplier (String/Decimal), warning\_threshold (String/Decimal or null), rollover\_value (String/Decimal or null), calculation\_method (StrEnum: delta/raw\_only), reading\_cycle (StrEnum: daily\_1x/daily\_2x/daily\_3x/weekly/monthly/every_two_months), purpose (StrEnum: operational/billing/audit)  
* **PlannedReadingTask**  
   * planned\_reading\_id, measurement\_definition\_id, meter\_id, location\_id, business\_date, reading\_session (StrEnum 初期案: 00:00/06:00/08:00/14:00/22:00), required  
* **帰り（Android \-\> PC / 例: POST /api/readings）**: MeterReadingSyncRequest  
   * reading\_items: MeterReadingSubmission[]  
   * replacement\_items: MeterReplacementSubmission[]  
* **同期応答（PC \-\> Android / 例: POST /api/readings 正常応答）**: MeterReadingSyncResponse  
   * accepted\_reading\_items: Integer  
   * accepted\_replacement\_items: Integer  
   * ignored\_reading\_items: IgnoredReadingSyncItem[]  
   * ignored\_replacement\_items: IgnoredReplacementSyncItem[]  
   * rejected\_reading\_items: RejectedReadingSyncItem[]  
   * rejected\_replacement\_items: RejectedReplacementSyncItem[]  
   * detail (optional)  
* **request 単位エラー（GET / POST 共通）**: ApiRequestErrorResponse  
   * code, detail, request\_device\_id (optional), retryable, mismatched\_reading\_item\_indexes[]  
* **MeterReadingSubmission**  
   * planned\_reading\_id, result\_status (StrEnum: measured/skipped/unreachable/meter\_fault/replaced), raw\_reading (String/Decimal or null), structured\_payload (object or null), recorded\_at (offset付き ISO8601), client\_updated\_at (offset付き ISO8601), device\_id, updated\_by（`site_label-device_label` 形式の監査補助 snapshot）, signature (ハッシュ値), note (optional)
* **MeterReplacementSubmission**  
   * replacement\_event\_id, location\_id, old\_meter\_id, new\_meter\_id, replaced\_at (offset付き ISO8601), client\_updated\_at (offset付き ISO8601), old\_final\_reading (String/Decimal or null), new\_initial\_reading (String/Decimal or null), updated\_by（`site_label-device_label` 形式の監査補助 snapshot）, note (optional)
* **IgnoredReadingSyncItem / IgnoredReplacementSyncItem**  
   * item\_index, planned\_reading\_id or replacement\_event\_id, code (`stale_client_update`), detail (optional)  
* **RejectedReadingSyncItem / RejectedReplacementSyncItem**  
   * item\_index, planned\_reading\_id or replacement\_event\_id, code (`unknown_planned_reading` / `unknown_measurement_definition` / `invalid_reading_submission` / `missing_device_id` / `signature_canonicalization_failed` / `invalid_signature`), detail  

※ recorded\_at は通信上は現地時刻の offset を保持し、永続化時に UTC へ正規化する。business\_date と reading\_session は PC が planned reading として事前配布し、Android UI 側ではその予定枠の表示・状態管理に利用する。Android 側は業務日付を自由に決めず、planned\_reading\_id を通じて予定枠に結果を紐づける。`work_package` は UI 分割済みの辞書として返し、`daily_tasks` は日次系、`weekly_tasks`は週次系、`monthly_tasks` は月次系、`every_two_months_tasks` は隔月系、`ad_hoc_tasks` は臨時・例外枠を表す。個々の task は引き続き reading\_cycle を保持するが、クライアント側で再分類ロジックを持つ前提にはしない。`measurement_definition_id` により「同じ location にある別種の測定対象」を独立した予定枠として識別し、交換や再配本は `meter_id` の差し替えとして扱う。`daily_1x` / `daily_2x` / `daily_3x` の検針時刻、`weekly` の対象曜日、`monthly` の対象日、`every_two_months` の基準月・対象日・reading\_session は API 契約の enum 値へ埋め込まず、PC 側 `app_settings` から planned reading 生成時に解決する。GET と POST に対する `X-Device-Id` header は主端末制の照合に使う request-level 情報であり、署名シードには含めない。request 単位の 4xx では `ApiRequestErrorResponse` を返し、フィールドは `code`、`detail`、`request_device_id`、`retryable`、`mismatched_reading_item_indexes` の 5 つに固定する。`mismatched_reading_item_indexes` は header/body 不一致時だけ使い、それ以外では空配列とする。主端末不一致は `409` と `code=device_not_active`、header 欠落や header/body 不一致は `400` の request 形式エラーとして扱う。正常な batch 応答では `accepted_*` を件数、`ignored_*` を「受理したが適用しなかった item の配列」、`rejected_*` を「同一 batch 内で他 item と独立に reject された item の配列」として返す。Android は `accepted` と `ignored` を backlog から消し込み、`rejected` はローカルに保持したまま修復対象として扱う。raw\_reading は対象の value\_schema が decimal\_string のときに必須とし、それ以外のときは null を許容する。structured\_payload は対象の value\_schema が power\_factor\_percent\_phase など複合型のときに必須とし、初回は measurement definition ごとの schema に従って検証する。`device_id` は Android 側で一度生成して永続化する安定識別子であり、署名生成に使う。`updated_by` は `site_label` と `device_label` を別キーで保持したうえで、保存時に `${site_label}-${device_label}` 形式へ結合した監査補助 snapshot とする。両ラベルは trim 後に必須とし、各 32 文字以内に制限し、設定変更後も既存 backlog row の `updated_by` は自動更新しない。`signature` は `${planned_reading_id}|${result_status}|${raw_reading ?: structured_payload_json ?: ""}|${recorded_at}|${device_id}` のシード文字列をハッシュ化したものとし、`structured_payload_json` は canonical JSON 文字列へ正規化して用いる。サーバーは `device_id` 未指定、canonical 化不能、不正な署名を item 単位で `rejected` として返す。POST /api/readings の `device_id` は item 単位の署名検証と監査のため body に保持し続ける。サーバーは request header `X-Device-Id` と reading item の body 側 `device_id` が一致することを確認し、不一致や header 欠落は request 単位の不正として扱う。`client_updated_at` は検針結果・交換イベントの上書き優先順位を決めるクライアント側更新時刻として必須とし、サーバーは `planned_reading_id` または `replacement_event_id` を自然キーとした upsert 時にこの値を比較して、より新しい入力を採用する。`client_updated_at` が同値の場合は後着の入力を採用する。POST /api/readings は前日分に限定せず、未送信 backlog の複数業務日データを受け付ける。PC 側は current day の予定枠だけでなく、停止期間や未配布期間に相当する過去日分 planned reading も backfill 生成し、Android へ再配布できるようにする。Android は受け取った過去日分 planned reading に対して、手書き記録などをもとに後追い入力した結果を通常の MeterReadingSubmission として返却できる。PC 側の auto\_closed は planned reading の内部状態として扱い、Android が送信する result\_status へは露出しない。skipped や unreachable の recorded\_at は欠測状態を現場で確定した監査時刻として保持し、後続の measured に対する差分計算では「直近の measured を recorded\_at 順に探索する」ための順序キーとしてのみ利用する。欠測枠そのものへ usage\_delta を逆算配賦せず、欠測期間中の使用量按分は請求業務フロー側で扱う。MeterReplacementSubmission は通常の検針結果とは独立に送信し、PC 側で meter assignment 履歴と予定枠差し替えへ反映する。replacement item は request header `X-Device-Id` をその request の送信元端末識別子として扱い、Phase 3B 時点では body に別の `device_id` を追加しない。サーバー内部では replacement submission を measurement definition 単位の replacement boundary fact へ展開し、対象は calculation\_method=delta かつ value\_schema=decimal\_string に限定する。boundary の消費判定は accepted された最初の新メーター measured reading transaction の存在で行い、Phase 4 では consumed\_at は持たない。交換イベントが auto\_closed 後に遅延到着した場合は、open / auto\_closed の予定枠を superseded へ巻き戻し、completed 済み reading は task\_state を completed のまま維持して再反映する。reading\_cycle、calculation\_method、value\_schema、purpose は measurement definition の必須項目として扱い、検針対象の抽出や期待件数判定の基準に用いる。purpose は集計クエリで用途別に usage\_delta を抽出する際のフィルタキーとして利用し、delta 計算ロジック自体には関与しない。

## **7\. セキュリティとデータ保全**

* **DBの隠蔽**: SQLiteファイルの拡張子変更によるカジュアルな誤操作防止。本番環境では~/%LOCALAPPDATA%に隠蔽して配置する方針。  
* **改ざん検知**: Androidからの送信データにはハッシュ値（signature）を付与する。初期ルールは `${planned_reading_id}|${result_status}|${raw_reading ?: structured_payload_json ?: ""}|${recorded_at}|${device_id}` を canonical なシード文字列として `SHA-256` 等でハッシュ化する方式とし、`structured_payload_json` はキー順と空白を固定した canonical JSON 文字列へ正規化する。`device_id` は Android 側の安定識別子であり、`updated_by` とは別に扱う。サーバーは署名不一致、`device_id` 欠落、canonical 化不能を item 単位で `rejected` として扱い、監査ログへ残す。  
* **主端末制による配布先制御**: PC 側は `app_settings` に `active_device_id` を 1 つ保持し、通常運用では一致する Android 端末にのみ work package download と backlog upload を許可する。初回は最初に同期成功した端末を `active_device_id` として自動登録する。download 時は `X-Device-Id` header を使って request-level に端末を識別し、upload 時は `X-Device-Id` header と body 内 `device_id` を併用して request-level と item-level の両方で整合性を確認する。主端末不一致は `device_not_active` を理由とした request 単位拒否とし、header 欠落や header/body 不一致は形式不正として別扱いにする。端末故障や再インストール時は管理画面から `active_device_id` を切り替え、代替端末へ移行できるようにする。  
* **真実の源泉（Single Source of Truth）**: 検針予定枠（business\_date, reading\_session, planned\_reading\_id, measurement\_definition\_id）は PC が生成した work package を正とし、各予定枠に対する現場結果（raw\_reading または structured\_payload, result\_status, recorded\_at）および交換イベントは Android 端末からの入力を正とする。計算済み使用量（usage\_delta）はトランザクションテーブルに結果スナップショットとして保存し、遅延アップロードや accepted replacement 訂正で影響範囲が確定した場合に必要範囲だけ再確定する（結果保存型）。
* **更新競合の解決**: Android からの再送・訂正入力は、`planned_reading_id` と `replacement_event_id` を自然キーとした upsert で受け付ける。上書き優先順位は `client_updated_at` により決定し、サーバーはより新しい更新を採用する。`client_updated_at` が同値の場合は後着の更新を採用する。古い更新は既存値を上書きしない。
* **交換イベント訂正の適用範囲**: same replacement\_event\_id の newer accepted update は、location\_id・old\_meter\_id・new\_meter\_id が不変な訂正に限って replacement boundary と affected reading transaction へ再適用する。meter pair の変更を伴う correction / rollback は別タスクとして扱う。
* **予定枠ライフサイクルの分離管理**: planned reading の open / completed / auto\_closed / superseded は PC 側の内部状態として管理し、Android が送る result\_status と混同しない。PC のシステム時計が設定された締め時刻を越えた時点で、前日以前の open タスクを auto\_closed に自律遷移させることで業務日を閉じるが、この system 状態は現場由来の skipped / unreachable と別物として監査する。
* **現場入力優先の再反映**: auto\_closed 後に Android から遅延アップロードが到着した場合、サーバーは system による強制クローズより現場入力を優先し、planned reading の状態と usage\_delta、関連集計を再計算する。`client_updated_at` は client 同士の競合解決にのみ用い、system auto\_close との優先順位判定には使わない。
* **予定枠の backfill 生成**: planned reading は PC 操作の有無ではなく business\_date、measurement definition ごとの reading\_cycle、そして PC 側 `app_settings` に保持された週次・月次・隔月の具体スケジュール設定に基づいて生成する。PC が数日停止していた場合でも、次回起動時または同期時に欠けている business\_date 分の planned reading を backfill 生成し、過去日分の未配布タスクを Android へ再配布できるようにする。
* **ローカルDBの一時性**: Android 側のローカル DB は未送信 backlog と当面の UI 表示を支える一時保管領域として扱い、PC 側で `accepted` または `ignored` と判定された reading / replacement は端末内バックログから速やかに削除する。端末内を業務履歴の恒久保存先にはしない。
* **UI 分割済み payload の一貫性**: work package は `daily_tasks`、`weekly_tasks`、`monthly_tasks`、`every_two_months_tasks`、`ad_hoc_tasks` の辞書形で配信し、Android はこの構造をそのまま UI 導線へ反映する。どの planned reading がどのページに現れるかは PC 側の分類結果を正とし、クライアント側で再分類した結果を真実の源泉にしない。
* **時刻の二層管理**: 監査や照合に使う canonical time は UTC 正規化済み recorded\_at とし、業務集計は business\_date と reading\_session を用いて行う。
* **欠測時の差分計算ルール**: skipped や unreachable は欠測状態そのものを結果として保存し、差分計算の基準値には採用しない。次の measured が到着した時点で、同一連続区間に属する直近の measured を recorded\_at 順に探索し、その差分を当該 measured レコードの usage\_delta として確定する。
* **欠測期間の按分責務の分離**: 欠測期間をまたいで確定した cumulative な usage\_delta を日次・時間帯別・請求区分別へ按分する処理は、検針 API / 同期処理の責務に含めない。本システムでは欠測区間の事実と、次回 measured 到着時点で確定した累積差分までを保持し、その按分ロジックは後続の請求業務フロー側で補完ルールに従って対処する。
* **交換イベント遅延時の巻き戻し**: MeterReplacementSubmission が遅延到着した場合、PC 側は旧 meter\_id 前提で作成済みの affected planned reading のうち open / auto\_closed を superseded として巻き戻し、新メーター向け planned reading と meter assignment 履歴を再生成する。completed 済み reading は task\_state を completed のまま維持し、replacement boundary と reading transaction を再評価したうえで必要な usage\_delta と期待件数判定を補正する。
* **固定語彙の境界検証**: category、calculation\_method、value\_schema、reading\_cycle、reading\_session、result\_status、purpose は DB では文字列として保持しつつ、API 境界では StrEnum により検証する。OpenAPI には文字列 enum として公開し、クライアント契約を明示する。

## **8\. アーキテクチャのトレードオフと代替案（リスク管理）**

* **リスク1：mDNS（ZeroConf）のパケット破棄**  
  * *事象*: 企業内LAN等のセキュリティ設定によりmDNSが遮断される。  
   * *代替案*: Androidアプリ上に手動で「PC のベース URL（scheme / host / port）」を入力・保存できる接続設定 UI を実装する。mDNS の開始失敗や遮断は warning ログに残すが、PC 側 API と管理画面は継続起動し、管理画面に手動入力用の接続先候補を表示する。  
* **リスク2：SQLAlchemyのFloat暗黙変換トラップ**  
  * *事象*: SQLAlchemy標準の Numeric/DECIMAL 型をSQLiteで使用するとFloatに劣化し誤差が生じる。  
  * *対策*: TypeDecorator を用いた明示的な String へのキャスト処理をリポジトリ層に強制する（第5項-1参照）。  
* **リスク3：深夜帯検針による日次/月次集計の誤帰属**  
   * *事象*: recorded\_at の日付だけで集計すると、午前0時をまたいだ検針が別日の使用量として扱われる。  
   * *対策*: UTC 正規化済み時刻とは別に business\_date と reading\_session を保持し、集計基準を業務サイクルに寄せる。  
* **リスク4：欠測データが「存在しないデータ」として埋もれる**  
   * *事象*: 検針不能や未実施が単なる未送信として扱われると、集計や未検針チェックが破綻する。  
   * *対策*: PC が事前に planned\_reading\_id を持つ予定枠を配布し、Android は各予定枠に対して result\_status を持つ結果を必ず返す。skipped や unreachable は usage\_delta=null の明示的な欠測結果として残し、後続の measured 到着時にのみ直近実測との差分を計算する。  
* **リスク5：同日中のメーター交換で予定枠と実メーターの対応が崩れる**  
   * *事象*: 交換前に生成された planned reading が旧 meter\_id のままだと、交換後の検針結果と整合しなくなる。  
   * *対策*: MeterReplacementSubmission を導入し、旧メーターの残り予定枠を replaced または superseded 扱いにしたうえで新メーター向け planned reading に差し替える。location\_id を集計キーとして維持し、delta + decimal\_string 対象では replacement boundary fact に旧メーター側差分を保存する。新メーター初回は new\_initial\_reading を優先し、値がない場合だけ usage\_delta=0 とする。  
* **リスク6：未操作タスクが永続的に open のまま残る**  
   * *事象*: バッテリー切れ、担当者不在、端末故障などで現場結果が一切返却されないと、前日分 planned reading が未完了のまま残り、期待件数判定や締め処理が破綻する。  
   * *対策*: PC のシステム時計が設定された締め時刻を越えたら、Android からのアップロード有無に関わらず、前日以前の結果未着 planned reading を auto\_closed として照合・強制クローズする。締め時刻は設定値として変更可能とし、auto\_closed は result\_status ではなく planned reading の内部状態として保持する。  
* **リスク9：PC 停止期間の予定枠が生成されず業務日が欠落する**  
   * *事象*: 数日 PC を起動していなかった、または担当者が UI を操作していなかった場合に、その期間の planned reading が生成されないと、後から手書き記録を入力しても正規の planned\_reading\_id に紐づけられない。  
   * *対策*: planned reading は業務日カレンダーと reading\_cycle に基づいて backfill 生成し、PC 復旧後に過去日分の予定枠も Android へ再配布できるようにする。これにより、手書き記録の後追い入力も planned reading を基準に正規処理できる。  
* **リスク8：締め時刻の固定化が運用変更に追従できない**  
   * *事象*: シフト変更や施設運用の見直しで業務締めの基準時刻を変更したくなっても、固定値実装だと設定変更だけで済まず、改修や再配布が必要になる。  
   * *対策*: auto\_close の締め時刻は PC 側設定値として保持し、既定値を持ちつつ運用中に変更可能とする。設定変更後は次回以降の自動照合から新しい締め時刻を適用する。  
* **リスク7：交換イベントの遅延到着で旧メーター前提の予定枠が残る**  
   * *事象*: Android が数日故障して replacement event の送信が遅れると、PC 側は旧 meter\_id 前提の planned reading や期待件数を保持したまま運用してしまう。  
   * *対策*: replacement\_event\_id を自然キーに遅延イベントも受け付け、到着時に affected な planned reading のうち open / auto\_closed は superseded へ巻き戻して新メーター向け予定枠と meter assignment 履歴を再生成する。completed 済み reading は state を維持したまま replacement boundary と reading transaction を再評価し、関連集計を補正する。  
* **UI計算ロジック排除への反論と妥協点**:  
   * 一般的にオフラインアプリではUI側でも計算・状態保持を行う（Fat Client構成）ことが多いが、本設計では「マスター情報の同期による閾値判定」のみをAndroidに許容し、実際の「使用量算出」と「日次/週次/月次/隔月/随時タスクへの分類」はPC側に一任するThin Client寄りの構成をとる。これにより、将来的な計算ルールや同期単位の変更に対する保守性を最大化している。

### **補足: 永続化スキーマへの反映方針**

* **物理メーターマスターテーブル**: meter\_id を主キーに、location\_id, meter\_name, meter\_serial など物理デバイス固有の属性を保持する。ここには calculation\_method や reading\_cycle のような「何をどう読むか」の意味論は持たせない。`is_active`（Boolean、デフォルト True）を保持し、運用中に不要となったメーターを論理削除できるようにする。物理削除は行わず、過去の reading transaction からの FK 参照を維持する。  
* **measurement definition テーブル**: measurement\_definition\_id を主キーに、location\_id, category, unit, value\_schema, multiplier, warning\_threshold, rollover\_value, calculation\_method, reading\_cycle, purpose を文字列または Decimal 文字列で保持する。purpose は検針の業務目的（operational/billing/audit）を示し、デフォルト値は operational とする。`is_active`（Boolean、デフォルト True）を保持し、運用中に不要となった測定対象を論理削除できるようにする。これにより、同一 location に複数の独立した測定対象を並立でき、同一物理メーターに対して異なる目的の measurement definition を定義しても集計時に用途別のフィルタが可能となる。  
* **app settings テーブル**: Web 管理基盤から変更できる運用設定を key/value で保持し、cutoff 時刻に加えて、planned reading の具体スケジュール条件と主端末制の状態を保持する。cutoff 時刻のキーは `auto_close.cutoff_time.v1` とし、値は `"07:00"` 形式の HH:MM 文字列で保持する。主端末制の現在値は `device_binding.active_device_id.v1` のような versioned key に `device_id` 文字列として保持する。日次は reading\_cycle が示す回数分の reading\_session 時刻配列、週次は曜日と reading\_session、月次は `selector_type=day_of_month|nth_weekday|month_edge` と対応する値、隔月は月次と同じ selector に加えて `month_parity=odd|even` と reading\_session を持つ JSON 文字列として保持する。キーは `planned_reading_schedule.daily_1x.v1`、`planned_reading_schedule.daily_2x.v1`、`planned_reading_schedule.daily_3x.v1`、`planned_reading_schedule.weekly.v1`、`planned_reading_schedule.monthly.v1`、`planned_reading_schedule.every_two_months.v1` のような versioned な命名を採用し、`day_of_month` で指定した日がその月に存在しない場合は末日へ丸める。これらは measurement definition ごとの属性ではなく、PC 全体の運用設定として扱う。service 層は DB に値が未設定の場合はコード上のデフォルト値にフォールバックするため、app\_settings テーブルへの明示的な初期投入がなくても動作する。`bootstrap --init-settings` CLI でデフォルト値を明示的に DB へ一括投入する手段も提供する。  
* **meter assignment / replacement event テーブル**: measurement\_definition\_id ごとの meter\_id 有効期間と、replacement\_event\_id, old\_meter\_id, new\_meter\_id, replaced\_at, old\_final\_reading, new\_initial\_reading を保持し、交換履歴を追跡する。  
* **replacement boundary テーブル**: replacement\_event\_id と measurement\_definition\_id を主キーに、location\_id, business\_date, old\_meter\_id, new\_meter\_id, replaced\_at, previous\_measured\_*, old\_final\_reading, old\_meter\_usage\_delta, new\_initial\_reading を保持する。対象は calculation\_method=delta かつ value\_schema=decimal\_string の measurement definition に限定し、boundary の消費判定は accepted された最初の新メーター measured reading transaction の存在で行う。  
* **planned reading テーブル**: planned\_reading\_id, measurement\_definition\_id, meter\_id, location\_id, business\_date, reading\_session, required に加えて、task\_state(open/completed/auto\_closed/superseded)、closed\_at、close\_reason を保持し、当日の業務枠とその締め状態を表現する。締め判定に使う cutoff や、`daily_1x` / `daily_2x` / `daily_3x` の検針時刻、`weekly` / `monthly` / `every_two_months` の具体スケジュール条件は PC 側 `app_settings` から解決して planned reading 生成へ反映する。PC 再起動時には missing な business\_date 範囲の planned reading を backfill 生成できるよう、生成済み業務日範囲を判定できる構造とする。  
* **検針トランザクションテーブル**: planned\_reading\_id, raw\_reading, structured\_payload\_json, last\_reading, usage\_delta, result\_status, recorded\_at(UTC), device\_id, updated\_by, signature, note を保持する。必要に応じて result\_source(client/system/manual) を追加し、system auto\_close や手動補正と現場入力を監査上区別できるようにする。business\_date と reading\_session は planned reading を参照しつつ、検索性のために冗長保持してもよい。`device_id` は署名再計算と監査のために保持し、`updated_by` は `site_label-device_label` 形式の人間可読な snapshot として保持する。  
* **集計クエリ**: 監査などでは UTC recorded\_at を参照し、日次・月次の業務集計では business\_date と reading\_session、measurement\_definition\_id を参照する。直近実測値の探索では recorded\_at を順序キーとして用いるが、対象件数の期待値や未検針判定では planned reading と reading\_cycle、task\_state を参照する。欠測期間をまたぐ cumulative usage\_delta の日別・時間帯別按分はこの層では行わず、請求業務フロー側で対処する。交換発生日は meter assignment / replacement 履歴に加えて replacement boundary fact を参照して location\_id 単位の連続性と measurement definition 単位の意味づけを同時に保つ。月次・年次集計 query の本格統合は後続フェーズで扱い、Phase 4 では business\_date と location\_id を持つ boundary fact を集計入口として整備する。
* **外部アプリケーションとの連携**: 将来的に料金試算ツールや業務日誌アプリと連携する際は、DB 読み出しをもとに JSON 形式でエクスポートされたデータを提供することとし、検針 API とは別機能として疎結合に保つ。
* **マスターデータ管理方針**: bootstrap CLI（`--init-master-data` / `--init-settings`）は初回セットアップ用の一括投入手段とし、本番運用開始後のメーター追加・測定対象変更・割り当て変更は Web 管理基盤（`/admin/meters`、`/admin/measurement-definitions`、`/admin/meter-assignments`）から行う。削除は物理削除を行わず `is_active=False` による論理削除のみとし、過去の reading transaction からの FK 参照を維持する。`is_active=False` のレコードは work package 生成・API レスポンスの対象外とするが、管理画面上では非アクティブレコードの切替表示を可能とする。measurement definition 新規登録時に `meter_id` を指定した場合は `meter_assignments` レコードを自動作成する。入力値の検証は必須フィールドチェック、FK 整合性（`meter_id` が `physical_meters` に存在すること等）、および StrEnum 語彙チェック（`category`、`calculation_method`、`value_schema`、`reading_cycle`、`purpose` など）を行う。共通管理機能とは別に、日々の運用導線に強く依存する画面は現場専用の Web ページとして後から追加する。

### **補足: Android 側の安全弁**

* Android 側では category、calculation\_method、value\_schema、reading\_cycle、reading\_session、result\_status、purpose を enum class で扱いつつ、UNKNOWN フォールバックを持たせる。  
* reading\_session の初期値集合は 00:00、06:00、08:00、14:00、22:00 とし、UI 表示もこの文字列をそのまま利用する。  
* Android 側では `device_id` を初回起動時に UUID v4 で生成し、DataStore に永続化して `signature` のシードに必ず含める。再インストール後は別 ID として扱う。`updated_by` は操作者名や端末表示名などの監査補助情報であり、`device_id` の代用にしない。  
* Android 側では `site_label` と `device_label` を Preferences DataStore の別キーで保持し、trim 後に必須・各 32 文字以内とする。保存時は `${site_label}-${device_label}` を `updated_by` の snapshot として利用し、設定変更後も既存 backlog row の `updated_by` は自動更新しない。  
* Android 側では接続先 `base_url`、直近同期成功時刻 `last_sync_ok_at`、直近 request 単位 4xx を Preferences DataStore に保持し、接続設定画面から参照できるようにする。接続設定画面は `site_label` と `device_label` の保存画面も兼ね、`${site_label}-${device_label}` の preview を表示してよい。`base_url`、`site_label`、`device_label` のいずれかが未設定の間は、同期トップへ進む前に接続設定を完了させる。  
* work package download の GET と backlog upload の POST では `X-Device-Id` header に `device_id` を必ず載せる。主端末切替後は Android 側の接続確認導線で、現在の `device_id` が active 端末として受理されるかを確認できるようにする。  
* Android 側の HTTP client はベース URL を client 初期化時に固定せず、接続設定から request ごとに組み立てる。接続確認は `GET /api/work-packages/today` の受理可否で代用し、GET / POST の両方に `X-Device-Id` を自動付与する。  
* Android 側のローカル差分計算は参考値表示と警告表示のためにのみ使い、usage\_delta の正規計算結果としてサーバーへ送信しない。  
* 同期操作は朝の基本導線に加え、日中の任意タイミングでも手動実行できる。  
* work package 内のページ分割は PC 側のグループをそのまま使い、各ページ内での並び替え・絞り込みだけを汎用 UI 機能として提供する。  
* Android 側のローカル DB は未送信 backlog の一次保管を主目的とし、`accepted` または `ignored` 済みデータは速やかに消し込み、`rejected` は `UPLOAD_REJECTED` と error detail を保持したまま再編集対象として残す。  
* これにより、将来 PC 側で新しい語彙が追加されても既存クライアントが即時クラッシュせず、未知値として表示・記録できるようにする。

*Reference Materials:*

* **Floating Point Math**: What Every Computer Scientist Should Know About Floating-Point Arithmetic (Oracle)  
* **API Design**: Contract-First API Development