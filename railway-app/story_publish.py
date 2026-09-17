# -*- coding: utf-8 -*-
"""
story_publish.py — Instagram ストーリーズ自動投稿（Content Publishing API）

Instagram API with Instagram Login（graph.instagram.com）で、公開URLの画像を
ストーリーズとして投稿する。2ステップ:
  1) POST /{IG_USER_ID}/media?media_type=STORIES&image_url=<公開URL>  → creation_id
  2) POST /{IG_USER_ID}/media_publish?creation_id=<id>                 → 公開（本番反映）

★安全ガード（重要）:
  - 既定では **コンテナ作成まで（=非公開）** しか行わない。
  - 実際にストーリーへ公開するのは、環境変数 STORY_PUBLISH=1 または引数 --publish がある時だけ。
  - これは「初回の実publishはオーナー明示GO必須／無人ローテはGO後」という運用制約を、コード側でも二重に守るため。

画像ホスティング: APIは公開URLで画像を渡す必要がある（ローカル直アップ不可）。
  当プロジェクトは GitHub Pages（miyanism.github.io/ponshutagram-sake/…）で静的配信できるので、
  画像はそこに置いた公開URLを渡す想定。リンク/テキストスタンプはAPI不可＝CTAは画像に焼き込む（仕入れ企画部が対応済み）。

必要な環境変数:
  IG_ACCESS_TOKEN   … content_publish 入りの長期トークン（Railway共有変数・自動更新）
  IG_USER_ID        … 省略可（既定 17841449174401056 = ponshutagram_bar）
  IG_GRAPH_BASE     … 省略可（既定 https://graph.instagram.com）
  STORY_PUBLISH     … "1" の時だけ本番公開（既定は未公開＝コンテナ作成のみ）

使い方（例）:
  # コンテナ作成だけ（非公開・検証用）
  python story_publish.py --image-url "https://miyanism.github.io/ponshutagram-sake/ig-stories/test.jpg"
  # 実際に公開（オーナーGO後だけ）
  python story_publish.py --image-url "<公開URL>" --publish
"""
import os
import sys
import json
import time
import argparse
import urllib.parse
import urllib.request
import urllib.error

GRAPH = os.environ.get("IG_GRAPH_BASE", "https://graph.instagram.com")
IG_USER_ID = os.environ.get("IG_USER_ID", "17841449174401056")
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")


class IGAuthError(Exception):
    """トークン失効・権限喪失など人手対応が必要な恒久エラー。"""


def _is_auth_error(body: str) -> bool:
    low = (body or "").lower()
    return ('"code": 190' in low or '"code":190' in low
            or "oauthexception" in low or "access token" in low
            or "session has been invalidated" in low or "expired" in low)


def _api_post(path: str, data: dict, retries: int = 3) -> dict:
    """graph.instagram.com への POST（reply_comments.py と同型のリトライ）。"""
    body = urllib.parse.urlencode({**data, "access_token": TOKEN}).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{GRAPH}/{path}", data=body, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            if _is_auth_error(txt):
                raise IGAuthError(f"POST {path}: HTTP {e.code} 認可エラー（要トークン再発行） {txt}") from e
            last = e
            if e.code not in (429, 500, 502, 503, 504) or i == retries - 1:
                raise RuntimeError(f"POST {path}: HTTP {e.code} {txt}") from e
            print(f"  [retry {i+1}/{retries}] POST {path}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if i == retries - 1:
                raise
            print(f"  [retry {i+1}/{retries}] POST {path}: {type(e).__name__}")
        time.sleep(2 * (i + 1))
    if last:
        raise last
    raise RuntimeError(f"POST {path}: 不明な失敗")


def create_story_container(image_url: str) -> str:
    """STORIESコンテナを作成し creation_id を返す（この時点では非公開）。"""
    res = _api_post(f"{IG_USER_ID}/media", {"media_type": "STORIES", "image_url": image_url})
    cid = res.get("id")
    if not cid:
        raise RuntimeError(f"creation_id が返らない: {res}")
    return cid


def publish_container(creation_id: str) -> str:
    """コンテナを公開し、公開されたメディアIDを返す（★ここで本番反映）。"""
    res = _api_post(f"{IG_USER_ID}/media_publish", {"creation_id": creation_id})
    mid = res.get("id")
    if not mid:
        raise RuntimeError(f"media_id が返らない: {res}")
    return mid


def post_story(image_url: str, do_publish: bool) -> dict:
    """1枚をストーリーズ投稿。do_publish=False ならコンテナ作成まで（非公開）。"""
    cid = create_story_container(image_url)
    print(f"[container] creation_id={cid}  image={image_url}")
    if not do_publish:
        print("[dry] STORY_PUBLISH 未設定のため公開しません（コンテナ作成のみ・非公開）")
        return {"creation_id": cid, "published": False}
    # Metaはコンテナ作成直後は処理中のことがあるため、少し待ってからpublish
    time.sleep(5)
    mid = publish_container(cid)
    print(f"[published] media_id={mid}  ★ストーリーに公開されました")
    return {"creation_id": cid, "media_id": mid, "published": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-url", required=True, help="ストーリーに載せる画像の公開URL")
    ap.add_argument("--publish", action="store_true",
                    help="実際に公開する（付けなければコンテナ作成のみ＝非公開）")
    args = ap.parse_args()

    if not TOKEN:
        print("[FATAL] IG_ACCESS_TOKEN 未設定")
        sys.exit(1)

    # 二重ガード: --publish かつ 環境変数 STORY_PUBLISH=1 の両方が揃った時だけ公開する運用も可能だが、
    # ここでは「明示的な --publish もしくは STORY_PUBLISH=1」のどちらかで公開（cron運用はSTORY_PUBLISH=1で回す）。
    do_publish = args.publish or os.environ.get("STORY_PUBLISH") == "1"
    try:
        post_story(args.image_url, do_publish)
    except IGAuthError as e:
        print(f"[FATAL] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
