# IGアクセストークンの恒久リフレッシュ — 設計

- 日付: 2026-08-01
- 対象リポジトリ: `miyanism/ponshutagram-sake`（ローカル `C:\Users\user\claude.honten` の `railway-app/`）
- 上位原則の正: second-brain `wiki/システム/自動化の認証と失敗検知の設計原則.md`（[[自動化の認証と失敗検知の設計原則]]）
- 関連メモリ: [[instagram]]（IGコメント自動返信）/ [[reels-insights-weekly]]（Reels週次収集）

## 背景・課題（WHY）

Instagram API with Instagram Login（`graph.instagram.com`）の長期アクセストークンは**約60日で失効**する。
現状、これを共有する2つのRailway cronサービスは失効時に「クラッシュ→Railway通知」で人手の再発行を促すだけで、
**自動更新の仕組みが無い**。オーナーの手番を「切れそうなら手動再発行」というフォールバックのみに縮小し、
恒久対策（無人リフレッシュ）を honten 側に実装するのが本タスク。

現行の資産:
- 2つのcronサービス（同一Metaアプリ・同一トークン）
  - `ponshu-comment-reply`（service `0f618fa2-b79f-4e50-b47d-a89dcf683f21`・cron `*/15 * * * *`）
  - `reels-insights`（service `205a9ff7-c026-4c18-8abb-2c2952d1f0e8`・cron `0 0 * * 2`＝火曜9:00 JST）
  - 両者は project `pleasing-solace`（`beee1e4b…`）/ env production（`82a6332e…`）に属す。
  - 両者とも env の `IG_ACCESS_TOKEN` を読み、`IGAuthError`（code 190 等）で `sys.exit(1)`→Railway通知する堅牢化済み。
- `_deploy.py`（`_honten_junk/ig-comment-reply/`）が Railway GraphQL `variableUpsert`／`serviceCreate`／`volumeCreate` 等を実装済み。
  Railway APIは Cloudflareがdefault UAを error 1010 でブロックするため**ブラウザ風User-Agent必須**＋Bearer認証。

## 受け入れ条件（カード）

1. **無人で回る** — 人手のOAuthセッションや手動トークン貼付に依存せず自動更新される。
2. **失敗時に通知** — リフレッシュが失敗したら気づける（原則2・3）。
3. **両cronで資格情報を共有** — 更新後トークンを comment-reply / reels 両方が確実に受け取る。
4. **失効前にNotionタスクで表示**（2026-08-01 追加）— 自動リフレッシュが数週間こけて失効が近づいたら、失効前にオーナーのNotionタスクとして立ち、手動再発行の猶予を作る。

## 設計判断（確定）

- **共有方式 = 1本集約（Railway 環境スコープの共有変数）**。トークンの保管場所を1箇所にし、片方だけ古いトークンになるドリフトを原理的に消す。
  - フォールバック: 共有変数まわりがRailway APIで素直に動かなければ、**両サービスに個別 `variableUpsert`**（IDは既知）へ無改修で切替可。リフレッシュのロジックは同一、upsertが2回になるだけ。
- **リフレッシュ場所 = 専用cronを新設・週次**。単一責任で、失敗が独立して見える（原則2）。週次（7日間隔）なら連続8回失敗しても60日失効前に余裕。
- **成功時通知 = 毎回LINEに1行ハートビート**（期限＝YYYY-MM-DD／あとN日）。原則3（成功と無音を区別・余裕日数を数字で見せる）に忠実。

## アーキテクチャ

新規ファイル **`railway-app/ig_token_refresh.py`**（既存2 cronと同じ無依存・単一ファイル様式。標準ライブラリ `urllib` のみ）。
新規Railwayサービス **`ig-token-refresh`**:
- project `pleasing-solace` / env production / Root `railway-app` / Start `python ig_token_refresh.py`
- Cron **週次・毎週月曜 朝 JST**（例 `0 21 * * 0` UTC ＝ 月曜 06:00 JST。時刻は実装時に確定・変更容易）
- **ステートレス**（`/data`ボリューム不要。現行トークンをenvから読み、新トークンを共有変数へ書くだけ）

### 処理フロー（`main`）

