# -*- coding: utf-8 -*-
"""
ingest_ig_stories.py — IGストーリーズ自動投稿の月次画像を取り込む（仕入れ企画部→honten）

仕入れ企画部が生成した `_ig_prod\\bio_01..NN.jpg`（1080×1920 JPG）＋`bio_stories_manifest.csv`を、
honten の `docs/ig-stories/` にミラーし、`manifest.json` を CSV 順で再生成する。
story_rotate.py（Railway cron）はこの公開フォルダ（GitHub Pages）を巡回する。

★設計（SB承認のガードレール 2026-10-04）:
  (a) 既定は **dry-run＝差分表示のみ（書き込み無し）**。--apply で初めてローカルに反映。
  (b) **git commit/push は行わない**＝人が `git diff` を見て、オーナーの画像OK（コンタクトシート確認が先）
      が出てから手動でコミット＆push する。「見てから出す」を機械化しても外さない。
  内容（銘柄・枚数・コピー）は仕入れ企画部の月次マスターが正＝このツールは中身を決めない（ホスト役）。

使い方:
  # 差分だけ見る（何も書かない）＝まずこれ
  python ingest_ig_stories.py --src "C:\\Users\\user\\仕入れ企画部\\ストーリーズ量産_10月\\_ig_prod"
  # ローカルに反映（docs/ig-stories/ を差し替え＋manifest.json再生成・gitは触らない）
  python ingest_ig_stories.py --src "...\\_ig_prod" --apply
  # 反映後、人が差分を確認してから手動で:
  #   git add docs/ig-stories ; git commit -m "feat(ig-stories): 10月版へ差し替え" ; git push origin main
"""
import os
import re
import sys
import csv
import json
import shutil
import hashlib
import argparse
import datetime

# Windows の cp932 コンソールでも日本語の差分が化けないよう UTF-8 出力に揃える
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from PIL import Image
except Exception:
    Image = None  # 寸法チェックはスキップして警告

REPO = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(REPO, "docs", "ig-stories")
MANIFEST_JSON = os.path.join(DEST, "manifest.json")
CSV_NAME = "bio_stories_manifest.csv"
BIO_RE = re.compile(r"^bio_(\d{2,})\.jpg$", re.IGNORECASE)
EXPECT_W, EXPECT_H = 1080, 1920


def default_src() -> str:
    m = datetime.date.today().month
    return rf"C:\Users\user\仕入れ企画部\ストーリーズ量産_{m}月\_ig_prod"


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def list_bio(folder: str) -> list:
    """フォルダ内の bio_NN.jpg を番号順で返す（[(num, filename)...]）。"""
    out = []
    if not os.path.isdir(folder):
        return out
    for fn in os.listdir(folder):
        m = BIO_RE.match(fn)
        if m:
            out.append((int(m.group(1)), fn))
    out.sort(key=lambda t: t[0])
    return out


def read_csv_order(src: str):
    """CSV があれば (順序リスト[ascii_file], {ascii_file: jp_brand}) を返す。無ければ (None, {})。"""
    path = os.path.join(src, CSV_NAME)
    if not os.path.isfile(path):
        return None, {}
    order, brands = [], {}
    # BOM 有無どちらでも読めるよう utf-8-sig
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            fn = (row.get("ascii_file") or "").strip()
            if fn:
                order.append(fn)
                brands[fn] = (row.get("jp_brand") or "").strip()
    return order, brands


def validate(src: str, bios: list, csv_order):
    """検証。問題（errors）と注意（warns）のリストを返す。"""
    errors, warns = [], []
    if not bios:
        errors.append(f"src に bio_NN.jpg が1枚も無い: {src}")
        return errors, warns

    nums = [n for n, _ in bios]
    # 連番・欠番・重複
    expected = list(range(1, len(nums) + 1))
    if nums != expected:
        errors.append(f"連番が 1..{len(nums)} でない（欠番/飛び番/重複）: 実際={nums}")
    # ゼロ詰め桁の一貫性（bio_01 と bio_1 の混在など）
    widths = {len(re.match(BIO_RE, fn).group(1)) for _, fn in bios}
    if len(widths) > 1:
        warns.append(f"ゼロ詰め桁が不統一（{sorted(widths)}）。manifest順は番号で揃えるが命名統一推奨")

    # 寸法・形式
    for _, fn in bios:
        p = os.path.join(src, fn)
        if Image is None:
            warns.append("Pillow 未導入のため寸法チェックをスキップ（1080×1920 を目視確認）")
            break
        try:
            with Image.open(p) as im:
                if im.format != "JPEG":
                    errors.append(f"{fn}: JPEG でない（format={im.format}）")
                if im.size != (EXPECT_W, EXPECT_H):
                    errors.append(f"{fn}: 寸法が {im.size}（期待 {EXPECT_W}x{EXPECT_H}）")
        except Exception as e:
            errors.append(f"{fn}: 画像として開けない（{type(e).__name__}: {e}）")

    # CSV と実ファイルの突合
    if csv_order is not None:
        have = {fn for _, fn in bios}
        want = set(csv_order)
        miss = want - have
        extra = have - want
        if miss:
            errors.append(f"CSV にあるが src に無い: {sorted(miss)}")
        if extra:
            warns.append(f"src にあるが CSV に無い（manifestは番号順で出す）: {sorted(extra)}")
    else:
        warns.append(f"{CSV_NAME} が無い＝番号順で manifest を作る（銘柄サマリは出せない）")
    return errors, warns


