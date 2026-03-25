# -*- coding: utf-8 -*-
"""
毎月初に実行: sake_state.json をリセットして翌月の投稿準備をする
使い方: python reset_state.py
"""
import json, os, requests, base64
from datetime import datetime

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO  = os.environ["GITHUB_REPO"]
BRANCH       = "main"
GH_HEADERS   = {"Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"}
API_BASE     = f"https://api.github.com/repos/{GITHUB_REPO}"

r = requests.get(f"{API_BASE}/contents/sake_state.json", headers=GH_HEADERS)
sha = r.json()["sha"]

new_state = {"current_index": 0, "history": []}
encoded = base64.b64encode(
    json.dumps(new_state, ensure_ascii=False, indent=2).encode()
).decode()

requests.put(
    f"{API_BASE}/contents/sake_state.json",
    headers=GH_HEADERS,
    json={
        "message": f"Reset state for new month ({datetime.now().strftime('%Y-%m')})",
        "content": encoded,
        "sha": sha,
        "branch": BRANCH,
    },
)
print("sake_state.json をリセットしました（current_index: 0）")
