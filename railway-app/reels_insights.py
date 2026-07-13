# -*- coding: utf-8 -*-
"""
reels_insights.py  —  Railway cron 実行（週1・火曜 0 0 * * 2 = JST火曜9:00）

Reels単位の反響（再生・リーチ・保存・シェア・平均視聴時間）を週1で自動収集し、
second-brain Vault のレポートページ（wiki/シナリオ実績/Reels週次インサイト.md）を
GitHub Contents API で上書きする（週次Lintレポート方式）。

設計書の正: second-brain `wiki/システム/Reelsインサイト週次収集（IG bot拡張）.md`
指標の読み方・A/B/Cスコア基準の正: second-brain `wiki/シナリオ実績/反響の測り方.md`

reply_comments.py（IGコメント自動返信）と同じMetaアプリ・アクセストークンを使う。
トークンが失効するとこのcronも一緒に落ちる（IGAuthErrorでクラッシュ→Railway通知）。

API実査の結果（2026-07-13・実装時に確認済み）:
  - 取得可: views / reach / likes / comments / saved / shares / total_interactions /
            ig_reels_avg_watch_time(ms) / ig_reels_video_view_total_time(ms)
  - 取得不可: スキップ率・フォロワー外%（follow_type内訳）・動画尺（duration）
    → 維持率の代理は「平均再生時間（秒）」をそのまま使う（反響の測り方 2026-07-07 の採用どおり）

必要な環境変数:
  IG_USER_ID          … Instagram アカウントの user id（reply_comments と同じ）
  IG_ACCESS_TOKEN     … 長期アクセストークン（reply_comments と同じ）
  VAULT_GITHUB_TOKEN  … second-brain リポジトリに Contents:write できる fine-grained PAT
  VAULT_REPO          … 省略可（既定 miyanism/second-brain）
  VAULT_REPORT_PATH   … 省略可（既定 wiki/シナリオ実績/Reels週次インサイト.md）
  STATE_DIR           … 省略可。前週スナップショットの保存先（Railwayは永続ボリューム /data）
  REELS_COUNT         … 省略可。レポートに載せる直近Reels本数（既定 20）
  LINE_CHANNEL_TOKEN / LINE_TO_USER_ID … 省略可。設定時はレポート更新をLINEに1通通知
  DRY_RUN             … "1" なら収集とレポート生成だけ行い、Vault push・state保存・LINE通知をしない
"""
import os
import re
import sys
import json
import time
import base64
import datetime
import statistics
import urllib.parse
import urllib.request
import urllib.error

# Windowsローカルテスト時のcp932コンソールでも落ちないようにする（Railwayは元からUTF-8）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _load_dotenv(path: str):
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

