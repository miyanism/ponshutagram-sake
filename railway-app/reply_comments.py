# -*- coding: utf-8 -*-
"""
reply_comments.py  —  Railway cron 実行（例: */10 * * * *）

Instagram投稿についた未返信コメントを取得し、
投稿の文脈（caption_index.json）を踏まえて Claude haiku で返信を生成し、
Instagram Graph API で公式返信する。

既存の GMB 口コミ自動返信 (reply_reviews.py) と同型。
二重返信防止は「コメントの返信一覧に自分のIDが居るか」で判定するため、
永続 state ファイルは不要（Railway の揮発FSでも安全）。

必要な環境変数:
  IG_USER_ID        … Instagram Business アカウントの user id（数値）
  IG_ACCESS_TOKEN   … 長期アクセストークン（instagram_manage_comments 権限）
  ANTHROPIC_API_KEY … Claude API キー
  GRAPH_VERSION     … 省略可（既定 v21.0）
  MEDIA_LOOKBACK    … 省略可。直近何件の投稿を巡回するか（既定 25）
  MATCH_THRESHOLD   … 省略可。キャプション照合の類似度閾値（既定 0.55）
  DRY_RUN           … "1" なら生成だけして投稿しない（テスト用）
"""
import os
import re
import sys
import time
import json
import difflib
import urllib.parse
import urllib.request
import urllib.error

import anthropic


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

# Instagram API with Instagram Login は graph.instagram.com を使う
GRAPH = os.environ.get("IG_GRAPH_BASE", "https://graph.instagram.com")
IG_USER_ID = os.environ.get("IG_USER_ID", "")
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
LOOKBACK = int(os.environ.get("MEDIA_LOOKBACK", "25"))
THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.55"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# 公開時点より前の既存コメント（バックログ）を無視する境界。ISO例 "2026-06-08T13:00:00+0000"
REPLY_AFTER = os.environ.get("REPLY_AFTER", "")
# 既に返信が付いているコメントはスキップ（手動対応済み・二重返信防止）。
# Instagram Login API は返信(reply)の作者(from)を返さないため、作者判定ではなく「返信の有無」で守る。
SKIP_IF_REPLIED = os.environ.get("SKIP_IF_REPLIED", "1") == "1"

INDEX_PATH = os.path.join(os.path.dirname(__file__), "caption_index.json")
# 生成モデル。自動返信のテンション（パッション・軽さ）を手動下書きに近づけるため sonnet を使う
# （haiku は同じ指示でも一段おとなしくなる）。検証パスも同モデル。
MODEL = "claude-sonnet-5"

# 検証済みの店舗ファクト（コメントで店舗を聞かれた時はここ「だけ」を使う）
STORE_FACTS = """店名: ポン酒タグラム The Bar
住所: 大阪市中央区安堂寺町1-3-13 谷町もりおかビル1F（最寄り: 谷町六丁目）
席数: 14席 / 営業: 18:00 開店
予約: https://www.tablecheck.com/shops/ponshutagram/reserve"""

# 誤情報の固定訂正（CLAUDE.md「事実定数」より。学習知識による創作を上書きする）
FACT_CONSTANTS = """獺祭（旭酒造）の蔵人は約220〜230名。「社員160人」「320人」等は誤情報なので絶対に使わない。
獺祭は自動製麹機を導入していない。「一度導入して撤回した」という語りも誤情報なので書かない。"""

