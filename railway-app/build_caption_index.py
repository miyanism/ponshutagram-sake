# -*- coding: utf-8 -*-
"""
build_caption_index.py — caption_index.json 再生成

IG/TikTok コメント返信（reply_comments.py / tiktok_line_bot.py）が、
投稿を特定して文脈・トーン・ファクト根拠を引くための索引を作る。
新しいリール/動画が増えたら、これを実行して索引を更新する。

走査する3ソース（source 付きで統合）:
  1. claude.scenario/output/{YYYYMMDD}_{テーマ}/         → source="scenario"（文化・主張系）
       caption: c3_sns_captions_*.md の Instagram 節
       note   : c3_note_*.md（無ければ c2 のシナリオ本文で代替）
       slide_text: c3_slide_*.md
  2. claude.short/scenarios/{蔵}_{銘柄}_{日付}.md（または同名フォルダ内の.md）→ source="short"（銘柄紹介）
       caption: ### Instagram 節 / facts: ファクトチェック表 / script: PART1 動画台本
  3. claude.movie/input/                                  → 名前で判定
       ^\\d{8}_…  → scenario（output と同名は output 優先）
       …_\\d{8}$  → short    （short/scenarios と同名は short 優先）

出力: 同ディレクトリの caption_index.json（全エントリを作り直す）

注: Railway には動画フォルダ本体が無いので、生成はローカルで行い、
    caption_index.json だけを railway-app にコミットしてデプロイする。
"""
import os
import re
import json

HOME = os.path.expanduser("~")
SCENARIO_OUT = os.path.join(HOME, "claude.scenario", "output")
SHORT_DIR = os.path.join(HOME, "claude.short", "scenarios")
MOVIE_IN = os.path.join(HOME, "claude.movie", "input")
OUT_PATH = os.path.join(os.path.dirname(__file__), "caption_index.json")

# フィールド長の上限（索引肥大化を防ぐ）
CAP = {"caption": 2500, "facts": 2500, "script": 3500, "slide_text": 3500, "note": 4500}

DATE_PREFIX = re.compile(r"^\d{8}_")
DATE_SUFFIX = re.compile(r"_\d{8}$")


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def normalize(text: str) -> str:
    """reply_comments.normalize と同じ正規化（ファジー照合用）。"""
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    joined = "".join(lines)
    joined = re.sub(r"@[A-Za-z0-9_.]+", "", joined)
    joined = re.sub(r"\s+", "", joined)
    for ch in ("—", "ー", "〜", "～", "←", "→", "‥", "…"):
        joined = joined.replace(ch, "")
    return joined


def section(md: str, header_pat: str) -> str:
    """見出し header_pat にマッチする節の本文を、次の同/上位レベル見出しまで抜き出す。"""
    lines = md.splitlines()
    out, capturing, start_level = [], False, 99
    for ln in lines:
        m = re.match(r"^(#{1,6})\s*(.*)$", ln)
        if m:
            level = len(m.group(1))
            title = m.group(2)
            if not capturing and re.search(header_pat, title):
                capturing, start_level = True, level
                continue
            if capturing and level <= start_level:
                break
        if capturing:
            out.append(ln)
    return "\n".join(out).strip()


def first_codeblock(text: str) -> str:
    m = re.search(r"```.*?\n(.*?)```", text, re.S)
    return m.group(1).strip() if m else text.strip()


def cap(field: str, value: str) -> str:
    value = (value or "").strip()
    n = CAP.get(field)
    return value[:n] if n else value


def theme_from_scenario(folder: str) -> str:
    return folder.split("_", 1)[1] if "_" in folder else folder


def theme_from_short(stem: str) -> str:
    t = DATE_SUFFIX.sub("", stem)
    return t.replace("_", " ")


def glob_one(dirpath: str, prefix: str):
    """dir 内の prefix で始まる .md の最初の1つを返す。"""
    try:
        for name in sorted(os.listdir(dirpath)):
            if name.startswith(prefix) and name.endswith(".md"):
                return os.path.join(dirpath, name)
    except Exception:
        pass
    return None


