# -*- coding: utf-8 -*-
"""
tiktok_line_bot.py — Railway Web サービス（常駐・$PORT で待受）

LINE に「投稿タグ＋コメント」を送ると、IG自動返信とまったく同じエンジン
（reply_comments.py の build_context / analyze_comment / verify_reply）で
返信案を生成して LINE に返す、TikTok 向け半自動ボット。

背景: TikTok には公式の organic コメントAPIが無い（取得が自動化できない）。
そこで owner が画面で見たコメントを LINE に転記 → 本ボットが投稿文脈
（caption_index.json）を踏まえて返信案を返す。生成ルール（ダッシュ禁止・
ファクト定数・段階分け・検証パス）は reply_comments.py をそのまま継承する。

入力フォーマット（owner が LINE に送るメッセージ）:
  1行目  … 投稿タグ（テーマ名 / short/... / scenario/... / 20260610_冷やの正体）
  2行目〜 … コメント本文
  ※1行で送るときは「タグ｜コメント」と全角／半角の縦棒で区切ってもOK

必要な環境変数:
  ANTHROPIC_API_KEY    … Claude API（reply_comments と共有）
  LINE_CHANNEL_TOKEN   … Messaging API チャネルアクセストークン（reply/push・既存と共有）
  LINE_CHANNEL_SECRET  … チャネルシークレット（Webhook署名検証用・新規）
  LINE_TO_USER_ID      … owner の userId（この人以外からの送信は無視＝悪用防止）
  PORT                 … Railway が自動注入（未設定なら 8080）
  BOT_VERIFY_PASS      … 省略可。"0" で敵対的検証パスを無効化（既定 "1"）
"""
import os
import io
import re
import sys
import json
import hmac
import base64
import hashlib
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 絵文字出力対策＋行バッファリング（Railwayログに即時反映させる）
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
except Exception:
    pass

# 既存の返信エンジンをそのまま再利用（import 時点では main() は走らない）
import reply_comments as eng

LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
OWNER = os.environ.get("LINE_TO_USER_ID", "")
VERIFY_PASS = os.environ.get("BOT_VERIFY_PASS", "1") != "0"
PORT = int(os.environ.get("PORT", "8080"))

HELP = (
    "🍶 TikTok返信案ボット\n"
    "1行目に投稿タグ、2行目以降にコメントを送ってください。返信案を返します。\n\n"
    "例:\n"
    "冷やの正体\n"
    "冷やで常温出てきたことないですよ\n\n"
    "タグは次のどれでもOK:\n"
    "・テーマ名（冷やの正体 / 日本酒度酸度 / 水 など）\n"
    "・short/平和酒造_紀土無量山純米雄町_20260614\n"
    "・scenario/20260610_冷やの正体\n"
    "・20260610_冷やの正体\n\n"
    "1行で送るときは ｜ で区切ってもOK:\n"
    "冷やの正体｜冷やで常温出てきたことないですよ\n\n"
    "🔁 議論の続き（ラリー）は、やり取りを時系列で貼る:\n"
    "獺祭第三号蔵\n"
    "相手: そこに美味しさの追求は無いのですね\n"
    "自分: 美味しさの追求って、どの段階の…\n"
    "相手: 世界の棚ではなく…\n"
    "→ 文脈を踏まえた次の返信案を返します。"
)

# ---- Webhook 再送（リトライ）対策の簡易メモ化 ----
_seen = set()
_seen_lock = threading.Lock()


# ============================ 返信案の生成 ============================

def parse_message(text: str):
    """owner のメッセージを (タグ, コメント) に分解する。"""
    text = (text or "").strip()
    # 明示セパレータ
    for sep in ("\n---\n", "\n／\n", "\n//\n", "\n|\n"):
        if sep in text:
            tag, comment = text.split(sep, 1)
            return tag.strip(), comment.strip()
    lines = [ln for ln in text.split("\n")]
    if len(lines) >= 2:
        return lines[0].strip(), "\n".join(lines[1:]).strip()
    # 1行のみ: 縦棒区切りを許容
    one = lines[0] if lines else ""
    for bar in ("｜", "|"):
        if bar in one:
            tag, comment = one.split(bar, 1)
            return tag.strip(), comment.strip()
    # タグ無し（コメントだけ）
    return "", one.strip()