GRAPH = os.environ.get("IG_GRAPH_BASE", "https://graph.instagram.com")
IG_USER_ID = os.environ.get("IG_USER_ID", "")
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
REELS_COUNT = int(os.environ.get("REELS_COUNT", "20"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

VAULT_TOKEN = os.environ.get("VAULT_GITHUB_TOKEN", "")
VAULT_REPO = os.environ.get("VAULT_REPO", "miyanism/second-brain")
VAULT_REPORT_PATH = os.environ.get(
    "VAULT_REPORT_PATH", "wiki/シナリオ実績/Reels週次インサイト.md")

STATE_PATH = os.path.join(
    os.environ.get("STATE_DIR", os.path.dirname(__file__)), "reels_insights_state.json")

LINE_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
LINE_TO = os.environ.get("LINE_TO_USER_ID", "")

# 1本のReelsに対して取る指標（実査で取得可を確認済みのもののみ）
METRICS = ("views,reach,likes,comments,saved,shares,total_interactions,"
           "ig_reels_avg_watch_time,ig_reels_video_view_total_time")

JST = datetime.timezone(datetime.timedelta(hours=9))

# キャプション末尾の投稿タグ（reply_comments.py と同じ運用）: 投稿:short/... ・ 投稿:scenario/...
SOURCE_TAG = re.compile(r"(short|scenario)/([^\s#、。,]+)")
BARE_TAG = re.compile(r"\d{8}_[^\s#、。,/]+")


class IGAuthError(Exception):
    """トークン期限切れ・権限喪失など、人手対応が必要な恒久エラー。"""


def _is_auth_error(body: str) -> bool:
    low = (body or "").lower()
    return ('"code": 190' in low or '"code":190' in low
            or "oauthexception" in low or "access token" in low
            or "session has been invalidated" in low or "expired" in low)


def _api_call(opener, what: str, retries: int = 4):
    """Meta Graph API 呼び出しの共通リトライ（reply_comments.py と同型）。"""
    last = None
    for i in range(retries):
        try:
            with opener() as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            if _is_auth_error(body):
                raise IGAuthError(f"{what}: HTTP {e.code} 認可エラー（要トークン再発行） {body}") from e
            last = e
            transient = e.code in (429, 500, 502, 503, 504) or e.code == 400
            if not transient or i == retries - 1:
                raise
            print(f"  [retry {i+1}/{retries}] {what}: HTTP {e.code} {body[:120]}")
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if i == retries - 1:
                raise
            print(f"  [retry {i+1}/{retries}] {what}: {type(e).__name__} {e}")
        time.sleep(2 * (i + 1))
    if last:
        raise last


def api_get(path: str, params: dict) -> dict:
    params = dict(params)
    params["access_token"] = TOKEN
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    return _api_call(lambda: urllib.request.urlopen(url, timeout=30), f"GET {path}")


def fetch_recent_reels(count: int) -> list:
    """直近の投稿をページングし、REELSだけを count 本集める。"""
    reels, after = [], None
    for _ in range(6):  # 50件×6ページ=300投稿まで遡れば十分
        params = {"fields": "id,caption,timestamp,permalink,media_product_type",
                  "limit": 50}
        if after:
            params["after"] = after
        res = api_get(f"{IG_USER_ID}/media", params)
        for m in res.get("data", []):
            if m.get("media_product_type") == "REELS":
                reels.append(m)
                if len(reels) >= count:
                    return reels
        after = res.get("paging", {}).get("cursors", {}).get("after")
        if not after or not res.get("data"):
            break
    return reels


def fetch_insights(media_id: str) -> dict:
    res = api_get(f"{media_id}/insights", {"metric": METRICS})
    return {d["name"]: (d.get("values") or [{}])[0].get("value")
            for d in res.get("data", [])}


def post_label(caption: str) -> str:
    """レポートの行ラベル。投稿タグ（投稿:short/… 等）があれば優先、無ければキャプション先頭。"""
    caption = caption or ""
    m = SOURCE_TAG.search(caption)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = BARE_TAG.search(caption)
    if m:
        return m.group(0)
    head = caption.strip().splitlines()[0] if caption.strip() else "(キャプション無し)"
    return head[:24]


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"updated": "", "media": {}}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def fmt_delta(cur, prev) -> str:
    """前週スナップショットとの増分表記。前週値が無ければ空。"""
    if prev is None or cur is None:
        return ""
    d = cur - prev
    return f" (+{d:,})" if d >= 0 else f" ({d:,})"


def judge(row: dict, median_reach: float) -> str:
    """反響の測り方（較正済み基準）に沿った参考判定。正式スコアは人間が台帳で付ける。

    - リーチが自己中央値の3倍超 → A候補（アルゴ拡散）
    - 保存率0.20%未満かつコメント+シェアほぼ0 → C域
    - それ以外 → B域
    - 投稿後7日未満は数字が伸び切っていないので注記
    """
    mark = "B域"
    if median_reach and row["reach"] and row["reach"] >= 3 * median_reach:
        mark = "A候補🔥"
    elif (row["save_rate"] is not None and row["save_rate"] < 0.002
          and (row["comments"] or 0) + (row["shares"] or 0) == 0):
        mark = "C域"
    if row["age_days"] < 7:
        mark += "(7日未満)"
    return mark


def build_report(rows: list, run_dt: datetime.datetime, prev_updated: str) -> str:
    reaches = [r["reach"] for r in rows if r["reach"]]
    median_reach = statistics.median(reaches) if reaches else 0

    lines = [
        "---",
        "title: Reels週次インサイト",
        "tags: [シナリオ実績, 反響計測, Instagram, 自動収集]",
        "source: Railway cron（reels_insights.py・毎週火曜）",
        "---",
        "",
        "# Reels週次インサイト（自動収集）",
        "",
        f"実行日: {run_dt.strftime('%Y-%m-%d %H:%M')} JST"
        + (f"／前回: {prev_updated}" if prev_updated else "（初回・前週比なし）"),
        f"対象: 直近Reels {len(rows)}本／リーチ自己中央値: {median_reach:,.0f}",
        "",
        "このページは毎週火曜に上書きされます（[[週次Lintレポート]]と同方式）。",
        "指標の読み方・A/B/Cスコアの正は [[反響の測り方]]。正式スコアは [[シナリオ実績 — 台帳]] で人間が付ける（目安列は較正済み基準による参考値）。",
        "数字は投稿後7日時点を正とする運用のため、経過7日未満の行は判定を保留する。",
        "",
        "| 投稿日 | 経過 | 投稿 | 再生 | リーチ(前週比) | 保存(率) | シェア | コメ | 平均再生 | 目安 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        save_rate = f"{r['save_rate']*100:.2f}%" if r["save_rate"] is not None else "-"
        avg_s = f"{r['avg_watch_s']:.1f}s" if r["avg_watch_s"] is not None else "-"
        lines.append(
            f"| {r['date']} | {r['age_days']}日 "
            f"| [{r['label']}]({r['permalink']}) "
            f"| {r['views']:,}{fmt_delta(r['views'], r['prev'].get('views'))} "
            f"| {r['reach']:,}{fmt_delta(r['reach'], r['prev'].get('reach'))} "
            f"| {r['saved']:,}{fmt_delta(r['saved'], r['prev'].get('saved'))} ({save_rate}) "
            f"| {r['shares']:,} | {r['comments']:,} | {avg_s} "
            f"| {judge(r, median_reach)} |")
    lines += [
        "",
        "## 取得できない指標（API制約・2026-07-13実査）",
        "",
        "- スキップ率・フォロワー外%（follow_type内訳）・動画尺は Instagram Login API の media insights では非対応。",
        "- 維持率の代理は **平均再生時間（秒）** をそのまま使う（[[反響の測り方]] 2026-07-07 の採用どおり。目安: 再定義/物語型26〜28s／蔵銘柄紹介13〜17s）。",
        "- フォロワー外%はIGアプリのインサイト画面でのみ確認可（必要な本だけ手動で控える）。",
        "",
        "## 関連",
        "",
        "- [[反響の測り方]] — 指標とスコア基準の正",
        "- [[シナリオ実績 — 台帳]] — 正式スコアの記入先",
        "- [[Reelsインサイト週次収集（IG bot拡張）]] — この仕組みの設計書",
        "",
    ]
    return "\n".join(lines)


def push_to_vault(report: str, run_dt: datetime.datetime):
    """GitHub Contents API で second-brain のレポートページを上書きコミットする。"""
    api = f"https://api.github.com/repos/{VAULT_REPO}/contents/{urllib.parse.quote(VAULT_REPORT_PATH)}"
    headers = {
        "Authorization": f"Bearer {VAULT_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ponshu-reels-insights",
    }
    sha = None
    req = urllib.request.Request(api, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            sha = json.load(r).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    payload = {
        "message": f"Reels週次インサイト {run_dt.strftime('%Y-%m-%d')}（自動収集）",
        "content": base64.b64encode(report.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(api, data=json.dumps(payload).encode(),
                                 headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)
    print(f"  [vault] pushed: {res.get('commit', {}).get('sha', '')[:10]} {VAULT_REPORT_PATH}")


def notify_line(rows: list, run_dt: datetime.datetime):
    """レポート更新の1通通知（任意）。リーチ上位3本だけ添える。"""
    if not (LINE_TOKEN and LINE_TO):
        return
    top = sorted(rows, key=lambda r: r["reach"] or 0, reverse=True)[:3]
    body = "\n".join(f"・{r['label']} リーチ{r['reach']:,} 保存{r['saved']} 平均{r['avg_watch_s']:.0f}s"
                     for r in top)
    msg = (f"📊 Reels週次インサイト更新（{run_dt.strftime('%m/%d')}・{len(rows)}本）\n"
           f"リーチ上位:\n{body}\n\n"
           f"レポート: second-brain/{VAULT_REPORT_PATH}")
    payload = json.dumps({"to": LINE_TO, "messages": [{"type": "text", "text": msg[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"})
    try:
        urllib.request.urlopen(req, timeout=30)
        print("  [LINE] 更新通知を送信")
    except Exception as e:
        print(f"  [LINE送信失敗] {e}")


def main():
    if not (IG_USER_ID and TOKEN):
        print("[FATAL] 必須環境変数が未設定 (IG_USER_ID / IG_ACCESS_TOKEN)")
        sys.exit(1)
    if not (VAULT_TOKEN or DRY_RUN):
        print("[FATAL] VAULT_GITHUB_TOKEN が未設定（DRY_RUN=1 ならレポート生成のみ可）")
        sys.exit(1)

    run_dt = datetime.datetime.now(JST)
    state = load_state()
    prev_media = state.get("media", {})
    print(f"[start] reels={REELS_COUNT}本, dry_run={DRY_RUN}, "
          f"vault={VAULT_REPO}/{VAULT_REPORT_PATH}, "
          f"prev={state.get('updated') or '(初回)'}")

    try:
        reels = fetch_recent_reels(REELS_COUNT)
    except IGAuthError as e:
        # トークン失効等の恒久エラーはクラッシュさせてRailway通知を出す（コメントbotと同じ扱い）
        print(f"[FATAL] Instagram認可エラー: {e}")
        sys.exit(1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        # 週1cronなので黙って正常終了せず、クラッシュ通知で気づけるようにする
        print(f"[FATAL] メディア取得失敗: {e}")
        sys.exit(1)

    rows, new_media = [], {}
    for m in reels:
        try:
            ins = fetch_insights(m["id"])
        except IGAuthError as e:
            print(f"[FATAL] Instagram認可エラー: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"  [warn] insights取得失敗 {m['id']}: {e}")
            continue
        posted = datetime.datetime.fromisoformat(
            m["timestamp"].replace("+0000", "+00:00")).astimezone(JST)
        reach = ins.get("reach") or 0
        saved = ins.get("saved") or 0
        avg_ms = ins.get("ig_reels_avg_watch_time")
        row = {
            "id": m["id"],
            "label": post_label(m.get("caption", "")),
            "permalink": m.get("permalink", ""),
            "date": posted.strftime("%m/%d"),
            "age_days": (run_dt - posted).days,
            "views": ins.get("views") or 0,
            "reach": reach,
            "likes": ins.get("likes") or 0,
            "comments": ins.get("comments") or 0,
            "saved": saved,
            "shares": ins.get("shares") or 0,
            "save_rate": (saved / reach) if reach else None,
            "avg_watch_s": (avg_ms / 1000) if avg_ms is not None else None,
            "prev": prev_media.get(m["id"], {}),
        }
        rows.append(row)
        new_media[m["id"]] = {k: row[k] for k in
                              ("views", "reach", "likes", "comments", "saved", "shares")}
        print(f"  [ok] {row['date']} {row['label']}: 再生{row['views']:,} リーチ{reach:,} "
              f"保存{saved} 平均{row['avg_watch_s'] or 0:.1f}s")

    if not rows:
        print("[FATAL] Reelsが1本も取得できなかった")
        sys.exit(1)

    report = build_report(rows, run_dt, state.get("updated", ""))

    if DRY_RUN:
        print("\n===== レポート（DRY_RUN・push無し） =====\n")
        print(report)
        print("[done] dry_run: state保存・Vault push・LINE通知はスキップ")
        return

    push_to_vault(report, run_dt)
    save_state({"updated": run_dt.strftime("%Y-%m-%d"), "media": new_media})
    notify_line(rows, run_dt)
    print(f"[done] {len(rows)}本を収集・レポート更新")


if __name__ == "__main__":
    main()
