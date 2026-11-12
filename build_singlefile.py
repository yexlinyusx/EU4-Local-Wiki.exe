# -*- coding: utf-8 -*-
"""build_singlefile.py — 构建「EU4百科离线版」单文件直读版。

流程:
  1. PyInstaller 构建 exe（可 --skip-exe-build 跳过，复用 dist 里的）
  2. 把镜像文件夹 + 索引数据库 + 标题清单 打包成 wiki_data.zip
     （文本/数据库 DEFLATED，图片/字体 STORED——已压缩格式不再压）
  3. 把 zip 追加到 exe 尾部 → 单文件版
  4. 校验：zipfile 打开单文件，抽读条目与数据库头

用法:
  python build_singlefile.py [--mirror <镜像目录>] [--out <输出exe>]
"""
import argparse
import os
import shutil
import sys
import time
import zipfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = r"D:\AI workspace\Zcode\default\EU4百科离线版"
MIRROR_DEFAULT = os.path.join(APP_ROOT, "wiki")
DB_DEFAULT = os.path.join(APP_ROOT, "search_index.db")
TITLES_DEFAULT = os.path.join(APP_ROOT, "titles.json")
EXE_SRC = os.path.join(HERE, "dist", "EU4百科离线版.exe")
OUT_DEFAULT = r"D:\AI workspace\Zcode\default\EU4百科离线版-单文件版.exe"

STORED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2",
              ".ttf", ".otf", ".eot", ".mp4", ".webm", ".ogg", ".mp3", ".pdf", ".cur"}
CHUNK = 8 * 1024 * 1024


def compress_type(name):
    ext = os.path.splitext(name)[1].lower()
    return zipfile.ZIP_STORED if ext in STORED_EXT else zipfile.ZIP_DEFLATED


def build_exe():
    print("[1/4] PyInstaller 构建 exe ...")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconsole", "--onefile",
                    "--name", "EU4百科离线版", "--icon", "app.ico",
                    "--collect-all", "webview", "app.py"],
                   cwd=HERE, check=True)


def build_data_zip(zip_path, mirror, db, titles):
    print("[2/4] 打包数据 zip ...")
    # 用 \\?\ 长路径前缀遍历（镜像里有超过 260 字符的缩略图路径）
    root_p = "\\\\?\\" + os.path.abspath(mirror)
    skip_names = {"search_index.db", "titles.json", "eu4_prefs.json",
                  "EU4百科离线版.log", "EU4百科离线版.ini"}
    entries = []
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(root_p):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn in skip_names:
                continue  # 索引/清单由下方显式加入；日志与配置不入包
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root_p).replace("\\", "/")
            entries.append((full, rel))
    entries.append((db, "search_index.db"))
    entries.append((titles, "titles.json"))
    total = 0
    for f, _ in entries:
        try:
            total += os.path.getsize(f)
        except OSError:
            skipped += 1
    print("    条目 %d 个, 原始 %.2f GB, 跳过 %d 个不可读" % (len(entries), total / 1073741824, skipped))

    t0 = time.time()
    done = 0
    with zipfile.ZipFile(zip_path, "w", allowZip64=True) as zf:
        for full, rel in entries:
            try:
                ct = compress_type(rel)
                if ct == zipfile.ZIP_STORED:
                    zf.write(full, rel, compress_type=zipfile.ZIP_STORED)
                else:
                    zf.write(full, rel, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
            except OSError as e:
                skipped += 1
                print("    跳过不可读: %s (%s)" % (rel[:60], e), flush=True)
                continue
            try:
                done += os.path.getsize(full)
            except OSError:
                pass
            if done % (200 * 1024 * 1024) < 8 * 1024 * 1024:
                print("    %.0f%% (%.2f GB)" % (done * 100.0 / total, done / 1073741824), flush=True)
    print("    zip 完成: %.2f GB, 用时 %.0f 秒, 跳过 %d" % (os.path.getsize(zip_path) / 1073741824,
                                                          time.time() - t0, skipped))


def append_files(out_path, parts):
    print("[3/4] 拼接单文件 ...")
    t0 = time.time()
    total = sum(os.path.getsize(p) for p in parts)
    done = 0
    with open(out_path, "wb") as out:
        for p in parts:
            with open(p, "rb") as src:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    print("    %.0f%%\r" % (done * 100.0 / total), end="", flush=True)
    print("\n    单文件完成: %.2f GB, 用时 %.0f 秒" % (os.path.getsize(out_path) / 1073741824,
                                                    time.time() - t0))


def verify(out_path):
    print("[4/4] 校验单文件 ...")
    import urllib.parse
    zf = zipfile.ZipFile(out_path, "r")
    names = zf.namelist()
    print("    条目数:", len(names))
    assert "search_index.db" in names, "缺少 search_index.db"
    assert "titles.json" in names, "缺少 titles.json"
    wiki_pages = [n for n in names if n.startswith("wiki/") and n.endswith(".html")]
    assert wiki_pages, "缺少 wiki 页面"
    sample = random_pick(wiki_pages, 5) + ["search_index.db", "titles.json"]
    for n in sample:
        head = zf.open(n).read(64)
        assert head, "条目为空: " + n
    print("    抽读 %d 个条目 OK; wiki 页面 %d 页" % (len(sample), len(wiki_pages)))
    zf.close()


def random_pick(seq, n):
    import random
    rnd = random.Random(42)
    return [rnd.choice(seq) for _ in range(n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", default=MIRROR_DEFAULT)
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--titles", default=TITLES_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--skip-exe-build", action="store_true")
    args = ap.parse_args()

    assert os.path.isdir(args.mirror), "镜像目录不存在: " + args.mirror
    assert os.path.isfile(args.db), "索引不存在: " + args.db
    assert os.path.isfile(args.titles), "标题清单不存在: " + args.titles

    free = shutil.disk_usage(os.path.dirname(args.out) or ".").free
    need = 8 * 1073741824
    if free < need:
        print("警告: 输出盘剩余空间 %.1f GB 偏小（建议 ≥8 GB）" % (free / 1073741824))

    if not args.skip_exe_build:
        build_exe()
    else:
        print("[1/4] 跳过 exe 构建（复用 dist）")

    zip_tmp = os.path.join(HERE, "build", "wiki_data.zip")
    os.makedirs(os.path.dirname(zip_tmp), exist_ok=True)
    build_data_zip(zip_tmp, args.mirror, args.db, args.titles)

    append_files(args.out, [EXE_SRC, zip_tmp])
    verify(args.out)
    print("完成:", args.out)


if __name__ == "__main__":
    main()
