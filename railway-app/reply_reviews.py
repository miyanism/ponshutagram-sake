# -*- coding: utf-8 -*-
"""
Railway上で毎日1回実行される Google My Business レビュー自動返信スクリプト
未返信レビューを取得し、Claude APIで返信文を生成して投稿する
"""
import requests
import os
import sys
from datetime import datetime
import anthropic

# ── 環境変数 ─────────────────────────────────────────
GMB_LOCATION    = os.environ["GMB_LOCATION"]
CLIENT_ID       = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET   = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN   = os.environ["GOOGLE_REFRESH_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

SHOP_NAME = "ポン酒タグラム The Bar"

STAR_MAP = {
    "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
}


# ── Google OAuth2 ──────────────────────────────────────
def get_access_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    })
    r.raise_for_status()
    return r.json()["access_token"]


# ── GMB レビュー取得 ───────────────────────────────────
def get_reviews(access_token):
    url = f"https://mybusiness.googleapis.com/v4/{GMB_LOCATION}/reviews"
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"})
    r.raise_for_status()
    return r.json().get("reviews", [])


# ── Claude で返信文生成 ────────────────────────────────
def generate_reply(review_text, star_rating_str):
    stars = STAR_MAP.get(star_rating_str, 0)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if stars >= 4:
        tone = "喜びと感謝を伝え、また来店を歓迎する"
    elif stars == 3:
        tone = "感謝しつつ、より良い体験を提供したいという姿勢を伝える"
    else:
        tone = "真摯にお詫びし、改善への取り組みを伝える"

    prompt = (
        f"あなたは大阪・谷町の日本酒バー「{SHOP_NAME}」のオーナーです。\n"
        f"Googleマップに以下のレビューが投稿されました。\n"
        f"{tone}返信を日本語で書いてください。\n"
        f"返信は150文字以内で、自然で温かみのある文章にしてください。\n\n"
        f"星評価: {stars}つ星\n"
        f"レビュー内容: {review_text if review_text else '（コメントなし）'}\n\n"
        f"返信文のみを出力してください。"
    )

    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ── GMB 返信投稿 ───────────────────────────────────────
def post_reply(access_token, review_name, reply_text):
    url = f"https://mybusiness.googleapis.com/v4/{review_name}/reply"
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"comment": reply_text},
    )
    if r.status_code not in (200, 201):
        print(f"[ERROR] reply API: {r.status_code} {r.text}")
        r.raise_for_status()
    return r.json()


# ── メイン ────────────────────────────────────────────
def main():
    print(f"[{datetime.now().isoformat()}] レビュー自動返信 開始")

    access_token = get_access_token()
    reviews = get_reviews(access_token)

    print(f"レビュー合計: {len(reviews)}件")

    unanswered = [r for r in reviews if not r.get("reviewReply")]
    print(f"未返信: {len(unanswered)}件")

    if not unanswered:
        print("未返信レビューはありません。終了します。")
        sys.exit(0)

    replied_count = 0
    for review in unanswered:
        review_name   = review["name"]           # accounts/.../reviews/xxx
        star_rating   = review.get("starRating", "UNKNOWN")
        review_text   = review.get("comment", "")
        reviewer      = review.get("reviewer", {}).get("displayName", "お客様")

        print(f"  [{replied_count + 1}] {reviewer} ({star_rating}) - 返信生成中...")

        reply_text = generate_reply(review_text, star_rating)
        post_reply(access_token, review_name, reply_text)

        print(f"      返信完了: {reply_text[:60]}...")
        replied_count += 1

    print(f"\n[完了] {replied_count}件に返信しました。")


if __name__ == "__main__":
    main()