def build_manifest_order(bios: list, csv_order):
    """manifest.json に書く images 配列の順序を決める（CSV順を優先、無ければ番号順）。"""
    by_num = [fn for _, fn in bios]
    if not csv_order:
        return by_num
    have = set(by_num)
    # CSV順のうち実在するものだけ＋CSV漏れを末尾に番号順で足す
    ordered = [fn for fn in csv_order if fn in have]
    ordered += [fn for fn in by_num if fn not in set(ordered)]
    return ordered


def current_manifest() -> list:
    if not os.path.isfile(MANIFEST_JSON):
        return []
    try:
        with open(MANIFEST_JSON, "r", encoding="utf-8") as f:
            return json.load(f).get("images", [])
    except Exception:
        return []


def print_diff(src: str, bios: list, new_order: list, brands: dict):
    """現行 docs/ig-stories と src の差分を表示（内容ハッシュまで比較）。"""
    cur = current_manifest()
    cur_set, new_set = set(cur), set(new_order)
    added = [fn for fn in new_order if fn not in cur_set]
    removed = [fn for fn in cur if fn not in new_set]
    common = [fn for fn in new_order if fn in cur_set]

    print(f"\n=== 差分（現行 {len(cur)}枚 → 新 {len(new_order)}枚）===")
    changed, same = [], []
    for fn in common:
        dp, sp = os.path.join(DEST, fn), os.path.join(src, fn)
        if os.path.isfile(dp) and os.path.isfile(sp):
            (changed if sha(dp) != sha(sp) else same).append(fn)
        else:
            changed.append(fn)
    print(f"  同名で中身が変わる : {len(changed)}枚  {changed if changed else ''}")
    print(f"  同名で中身も同じ   : {len(same)}枚")
    print(f"  追加（新規枠）     : {len(added)}枚  {added if added else ''}")
    print(f"  削除（落とす枠）   : {len(removed)}枚  {removed if removed else ''}")

    if brands:
        print("\n=== 新セットの銘柄（manifest順）===")
        for i, fn in enumerate(new_order, 1):
            print(f"  {i:2d}. {fn}  {brands.get(fn, '')}")


def apply_swap(src: str, bios: list, new_order: list):
    """docs/ig-stories の bio_*.jpg を全消し→src からコピー→manifest.json 再生成。gitは触らない。"""
    os.makedirs(DEST, exist_ok=True)
    # 既存の bio_NN.jpg のみ削除（test_*.jpg や manifest.json は残す）
    removed = 0
    for fn in os.listdir(DEST):
        if BIO_RE.match(fn):
            os.remove(os.path.join(DEST, fn))
            removed += 1
    # コピー
    for _, fn in bios:
        shutil.copy2(os.path.join(src, fn), os.path.join(DEST, fn))
    # manifest.json 再生成
    with open(MANIFEST_JSON, "w", encoding="utf-8") as f:
        json.dump({"images": new_order}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"\n[apply] docs/ig-stories: 旧{removed}枚削除 → 新{len(bios)}枚コピー → manifest.json再生成（{len(new_order)}件）")
    print("[apply] git は触っていません。次の手順:")
    print("        git -C \"%s\" add docs/ig-stories" % REPO)
    print("        git -C \"%s\" status   # 差分を目視確認（オーナー画像OK後）" % REPO)
    print('        git -C "%s" commit -m "feat(ig-stories): ○月版へ差し替え（NN枚）"' % REPO)
    print("        git -C \"%s\" push origin main" % REPO)


def main():
    ap = argparse.ArgumentParser(description="IGストーリーズ月次画像の取り込み（dry-run既定）")
    ap.add_argument("--src", default=default_src(),
                    help=r"仕入れ企画部の _ig_prod フォルダ（既定=当月 ストーリーズ量産_N月\_ig_prod）")
    ap.add_argument("--apply", action="store_true",
                    help="ローカルに反映（docs/ig-stories差し替え＋manifest再生成）。gitは触らない")
    args = ap.parse_args()

    src = args.src
    print(f"src  = {src}")
    print(f"dest = {DEST}")
    bios = list_bio(src)
    csv_order, brands = read_csv_order(src)

    errors, warns = validate(src, bios, csv_order)
    for w in warns:
        print(f"  [warn] {w}")
    if errors:
        print("\n[NG] 検証エラー（取り込み中止）:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    new_order = build_manifest_order(bios, csv_order)
    print_diff(src, bios, new_order, brands)

    if not args.apply:
        print("\n[dry-run] 書き込みしていません。問題なければ --apply を付けて再実行。")
        return
    apply_swap(src, bios, new_order)


if __name__ == "__main__":
    main()
