# -*- coding: utf-8 -*-
"""
Railway上で毎日20:00(JST)に実行される日本酒GMB自動投稿スクリプト
"""
import requests
import base64
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

RESERVE_URL   = "https://www.tablecheck.com/shops/ponshutagram/reserve?utm_source=line"
BRANCH        = "main"

GH_HEADERS  = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json"}
API_BASE    = f"https://api.github.com/repos/{GITHUB_REPO}"
RAW_BASE    = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{BRANCH}"


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
    sake_list, _          = gh_get_json("sake_data.json")
    state,     state_sha  = gh_get_json("sake_state.json")

    idx = state.get("current_index", 0)
    print(f"次の投稿インデックス: {idx} / {len(sake_list)}")

    if idx >= len(sake_list):
        print("全銘柄の投稿が完了しています。sake_state.jsonをリセットしてください。")
        sys.exit(0)

    sake = sake_list[idx]
    image_url = f"{RAW_BASE}/images/{sake['num']}.JPG"
    print(f"銘柄: {sake['name']}  画像URL: {image_url}")

    # GMB 投稿
    access_token = get_access_token()
    result = post_to_gmb(access_token, sake, image_url)
    print(f"投稿完了: {result.get('name', result)}")

    # 状態更新（GitHubにコミット）
    state["current_index"] = idx + 1
    state.setdefault("history", []).append({
        "index": idx,
        "name":  sake["name"],
        "posted_at": datetime.now().isoformat(),
    })
    gh_put_json(
        "sake_state.json",
        state,
        state_sha,
        f"Auto post #{idx + 1}: {sake['name']}",
    )
    print(f"sake_state.json を更新しました (index {idx} → {idx + 1})")


if __name__ == "__main__":
    main()