def resolve_entry(tag_line: str, index: list):
    """タグ文字列を caption_index のエントリに解決する。(entry|None, how)。"""
    tag_line = (tag_line or "").strip()
    for lab in ("投稿:", "投稿：", "タグ:", "タグ：", "post:", "Post:"):
        if tag_line.startswith(lab):
            tag_line = tag_line[len(lab):].strip()
    if not tag_line:
        return None, "none"

    # 1) 明示タグ（short/... ・ scenario/... ・ 20260610_...）＋強いファジー（>=0.55）
    e, how = eng.match_post(tag_line, index)
    if e:
        return e, how

    # 2) テーマ／フォルダの完全一致
    for x in index:
        if x.get("theme") == tag_line or x.get("folder") == tag_line:
            return x, "name:exact"

    # 3) テーマ／フォルダの部分一致（空白無視）
    norm = tag_line.replace(" ", "").replace("　", "")
    cand = []
    for x in index:
        th = (x.get("theme") or "").replace(" ", "")
        fo = (x.get("folder") or "").replace(" ", "")
        if norm and (norm in th or th in norm or norm in fo or fo.endswith("_" + norm)):
            cand.append(x)
    if len(cand) == 1:
        return cand[0], "name:substr"
    if len(cand) > 1:
        # 複数該当は新しいフォルダ（日付プレフィックス降順）を優先
        cand.sort(key=lambda x: x.get("folder", ""), reverse=True)
        return cand[0], "name:substr*"
    return None, "none"


_TIER_DISP = {
    "auto": "🟢 そのまま投稿OK",
    "review": "🟡 要確認（手を入れて）",
    "skip": "⚪ 返信不要",
}

# ラリー形式の行頭ロール（相手:／自分:／店:／客: 半角・全角コロン両対応）
ROLE_RE = re.compile(r"^(相手|自分|店|客)\s*[:：]\s*(.*)$")


def parse_thread(body: str):
    """「相手:／自分:」形式のラリーを解析する。

    行頭にロールが付いた行で発言を区切り、無印の行は直前の発言の続きとして結合。
    「相手」「自分(店)」の両方が居て2発言以上あればラリーとみなし [(role, text), ...] を返す。
    該当しなければ None（＝通常の単発コメント扱い）。
    """
    turns = []
    cur = None
    for ln in body.split("\n"):
        m = ROLE_RE.match(ln.strip())
        if m:
            role = "自分" if m.group(1) in ("自分", "店") else "相手"
            cur = [role, m.group(2).strip()]
            turns.append(cur)
        elif cur is not None and ln.strip():
            cur[1] += "\n" + ln.strip()
    roles = {r for r, _ in turns}
    if len(turns) >= 2 and "自分" in roles and "相手" in roles:
        return [(r, t) for r, t in turns]
    return None


def generate(text: str) -> str:
    tag, comment = parse_message(text)
    if not comment:
        return "コメント本文が空でした。\n\n" + HELP

    index = eng.load_index()
    entry, how = resolve_entry(tag, index)
    context = eng.build_context(entry)
    theme_disp = entry["theme"] if entry else "未照合"

    # ラリー形式（相手:／自分:）なら、やり取り全体を文脈として渡す
    turns = parse_thread(comment)
    is_rally = turns is not None
    if is_rally:
        thread = "\n\n".join(
            f"{'店(自分)' if r == '自分' else '相手'}: {t}" for r, t in turns)
        comment_for_ai = (
            "以下は、店の返信に対する相手との一連のやり取りです（時系列・上が古い）。\n"
            "これまでの脈略を踏まえ、最後の相手コメントへの次の返信を作ってください。\n"
            "既に譲った点を蒸し返さない・既に答えた論点を初見のように扱わない・"
            "ラリーが平行線なら礼をもって締める判断も含めて。\n\n" + thread)
        if turns[-1][0] != "相手":
            comment_for_ai += "\n\n※注意: 最後の発言が店側です。相手の最新コメントの貼り忘れの可能性あり。"
        latest = next((t for r, t in reversed(turns) if r == "相手"), comment)
    else:
        comment_for_ai = comment
        latest = comment

    res = eng.analyze_comment(comment_for_ai, context, is_rally=is_rally)
    label = res.get("label", "不明")
    tier = res.get("tier", "review")
    reply = (res.get("reply") or "").strip()
    reflection = res.get("reflection", "")

    verify_note = ""
    if VERIFY_PASS and tier != "skip" and reply:
        v = eng.verify_reply(comment_for_ai, context, reply)
        verdict = v.get("verdict", "escalate")
        if verdict == "ok":
            verify_note = "✅ 検証ok"
        elif verdict == "revise" and (v.get("revised_reply") or "").strip():
            reply = v["revised_reply"].strip()
            verify_note = "✏️ 検証で微修正済み"
        else:
            verify_note = f"⚠️ 要注意: {v.get('issues', '')}".strip()

    lines = [
        f"🍶 返信案 [{label}/{_TIER_DISP.get(tier, tier)}]",
        f"投稿: {theme_disp}（{how}）",
    ]
    if is_rally:
        lines.append(f"🔁 ラリー: {len(turns)}発言の文脈を踏まえて生成")
    if not entry:
        lines.append("※索引に無い投稿のため一般トーンで生成。caption_index更新を推奨。")
    lines += [
        "",
        f"💬 {latest[:300]}",
        "",
        "↩️",
        reply if reply else "（返信不要と判断）",
    ]
    if reflection:
        lines += ["", reflection]
    if verify_note:
        lines.append(verify_note)
    return "\n".join(lines)


