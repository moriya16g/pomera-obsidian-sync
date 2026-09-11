#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pomera_sync.py -- ポメラのSDカードと Obsidian vault を双方向同期する。

  * ポメラ側のテキストは .txt、vault 側は .md として扱う（自動変換）
  * .txt / .md 以外の拡張子は変換せず、そのままの名前で同期する
  * 前回同期時の内容（ベース）を保存しておき、両側が変更されていれば
    3-way マージを試みる。マージできれば統合、衝突したら新しい方を採用し、
    古い方は .conflict ファイルとして隣に残す
  * 削除は片側だけ消えていればもう一方も消す（ゴミ箱へ退避）
  * 文字コードと改行コードをポメラ側／vault側で個別に指定できる

標準ライブラリのみ。Python 3.7+。
"""

import argparse
import difflib
import json
import os
import shutil
import sys
import time

TEXT_EXTS = {".txt", ".md"}
DEFAULT_IGNORE = [
    ".obsidian", ".trash", ".git", ".stfolder", ".DS_Store",
    "__MACOSX", "System Volume Information", ".Spotlight-V100",
]

# ---------------------------------------------------------------- utilities


def log(msg):
    print(msg)


def ignored(rel, ignore_names):
    parts = rel.replace("\\", "/").split("/")
    for p in parts:
        if p in ignore_names:
            return True
        if p.startswith("."):
            return True
        if ".conflict-" in p:
            return True
    return False


def scan(root, ignore_names):
    """root 以下の相対パス一覧を返す。"""
    found = []
    if not os.path.isdir(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ignore_names and not d.startswith(".")]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if ignored(rel, ignore_names):
                continue
            found.append(rel)
    return found


def split_key(rel):
    """相対パスから同期キーを作る。

    テキスト（.txt/.md）は拡張子を落としたキーにして両側を対応づける。
    それ以外はパスそのものがキー。
    戻り値: (key, is_text)
    """
    stem, ext = os.path.splitext(rel)
    if ext.lower() in TEXT_EXTS:
        return ("T:" + stem, True)
    return ("B:" + rel, False)


def key_to_name(key, is_text, side):
    """キーから、各サイドでの相対パスを作る。"""
    body = key[2:]
    if not is_text:
        return body
    return body + (".txt" if side == "pomera" else ".md")


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def decode(data, encodings):
    for enc in encodings:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 最後の手段：置換しつつ読む（内容は失われるが処理は止めない）
    return data.decode(encodings[0], errors="replace")


def norm(text):
    """比較・マージ用に改行を LF に揃え、BOM を落とす。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text(path, text, encoding, newline):
    ensure_dir(os.path.dirname(path))
    body = norm(text)
    if newline == "crlf":
        body = body.replace("\n", "\r\n")
    with open(path, "wb") as f:
        f.write(body.encode(encoding, errors="replace"))


def ensure_dir(d):
    if d and not os.path.isdir(d):
        os.makedirs(d)


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ------------------------------------------------------------- 3-way merge