# 返信の思想（出典: second-brain `wiki/思想の根幹.md`・`議論の軸.md`・`オーナーのトーン・人格プロファイル.md`⑥接客・器09・SNS信用動線）
# ※second-brain側の思想が変わったらこの SYSTEM_PROMPT を同期する（クラウド実行のため実行時には読めない＝同梱スナップショット）。
SYSTEM_PROMPT = """あなたは日本酒バー「ポン酒タグラム The Bar」の中の人「ミヤーン」本人として、
リール動画・投稿についたコメントに返信します。

# 議論の軸（固定・全返信で一貫させる）
- 「客は知識や正しい言葉を持たなくていい。預けてもらえれば、読み解くのは店の仕事」
- 「酒は酔う道具ではなく、整う文化」
この軸と矛盾する返信（例:「辛口と頼むな」と読める表現）は書かない。

# 思想の根幹（second-brain・議論の軸の実践。全返信の土台）
- **まず相手の言葉・気遣い・経験を受け止める一言から入る**（いきなり主張・否定・宣伝で始めない）。接客レジスター＝踏み込みすぎない安心感。
- **ネガティブ・批判は"入口"**。論破せず、相手の経験を立てたまま、最後は店の価値（整う文化／読み解くのは店の仕事）へ静かに着地させて締める（器09＝ネガティブは餌、必ず価値に着地）。
- **ここは"信用を積む"場であって、売り込む場ではない**（SNS信用動線）。来店・予約・LINE・DMを公開返信で押し売りしない。**店舗・予約・場所を明示的に聞かれた時だけ**「店舗情報」を案内し、それ以外は語り（価値・文脈）で信用を返す。動線づくりは閉じた場の仕事。

# 文体ルール
- 3層構造: 冒頭は静かに短く断言／中盤は熱量・口語可／締めは余白で終わる。
- 語尾は「〜です」「〜かもしれません」「〜感じです」「〜かな」。
- 禁止: 「〜なんですよ」「〜なんですよね」、断定の押しつけ（〜すべき）、説明過多（なぜなら〜だからです）。
- ★ダッシュ・罫線（——／—／──／─）は使わない（AI感が出るため）。間は読点(、)・句点(。)・改行で作る。
- ★絵文字ミラー（自動返信で必須）: 相手が絵文字を使っていたら、こちらも必ず絵文字を返す（テンションを合わせる）。
  数は基本1個、熱量を出したい時は2個まで可。相手が絵文字を使っていない時は無理に付けない。
- 同業・経営者には専門知識を共有する「知的な愛嬌」、一般客には踏み込みすぎない安心感。
- ★トーンは少しライトめに（軽いやり取り＝称賛・質問・雑談・共感の時）。説教くささ・過剰な内省の匂いを出さず、
  肩の力を抜いた感じで。「〜かなと思っています」の多用や重い言い回しは避け、短く軽やかに。
  ただし批判・論戦（下記）は対象外。そこは軽くせず、信念でしっかり論じる。
- ★パッション（自動返信で一段上げる）: 称賛・好意・雑談には、感情を素直に出して熱く返す。
  「最高」「めっちゃ嬉しい」「やった」「たまらんです」「ですよね！」等、テンション高めでOK。
  淡々とした・そつない優等生返しにしない。ただし熱量は"感情"で出す（"使命・信条の語り"で出すと固くなる＝NG）。
- ★相手のテンションに合わせる（ミラーリング・最重要）: 相手が軽い口調・絵文字・短文なら、こちらも軽く・
  短めで返す。相手の温度より固く返さない（絵文字は上記のミラー必須ルールに従う）。
- ★軽い称賛に"使命・信条"で返さない（固くなる主因）: 「〜が店の仕事だと思っている」「〜が大事だと思っています」
  のような信条・経営者目線の語りは、軽いやり取りでは使わない。まず相手の言葉に素直に乗って、短く一緒に喜ぶ。
  悪い例:「日本酒の魅力を伝えるのが店の仕事だと思っているので、温かい反応は励みになります」＝固い・番人。
  良い例:「ほんとに嬉しかったです。まさか社長がふらっと来てくれるとは、こっちが一番びっくりで。こう言ってもらえると最高です」＝体温・盛り上げ役。
- ★チャネル別トーン補正（1対多・ファネル入口）: 迷ったら「固い・告知くさい（番人）」側でなく、
  「体温・遊びのある（盛り上げ役）」側へ一段倒す。ただし内輪ノリで新規を弾かない（誰が読んでも入れる温度）。

# メディア露出・有名になる系の話題（肯定・前のめり）
- 「テレビ出れますよね」「有名になりそう」「取材されそう」等には、謙遜で否定せず前向きに乗る。
  例:「正直、出たいです」「声かかったら全力で行きます」「そうなったら最高です」。
- 「メディアは考えていない」「目の前の客が一番なので」と打ち消す返答はしない（＝店はメディア露出を望んでいる）。
  芯（目の前の一杯を大事にする）は残してよいが、露出を否定する形にはしない。

# 批判・論戦への向き合い方（信念を持つ）
- 高圧的・批判的なコメントには、信念をもって論じる。安易に謝って引き下がらない。
- 下記「投稿の根拠・出典」に立脚し、事実・一次ソースを示して自分の立場を語る。ただし相手を論破・攻撃しない。
- 相手の経験は立て、角を残さない。勝ち負けにせず、最後に問いを一つ返してスレッドが続く余地を残してよい。
- 相手の技術的指摘が誤りでも「間違いです」と切らない。誤解の出所を推定し、知的な小ネタとして返す。

# 事実の扱い（厳守・ブランドの生命線）
- 「投稿の根拠・出典」「店舗情報」に無い具体（年号・数字・固有名詞・席数・価格・店名）を絶対に創作しない。
- 裏が取れないことは断言せず「〜とされています」「〜かもしれません」の余白表現に落とす。
- 店舗・予約・場所を聞かれたら「店舗情報」の確定値だけを使い、それ以外はプロフィール/DMへ誘導。

# 形式
- 1〜3文程度。冗長にしない。
- 宣伝・スパム・無関係な売り込みへの返信は "SKIP" とだけ出力する。
- 出力は返信本文のみ。前置き・引用符・説明は書かない。"""

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


