# IGアクセストークン恒久リフレッシュ 実装プラン

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** IGアクセストークン（約60日失効）を無人で自己リフレッシュし、更新後トークンを両cronで共有、失敗時はLINE＋クラッシュ＋失効前Notionタスクで気づけるようにする。

**Architecture:** 新規Railway cron `ig-token-refresh`（週次）が `graph.instagram.com/refresh_access_token` で新トークンを取得し、Railway GraphQL `variableUpsert` で**環境スコープの共有変数** `IG_ACCESS_TOKEN`/`IG_TOKEN_EXPIRES_AT` を1本更新する。既存2 cron（reply_comments.py / reels_insights.py）は無変更で、その共有変数を参照する。純ロジック（期限計算・判定・メッセージ・Notionペイロード）とI/O（refresh/Railway/LINE/Notion）を分離し、純ロジックを stdlib `unittest` でTDDする。

**Tech Stack:** Python 3.11・標準ライブラリのみ（`urllib`）。テストは stdlib `unittest`（pytch不要・追加依存なし）。デプロイは Railway GraphQL（`backboard.railway.com/graphql/v2`・ブラウザUA必須）。

設計spec: [`docs/superpowers/specs/2026-08-01-ig-token-refresh-design.md`](../specs/2026-08-01-ig-token-refresh-design.md)

---

## ファイル構成

| ファイル | 責務 |
|---|---|
| 新規 `railway-app/ig_token_refresh.py` | cron本体。設定・純ロジック・I/O・`main()` を1ファイルに（既存2 cronと同じ無依存・単一ファイル様式） |
| 新規 `railway-app/test_ig_token_refresh.py` | 純ロジックの `unittest`（期限計算・判定・メッセージ・Notionペイロード・is_auth_error） |
| 新規 `_honten_junk/ig-comment-reply/_deploy_refresh.py` | 新サービス作成＋共有変数化のデプロイ道具（ローカル一時利用・gitignore配下） |
| 既存 `railway-app/reply_comments.py` / `reels_insights.py` | **無変更** |

**純/ I/O 分離方針**：`main()` から呼ぶ副作用（refresh API・Railway upsert・LINE・Notion）は薄いラッパにし、判定と文字列組み立ては引数だけで完結する純関数にする。テストは純関数のみを対象にし、I/O は `DRY_RUN=1` の手動実行で検証する。

**既知の実値（`_deploy.py` より・プランにハードコード可）**
- Railway GraphQL: `https://backboard.railway.com/graphql/v2`
- UA: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36`
- PROJECT_ID `beee1e4b-55e4-4ed4-9938-26ebed659a64` / ENVIRONMENT_ID `82a6332e-beef-4fea-aff2-5e20d9623259`
- 既存サービス: comment-reply `0f618fa2-b79f-4e50-b47d-a89dcf683f21` / reels-insights `205a9ff7-c026-4c18-8abb-2c2952d1f0e8`
- Notion タスクDB（暫定）: `677624d3-89e0-41f5-ba01-a1aea07d3bd0`（「マイタスク」・**Task 6 でプロパティ名を要確認**）

すべての作業は既存ブランチ `feat/ig-token-refresh` 上で行う。作業ディレクトリは `C:\Users\user\claude.honten`。

---

## Task 1: モジュール雛形（設定・dotenv・IGAuthError・is_auth_error）

**Files:**
- Create: `railway-app/ig_token_refresh.py`
- Test: `railway-app/test_ig_token_refresh.py`

- [ ] **Step 1: 失敗するテストを書く**

`railway-app/test_ig_token_refresh.py`:

```python
# -*- coding: utf-8 -*-
import datetime
import unittest

import ig_token_refresh as m

UTC = datetime.timezone.utc