def three_way_merge(base, a, b):
    """行リスト base/a/b を 3-way マージする。

    戻り値: (merged_lines, conflicted)
    衝突があった場合 conflicted=True（merged_lines は信用しない）
    """
    changes = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, base, a, autojunk=False).get_opcodes():
        if tag != "equal":
            changes.append((i1, i2, a[j1:j2], "a"))
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, base, b, autojunk=False).get_opcodes():
        if tag != "equal":
            changes.append((i1, i2, b[j1:j2], "b"))

    changes.sort(key=lambda c: (c[0], c[1]))

    merged = []
    pos = 0
    i = 0
    while i < len(changes):
        group = [changes[i]]
        end = changes[i][1]
        j = i + 1
        # base 上の範囲が重なっているものを 1 グループにまとめる
        while j < len(changes) and changes[j][0] < end:
            group.append(changes[j])
            end = max(end, changes[j][1])
            j += 1
        start = min(g[0] for g in group)
        merged.extend(base[pos:start])

        sides = set(g[3] for g in group)
        if len(sides) == 1:
            cur = start
            for g in sorted(group, key=lambda x: (x[0], x[1])):
                merged.extend(base[cur:g[0]])
                merged.extend(g[2])
                cur = max(cur, g[1])
            merged.extend(base[cur:end])
        else:
            # 同じ範囲を両側が触った。結果が同一なら衝突ではない
            def apply(side):
                out = []
                cur = start
                for g in sorted([x for x in group if x[3] == side],
                                key=lambda x: (x[0], x[1])):
                    out.extend(base[cur:g[0]])
                    out.extend(g[2])
                    cur = max(cur, g[1])
                out.extend(base[cur:end])
                return out

            ra, rb = apply("a"), apply("b")
            if ra == rb:
                merged.extend(ra)
            else:
                return ([], True)

        pos = end
        i = j

    merged.extend(base[pos:])
    return (merged, False)


def merge_text(base_t, a_t, b_t):
    base = norm(base_t).split("\n")
    a = norm(a_t).split("\n")
    b = norm(b_t).split("\n")
    merged, conflicted = three_way_merge(base, a, b)
    if conflicted:
        return (None, True)
    return ("\n".join(merged), False)


# -------------------------------------------------------------------- sync