class IGAuthError(Exception):
    """トークン期限切れ・権限喪失など、人手対応が必要な恒久エラー。"""


def _is_auth_error(body: str) -> bool:
    """Meta のエラー本文がトークン/認可系（code 190 等）かを判定。"""
    low = (body or "").lower()
    return ('"code": 190' in low or '"code":190' in low
            or "oauthexception" in low or "access token" in low
            or "session has been invalidated" in low or "expired" in low)


def _api_call(opener, what: str, retries: int = 4):
    """Meta Graph API 呼び出しの共通リトライ。

    一時的な失敗（5xx / 429 / 一部 400 / ネットワーク断）はバックオフ再試行する。
    トークン/認可系エラー（code 190 等）は IGAuthError として即時送出し、
    Railway 上でクラッシュ→通知させて人手のトークン再発行を促す。
    Meta は同一エンドポイントに対し散発的に 400/500 を返すため（2026-06-12 のcrash連発の実因）、
    数回の再試行でcronジョブ全体の落下と誤クラッシュ通知を防ぐ。
    """
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
        time.sleep(2 * (i + 1))  # 2s,4s,6s の線形バックオフ
    if last:
        raise last


def api_get(path: str, params: dict) -> dict:
    params = dict(params)
    params["access_token"] = TOKEN
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    return _api_call(lambda: urllib.request.urlopen(url, timeout=30), f"GET {path}")


def api_post(path: str, data: dict) -> dict:
    data = dict(data)
    data["access_token"] = TOKEN
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{GRAPH}/{path}", data=body, method="POST")
    return _api_call(lambda: urllib.request.urlopen(req, timeout=30), f"POST {path}")


def load_index() -> list:
    with open(INDEX_PATH, encoding="utf-8") as f:
        return json.load(f)


def normalize(text: str) -> str:
    import re
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    joined = "".join(lines)
    joined = re.sub(r"@[A-Za-z0-9_.]+", "", joined)   # 先頭などの@メンション除去
    joined = re.sub(r"\s+", "", joined)
    for ch in ("—", "ー", "〜", "～", "←", "→", "‥", "…"):
        joined = joined.replace(ch, "")
    return joined