def build_scenario_entry(folder: str, dirpath: str):
    """scenario フォルダ（c1/c2/c3 を含む）から1エントリ。投稿物が無ければ None。"""
    cap_file = glob_one(dirpath, "c3_sns_captions_")
    slide_file = glob_one(dirpath, "c3_slide_")
    note_file = glob_one(dirpath, "c3_note_")
    c2_file = glob_one(dirpath, "c2_")
    c1_file = glob_one(dirpath, "c1_")
    if not any([cap_file, slide_file, note_file, c2_file, c1_file]):
        return None

    caption = section(read(cap_file), r"Instagram") if cap_file else ""
    slide_text = read(slide_file) if slide_file else ""
    note = read(note_file) if note_file else ""
    if not note and c2_file:
        # c3_note が無い投稿は、c2 のシナリオ本文を文脈源に代替（冷やの正体 等）
        c2 = read(c2_file)
        note = section(c2, r"シナリオ") or c2
    return {
        "source": "scenario",
        "folder": folder,
        "theme": theme_from_scenario(folder),
        "caption": cap("caption", caption),
        "caption_norm": normalize(caption),
        "slide_text": cap("slide_text", slide_text),
        "note": cap("note", note),
        "facts": "",
        "script": "",
    }


def build_short_entry(stem: str, md_path: str):
    """銘柄紹介の短冊MDから1エントリ。"""
    md = read(md_path)
    if not md.strip():
        return None
    caption = section(md, r"Instagram")
    facts = section(md, r"ファクトチェック")
    script = section(md, r"PART\s*1")
    if script:
        script = first_codeblock(script)
    return {
        "source": "short",
        "folder": stem,
        "theme": theme_from_short(stem),
        "caption": cap("caption", caption),
        "caption_norm": normalize(caption),
        "slide_text": "",
        "note": "",
        "facts": cap("facts", facts),
        "script": cap("script", script),
    }


def main():
    by_key = {}   # (source, folder) -> entry ；先に入れた方を優先（output/short 優先）

    def add(entry):
        if not entry:
            return
        key = (entry["source"], entry["folder"])
        if key not in by_key:
            by_key[key] = entry

    # 1) scenario/output（最優先）
    if os.path.isdir(SCENARIO_OUT):
        for folder in sorted(os.listdir(SCENARIO_OUT)):
            dirpath = os.path.join(SCENARIO_OUT, folder)
            if os.path.isdir(dirpath) and DATE_PREFIX.match(folder):
                add(build_scenario_entry(folder, dirpath))

    # 2) short/scenarios（.md 直置き or 同名フォルダ内）
    if os.path.isdir(SHORT_DIR):
        for name in sorted(os.listdir(SHORT_DIR)):
            path = os.path.join(SHORT_DIR, name)
            if os.path.isfile(path) and name.endswith(".md"):
                add(build_short_entry(name[:-3], path))
            elif os.path.isdir(path):
                inner = os.path.join(path, name + ".md")
                if os.path.isfile(inner):
                    add(build_short_entry(name, inner))

    # 3) movie/input（名前で scenario/short を判定。同名は上の優先が残る）
    if os.path.isdir(MOVIE_IN):
        for name in sorted(os.listdir(MOVIE_IN)):
            path = os.path.join(MOVIE_IN, name)
            if not os.path.isdir(path):
                continue
            if DATE_PREFIX.match(name):
                add(build_scenario_entry(name, path))
            elif DATE_SUFFIX.search(name):
                inner = os.path.join(path, name + ".md")
                if os.path.isfile(inner):
                    add(build_short_entry(name, inner))

    entries = list(by_key.values())
    entries.sort(key=lambda e: (e["source"], e["folder"]))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)

    n_sc = sum(1 for e in entries if e["source"] == "scenario")
    n_sh = sum(1 for e in entries if e["source"] == "short")
    print(f"[done] {len(entries)} entries -> {OUT_PATH}")
    print(f"       scenario={n_sc}  short={n_sh}")
    # 直近の追加分が入っているか軽く可視化
    recent = [e["folder"] for e in entries
              if any(k in e["folder"] for k in ("冷や", "果実", "紀土", "日本酒度", "磨き", "かたの桜", "みむろ", "仙介"))]
    if recent:
        print("       recent hits:", "; ".join(recent))


if __name__ == "__main__":
    main()