class Sync(object):

    def __init__(self, args):
        self.pomera = os.path.abspath(args.pomera)
        self.vault = os.path.abspath(args.vault)
        self.state = os.path.abspath(args.state)
        self.base_dir = os.path.join(self.state, "base")
        self.trash_dir = os.path.join(
            self.state, "trash", time.strftime("%Y%m%d-%H%M%S"))
        self.dry = args.dry_run
        self.p_read = args.pomera_read_encodings.split(",")
        self.p_write = args.pomera_encoding
        self.p_newline = args.pomera_newline
        self.ignore = set(DEFAULT_IGNORE) | set(
            x for x in args.ignore.split(",") if x)
        self.stats = {"new": 0, "update": 0, "merge": 0,
                      "conflict": 0, "delete": 0, "skip": 0}

    # ---- paths

    def p_path(self, key, is_text):
        return os.path.join(self.pomera, key_to_name(key, is_text, "pomera"))

    def v_path(self, key, is_text):
        return os.path.join(self.vault, key_to_name(key, is_text, "vault"))

    def b_path(self, key, is_text):
        return os.path.join(self.base_dir, key_to_name(key, is_text, "vault"))

    # ---- io

    def read_p(self, key, is_text):
        data = read_bytes(self.p_path(key, is_text))
        return norm(decode(data, self.p_read)) if is_text else data

    def read_v(self, key, is_text):
        data = read_bytes(self.v_path(key, is_text))
        return norm(decode(data, ["utf-8-sig", "utf-8"])) if is_text else data

    def read_b(self, key, is_text):
        p = self.b_path(key, is_text)
        if not os.path.exists(p):
            return None
        data = read_bytes(p)
        return norm(decode(data, ["utf-8-sig", "utf-8"])) if is_text else data

    def put_p(self, key, is_text, content):
        if self.dry:
            return
        path = self.p_path(key, is_text)
        if is_text:
            write_text(path, content, self.p_write, self.p_newline)
        else:
            ensure_dir(os.path.dirname(path))
            with open(path, "wb") as f:
                f.write(content)

    def put_v(self, key, is_text, content):
        if self.dry:
            return
        path = self.v_path(key, is_text)
        if is_text:
            write_text(path, content, "utf-8", "lf")
        else:
            ensure_dir(os.path.dirname(path))
            with open(path, "wb") as f:
                f.write(content)

    def put_b(self, key, is_text, content):
        if self.dry:
            return
        path = self.b_path(key, is_text)
        if is_text:
            write_text(path, content, "utf-8", "lf")
        else:
            ensure_dir(os.path.dirname(path))
            with open(path, "wb") as f:
                f.write(content)

    def drop_b(self, key, is_text):
        if self.dry:
            return
        p = self.b_path(key, is_text)
        if os.path.exists(p):
            os.remove(p)

    def trash(self, path, side):
        if self.dry or not os.path.exists(path):
            return
        root = self.pomera if side == "pomera" else self.vault
        rel = os.path.relpath(path, root)
        dest = os.path.join(self.trash_dir, side, rel)
        ensure_dir(os.path.dirname(dest))
        shutil.move(path, dest)

    def conflict_copy(self, key, is_text, side, content):
        """採用されなかった方を .conflict として残す。"""
        if self.dry:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if side == "pomera":
            base = self.p_path(key, is_text)
            root, ext = os.path.splitext(base)
            dest = "%s.conflict-%s%s" % (root, stamp, ext)
            if is_text:
                write_text(dest, content, self.p_write, self.p_newline)
            else:
                with open(dest, "wb") as f:
                    f.write(content)
        else:
            base = self.v_path(key, is_text)
            root, ext = os.path.splitext(base)
            dest = "%s.conflict-%s%s" % (root, stamp, ext)
            if is_text:
                write_text(dest, content, "utf-8", "lf")
            else:
                with open(dest, "wb") as f:
                    f.write(content)
        return dest

    # ---- main

    def collect(self):
        table = {}
        dupes = []
        for side, root in (("pomera", self.pomera), ("vault", self.vault)):
            seen = {}
            for rel in scan(root, self.ignore):
                key, is_text = split_key(rel)
                if key in seen and seen[key] != rel:
                    dupes.append((side, seen[key], rel))
                    continue
                seen[key] = rel
                entry = table.setdefault(key, {"is_text": is_text})
                entry[side] = True
        return table, dupes

    def run(self):
        ensure_dir(self.base_dir)
        table, dupes = self.collect()

        for side, a, b in dupes:
            log("!  重複のためスキップ (%s): %s / %s" % (side, a, b))
            self.stats["skip"] += 1

        # ベースにしか無いキー（両側から消えた）も拾う
        for rel in scan(self.base_dir, self.ignore):
            key, is_text = split_key(rel)
            table.setdefault(key, {"is_text": is_text})

        for key in sorted(table):
            self.handle(key, table[key])

        log("")
        log("--- 結果 ---")
        log("新規:%d  更新:%d  マージ:%d  衝突:%d  削除:%d  スキップ:%d"
            % (self.stats["new"], self.stats["update"], self.stats["merge"],
               self.stats["conflict"], self.stats["delete"],
               self.stats["skip"]))
        if self.dry:
            log("(dry-run のため実際の書き込みは行っていません)")
        if self.stats["conflict"]:
            log("衝突したファイルは .conflict-<日時> として残してあります。")

    def handle(self, key, info):
        is_text = info["is_text"]
        has_p = os.path.exists(self.p_path(key, is_text))
        has_v = os.path.exists(self.v_path(key, is_text))
        base = self.read_b(key, is_text)
        name = key_to_name(key, is_text, "vault")

        # --- どちらも無い
        if not has_p and not has_v:
            if base is not None:
                self.drop_b(key, is_text)
            return

        # --- 片側だけ存在
        if has_p and not has_v:
            content = self.read_p(key, is_text)
            if base is None:
                log("+  vault へ新規: %s" % name)
                self.put_v(key, is_text, content)
                self.put_b(key, is_text, content)
                self.stats["new"] += 1
            elif content == base:
                log("-  vault で削除されたので ポメラ からも削除: %s" % name)
                self.trash(self.p_path(key, is_text), "pomera")
                self.drop_b(key, is_text)
                self.stats["delete"] += 1
            else:
                log("+  vault で削除されたが ポメラ 側が変更済み → 復活: %s" % name)
                self.put_v(key, is_text, content)
                self.put_b(key, is_text, content)
                self.stats["new"] += 1
            return

        if has_v and not has_p:
            content = self.read_v(key, is_text)
            if base is None:
                log("+  ポメラ へ新規: %s" % name)
                self.put_p(key, is_text, content)
                self.put_b(key, is_text, content)
                self.stats["new"] += 1
            elif content == base:
                log("-  ポメラ で削除されたので vault からも削除: %s" % name)
                self.trash(self.v_path(key, is_text), "vault")
                self.drop_b(key, is_text)
                self.stats["delete"] += 1
            else:
                log("+  ポメラ で削除されたが vault 側が変更済み → 復活: %s" % name)
                self.put_p(key, is_text, content)
                self.put_b(key, is_text, content)
                self.stats["new"] += 1
            return

        # --- 両側に存在
        cp = self.read_p(key, is_text)
        cv = self.read_v(key, is_text)

        if cp == cv:
            if base != cp:
                self.put_b(key, is_text, cp)
            return

        if base is not None and cp == base:
            log("→  ポメラ を更新: %s" % name)
            self.put_p(key, is_text, cv)
            self.put_b(key, is_text, cv)
            self.stats["update"] += 1
            return

        if base is not None and cv == base:
            log("→  vault を更新: %s" % name)
            self.put_v(key, is_text, cp)
            self.put_b(key, is_text, cp)
            self.stats["update"] += 1
            return

        # 両側変更、またはベース無し
        if is_text and base is not None:
            merged, conflicted = merge_text(base, cp, cv)
            if not conflicted:
                log("M  マージ: %s" % name)
                self.put_p(key, is_text, merged)
                self.put_v(key, is_text, merged)
                self.put_b(key, is_text, merged)
                self.stats["merge"] += 1
                return

        # 衝突 → 新しい方を採用し、古い方を conflict として残す
        tp = mtime(self.p_path(key, is_text))
        tv = mtime(self.v_path(key, is_text))
        if tp >= tv:
            winner, loser, lside, lcontent = "ポメラ", "vault", "vault", cv
            content = cp
        else:
            winner, loser, lside, lcontent = "vault", "ポメラ", "pomera", cp
            content = cv
        log("!  衝突 → %s 側を採用（%s 側は .conflict に退避）: %s"
            % (winner, loser, name))
        self.conflict_copy(key, is_text, lside, lcontent)
        self.put_p(key, is_text, content)
        self.put_v(key, is_text, content)
        self.put_b(key, is_text, content)
        self.stats["conflict"] += 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="ポメラのSDカードと Obsidian vault を双方向同期する")
    ap.add_argument("pomera", help="ポメラ側フォルダ（SDカード）。'.' も可")
    ap.add_argument("--vault", required=True, help="Obsidian vault のパス")
    ap.add_argument("--state",
                    default=os.path.expanduser("~/Documents/.pomera_sync"),
                    help="同期状態の保存先（既定: ~/Documents/.pomera_sync）")
    ap.add_argument("--dry-run", action="store_true", help="変更せず結果だけ表示")
    ap.add_argument("--pomera-encoding", default="utf-8",
                    help="ポメラ側の書き出し文字コード（既定: utf-8 / 例: cp932）")
    ap.add_argument("--pomera-read-encodings", default="utf-8-sig,cp932",
                    help="ポメラ側の読み込み時に試す文字コード（カンマ区切り）")
    ap.add_argument("--pomera-newline", default="lf", choices=["lf", "crlf"],
                    help="ポメラ側の改行コード（既定: lf）")
    ap.add_argument("--ignore", default="",
                    help="追加で無視するフォルダ／ファイル名（カンマ区切り）")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.pomera):
        print("ポメラ側フォルダが見つかりません: %s" % args.pomera)
        return 1
    if not os.path.isdir(args.vault):
        print("vault が見つかりません: %s" % args.vault)
        return 1

    Sync(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