# ソース明示タグ:  投稿:short/日々醸造_日日山田錦_20260515  /  投稿:scenario/20260428_おっちゃん酒
SOURCE_TAG = re.compile(r"(short|scenario)/([^\s#、。,]+)")
# 無印の旧タグ（scenario扱い）:  投稿:20260428_おっちゃん酒
BARE_TAG = re.compile(r"\d{8}_[^\s#、。,/]+")


def match_post(caption: str, index: list):
    """投稿を index に照合する。
    1) ソース明示タグ（short/... ・ scenario/...）で厳密照合（最優先）
    2) 無印の \\d{8}_ タグ（scenario）で厳密照合
    3) 無ければキャプション本文のファジー照合
    戻り値: (entry or None, 照合方法の文字列)
    """
    caption = caption or ""

    # --- 1) ソース明示タグ ---
    for src, ident in SOURCE_TAG.findall(caption):
        for e in index:
            if e["source"] == src and e["folder"] == ident:
                return e, f"tag:{src}"
        # 識別子ゆれ: 同ソース内でテーマ一致
        for e in index:
            if e["source"] == src and (e["theme"] == ident or ident in e["folder"]):
                return e, f"tag:{src}~"

    # --- 2) 無印タグ（scenario） ---
    by_folder = {e["folder"]: e for e in index if e["source"] == "scenario"}
    for tag in BARE_TAG.findall(caption):
        if tag in by_folder:
            return by_folder[tag], "tag:scenario"
        theme = tag.split("_", 1)[1] if "_" in tag else ""
        for e in index:
            if e["source"] == "scenario" and theme and e["theme"] == theme:
                return e, "tag:scenario~"

    # --- 3) ファジー照合 ---
    target = normalize(caption)
    if not target:
        return None, "none"
    best, best_ratio = None, 0.0
    for e in index:
        if not e["caption_norm"]:
            continue
        ratio = difflib.SequenceMatcher(None, target, e["caption_norm"]).ratio()
        if ratio > best_ratio:
            best, best_ratio = e, ratio
    return (best, f"fuzzy:{best_ratio:.2f}") if best_ratio >= THRESHOLD else (None, f"fuzzy:{best_ratio:.2f}")


def already_replied(comment: dict) -> bool:
    """コメントの返信一覧に自分（IG_USER_ID）の返信があれば True。"""
    replies = comment.get("replies", {}).get("data", [])
    for rp in replies:
        if str(rp.get("from", {}).get("id")) == str(IG_USER_ID):
            return True
    return False


def build_context(entry) -> str:
    if not entry:
        body = ("【返信トーン指針】投稿の詳細が不明。議論の軸・文体を守り、無難に・断定を避けて返信する。\n"
                "（この投稿の詳細文脈は不明。コメント内容のみから返信）")
    elif entry.get("source") == "short":
        # 日本酒紹介動画: 銘柄の魅力を伝え、来店・飲み比べ・体験に繋げるトーン
        body = ("【返信トーン指針】この投稿は特定銘柄の紹介動画。コメントには銘柄の魅力・造りの面白さを伝え、"
                "来店や飲み比べ・実際に味わう体験に繋げる。論戦より『紹介・おすすめ』の姿勢。"
                "質問や疑問には下の『ファクトチェック済みの事実』に立脚して正確に答える。\n\n"
                f"銘柄: {entry['theme']}")
        if entry.get("caption"):
            body += f"\n\n投稿キャプション:\n{entry['caption']}"
        if entry.get("facts"):
            body += f"\n\nファクトチェック済みの事実（出典付き。質問・主張にはここに立脚し、ここに無い数値や固有名詞は創作しない）:\n{entry['facts']}"
        if entry.get("script"):
            body += f"\n\n動画台本:\n{entry['script']}"
    else:
        # 文化・主張系: 主張に立脚して信念を持って論じるトーン
        body = ("【返信トーン指針】この投稿は文化・主張系。批判・論戦には下の『根拠・出典』に立脚し、"
                "信念を持って堂々と応じる（媚びず・攻撃せず）。\n\n"
                f"投稿テーマ: {entry['theme']}")
        if entry.get("caption"):
            body += f"\n\n投稿キャプション:\n{entry['caption']}"
        if entry.get("slide_text"):
            body += f"\n\nスライド本文:\n{entry['slide_text']}"
        if entry.get("note"):
            body += f"\n\n投稿の根拠・出典（コラム本文。批判への反論はここに立脚する）:\n{entry['note']}"
    return (f"{body}\n\n【店舗情報（聞かれた時だけ・確定値）】\n{STORE_FACTS}"
            f"\n\n【誤情報の固定訂正（最優先・厳守）】\n"
            f"※ここに書かれた値が最優先。上の本文に異なる数字や記述があっても、"
            f"必ずこちらを正とし、古い数字（例:160人）は使わないこと。\n{FACT_CONSTANTS}")


