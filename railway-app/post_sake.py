# -*- coding: utf-8 -*-
"""
Railway上で毎日20:00(JST)に実行される日本酒GMB自動投稿スクリプト
"""
import requests
import base64
import csv
import io
import json
import os
import sys
from datetime import datetime

# ── 環境変数 ─────────────────────────────────────────
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
GITHUB_REPO   = os.environ["GITHUB_REPO"]    # 例: yourname/ponshutagram-sake
GMB_LOCATION  = os.environ["GMB_LOCATION"]   # 例: accounts/123/locations/456
CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
LINE_TOKEN    = os.environ.get("LINE_CHANNEL_TOKEN", "")   # 打ち止め通知用（未設定なら通知なし）
LINE_TO       = os.environ.get("LINE_TO_USER_ID", "")

RESERVE_URL   = "https://www.tablecheck.com/shops/ponshutagram/reserve?utm_source=line"
BRANCH        = "main"
SHEET_ID      = "1EVZQNXnRGx0Pf4m9RWWXBFrtXoa0le43IGP6dGkgEKk"

GH_HEADERS  = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json"}
API_BASE    = f"https://api.github.com/repos/{GITHUB_REPO}"
RAW_BASE    = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{BRANCH}"


# ── Google スプレッドシート ───────────────────────────
def to_direct_image_url(url):
    """Google DriveのビューURLを直接画像URLに変換する。
    thumbnail形式は uc?export=view と違い確認ページを挟まず実画像バイトを返すため確実。"""
    import re
    m = re.search(r"/file/d/([^/]+)", url) or re.search(r"[?&]id=([^&]+)", url)
    if m:
        return f"https://drive.google.com/thumbnail?id={m.group(1)}&sz=w2048"
    return url


def image_is_valid(url):
    """GMBが受け付ける画像か事前検証する（実画像で10KB以上か）。
    Driveの非公開ファイル等はHTML(確認ページ)を返すため、それを弾く。"""
    try:
        r = requests.get(url, timeout=30)
        ct = r.headers.get("Content-Type", "")
        return r.status_code == 200 and ct.startswith("image/") and len(r.content) >= 10240
    except Exception as e:
        print(f"[WARN] 画像取得失敗: {e}")
        return False


def fetch_sake_list_from_sheets():
    """スプレッドシートから銘柄データを取得。
    列は位置でなくヘッダー名で特定する（月次でシートの列構成が変わってもズレない）。
    2026-08にG列ノート/I列画像の決め打ちが列追加でズレ、在庫記号を画像URLとして
    読んで投稿が全停止した事故の再発防止。"""
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
    r = requests.get(url)
    r.raise_for_status()
    reader = csv.reader(io.StringIO(r.content.decode("utf-8")))
    rows = list(reader)

    header = [h.strip() for h in rows[0]]
    def col(name):
        return header.index(name) if name in header else -1
    i_name, i_note = col("銘柄名"), col("テイスティングノート")
    i_img, i_stock = col("画像URL"), col("在庫")
    if i_name < 0 or i_img < 0:
        raise RuntimeError(f"シートに『銘柄名』『画像URL』のヘッダーが見つかりません: {header}")

    def cell(row, i):
        return row[i].strip() if 0 <= i < len(row) else ""

    sake_list = []
    for row in rows[1:]:
        name      = cell(row, i_name)
        note      = cell(row, i_note)
        image_url = cell(row, i_img)
        if not name or not image_url:
            continue
        if cell(row, i_stock) == "×":  # 売切は投稿しない（在庫列が無いシートでは無条件で通る）
            continue
        sake_list.append({
            "name": name,
            "note": note,
            "image_url": to_direct_image_url(image_url),
        })

    return sake_list


# ── GitHub ユーティリティ ─────────────────────────────
def gh_get_json(path):
    """GitHub上のJSONファイルを取得。(content, sha) を返す"""
    r = requests.get(f"{API_BASE}/contents/{path}", headers=GH_HEADERS)
    r.raise_for_status()
    data = r.json()
    text = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(text), data["sha"]


def gh_put_json(path, obj, sha, message):
    """GitHub上のJSONファイルを更新（コミット）"""
    encoded = base64.b64encode(
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode()
    payload = {"message": message, "content": encoded, "sha": sha, "branch": BRANCH}
    r = requests.put(f"{API_BASE}/contents/{path}", headers=GH_HEADERS, json=payload)
    r.raise_for_status()


# ── LINE 通知 ─────────────────────────────────────────
def notify_line(text):
    """オーナーにLINE pushで知らせる（LINE未設定ならコンソール出力のみ）。"""
    if not (LINE_TOKEN and LINE_TO):
        print(f"[LINE未設定] {text}")
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
            json={"to": LINE_TO, "messages": [{"type": "text", "text": text[:4900]}]},
            timeout=30,
        )
        print("[LINE] 通知を送信しました")
    except Exception as e:
        print(f"[LINE送信失敗] {e}")