```
[毎週月 朝JST] ig-token-refresh
  0. 必須env検証（無ければ FATAL・exit 1）
  1. 保存済み失効日 IG_TOKEN_EXPIRES_AT（共有変数・前回の成功が書いた値）を読み、現時点の残余裕を算出
  2. 現行 IG_ACCESS_TOKEN で GET graph.instagram.com/refresh_access_token
        ?grant_type=ig_refresh_token&access_token={current}
     （既存の _api_call リトライ＋_is_auth_error/IGAuthError パターンを流用）
  3a. 成功: 応答 { access_token, token_type, expires_in(秒) } を取得
      → Railway 共有変数 IG_ACCESS_TOKEN を variableUpsert で更新
           input:{ projectId, environmentId, name:"IG_ACCESS_TOKEN", value:new }  ← serviceId なし＝共有スコープ・応答 true を検証
      → 新しい失効日 IG_TOKEN_EXPIRES_AT（＝now+expires_in）も同様に variableUpsert
      → ✅ LINE 1行「IGトークン更新 期限=YYYY-MM-DD（あとN日）」（N = expires_in // 86400）
  3b. 失敗（IGAuthError / Railway API / ネットワーク）: ❌ LINE（保存済み失効日からの残余裕「残りN日・refresh失敗中」を明記）＋ 失効日は更新しない
  4. 失効前エスカレーション（3a/3bどちらでも実施）:
       最新の既知失効日から残余裕 < NOTION_TASK_DAYS なら Notionタスクを idempotent に作成/更新
       （既存の未完了マーカータスクがあれば重複作成しない）
  5. 3b だった場合は最後に sys.exit(1)（Railwayクラッシュメール）
```
※余裕日数の出どころ：正常運転（週次リフレッシュ成功）ではトークンが毎回約60日にリセットされるため残余裕は縮まらず、
Notionタスクもハートビートの⚠️も出ない。**自動リフレッシュが数週間連続で失敗した時だけ** `IG_TOKEN_EXPIRES_AT` が
更新されずカウントダウンし、失効前にNotionタスク／⚠️が発火する。

### 既存2 cron への影響

**コード無変更**。両者は今も env の `IG_ACCESS_TOKEN` を読むだけで、その値が「共有変数への参照」に変わるのみ。
既存の `IGAuthError`→クラッシュ通知は**原則2の外側の証人**として温存する（リフレッシュが静かに止まっても、最終的に失効すれば両消費者が絶叫して気づける）。

## 失敗検知（受け入れ条件2の設計・原則2/3への対応）

- **原則1（マシン認証）**: 人手OAuthセッション不在。IGトークンは自己リフレッシュ、Railway APIは長寿命 `RAILWAY_TOKEN`。
  オーナー手番は「完全失効時の手動再発行」フォールバックのみ。
- **原則2（処理の外側で検知）**:
  - 走って落ちた → LINE ＋ Railwayクラッシュメールの二重。
  - **そもそも走らなかった**（サービス削除/cron設定ミス）→ 外側の証人が2系統:
    (a) 週次✅ハートビートが**来なくなる**こと自体が合図、
    (b) いずれ本当に失効すれば消費側2 cronが `IGAuthError` でクラッシュ通知。
- **原則3（成功と無音を区別）**:
  - 成功時は必ず余裕日数を**数字付き**で送る。
  - 成功したのに `expires_in` が想定（約60日）より短ければ `⚠️` を付す（過去に静かに失敗していた兆候の早期警告）。閾値は実装時に確定（目安 < 50日）。

## 失効前のNotionタスク（受け入れ条件4・2026-08-01追加）

自動リフレッシュが数週間こけて残余裕が `NOTION_TASK_DAYS`（既定 14日）を割ったら、**失効前に**オーナーのNotionタスクを立てる。
これは原則2/3の外側エスカレーションを、LINEだけでなくオーナーの実タスク管理（Notion）に着地させ、
縮小後カードの「オーナー手番＝切れそうなら手動再発行」フォールバックの引き金になる。

- **発火条件**: 最新の既知失効日（`IG_TOKEN_EXPIRES_AT`）からの残余裕 < `NOTION_TASK_DAYS`。正常運転では発火しない（残余裕が常に約60日にリセットされるため）。
- **実装**: Railway の素Pythonから直Notion API（`POST https://api.notion.com/v1/pages`・`Notion-Version: 2022-06-28`）。MCPは使えないためLINE同様の直叩き。
- **対象DB**: 「マイタスク」DB（id `677624d3-89e0-41f5-ba01-a1aea07d3bd0`・プロパティ タスク名(title)/期限(date)/ステータス(status)/ソース）。
  - **実装時に最終確定する開放点**: GAS「今日のサマリー」朝メールが読むタスクDB（[[project-notion-daily-summary]] の `private` 系）と**同じDBにすれば、Notionと朝メールの両方に自動で載る**。この相乗りが可能かを実装時に確認し、可能なら朝メールが読むDBを優先。
- **タスク内容（例）**: タスク名「🔑 IGアクセストークンを手動再発行（自動リフレッシュ失敗中）」／期限＝失効日／ステータス To-do／本文に「reply_comments・reels の両cronが失効で止まる前に、Metaでトークン再発行→Railway共有変数を更新」。
- **冪等性**: 作成前に、安定マーカー（タスク名の固定接頭辞 or 専用プロパティ値）を持つ**未完了タスク**をNotion検索し、あれば新規作成せず既存を更新（期限を最新失効日に）。毎週の重複タスク量産を防ぐ。
- **失敗時**: Notion API 呼び出し自体が失敗しても、リフレッシュ本体の成否判定は変えない（Notionは付随エスカレーションのため、失敗はログ＋LINE補足に留める）。

## トークン失効の安全余裕