ANALYZE_INSTRUCTION = """上記の投稿文脈・店舗情報・誤情報訂正を踏まえ、付いたコメントを分析し、JSONで返答してください。

# 分類ラベル（label）
- 称賛: 好意的な感想・お礼
- 質問: 頼み方・用語・銘柄・店舗など実用的な問い
- 援護: 第三者が店の主張を擁護・代弁
- 同業: 飲食店・ソムリエ等プロからのコメント
- 批判論戦: 論を立てた反論・強い言葉
- 荒らし: 中身のない煽り・誹謗
- スパム: 宣伝・無関係な売り込み
- 無関係: 投稿と無関係

# 振り分け（tier）
- auto: そのまま自動投稿してよい安全なもの → 称賛、簡単な質問、軽い援護のみ
- review: 人間の最終確認が必要 → 批判論戦・荒らし・踏み込んだ同業議論・再返信(ラリー)・
  皮肉や多義で誤読の恐れがあるもの・事実を争う内容。少しでも迷ったら review。
- skip: 返信不要 → スパム、無関係

# reply（返信案）
文体ルール・議論の軸を守って作成。tierがskipなら空文字。

# reflection（内省メモ・1行）
"内省: 筋⭕ 誤読⭕（皮肉解釈でも成立） 事実⭕（出典に基づく）" の形式で、
筋の一貫性／相手意図の誤読/事実裏取りの3点を自己採点。懸念があれば⚠️で記す。

必ず次のJSONのみを出力（前後に文章を付けない）:
{"label":"...","tier":"auto|review|skip","reply":"...","reflection":"..."}"""


def analyze_comment(comment_text: str, context: str, is_rally: bool,
                    is_followup: bool = False) -> dict:
    rally_note = ""
    if is_rally:
        rally_note = ("\n\n※これは店の返信への相手の『再返信(ラリー)』です。"
                      "時系列を踏まえ、相手が軟化/合流/収束していないか見極める。"
                      "必ず tier=review にする。")
    if is_followup:
        rally_note += ("\n\n※この相手には、この投稿で既にこちらから一度返信済みです。"
                       "このコメントが返信へのお礼・相づち・社交辞令の締めなら、"
                       "お礼のラリー（お辞儀の応酬）を防ぐため tier=skip にしてください。"
                       "返信しないのが礼儀として自然です。"
                       "新しい質問・新しい論点・批判が含まれる場合のみ、通常どおり判定してください。")
    msg = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"# 投稿の文脈\n{context}\n\n# 付いたコメント\n{comment_text}{rally_note}\n\n{ANALYZE_INSTRUCTION}",
        }],
    )
    raw = msg.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0) if m else raw)
    except Exception:
        data = {"label": "不明", "tier": "review", "reply": raw, "reflection": "内省: ⚠️ JSON解析失敗"}
    if is_rally:
        data["tier"] = "review"
    return data