# ── 投稿対象の選定 ────────────────────────────────────
def select_targets(sake_list, history):
    """履歴ベースで投稿候補を決める（シート上の位置番号は一切使わない）。

    - 当月にすでに投稿した銘柄は除外（同月内の重複を構造的に防ぐ）
    - 一度も投稿していない銘柄をシート順で最優先
    - その後は最終投稿が古い銘柄から再投稿（月替わりで同じ銘柄をまた出せる）
    """
    last_posted = {}
    for h in history:
        name, ts = h.get("name"), h.get("posted_at", "")
        if name and ts > last_posted.get(name, ""):
            last_posted[name] = ts

    this_month = datetime.now().strftime("%Y-%m")
    seen = set()
    never, reposts = [], []
    for s in sake_list:
        if s["name"] in seen:  # シート内の同名重複行は1件に集約
            continue
        seen.add(s["name"])
        ts = last_posted.get(s["name"])
        if ts is None:
            never.append(s)
        elif not ts.startswith(this_month):
            reposts.append(s)
    reposts.sort(key=lambda s: last_posted[s["name"]])
    return never + reposts


# ── Google OAuth2 ──────────────────────────────────────
def get_access_token():
    """リフレッシュトークンからアクセストークンを取得"""
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    })
    r.raise_for_status()
    return r.json()["access_token"]


# ── GMB 投稿 ──────────────────────────────────────────
def post_to_gmb(access_token, sake, image_url):
    """Google My Business に日本酒投稿を作成"""
    summary = f"【{sake['name']}】\n{sake['note']}" if sake.get("note") else f"【{sake['name']}】"

    body = {
        "languageCode": "ja",
        "summary": summary,
        "callToAction": {
            "actionType": "BOOK",
            "url": RESERVE_URL,
        },
        "media": [{"mediaFormat": "PHOTO", "sourceUrl": image_url}],
        "topicType": "STANDARD",
    }

    url = f"https://mybusiness.googleapis.com/v4/{GMB_LOCATION}/localPosts"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        json=body,
    )

    if r.status_code not in (200, 201):
        print(f"[ERROR] GMB API response: {r.status_code} {r.text}")
        r.raise_for_status()

    return r.json()


# ── メイン ────────────────────────────────────────────
def main():
    print(f"[{datetime.now().isoformat()}] 日本酒GMB投稿 開始")

    # GMB_LOCATIONが未設定の場合はスキップ（Google API承認待ち）
    if not GMB_LOCATION or GMB_LOCATION.startswith("PLACEHOLDER") or "/" not in GMB_LOCATION:
        print("[SKIP] GMB_LOCATIONが未設定です。Google API承認後に設定してください。")
        sys.exit(0)

    # データ取得
    sake_list             = fetch_sake_list_from_sheets()
    state,     state_sha  = gh_get_json("sake_state.json")
    history               = state.setdefault("history", [])

    targets = select_targets(sake_list, history)
    print(f"シート有効銘柄: {len(sake_list)}件 / 今日の投稿候補: {len(targets)}件")

    if not targets:
        notify_line(
            "⚠️ GMB自動投稿: 投稿できる銘柄がありません。\n"
            f"シートの有効銘柄{len(sake_list)}件はすべて当月投稿済みです。"
            "スプレッドシートに新しい銘柄を追加してください。"
        )
        sys.exit(0)

    access_token = get_access_token()

    # 画像が不正な銘柄はスキップして次へ進む（1枚の不良で全体を止めない）
    skipped = []
    for sake in targets:
        image_url = sake["image_url"]
        print(f"銘柄: {sake['name']}  画像URL: {image_url}")

        if not image_is_valid(image_url):
            print(f"[SKIP] 画像が無効（非公開/小さすぎ等）のためスキップ: {sake['name']}")
            skipped.append({"name": sake["name"], "reason": "invalid_image"})
            continue

        # GMB 投稿
        result = post_to_gmb(access_token, sake, image_url)
        print(f"投稿完了: {result.get('name', result)}")

        # 状態更新（GitHubにコミット）。current_index は廃止（履歴が唯一の状態）
        state.pop("current_index", None)
        history.append({
            "name":  sake["name"],
            "posted_at": datetime.now().isoformat(),
            "skipped": skipped,
        })
        gh_put_json(
            "sake_state.json",
            state,
            state_sha,
            f"Auto post #{len(history)}: {sake['name']}",
        )
        print(f"sake_state.json を更新しました (history: {len(history)}件)")
        if skipped:
            print(f"※ 今回スキップした銘柄: {[s['name'] for s in skipped]}")
        return

    # ループを抜けた = 候補全てが無効画像だった
    print(f"[完了] 投稿可能な銘柄がありませんでした。スキップ: {[s['name'] for s in skipped]}")
    notify_line(
        f"⚠️ GMB自動投稿: 候補{len(targets)}件すべて画像が無効で投稿できませんでした。\n"
        "スプレッドシートI列の画像URL（Drive共有設定）を確認してください。\n"
        f"例: {', '.join(s['name'] for s in skipped[:5])}"
    )


if __name__ == "__main__":
    main()