class AuthError(unittest.TestCase):
    def test_is_auth_error_true_code190(self):
        self.assertTrue(m.is_auth_error('{"error":{"code":190,"message":"expired"}}'))

    def test_is_auth_error_true_oauth(self):
        self.assertTrue(m.is_auth_error('OAuthException: session invalidated'))

    def test_is_auth_error_false(self):
        self.assertFalse(m.is_auth_error('{"error":{"code":500,"message":"boom"}}'))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'ig_token_refresh'`）

- [ ] **Step 3: 最小実装を書く**

`railway-app/ig_token_refresh.py`:

```python
# -*- coding: utf-8 -*-
"""ig_token_refresh.py — Railway cron（週次）: IGアクセストークンの自動リフレッシュ

Instagram API with Instagram Login の長期アクセストークン（約60日失効）を
graph.instagram.com/refresh_access_token で自己リフレッシュし、更新後トークンを
Railway の「環境スコープ共有変数」IG_ACCESS_TOKEN として variableUpsert で1本更新する。
既存2 cron（reply_comments.py / reels_insights.py）は無変更でこの共有変数を参照する。

失敗時: LINE通知 ＋ sys.exit(1)（Railwayクラッシュメール）の二重。
失効前: 残余裕が閾値を割ったらオーナーのNotionタスクを idempotent に立てる。

設計: docs/superpowers/specs/2026-08-01-ig-token-refresh-design.md

必要な環境変数:
  IG_ACCESS_TOKEN        … 現行トークン（共有変数への参照）。更新対象そのもの
  RAILWAY_TOKEN          … variableUpsert 用の長寿命Railwayトークン
  RAILWAY_PROJECT_ID     … 省略可（既定あり）
  RAILWAY_ENVIRONMENT_ID … 省略可（既定あり）
  IG_TOKEN_EXPIRES_AT    … 最新失効日時ISO（成功時に自動upsert・残余裕の算出元）
  LINE_CHANNEL_TOKEN / LINE_TO_USER_ID … 成功/失敗のLINE通知
  NOTION_TOKEN           … 失効前タスク作成用のNotion Integrationトークン
  NOTION_TASKS_DB_ID     … 省略可（既定 マイタスクDB）
  IG_GRAPH_BASE          … 省略可（既定 https://graph.instagram.com）
  DRY_RUN                … "1" で variableUpsert・LINE・Notion をスキップ
  EXPIRY_WARN_DAYS       … 省略可。⚠️を出す残余裕の閾値（既定 50）
  NOTION_TASK_DAYS       … 省略可。失効前タスクを立てる残余裕の閾値（既定 14）
"""
import os
import sys
import json
import datetime
import urllib.parse
import urllib.request
import urllib.error

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _load_dotenv(path):
    """依存なしの簡易 .env ローダ（ローカルテスト用。Railwayでは環境変数を使う）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(os.path.dirname(__file__), ".env.local"))

UTC = datetime.timezone.utc
JST = datetime.timezone(datetime.timedelta(hours=9))

GRAPH = os.environ.get("IG_GRAPH_BASE", "https://graph.instagram.com")
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")

RAILWAY_API = "https://backboard.railway.com/graphql/v2"
RAILWAY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
RAILWAY_TOKEN = os.environ.get("RAILWAY_TOKEN", "")
PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "beee1e4b-55e4-4ed4-9938-26ebed659a64")
ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "82a6332e-beef-4fea-aff2-5e20d9623259")

STORED_EXPIRES_AT = os.environ.get("IG_TOKEN_EXPIRES_AT", "")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
LINE_TO = os.environ.get("LINE_TO_USER_ID", "")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DB = os.environ.get("NOTION_TASKS_DB_ID", "677624d3-89e0-41f5-ba01-a1aea07d3bd0")
NOTION_TASK_MARKER = "🔑 IGアクセストークンを手動再発行"

DRY_RUN = os.environ.get("DRY_RUN") == "1"
EXPIRY_WARN_DAYS = int(os.environ.get("EXPIRY_WARN_DAYS", "50"))
NOTION_TASK_DAYS = int(os.environ.get("NOTION_TASK_DAYS", "14"))


class IGAuthError(Exception):
    """トークン期限切れ・権限喪失など、人手対応が必要な恒久エラー。"""


def is_auth_error(body):
    """Meta のエラー本文がトークン/認可系（code 190 等）かを判定。"""
    low = (body or "").lower()
    return ('"code": 190' in low or '"code":190' in low
            or "oauthexception" in low or "access token" in low
            or "session has been invalidated" in low or "expired" in low)
```

- [ ] **Step 4: テストを実行して成功を確認**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: PASS（3 tests）

- [ ] **Step 5: コミット**

```bash
git add railway-app/ig_token_refresh.py railway-app/test_ig_token_refresh.py
git commit -m "feat(ig-token-refresh): モジュール雛形と is_auth_error"
```

---

## Task 2: 期限・残余裕の純関数

**Files:**
- Modify: `railway-app/ig_token_refresh.py`（関数追加）
- Test: `railway-app/test_ig_token_refresh.py`（クラス追加）

- [ ] **Step 1: 失敗するテストを追加**

`test_ig_token_refresh.py` の `AuthError` クラスの前に追記:

```python
class ExpiryMath(unittest.TestCase):
    def test_expiry_from_now(self):
        now = datetime.datetime(2026, 8, 1, tzinfo=UTC)
        self.assertEqual(m.expiry_from_now(60 * 86400, now),
                         datetime.datetime(2026, 9, 30, tzinfo=UTC))

    def test_parse_expiry_roundtrip(self):
        dt = datetime.datetime(2026, 9, 30, 12, tzinfo=UTC)
        self.assertEqual(m.parse_expiry(dt.isoformat()), dt)

    def test_parse_expiry_empty_is_none(self):
        self.assertIsNone(m.parse_expiry(""))

    def test_parse_expiry_naive_becomes_utc(self):
        self.assertEqual(m.parse_expiry("2026-09-30T00:00:00").tzinfo, UTC)

    def test_days_remaining_positive(self):
        now = datetime.datetime(2026, 8, 1, tzinfo=UTC)
        exp = datetime.datetime(2026, 8, 15, tzinfo=UTC)
        self.assertEqual(m.days_remaining(exp, now), 14)

    def test_days_remaining_none(self):
        self.assertIsNone(m.days_remaining(None, datetime.datetime(2026, 8, 1, tzinfo=UTC)))

    def test_days_remaining_expired_is_negative(self):
        now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
        exp = datetime.datetime(2026, 8, 1, tzinfo=UTC)
        self.assertEqual(m.days_remaining(exp, now), -9)
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: FAIL（`AttributeError: module 'ig_token_refresh' has no attribute 'expiry_from_now'`）

- [ ] **Step 3: 実装を追加**

`ig_token_refresh.py` の `is_auth_error` の下に追記:

```python
def expiry_from_now(expires_in_sec, now):
    """now(aware) + expires_in秒 → aware datetime(UTC)。"""
    return now.astimezone(UTC) + datetime.timedelta(seconds=int(expires_in_sec))


def parse_expiry(s):
    """ISO文字列 → aware datetime(UTC)。空/不正なら None。"""
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def days_remaining(expires_at, now):
    """失効まで何日か（floor）。expires_at が None なら None。負値は失効後。"""
    if expires_at is None:
        return None
    delta = expires_at.astimezone(UTC) - now.astimezone(UTC)
    return int(delta.total_seconds() // 86400)
```

- [ ] **Step 4: テストを実行して成功を確認**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: PASS（10 tests）

- [ ] **Step 5: コミット**

```bash
git add railway-app/ig_token_refresh.py railway-app/test_ig_token_refresh.py
git commit -m "feat(ig-token-refresh): 期限・残余裕の純関数"
```

---

## Task 3: 判定とメッセージ組み立ての純関数

**Files:**
- Modify: `railway-app/ig_token_refresh.py`
- Test: `railway-app/test_ig_token_refresh.py`

- [ ] **Step 1: 失敗するテストを追加**

`test_ig_token_refresh.py` に追記:

```python
class Decisions(unittest.TestCase):
    def test_should_warn_true_when_short(self):
        self.assertTrue(m.should_warn(40 * 86400, 50))

    def test_should_warn_false_when_full(self):
        self.assertFalse(m.should_warn(60 * 86400, 50))

    def test_should_create_task(self):
        self.assertTrue(m.should_create_task(10, 14))
        self.assertFalse(m.should_create_task(20, 14))
        self.assertFalse(m.should_create_task(None, 14))


class Messages(unittest.TestCase):
    def test_heartbeat_normal(self):
        exp = datetime.datetime(2026, 9, 30, tzinfo=UTC)
        msg = m.heartbeat_message(exp, 59, False)
        self.assertIn("✅", msg)
        self.assertIn("あと59日", msg)
        self.assertIn("2026-09-30", msg)
        self.assertNotIn("⚠️", msg)

    def test_heartbeat_warn(self):
        exp = datetime.datetime(2026, 8, 20, tzinfo=UTC)
        msg = m.heartbeat_message(exp, 19, True)
        self.assertIn("⚠️", msg)

    def test_failure_message_known_days(self):
        msg = m.failure_message(5, "boom")
        self.assertIn("残り5日", msg)
        self.assertIn("boom", msg)

    def test_failure_message_unknown_days(self):
        self.assertIn("不明", m.failure_message(None, "boom"))
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: FAIL（`has no attribute 'should_warn'`）

- [ ] **Step 3: 実装を追加**

`ig_token_refresh.py` の `days_remaining` の下に追記:

```python
def should_warn(expires_in_sec, warn_days):
    """成功リフレッシュなのに期限が想定より短い（＝過去の静かな失敗の兆候）か。"""
    return int(expires_in_sec) < warn_days * 86400


def should_create_task(days, task_days):
    """残余裕が閾値未満なら失効前Notionタスクを立てる。days が None なら立てない。"""
    return days is not None and days < task_days


def heartbeat_message(expires_at, days, warn):
    """成功時のLINEハートビート。warn の時だけ ⚠️ と注記を付す。"""
    mark = "⚠️" if warn else "✅"
    tail = "（想定より短い期限。過去のリフレッシュ失敗を確認）" if warn else ""
    return (f"{mark} IGトークン更新 期限={expires_at.astimezone(JST):%Y-%m-%d}"
            f"（あと{days}日）{tail}")


def failure_message(days, err):
    """失敗時のLINE。残余裕が分かれば数字を明記（原則3）。"""
    if days is None:
        return f"❌ IGトークン更新に失敗（残余裕: 不明・refresh失敗中）\n{err}"
    return f"❌ IGトークン更新に失敗（残り{days}日・refresh失敗中）\n{err}"
```

- [ ] **Step 4: テストを実行して成功を確認**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: PASS（17 tests）

- [ ] **Step 5: コミット**

```bash
git add railway-app/ig_token_refresh.py railway-app/test_ig_token_refresh.py
git commit -m "feat(ig-token-refresh): 判定とメッセージの純関数"
```

---

## Task 4: Notionタスクのペイロード純関数

**Files:**
- Modify: `railway-app/ig_token_refresh.py`
- Test: `railway-app/test_ig_token_refresh.py`

> 注: プロパティ名（`タスク名`/`期限`/`ステータス`）とステータス option 名（`To-do`）は暫定。Task 6 の実DB確認で差異があれば `notion_task_payload` を修正する。

- [ ] **Step 1: 失敗するテストを追加**

`test_ig_token_refresh.py` に追記:

```python
class NotionPayload(unittest.TestCase):
    def test_payload_shape(self):
        p = m.notion_task_payload("db123", "2026-08-15", 10)
        self.assertEqual(p["parent"]["database_id"], "db123")
        self.assertEqual(p["properties"]["期限"]["date"]["start"], "2026-08-15")
        title = p["properties"]["タスク名"]["title"][0]["text"]["content"]
        self.assertIn(m.NOTION_TASK_MARKER, title)
        self.assertIn("残り10日", title)

    def test_update_props_shape(self):
        up = m.notion_update_props("2026-08-15", 3)
        self.assertEqual(up["期限"]["date"]["start"], "2026-08-15")
        self.assertIn("残り3日", up["タスク名"]["title"][0]["text"]["content"])
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: FAIL（`has no attribute 'notion_task_payload'`）

- [ ] **Step 3: 実装を追加**

`ig_token_refresh.py` の `failure_message` の下に追記:

```python
def _task_title(days):
    return f"{NOTION_TASK_MARKER}（自動リフレッシュ失敗中・残り{days}日）"


def notion_task_payload(db_id, expiry_date_iso, days):
    """POST /v1/pages 用。タスク名(title)/期限(date)/ステータス(status)。"""
    return {
        "parent": {"database_id": db_id},
        "properties": {
            "タスク名": {"title": [{"text": {"content": _task_title(days)}}]},
            "期限": {"date": {"start": expiry_date_iso}},
            "ステータス": {"status": {"name": "To-do"}},
        },
    }


def notion_update_props(expiry_date_iso, days):
    """PATCH /v1/pages/{id} 用。既存タスクの期限とタイトルを最新化する。"""
    return {
        "期限": {"date": {"start": expiry_date_iso}},
        "タスク名": {"title": [{"text": {"content": _task_title(days)}}]},
    }
```

- [ ] **Step 4: テストを実行して成功を確認**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: PASS（19 tests）

- [ ] **Step 5: コミット**

```bash
git add railway-app/ig_token_refresh.py railway-app/test_ig_token_refresh.py
git commit -m "feat(ig-token-refresh): Notionタスクのペイロード純関数"
```

---

## Task 5: I/O ラッパ（refresh / Railway / LINE / Notion）

**Files:**
- Modify: `railway-app/ig_token_refresh.py`

> I/O は unit test の対象外（Task 6 の `DRY_RUN=1` 手動実行で検証）。本タスクは import が壊れないことだけ確認する。

- [ ] **Step 1: 実装を追加**

`ig_token_refresh.py` の `notion_update_props` の下に追記:

```python
def _http_json(req, timeout=30):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def refresh_token(token):
    """graph.instagram.com/refresh_access_token → {access_token, token_type, expires_in}。
    認可系エラーは IGAuthError で送出（要手動再発行）。"""
    params = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": token})
    url = f"{GRAPH}/refresh_access_token?{params}"
    try:
        return _http_json(urllib.request.Request(url))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        if is_auth_error(body):
            raise IGAuthError(f"refresh HTTP {e.code}（要トークン再発行） {body}") from e
        raise


def railway_gql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        RAILWAY_API, data=body, method="POST",
        headers={"Authorization": f"Bearer {RAILWAY_TOKEN}",
                 "Content-Type": "application/json", "User-Agent": RAILWAY_UA})
    r = _http_json(req, timeout=60)
    if r.get("errors"):
        raise RuntimeError(f"Railway GraphQL errors: {json.dumps(r['errors'], ensure_ascii=False)[:300]}")
    return r


def railway_upsert_shared(name, value):
    """環境スコープの共有変数を作成/更新（serviceId を渡さない＝共有スコープ）。"""
    q = "mutation($in:VariableUpsertInput!){ variableUpsert(input:$in) }"
    r = railway_gql(q, {"in": {"projectId": PROJECT_ID, "environmentId": ENVIRONMENT_ID,
                               "name": name, "value": value}})
    if r.get("data", {}).get("variableUpsert") is not True:
        raise RuntimeError(f"variableUpsert({name}) 応答が真でない: "
                           f"{json.dumps(r, ensure_ascii=False)[:200]}")


def notify_line(msg):
    """LINE Messaging API で本人へpush。未設定ならコンソール出力にフォールバック。"""
    if not (LINE_TOKEN and LINE_TO):
        print("  [LINE未設定] " + msg.replace("\n", " / "))
        return
    body = json.dumps({"to": LINE_TO, "messages": [{"type": "text", "text": msg[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        print(f"  [LINE送信失敗] {e}")


def _notion_headers():
    return {"Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"}


def notion_find_open_task():
    """マーカーを持つ未完了タスクの page id を返す（無ければ None）。"""
    url = f"https://api.notion.com/v1/databases/{NOTION_DB}/query"
    payload = {"filter": {"property": "タスク名", "title": {"contains": NOTION_TASK_MARKER}},
               "page_size": 5}
    r = _http_json(urllib.request.Request(url, data=json.dumps(payload).encode(),
                                          method="POST", headers=_notion_headers()))
    for pg in r.get("results", []):
        st = (pg.get("properties", {}).get("ステータス", {}).get("status") or {})
        if (st.get("name") or "") not in ("Done", "完了", "完了済み"):
            return pg["id"]
    return None


def notion_create_task(payload):
    _http_json(urllib.request.Request(
        "https://api.notion.com/v1/pages", data=json.dumps(payload).encode(),
        method="POST", headers=_notion_headers()))


def notion_update_task(page_id, expiry_date_iso, days):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": notion_update_props(expiry_date_iso, days)}
    _http_json(urllib.request.Request(url, data=json.dumps(payload).encode(),
                                      method="PATCH", headers=_notion_headers()))
```

- [ ] **Step 2: import が壊れていないことを確認**

Run: `cd railway-app && python -c "import ig_token_refresh; print('import OK')"`
Expected: `import OK`

- [ ] **Step 3: 既存テストが依然パスすることを確認（回帰なし）**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: PASS（19 tests）

- [ ] **Step 4: コミット**

```bash
git add railway-app/ig_token_refresh.py
git commit -m "feat(ig-token-refresh): I/Oラッパ（refresh/Railway/LINE/Notion）"
```

---

## Task 6: main() オーケストレーションと DRY_RUN 手動検証

**Files:**
- Modify: `railway-app/ig_token_refresh.py`

- [ ] **Step 1: 実装を追加**

`ig_token_refresh.py` の末尾（`notion_update_task` の下）に追記:

```python
def now_utc():
    return datetime.datetime.now(UTC)


def escalate_notion(expires_at, days):
    """残余裕 < NOTION_TASK_DAYS のとき失効前タスクを冪等に作成/更新（付随処理）。"""
    if not should_create_task(days, NOTION_TASK_DAYS):
        return
    expiry_iso = (expires_at.astimezone(JST).date().isoformat()
                  if expires_at else now_utc().date().isoformat())
    if not NOTION_TOKEN:
        print(f"  [Notion未設定] 失効前タスク（残り{days}日）をスキップ")
        return
    if DRY_RUN:
        print(f"  [DRY] Notion失効前タスク: 残り{days}日 期限{expiry_iso}")
        return
    try:
        pid = notion_find_open_task()
        if pid:
            notion_update_task(pid, expiry_iso, days)
            print(f"  [Notion] 既存タスク更新 残り{days}日")
        else:
            notion_create_task(notion_task_payload(NOTION_DB, expiry_iso, days))
            print(f"  [Notion] 失効前タスク作成 残り{days}日")
    except Exception as e:
        # Notionは付随エスカレーション。失敗しても本体の成否判定は変えない。
        print(f"  [Notion失敗] {e}")


def main():
    if not TOKEN:
        print("[FATAL] IG_ACCESS_TOKEN 未設定")
        sys.exit(1)
    if not (RAILWAY_TOKEN or DRY_RUN):
        print("[FATAL] RAILWAY_TOKEN 未設定（DRY_RUN=1 なら省略可）")
        sys.exit(1)

    now = now_utc()
    stored = parse_expiry(STORED_EXPIRES_AT)
    known_days = days_remaining(stored, now)
    print(f"[start] dry_run={DRY_RUN}, stored_expiry={STORED_EXPIRES_AT or '(なし)'}, "
          f"known_days={known_days}, "
          f"LINE={'on' if (LINE_TOKEN and LINE_TO) else 'off'}, "
          f"NOTION={'on' if NOTION_TOKEN else 'off'}")

    failed = None
    latest_expiry, latest_days = stored, known_days
    try:
        res = refresh_token(TOKEN)
        new_token = res["access_token"]
        expires_in = int(res["expires_in"])
        new_expiry = expiry_from_now(expires_in, now)
        ndays = days_remaining(new_expiry, now)
        warn = should_warn(expires_in, EXPIRY_WARN_DAYS)
        latest_expiry, latest_days = new_expiry, ndays
        if DRY_RUN:
            print(f"  [DRY] refresh ok: 期限{new_expiry.astimezone(JST):%Y-%m-%d} "
                  f"あと{ndays}日 warn={warn}（upsert/LINEスキップ）")
        else:
            railway_upsert_shared("IG_ACCESS_TOKEN", new_token)
            railway_upsert_shared("IG_TOKEN_EXPIRES_AT", new_expiry.isoformat())
            notify_line(heartbeat_message(new_expiry, ndays, warn))
            print(f"  [ok] token更新・期限{new_expiry.astimezone(JST):%Y-%m-%d}（あと{ndays}日）")
    except IGAuthError as e:
        failed = f"認可エラー（要手動再発行）: {e}"
        print(f"[FATAL] {failed}")
        notify_line(failure_message(known_days, str(e)[:300]))
    except Exception as e:
        failed = f"{type(e).__name__}: {e}"
        print(f"[FATAL] refresh/upsert失敗: {failed}")
        notify_line(failure_message(known_days, str(e)[:300]))

    # 失効前エスカレーション（成功/失敗どちらでも最新の既知失効日で判定）
    escalate_notion(latest_expiry, latest_days)

    if failed:
        sys.exit(1)
    print("[done] リフレッシュ完了")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 全テストが依然パスすることを確認**

Run: `cd railway-app && python -m unittest test_ig_token_refresh -v`
Expected: PASS（19 tests）

- [ ] **Step 3: Notion 実DBのプロパティ名を確認（重要・開放点の解消）**

Notion で「マイタスク」DB（`677624d3-89e0-41f5-ba01-a1aea07d3bd0`）のスキーマを確認する。
- 確認項目: タイトルプロパティ名（`タスク名` か？）／日付プロパティ名（`期限` か？）／ステータスのプロパティ名と To-do 相当の option 名（`To-do` / `未着手` / `Not started` のどれか）。
- あわせて **GAS「今日のサマリー」朝メールが読むタスクDB**（[[project-notion-daily-summary]]）と同一にできるか検討し、可能なら `NOTION_TASKS_DB_ID` をそのDBに差し替える（Notionと朝メール両方に載るため）。
- 差異があれば `notion_task_payload` / `notion_update_props` / `notion_find_open_task` のプロパティ名・ステータス option 名を実DBに合わせて修正し、Task 4 のテストも文字列に依存しない範囲で維持する。

修正した場合: `git add -A && git commit -m "fix(ig-token-refresh): Notionプロパティ名を実DBに整合"`

- [ ] **Step 4: ローカル DRY_RUN 手動検証**

`railway-app/.env.local`（gitignore）に現行トークン等を置く:

```
IG_ACCESS_TOKEN=<現行の長期トークン>
DRY_RUN=1
```

Run: `cd railway-app && python ig_token_refresh.py`
Expected（例）:
```
[start] dry_run=True, stored_expiry=(なし), known_days=None, LINE=off, NOTION=off
  [DRY] refresh ok: 期限2026-09-30 あと59日 warn=False（upsert/LINEスキップ）
[done] リフレッシュ完了
```
確認: refresh 応答が取れ、`expires_in` から期限・残日数が算出され、副作用（upsert/LINE/Notion）がスキップされていること。
（refresh は旧トークンを即時無効化しないため、この DRY 実行は安全。）

- [ ] **Step 5: `.env.local` を確実に無視（漏洩防止）**

Run: `cd railway-app && git status --porcelain .env.local`
Expected: 出力なし（＝`.gitignore` により追跡外）。もし出るなら `railway-app/.gitignore` に `.env.local` を追加してコミット。

- [ ] **Step 6: コミット**

```bash
git add railway-app/ig_token_refresh.py
git commit -m "feat(ig-token-refresh): main()オーケストレーションとDRY_RUN検証"
```

---

## Task 7: デプロイと初期設定（オーナー関与・非TDD／runbook）

新サービス作成・共有変数化・シークレット投入は本番副作用のため、**オーナーの手番**（トークン発行）を伴う。ここは手順書として実行し、各ステップの結果を確認しながら進める。

**オーナーに用意してもらうもの（要確認）:**
- `RAILWAY_TOKEN`（Railwayアカウント/チームトークン。variableUpsert 権限。長寿命）
- `NOTION_TOKEN`（Notion Integrationトークン。対象タスクDBに接続済みであること）

**デプロイ道具:** `_honten_junk/ig-comment-reply/`（`.deploy.env` に `RAILWAY_TOKEN`、`.env.local` に IG/LINE 等が既にある）。

- [ ] **Step 1: 共有変数を作成（環境スコープ）**

`_honten_junk/ig-comment-reply/_deploy_refresh.py` を作成（`_deploy.py` の作法を踏襲）:

```python
# -*- coding: utf-8 -*-
"""ig-token-refresh サービスの作成＋共有変数化（ローカル一時利用）。
使い方: python _deploy_refresh.py <shared|create|vars|trigger>
"""
import io, sys, json, urllib.request, urllib.error
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

env = dict(l.strip().split("=", 1) for l in open(".deploy.env") if "=" in l and not l.startswith("#"))
le = dict(l.strip().split("=", 1) for l in open(".env.local", encoding="utf-8")
          if "=" in l and not l.strip().startswith("#"))
RT = env["RAILWAY_TOKEN"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PROJECT = "beee1e4b-55e4-4ed4-9938-26ebed659a64"
ENVIRON = "82a6332e-beef-4fea-aff2-5e20d9623259"
REPO = "miyanism/ponshutagram-sake"
SVC_NAME = "ig-token-refresh"
# 既存サービス（共有変数への張替え対象）
SVC_COMMENT = "0f618fa2-b79f-4e50-b47d-a89dcf683f21"
SVC_REELS = "205a9ff7-c026-4c18-8abb-2c2952d1f0e8"


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request("https://backboard.railway.com/graphql/v2", data=body, method="POST",
        headers={"Authorization": f"Bearer {RT}", "Content-Type": "application/json", "User-Agent": UA})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:400]}
    if r.get("errors"):
        print("GraphQL errors:", json.dumps(r["errors"], ensure_ascii=False)[:400])
    return r


def upsert(name, value, service_id=None):
    q = "mutation($in:VariableUpsertInput!){ variableUpsert(input:$in) }"
    v = {"projectId": PROJECT, "environmentId": ENVIRON, "name": name, "value": value}
    if service_id:
        v["serviceId"] = service_id
    r = gql(q, {"in": v})
    ok = r.get("data", {}).get("variableUpsert") is True
    print(f"  upsert {name}{' @'+service_id[:8] if service_id else ' (shared)'}: "
          f"{'OK' if ok else json.dumps(r, ensure_ascii=False)[:200]}")
    return ok


def cmd_shared():
    # ① 共有変数を現行トークンで作成 ② 両既存サービスを参照に張替え
    upsert("IG_ACCESS_TOKEN", le["IG_ACCESS_TOKEN"])                 # 環境スコープ共有
    upsert("IG_TOKEN_EXPIRES_AT", "")                               # 空で初期化（成功時に更新される）
    upsert("IG_ACCESS_TOKEN", "${{shared.IG_ACCESS_TOKEN}}", SVC_COMMENT)
    upsert("IG_ACCESS_TOKEN", "${{shared.IG_ACCESS_TOKEN}}", SVC_REELS)


def find_service():
    q = 'query($id:String!){ project(id:$id){ services{edges{node{id name}}} } }'
    r = gql(q, {"id": PROJECT})
    for ed in r["data"]["project"]["services"]["edges"]:
        if ed["node"]["name"] == SVC_NAME:
            return ed["node"]["id"]
    return None


def cmd_create():
    sid = find_service()
    if not sid:
        q = "mutation($in:ServiceCreateInput!){ serviceCreate(input:$in){ id } }"
        r = gql(q, {"in": {"projectId": PROJECT, "name": SVC_NAME,
                           "source": {"repo": REPO}, "branch": "main"}})
        sid = r.get("data", {}).get("serviceCreate", {}).get("id")
        print("serviceCreate:", sid)
    else:
        print("既存再利用:", sid)
    open("_refresh_service_id.txt", "w").write(sid or "")
    q = ("mutation($e:String!,$s:String!,$in:ServiceInstanceUpdateInput!){"
         " serviceInstanceUpdate(environmentId:$e, serviceId:$s, input:$in) }")
    r = gql(q, {"e": ENVIRON, "s": sid, "in": {
        "rootDirectory": "railway-app",
        "startCommand": "python ig_token_refresh.py",
        "cronSchedule": "0 21 * * 0"}})   # 日曜21:00 UTC = 月曜06:00 JST
    print("instanceUpdate:", json.dumps(r, ensure_ascii=False)[:200])


def cmd_vars():
    sid = open("_refresh_service_id.txt").read().strip()
    upsert("IG_ACCESS_TOKEN", "${{shared.IG_ACCESS_TOKEN}}", sid)
    upsert("IG_TOKEN_EXPIRES_AT", "${{shared.IG_TOKEN_EXPIRES_AT}}", sid)
    upsert("RAILWAY_TOKEN", RT, sid)
    upsert("LINE_CHANNEL_TOKEN", le["LINE_CHANNEL_TOKEN"], sid)
    upsert("LINE_TO_USER_ID", le["LINE_TO_USER_ID"], sid)
    upsert("NOTION_TOKEN", le.get("NOTION_TOKEN", ""), sid)


def cmd_trigger():
    sid = open("_refresh_service_id.txt").read().strip()
    q = "mutation($s:String!,$e:String!){ serviceInstanceDeployV2(serviceId:$s, environmentId:$e) }"
    print(gql(q, {"s": sid, "e": ENVIRON}))


if __name__ == "__main__":
    {"shared": cmd_shared, "create": cmd_create, "vars": cmd_vars,
     "trigger": cmd_trigger}[sys.argv[1]]()
```

`_honten_junk/ig-comment-reply/.env.local` に `NOTION_TOKEN=...` を追記しておく。

- [ ] **Step 2: 共有変数化 → 既存2 cronの健全性を確認**

Run: `cd _honten_junk/ig-comment-reply && python _deploy_refresh.py shared`
Expected: `upsert IG_ACCESS_TOKEN (shared): OK` と両サービス参照張替えの OK。

**検証（重要）**: 既存2サービスが共有変数参照でトークンを読めているかを、次回cron実行のログか手動triggerで確認する。
- comment-reply か reels のログに `[start]` が出てトークン起因のエラーが無いこと。
- **もし `${{shared.…}}` が解決されず両サービスがトークンを読めない場合はフォールバックA**へ:
  共有変数化を取り消し（両サービスの `IG_ACCESS_TOKEN` を実トークン値へ戻す `upsert("IG_ACCESS_TOKEN", le["IG_ACCESS_TOKEN"], SVC_COMMENT)` 等）、
  `ig_token_refresh.py` の `railway_upsert_shared` を「両サービスに serviceId 付きで2回 upsert」する版に差し替える（spec のフォールバックA）。

- [ ] **Step 3: 新サービス作成 → 変数投入**

Run:
```bash
cd _honten_junk/ig-comment-reply
python _deploy_refresh.py create
python _deploy_refresh.py vars
```
Expected: `serviceCreate: <id>`、`instanceUpdate`、各 `upsert … OK`。

- [ ] **Step 4: 本番相当の1回実行で検証（＝初回シード）**

> 注意: Railway の **cron** サービスは redeploy では起動コマンドを実行せず次回cron時刻まで待つ（[[instagram]] メモリの実地確認）。
> したがって `trigger`（redeploy）では ig_token_refresh.py は走らない。初回の本番実行＝シードは **ローカルから本番相当envで1回走らせる**のが確実。

`railway-app/.env.local` に本番相当の値を置く（`DRY_RUN` は付けない）:
```
IG_ACCESS_TOKEN=<現行の長期トークン>
RAILWAY_TOKEN=<Railwayトークン>
NOTION_TOKEN=<Notion Integrationトークン>
LINE_CHANNEL_TOKEN=<...>
LINE_TO_USER_ID=<...>
```
Run: `cd railway-app && python ig_token_refresh.py`
これは実際に refresh → 共有変数 `IG_ACCESS_TOKEN`/`IG_TOKEN_EXPIRES_AT` を upsert → ✅LINE を行う（＝初回シード）。
確認:
- コンソールに `[ok] token更新・期限YYYY-MM-DD（あとN日）`。
- Railway ダッシュボードで共有変数 `IG_ACCESS_TOKEN` が新トークンに、`IG_TOKEN_EXPIRES_AT` に失効日が入っている。
- LINE に `✅ IGトークン更新 期限=…（あとN日）` が届く。
- 既存2 cron が次回実行で新トークンを使い正常稼働（`IGAuthError` が出ない）。

実行後、`railway-app/.env.local` から `RAILWAY_TOKEN`/`NOTION_TOKEN` 等の秘匿値を消す（ローカルに残さない）。
以降の定期実行は Railway の週次cronが担う（サービスenvに同じ変数が入っているため）。

- [ ] **Step 5: 使用したトークンを後始末**

`_deploy_refresh.py` 実行後、`RAILWAY_TOKEN` は Railway サービス env にのみ残す。ローカルの `.deploy.env`・一時IDファイル（`_refresh_service_id.txt`）は既存運用どおり保全/削除。GitHub PATを使った場合は失効を確認。

---

## Task 8: メモリとカードの更新

**Files:**
- Modify: `C:\Users\user\.claude\projects\c--Users-user-claude-honten\memory\project_instagram_comment_reply.md`
- Modify: `C:\Users\user\.claude\projects\c--Users-user-claude-honten\memory\project_reels_insights_weekly.md`

- [ ] **Step 1: [[instagram]] メモリの残タスク3を更新**

「残タスク3（IGトークン約60日失効）」を「**自動リフレッシュ実装済み**（`ig_token_refresh.py`・週次cron `ig-token-refresh`・共有変数 `IG_ACCESS_TOKEN`/`IG_TOKEN_EXPIRES_AT` に1本集約・失敗時LINE＋クラッシュ＋失効前Notionタスク）」に書き換える。デプロイ先サービスID・共有変数化の事実も追記。

- [ ] **Step 2: [[reels-insights-weekly]] メモリの共倒れ注記を更新**

「IGトークン約60日失効はコメント返信botと共倒れ」の箇所に「→ `ig-token-refresh` cron が共有変数を週次リフレッシュするため、通常運転では両者とも失効しない。完全失効時のみ手動再発行フォールバック」を追記。

- [ ] **Step 3: 状態ボードのカードを完了へ**

（オーナー運用）状態ボードの 🤝 claude.honten 待ち の 🔴「IGアクセストークンの恒久リフレッシュを実装」カードを、実装＋デプロイ完了として更新する旨をオーナーに報告。要確認2つ（共有方式＝1本集約／リフレッシュ場所＝専用cron週次）と受け入れ条件4つ（無人・失敗通知・両cron共有・失効前Notion）の充足を明記。

- [ ] **Step 4: コミット（コード側リポジトリに変更があれば）**

メモリはgit外のため、コード側で未コミットがなければ本タスクにコミットは不要。ブランチ `feat/ig-token-refresh` の最終状態を確認:
Run: `cd C:/Users/user/claude.honten && git log --oneline feat/ig-token-refresh -8`

---

## 完了の定義

- `python -m unittest test_ig_token_refresh -v` が全緑（19 tests）。
- `ig_token_refresh.py` を `DRY_RUN=1` でローカル実行し refresh→期限算出まで通る。
- Railway サービス `ig-token-refresh` が週次で稼働し、手動1回実行で共有変数更新＋✅LINEを実証。
- 既存2 cron が共有変数参照で正常稼働（無変更）。
- 失効前（残余裕 < 14日）に Notion タスクが立つ経路を実装・確認。
- 受け入れ条件4つ（無人／失敗通知／両cron共有／失効前Notion）を充足。

## マージ

全タスク完了後、`feat/ig-token-refresh` を main へ。cron本体は既存運用どおり origin/main の `railway-app/` に載る必要があるため、[[instagram]] メモリのデプロイ注記（worktree隔離→該当ファイルのみcommit→`git push origin HEAD:main`）に従う。superpowers:finishing-a-development-branch で締める。