週次（7日間隔）× 約60日寿命 ＝ **連続8回失敗しても失効前**。
かつ refresh は旧トークンを即時無効化しないため、「新トークン取得済みだが `variableUpsert` に失敗」も翌週リトライで安全に回復
（データ喪失なし・ただし通知は鳴らす）。

## 必要な資格情報・環境変数（`ig-token-refresh` サービス）

| 変数 | 用途 | 備考 |
|---|---|---|
| `IG_ACCESS_TOKEN` | 現行トークン（共有変数への参照） | 更新対象そのもの |
| `RAILWAY_TOKEN` | variableUpsert 用の長寿命Railwayトークン | **新規シークレット・このサービスのenvのみ**（原則1のマシン認証） |
| `RAILWAY_PROJECT_ID` | 既定 `beee1e4b…`（`_deploy.py`既知値） | 省略時は既定を使用 |
| `RAILWAY_ENVIRONMENT_ID` | 既定 `82a6332e…`（同上） | 省略時は既定を使用 |
| `IG_TOKEN_EXPIRES_AT` | 最新の失効日時（成功時に自動upsert） | 共有変数。残余裕の算出元。初回は空でも可 |
| `LINE_CHANNEL_TOKEN` / `LINE_TO_USER_ID` | 成功/失敗のLINE通知 | 既存2 cronと同値を流用 |
| `NOTION_TOKEN` | 失効前タスク作成用のNotion Integrationトークン | **新規シークレット・このサービスのenvのみ** |
| `NOTION_TASKS_DB_ID` | タスク作成先DB（既定 `677624d3…`／朝メール相乗り時は差替え） | 実装時に最終確定 |
| `IG_GRAPH_BASE` | 省略可（既定 `https://graph.instagram.com`） | |
| `DRY_RUN` | "1" で variableUpsert・LINE・Notion をスキップ | テスト用 |
| `EXPIRY_WARN_DAYS` | 省略可。⚠️を出す残余裕の閾値（既定 50） | 原則3の早期警告 |
| `NOTION_TASK_DAYS` | 省略可。失効前タスクを立てる残余裕の閾値（既定 14） | 受け入れ条件4 |

## テスト戦略

- `DRY_RUN=1`: refresh呼び出しと期限計算までは実行し、`variableUpsert`・LINE送信・Notion作成をスキップして「何をするか」を出力。
  refresh は旧トークンを非破壊なのでローカル実行で安全。Notion冪等検索（読み取り）はドライランでも実行して発火条件を確認してよい。
- ローカル: 現行トークンを `.env.local` に置き `DRY_RUN=1` で refresh 応答と `expires_in`／算出N日を確認。
- Railway: 初回は手動1回実行で refresh→upsert（共有変数の実値変化）→✅LINE を実証。
  共有変数 API 経路が想定通り動くことをこの時点で検証し、ダメならフォールバックA（両サービス個別upsert）へ。

## 初期設定（一度きり・実装フェーズで実施）

1. Railway 環境スコープの共有変数 `IG_ACCESS_TOKEN` を現行トークン値で作成。
2. 既存2サービス（comment-reply / reels-insights）の `IG_ACCESS_TOKEN` を共有変数参照に張替え、両方の起動・トークン読取りを確認。
   （cronサービスは変数変更で即実行されず次回cron時刻に新値で起動＝副作用なし）
3. `ig-token-refresh` サービスを作成（Root `railway-app`／Start `python ig_token_refresh.py`／週次cron）、上表の env を投入。
4. 手動1回実行で refresh→upsert→✅LINE を実証し、両消費者が新トークンで正常稼働することを確認。

## スコープ外（YAGNI）

- 重複ヘルパー（`_load_dotenv`/`_api_call`/`_is_auth_error`/`IGAuthError`/LINE push）の共通モジュール化は**しない**。
  稼働中の2 cronを揺らさない・単一ファイルデプロイの簡潔さを維持するため、`ig_token_refresh.py` に必要分をコピーして自己完結させる（将来の抽出は任意）。
- `IG_USER_ID` や `LINE_*` など静的変数の共有変数化は本タスクでは行わない（自動更新が要るのは `IG_ACCESS_TOKEN` のみ）。
- トークンの `debug_token` による厳密な残寿命取得はしない（Instagram Login API では素直に取れず、`expires_in`＋週次前提で十分）。

## 成果物

- 新規: `railway-app/ig_token_refresh.py`（refresh＋共有変数upsert＋LINEハートビート＋失効前Notionタスク）
- 新規: Railwayサービス `ig-token-refresh`（＋共有変数 `IG_ACCESS_TOKEN`/`IG_TOKEN_EXPIRES_AT` 化・初期設定）
- 新規シークレット: `RAILWAY_TOKEN`・`NOTION_TOKEN`（このサービスのenvのみ）
- 既存 `reply_comments.py` / `reels_insights.py`: **無変更**
- メモリ更新: [[instagram]] 残タスク3（トークン失効）を「自動リフレッシュ実装済み」に、[[reels-insights-weekly]] の共倒れ注記も更新。
