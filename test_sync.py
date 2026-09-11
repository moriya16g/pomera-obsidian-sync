#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pomera_sync as ps

ROOT = tempfile.mkdtemp()
P = os.path.join(ROOT, "sd")
V = os.path.join(ROOT, "vault")
S = os.path.join(ROOT, "state")

fails = []


def reset():
    for d in (P, V, S):
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)


def w(root, rel, text, enc="utf-8", nl="\n"):
    p = os.path.join(root, rel)
    ps.ensure_dir(os.path.dirname(p))
    with open(p, "wb") as f:
        f.write(text.replace("\n", nl).encode(enc))
    return p


def r(root, rel, enc="utf-8"):
    p = os.path.join(root, rel)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return f.read().decode(enc)


def rb(root, rel):
    p = os.path.join(root, rel)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return f.read()


def sync(extra=None):
    argv = [P, "--vault", V, "--state", S]
    if extra:
        argv += extra
    ps.main(argv)


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        fails.append(name)


def touch_newer(path):
    t = time.time() + 10
    os.utime(path, (t, t))


# ------------------------------------------------------------------ tests

print("\n[1] 新規ファイルの双方向コピーと拡張子変換")
reset()
w(P, "novel.txt", "第一章\nはじまり\n")
w(V, "idea.md", "# メモ\n")
w(P, "photo.jpg", "BINARYDATA")
sync()
check("txt -> md", r(V, "novel.md") == "第一章\nはじまり\n", repr(r(V, "novel.md")))
check("md -> txt", r(P, "idea.txt") == "# メモ\n", repr(r(P, "idea.txt")))
check("jpg は拡張子そのまま", rb(V, "photo.jpg") == b"BINARYDATA")
check("jpg が md にならない", not os.path.exists(os.path.join(V, "photo.md")))

print("\n[2] 片側だけ変更 → もう一方へ反映")
w(V, "novel.md", "第一章\nはじまり\nつづき\n")
sync()
check("vault の変更が ポメラ に", r(P, "novel.txt") == "第一章\nはじまり\nつづき\n",
      repr(r(P, "novel.txt")))

print("\n[3] 両側変更・別の場所 → 3-way マージ")
reset()
w(P, "a.txt", "1行目\n2行目\n3行目\n")
sync()
w(P, "a.txt", "1行目\n2行目\n3行目\nポメラ追記\n")
w(V, "a.md", "vault追記\n1行目\n2行目\n3行目\n")
sync()
got = r(V, "a.md")
check("マージ成立", got == "vault追記\n1行目\n2行目\n3行目\nポメラ追記\n", repr(got))
check("両側が同一", r(P, "a.txt") == got)

print("\n[4] 両側変更・同じ行 → 衝突、新しい方採用＋conflict退避")
reset()
w(P, "b.txt", "共通\n元の行\n")
sync()
w(V, "b.md", "共通\nvaultの行\n")
time.sleep(0.05)
p = w(P, "b.txt", "共通\nポメラの行\n")
touch_newer(p)
sync()
check("新しい ポメラ 側を採用", r(V, "b.md") == "共通\nポメラの行\n", repr(r(V, "b.md")))
conf = [f for f in os.listdir(V) if ".conflict-" in f]
check("vault 側に conflict 退避", len(conf) == 1, str(os.listdir(V)))
if conf:
    check("conflict の中身が古い方", r(V, conf[0]) == "共通\nvaultの行\n")

print("\n[5] 削除の伝播")
reset()
w(P, "c.txt", "本文\n")
sync()
os.remove(os.path.join(V, "c.md"))
sync()
check("ポメラ 側も削除", not os.path.exists(os.path.join(P, "c.txt")))
trash = os.path.join(S, "trash")
check("ゴミ箱に退避されている", os.path.isdir(trash) and len(os.listdir(trash)) > 0)

print("\n[6] 削除 vs 変更 → 復活させる")
reset()
w(P, "d.txt", "本文\n")
sync()
os.remove(os.path.join(V, "d.md"))
w(P, "d.txt", "本文\n加筆\n")
sync()
check("vault に復活", r(V, "d.md") == "本文\n加筆\n", repr(r(V, "d.md")))

print("\n[7] 文字コード変換（ポメラ = cp932 / CRLF）")
reset()
w(P, "e.txt", "日本語テスト\n二行目\n", enc="cp932", nl="\r\n")
sync(["--pomera-encoding", "cp932", "--pomera-newline", "crlf"])
check("vault は UTF-8 / LF", rb(V, "e.md") == "日本語テスト\n二行目\n".encode("utf-8"),
      repr(rb(V, "e.md")))
w(V, "e.md", "日本語テスト\n二行目\n三行目\n")
sync(["--pomera-encoding", "cp932", "--pomera-newline", "crlf"])
check("ポメラ は cp932 / CRLF",
      rb(P, "e.txt") == "日本語テスト\r\n二行目\r\n三行目\r\n".encode("cp932"),
      repr(rb(P, "e.txt")))

print("\n[8] サブフォルダと BOM")
reset()
w(P, "章/01.txt", "\ufeff冒頭\n")
sync()
check("サブフォルダ維持", r(V, "章/01.md") == "冒頭\n", repr(r(V, "章/01.md")))

print("\n[9] .obsidian は無視される")
reset()
w(V, ".obsidian/app.json", "{}")
w(V, "f.md", "本文\n")
sync()
check("設定は同期されない", not os.path.exists(os.path.join(P, ".obsidian")))
check("通常ファイルは同期", r(P, "f.txt") == "本文\n")

print("\n[10] dry-run は書き込まない")
reset()
w(P, "g.txt", "本文\n")
sync(["--dry-run"])
check("vault に作られない", not os.path.exists(os.path.join(V, "g.md")))

print("\n[11] 変更なしの再同期で何も壊れない")
reset()
w(P, "h.txt", "本文\n")
sync()
before = (r(P, "h.txt"), r(V, "h.md"))
sync()
sync()
check("べき等", (r(P, "h.txt"), r(V, "h.md")) == before)

print("\n[12] .txt と .md が同名で両方 SD にある場合はスキップ")
reset()
w(P, "i.txt", "A\n")
w(P, "i.md", "B\n")
sync()
check("落ちずに処理が終わる", True)

shutil.rmtree(ROOT)
print("\n===== %s =====" % ("全て成功" if not fails else "失敗: %s" % fails))
sys.exit(1 if fails else 0)
