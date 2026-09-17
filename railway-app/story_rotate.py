# -*- coding: utf-8 -*-
"""
story_rotate.py — ストーリーズ6枠/日ローテ投稿（Railway cron から実行）

毎日6枠（JST 14/16/17/19/20/22時 ＝ UTC 05/07/08/10/11/13時）に1枚ずつ、
画像セットを巡回してストーリーズに自動公開する。

cron想定（UTCで6時刻）:  0 5,7,8,10,11,13 * * *

画像:
  GitHub Pages で公開ホストした画像を巡回。ファイル一覧は docs/ig-stories/manifest.json
  （{"images": ["bio_01.jpg", ...]}）から読む。同じ画像が別日に再登場してOK（広告刷り込み運用）。
  画像URL = STORY_IMAGE_BASE + <filename>（既定 https://miyanism.github.io/ponshutagram-sake/ig-stories/）

巡回インデックス（状態を持たない決定論的方式）:
  グローバル枠番号 = 日付序数 * 6 + その日の枠index(0..5)  →  画像 = images[番号 % 枚数]
  ＝毎投稿+1で進み、枚数を一周したら頭に戻る。cronが多重発火しても同じ枠なら同じ画像＝冪等寄り。

★安全ガード:
  STORY_PUBLISH=1 の時だけ実際に公開する（既定はコンテナ作成のみ＝非公開）。
  ＝「無人ローテON」はこの環境変数で最終有効化する（オーナー最終GO後にRailwayで 1 にする）。

失敗（トークン失効・API失敗）は無音にしない＝LINEでオーナーに通知（[[自動化の認証と失敗検知の設計原則]]）。

必要な環境変数:
  IG_ACCESS_TOKEN / IG_USER_ID / IG_GRAPH_BASE … story_publish.py と共有（Railway共有変数）
  STORY_PUBLISH      … "1" で本番公開（既定 未公開）
  STORY_IMAGE_BASE   … 省略可（画像公開URLのベース）
  LINE_CHANNEL_TOKEN / LINE_TO_USER_ID … 失敗通知先（共有変数）
"""
import os
import sys
import json
import datetime
import urllib.request

import story_publish  # 同ディレクトリ。post_story / IGAuthError を再利用

SLOTS_UTC = [5, 7, 8, 10, 11, 13]  # 6枠（UTC時）＝ JST 14/16/17/19/20/22時
IMAGE_BASE = os.environ.get(
    "STORY_IMAGE_BASE",
    "https://miyanism.github.io/ponshutagram-sake/ig-stories/")
MANIFEST = os.path.join(os.path.dirname(__file__), "..", "docs", "ig-stories", "manifest.json")
DO_PUBLISH = os.environ.get("STORY_PUBLISH") == "1"

LINE_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
LINE_TO = os.environ.get("LINE_TO_USER_ID", "")


def notify_line(msg: str):
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


def load_images() -> list:
    with open(MANIFEST, encoding="utf-8") as f:
        imgs = json.load(f).get("images", [])
    if not imgs:
        raise RuntimeError("manifest.json の images が空")
    return imgs


def pick_index(now_utc: datetime.datetime, n: int) -> int:
    """状態を持たない巡回index。今の時刻がどの枠かを判定し、日付序数×6＋枠index を n で割る。"""
    hour = now_utc.hour
    slot = SLOTS_UTC.index(hour) if hour in SLOTS_UTC else _nearest_slot(hour)
    global_slot = now_utc.date().toordinal() * len(SLOTS_UTC) + slot
    return global_slot % n


def _nearest_slot(hour: int) -> int:
    """定刻外（手動実行など）の保険: 一番近い枠に丸める。"""
    return min(range(len(SLOTS_UTC)), key=lambda i: abs(SLOTS_UTC[i] - hour))


def main():
    if not os.environ.get("IG_ACCESS_TOKEN"):
        print("[FATAL] IG_ACCESS_TOKEN 未設定")
        sys.exit(1)

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        images = load_images()
        idx = pick_index(now, len(images))
        fname = images[idx]
        url = IMAGE_BASE + fname
        print(f"[start] {now.isoformat()} slot→idx={idx}/{len(images)} img={fname} "
              f"publish={'ON' if DO_PUBLISH else 'OFF(dry)'}")
        res = story_publish.post_story(url, DO_PUBLISH)
        print(f"[done] {res}")
    except story_publish.IGAuthError as e:
        msg = f"❌ ストーリー自動投稿：トークン認可エラー（要再発行）\n{e}"
        print(f"[FATAL] {msg}")
        notify_line(msg)
        sys.exit(1)
    except Exception as e:
        msg = f"❌ ストーリー自動投稿に失敗\n{type(e).__name__}: {e}"
        print(f"[FATAL] {msg}")
        notify_line(msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