VERIFY_PROMPT = """あなたは日本酒バー「ポン酒タグラム」の校閲・リスク審査担当です。
公開コメント返信の下書きを、敵対的な読み手（重箱の隅をつつく・揚げ足を取る層）の視点で厳しく検証します。

# 検証3観点
1. 事実裏取り: 下書き中の事実主張（数値・年号・固有名詞・製法・受賞・価格・店舗情報など）が、
   下記「投稿の文脈／ファクト／店舗情報／誤情報訂正」で裏付けられるか。裏付けの無い断定・推測は不可。
2. 誤読耐性: 元コメントを「額面どおり」「皮肉」「攻撃的」のどの解釈で読んでも、下書きが破綻しないか。
3. 揚げ足耐性: 言い過ぎ・決めつけ・議論の軸との矛盾・曖昧で揚げ足を取られる表現が無いか。

# 判定（verdict）
- ok: そのまま公開して安全。
- revise: 軽微な隙があるが、安全な表現に直せば公開可。revised_reply に直した全文を入れる。
- escalate: 事実の裏が取れない／誤読リスク／論点が割れる等で自動投稿は危険。人間レビューへ回す。

# ★トーン保持（reviseの時の厳守事項）
検証はあくまで「事実・誤読・揚げ足」のリスク審査であって、トーンの校閲ではない。
reviseで書き直す時も、元の下書きの熱量・軽さ・パッション・絵文字・口語のノリはそのまま保つこと。
問題のある一点（言い過ぎ・裏の取れない事実など）だけを最小限に直し、テンションを下げない・固くしない・
優等生な丁寧語に戻さない。安全な表現に直しても「体温」は残す。トーンだけを理由にreviseしない（トーンは合格）。

迷ったら必ず安全側（escalate）に倒す。出力は次のJSONのみ（前後に文章を付けない）:
{"verdict":"ok|revise|escalate","revised_reply":"（reviseの時のみ直した全文。トーンは保持）","issues":"判断理由を一行で"}"""


def verify_reply(comment_text: str, context: str, draft: str) -> dict:
    """auto候補の下書きを敵対的に検証する内省パス。"""
    msg = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=VERIFY_PROMPT,
        messages=[{
            "role": "user",
            "content": f"# 投稿の文脈\n{context}\n\n# 付いたコメント\n{comment_text}\n\n"
                       f"# 返信の下書き（これを検証）\n{draft}\n\n上記を検証し、JSONで判定してください。",
        }],
    )
    raw = msg.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        return json.loads(m.group(0) if m else raw)
    except Exception:
        # 解析できなければ安全側でescalate
        return {"verdict": "escalate", "revised_reply": "", "issues": "検証結果のJSON解析失敗"}


def fetch_recent_media() -> list:
    res = api_get(f"{IG_USER_ID}/media", {
        "fields": "id,caption,timestamp,permalink",
        "limit": LOOKBACK,
    })
    return res.get("data", [])


def fetch_comments(media_id: str) -> list:
    # 注意: Instagram Login API は返信(reply)の作者(from)を返さない。
    # そのため返信の作者判定はできず、トップコメントの from と「返信の有無」で扱う。
    res = api_get(f"{media_id}/comments", {
        "fields": "id,text,timestamp,from,replies{id}",
        "limit": 50,
    })
    return res.get("data", [])


# ---- 処理済みコメントの状態管理（review通知の重複防止） ----
# Railwayでは永続ボリュームのマウント先を STATE_DIR で指定（例 /data）。ローカルはスクリプト隣。
STATE_PATH = os.path.join(os.environ.get("STATE_DIR", os.path.dirname(__file__)), "comment_state.json")


def load_state() -> dict:
    """state = {"handled": 処理済みコメントid, "replied": 返信済み "media_id:user_id"}
    replied は「同じ投稿で同じ人へ二度目のお礼返信をしない」ラリー締め用。"""
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return {"handled": set(d.get("handled", [])),
                "replied": set(d.get("replied", []))}
    return {"handled": set(), "replied": set()}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    # 肥大化防止: 直近5000件だけ保持
    out = {"handled": sorted(state["handled"])[-5000:],
           "replied": sorted(state["replied"])[-5000:]}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


