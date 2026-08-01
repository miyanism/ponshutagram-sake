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
  NOTION_TASKS_DB_ID     … 省略可（既定「プライベートタスク」DB＝GAS今日のサマリーと同じ）
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
NOTION_DB = os.environ.get("NOTION_TASKS_DB_ID", "337f4cc11afd810983b9df91912d3507")
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


def _task_title(days):
    return f"{NOTION_TASK_MARKER}（自動リフレッシュ失敗中・残り{days}日）"


def notion_task_payload(db_id, expiry_date_iso, days):
    """POST /v1/pages 用。「プライベートタスク」DBスキーマに合わせる
    （タスク名=title / 期限=date / ステータス=select「未着手」/ 優先度=select「高」）。
    このDBはGAS「今日のサマリー」朝メールも読む（ステータス≠完了 かつ 期限≤今日+3日を表示）。"""
    return {
        "parent": {"database_id": db_id},
        "properties": {
            "タスク名": {"title": [{"text": {"content": _task_title(days)}}]},
            "期限": {"date": {"start": expiry_date_iso}},
            "ステータス": {"select": {"name": "未着手"}},
            "優先度": {"select": {"name": "高"}},
        },
    }


def notion_update_props(expiry_date_iso, days):
    """PATCH /v1/pages/{id} 用。既存タスクの期限とタイトルを最新化する。"""
    return {
        "期限": {"date": {"start": expiry_date_iso}},
        "タスク名": {"title": [{"text": {"content": _task_title(days)}}]},
    }


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
    if DRY_RUN:
        print("  [DRY] LINE通知スキップ: " + msg.replace("\n", " / "))
        return
    if not (LINE_TOKEN and LINE_TO):
        print("  [LINE未設定] " + msg.replace("\n", " / "))
        return
    body = json.dumps({"to": LINE_TO, "messages": [{"type": "text", "text": msg[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
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
        # ステータスは select 型（未着手/進行中/完了）。完了以外を「未完了」とみなす。
        st = (pg.get("properties", {}).get("ステータス", {}).get("select") or {})
        if (st.get("name") or "") != "完了":
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
        if DRY_RUN:
            print(f"  [DRY] refresh ok: 期限{new_expiry.astimezone(JST):%Y-%m-%d} "
                  f"あと{ndays}日 warn={warn}（upsert/LINEスキップ）")
            latest_expiry, latest_days = new_expiry, ndays
        else:
            railway_upsert_shared("IG_ACCESS_TOKEN", new_token)
            railway_upsert_shared("IG_TOKEN_EXPIRES_AT", new_expiry.isoformat())
            notify_line(heartbeat_message(new_expiry, ndays, warn))
            print(f"  [ok] token更新・期限{new_expiry.astimezone(JST):%Y-%m-%d}（あと{ndays}日）")
            latest_expiry, latest_days = new_expiry, ndays
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