# ============================ LINE I/O ============================

def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_SECRET:
        return False
    mac = hmac.new(LINE_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")


def _line_post(endpoint: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.line.me/v2/bot/message/{endpoint}",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_TOKEN}"})
    urllib.request.urlopen(req, timeout=30)


def line_reply(reply_token: str, text: str):
    _line_post("reply", {"replyToken": reply_token,
                         "messages": [{"type": "text", "text": text[:4900]}]})


def line_push(text: str):
    if not OWNER:
        return
    _line_post("push", {"to": OWNER,
                        "messages": [{"type": "text", "text": text[:4900]}]})


def handle_event(ev: dict):
    if ev.get("type") != "message":
        return
    msg = ev.get("message", {})
    reply_token = ev.get("replyToken", "")
    uid = (ev.get("source") or {}).get("userId")
    # owner 以外は黙殺（悪用・誤爆防止）
    if OWNER and uid != OWNER:
        print(f"[ignore] non-owner uid={uid}")
        return
    if msg.get("type") != "text":
        _safe_reply(reply_token, "テキストで送ってください。\n\n" + HELP)
        return
    text = (msg.get("text") or "").strip()
    if text.lower() in ("help", "ヘルプ", "使い方", "?", "？"):
        _safe_reply(reply_token, HELP)
        return
    try:
        out = generate(text)
    except Exception as e:
        out = f"⚠️ 生成エラー: {type(e).__name__}: {e}"
        print(out)
    _safe_reply(reply_token, out)


def _safe_reply(reply_token: str, text: str):
    """reply token で返信。失敗したら push にフォールバック。"""
    try:
        line_reply(reply_token, text)
    except Exception as e:
        print(f"[reply fail] {e} -> push")
        try:
            line_push(text)
        except Exception as e2:
            print(f"[push fail] {e2}")


# ============================ HTTP ============================

class Handler(BaseHTTPRequestHandler):
    def _send(self, code=200, body=b"OK"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, b"tiktok-line-bot ok")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        if not verify_signature(body, self.headers.get("X-Line-Signature", "")):
            self._send(403, b"bad signature")
            return
        # LINE には即 200 を返し、生成はバックグラウンドで（reply token は数十秒有効）
        self._send(200, b"OK")
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return
        for ev in payload.get("events", []):
            wid = ev.get("webhookEventId") or (ev.get("message") or {}).get("id")
            with _seen_lock:
                if wid and wid in _seen:
                    continue
                if wid:
                    _seen.add(wid)
                    if len(_seen) > 2000:
                        _seen.clear()
            threading.Thread(target=handle_event, args=(ev,), daemon=True).start()

    def log_message(self, *a):
        pass


def main():
    print(f"[tiktok-line-bot] start port={PORT} verify_pass={VERIFY_PASS} "
          f"owner={'set' if OWNER else 'UNSET(全員許可)'} "
          f"secret={'set' if LINE_SECRET else 'MISSING(署名検証不可→全拒否)'} "
          f"token={'set' if LINE_TOKEN else 'MISSING'}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