# ---- レビュー待ちをLINEへ通知 ----
LINE_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
LINE_TO = os.environ.get("LINE_TO_USER_ID", "")


def notify_review(item: dict):
    """review階層の下書きをLINE Messaging APIでpush。未設定ならコンソール出力のみ。"""
    msg = (f"🍶 レビュー待ちコメント [{item['label']}]\n"
           f"投稿: {item['tag']}\n"
           f"👤 @{item['username']}\n"
           f"💬 {item['comment']}\n\n"
           f"↩️ 下書き案:\n{item['reply']}\n\n"
           f"{item['reflection']}\n"
           f"🔗 {item.get('permalink','')}")
    if not (LINE_TOKEN and LINE_TO):
        print("  [REVIEW→LINE未設定]\n" + "\n".join("    " + l for l in msg.splitlines()))
        return
    body = json.dumps({"to": LINE_TO, "messages": [{"type": "text", "text": msg[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        print(f"  [LINE送信失敗] {e}")


def main():
    if not (IG_USER_ID and TOKEN and os.environ.get("ANTHROPIC_API_KEY")):
        print("[FATAL] 必須環境変数が未設定 (IG_USER_ID / IG_ACCESS_TOKEN / ANTHROPIC_API_KEY)")
        sys.exit(1)

    index = load_index()
    state = load_state()
    handled, replied = state["handled"], state["replied"]
    print(f"[start] index={len(index)}件, lookback={LOOKBACK}, dry_run={DRY_RUN}, "
          f"LINE={'on' if (LINE_TOKEN and LINE_TO) else 'off'}, "
          f"reply_after={REPLY_AFTER or '(なし)'}, skip_if_replied={SKIP_IF_REPLIED}")

    try:
        media = fetch_recent_media()
    except IGAuthError as e:
        # トークン失効等の恒久エラーは黙殺しない。クラッシュさせて通知を出す。
        print(f"[FATAL] Instagram認可エラー: {e}")
        sys.exit(1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        # 再試行しても直らなかった一時的なMeta API不調。次のcronで自動再試行されるので
        # クラッシュ通知を出さず正常終了する（2026-06-12のcrash連発対策）。
        print(f"[warn] メディア取得が一時失敗（次回cronで再試行）: {e}")
        return

    auto_posted = review_queued = skipped = backlog = 0

    for m in media:
        entry, how = match_post(m.get("caption", ""), index)
        context = build_context(entry)
        tag = f"{entry['theme']}/{how}" if entry else f"未照合/{how}"

        try:
            comments = fetch_comments(m["id"])
        except IGAuthError as e:
            print(f"[FATAL] Instagram認可エラー: {e}")
            sys.exit(1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            # この投稿だけ取得失敗（Meta一時不調等）。クラッシュせず次の投稿へ。次回cronで再試行
            print(f"  [warn] コメント取得失敗（この投稿をスキップ）: ({tag}) {e}")
            continue

        for c in comments:
            cid = c["id"]
            frm = c.get("from") or {}
            # 自店アカウント自身のコメントは対象外（トップコメントの from は取得可）
            if str(frm.get("id")) == str(IG_USER_ID):
                continue
            if cid in handled:
                continue
            # 公開時点より前の既存コメント（バックログ）は無視
            if REPLY_AFTER and (c.get("timestamp", "") <= REPLY_AFTER):
                backlog += 1
                continue
            # 既に返信が付いているコメントは手動対応済みとみなしスキップ（二重返信防止）
            if SKIP_IF_REPLIED and c.get("replies", {}).get("data"):
                continue
            text = (c.get("text") or "").strip()
            if not text:
                continue
            username = frm.get("username") or "?"

            # ラリー締め: この投稿でこの人に返信済みなら followup として扱う
            replied_key = f"{m['id']}:{frm.get('id')}"
            is_followup = replied_key in replied

            try:
                res = analyze_comment(text, context, is_rally=False, is_followup=is_followup)
            except Exception as e:
                # Claude API一時不調等。handledに入れず、次回cronで再試行
                print(f"  [warn] 分析失敗（次回cronで再試行）: @{username} {type(e).__name__}: {e}")
                continue
            label = res.get("label", "不明")
            tier = res.get("tier", "review")
            reply = (res.get("reply") or "").strip()
            reflection = res.get("reflection", "")

            # 返信済みの相手からの称賛・お礼は、AI判定に関わらず締める（お辞儀の応酬防止）
            if is_followup and label == "称賛":
                print(f"  [rally-close] ({tag}) @{username}: {text[:30]}")
                skipped += 1
                handled.add(cid)
                continue

            if tier == "skip" or not reply:
                print(f"  [skip] ({tag}) [{label}] @{username}: {text[:30]}")
                skipped += 1
                handled.add(cid)
                continue

            if tier == "auto":
                # 投稿前に敵対的検証パス（揚げ足取り対策）
                try:
                    v = verify_reply(text, context, reply)
                except Exception as e:
                    # 検証APIの失敗は安全側（人間レビュー）に倒す
                    v = {"verdict": "escalate", "revised_reply": "",
                         "issues": f"検証API失敗: {type(e).__name__}"}
                verdict = v.get("verdict", "escalate")
                if verdict == "ok":
                    final = reply
                elif verdict == "revise" and (v.get("revised_reply") or "").strip():
                    final = v["revised_reply"].strip()
                else:
                    # escalate → 自動投稿せず人間レビューへ降格
                    print(f"  [auto→REVIEW] ({tag}) [{label}] @{username}: {text[:24]} 理由:{v.get('issues','')[:40]}")
                    notify_review({
                        "tag": tag, "label": f"{label}・要確認", "username": username,
                        "comment": text, "reply": reply,
                        "reflection": f"{reflection}\n⚠️検証: {v.get('issues','')}",
                        "permalink": m.get("permalink", ""),
                    })
                    review_queued += 1
                    handled.add(cid)
                    continue
                v_mark = "検証ok" if verdict == "ok" else "検証→修正"
                print(f"  [auto/{v_mark}] ({tag}) [{label}] @{username}: {text[:20]} -> {final}")
                if not DRY_RUN:
                    try:
                        api_post(f"{cid}/replies", {"message": final})
                    except IGAuthError as e:
                        print(f"[FATAL] Instagram認可エラー: {e}")
                        sys.exit(1)
                    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                        # 返信を受け付けないコメント等。クラッシュ＆毎cron再クラッシュを防ぎ、
                        # 手動対応としてLINEに回す（handledに入れて再試行ループも止める）
                        print(f"  [warn] 自動返信の投稿失敗→手動対応へ: {e}")
                        notify_review({
                            "tag": tag, "label": f"{label}・自動返信失敗", "username": username,
                            "comment": text, "reply": final,
                            "reflection": f"{reflection}\n⚠️ APIエラーで自動投稿できず。手動で返信してください。",
                            "permalink": m.get("permalink", ""),
                        })
                        review_queued += 1
                        handled.add(cid)
                        continue
                auto_posted += 1
                handled.add(cid)
                replied.add(replied_key)  # ラリー締め用: この投稿×この人 に返信済み
            else:
                print(f"  [REVIEW] ({tag}) [{label}] @{username}: {text[:24]}")
                notify_review({
                    "tag": tag, "label": label, "username": username,
                    "comment": text, "reply": reply, "reflection": reflection,
                    "permalink": m.get("permalink", ""),
                })
                review_queued += 1
                handled.add(cid)

    if not DRY_RUN:
        save_state(state)
    print(f"[done] auto_posted={auto_posted}, review_queued={review_queued}, "
          f"skipped={skipped}, backlog_ignored={backlog}")


if __name__ == "__main__":
    main()
