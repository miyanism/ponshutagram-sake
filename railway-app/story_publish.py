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


def _api_get(path: str, params: dict, retries: int = 3) -> dict:
    """graph.instagram.com への GET（コンテナの status_code 確認に使う）。"""
    qs = urllib.parse.urlencode({**params, "access_token": TOKEN})
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{GRAPH}/{path}?{qs}", method="GET")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            if _is_auth_error(txt):
                raise IGAuthError(f"GET {path}: HTTP {e.code} 認可エラー（要トークン再発行） {txt}") from e
            last = e
            if e.code not in (429, 500, 502, 503, 504) or i == retries - 1:
                raise RuntimeError(f"GET {path}: HTTP {e.code} {txt}") from e
            print(f"  [retry {i+1}/{retries}] GET {path}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if i == retries - 1:
                raise
            print(f"  [retry {i+1}/{retries}] GET {path}: {type(e).__name__}")
        time.sleep(2 * (i + 1))
    if last:
        raise last
    raise RuntimeError(f"GET {path}: 不明な失敗")


VIDEO_EXTS = (".mp4", ".mov")


def is_video_url(media_url: str) -> bool:
    """URL末尾の拡張子で動画かどうかを判定（.mp4/.mov）。manifest混載の種別分岐に使う。"""
    path = urllib.parse.urlparse(media_url).path.lower()
    return path.endswith(VIDEO_EXTS)


def create_story_container(image_url: str) -> str:
    """画像STORIESコンテナを作成し creation_id を返す（この時点では非公開）。"""
    res = _api_post(f"{IG_USER_ID}/media", {"media_type": "STORIES", "image_url": image_url})
    cid = res.get("id")
    if not cid:
        raise RuntimeError(f"creation_id が返らない: {res}")
    return cid


def create_video_story_container(video_url: str) -> str:
    """動画STORIESコンテナを作成し creation_id を返す（非公開）。
    画像と違い動画はMeta側の処理が非同期＝publish前に status_code=FINISHED を待つ必要がある。"""
    res = _api_post(f"{IG_USER_ID}/media", {"media_type": "STORIES", "video_url": video_url})
    cid = res.get("id")
    if not cid:
        raise RuntimeError(f"creation_id が返らない: {res}")
    return cid


def wait_container_finished(creation_id: str, timeout_s: int = 240, interval_s: int = 8) -> None:
    """動画コンテナの status_code が FINISHED になるまでポーリング（最大 timeout_s 秒）。
    ERROR/EXPIRED は即例外。IN_PROGRESS はまだ処理中＝待つ。timeout で例外。"""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        res = _api_get(creation_id, {"fields": "status_code,status"})
        code = res.get("status_code")
        last = res
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"動画コンテナ処理失敗 status_code={code}: {res}")
        # IN_PROGRESS（またはPUBLISHED以外）＝まだ処理中
        print(f"  [video] 処理待ち status_code={code} …{interval_s}s後に再確認")
        time.sleep(interval_s)
    raise RuntimeError(f"動画コンテナが {timeout_s}s 以内に FINISHED にならない（最後の応答: {last}）")


def publish_container(creation_id: str) -> str:
    """コンテナを公開し、公開されたメディアIDを返す（★ここで本番反映）。"""
    res = _api_post(f"{IG_USER_ID}/media_publish", {"creation_id": creation_id})
    mid = res.get("id")
    if not mid:
        raise RuntimeError(f"media_id が返らない: {res}")
    return mid


def post_story(media_url: str, do_publish: bool) -> dict:
    """1件をストーリーズ投稿。画像/動画はURL拡張子で自動判定。
    do_publish=False ならコンテナ作成まで（非公開）。動画は作成後 status_code=FINISHED を待ってから公開。"""
    video = is_video_url(media_url)
    kind = "video" if video else "image"
    cid = create_video_story_container(media_url) if video else create_story_container(media_url)
    print(f"[container] creation_id={cid}  {kind}={media_url}")
    if not do_publish:
        print("[dry] STORY_PUBLISH 未設定のため公開しません（コンテナ作成のみ・非公開）")
        return {"creation_id": cid, "kind": kind, "published": False}
    if video:
        # 動画はMeta側の処理完了（FINISHED）を待ってからでないと publish が失敗する
        wait_container_finished(cid)
    else:
        # 画像もコンテナ作成直後は処理中のことがあるため、少し待ってからpublish
        time.sleep(5)
    mid = publish_container(cid)
    print(f"[published] media_id={mid}  ★ストーリーに公開されました（{kind}）")
    return {"creation_id": cid, "media_id": mid, "kind": kind, "published": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-url", help="ストーリーに載せる画像の公開URL")
    ap.add_argument("--video-url", help="ストーリーに載せる動画(mp4)の公開URL")
    ap.add_argument("--publish", action="store_true",
                    help="実際に公開する（付けなければコンテナ作成のみ＝非公開）")
    args = ap.parse_args()

    media_url = args.video_url or args.image_url
    if not media_url:
        ap.error("--image-url か --video-url のどちらかが必要です")
    if not TOKEN:
        print("[FATAL] IG_ACCESS_TOKEN 未設定")
        sys.exit(1)

    # 二重ガード: --publish かつ 環境変数 STORY_PUBLISH=1 の両方が揃った時だけ公開する運用も可能だが、
    # ここでは「明示的な --publish もしくは STORY_PUBLISH=1」のどちらかで公開（cron運用はSTORY_PUBLISH=1で回す）。
    do_publish = args.publish or os.environ.get("STORY_PUBLISH") == "1"
    try:
        post_story(media_url, do_publish)
    except IGAuthError as e:
        print(f"[FATAL] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
