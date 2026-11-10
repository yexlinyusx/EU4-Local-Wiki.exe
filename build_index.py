# -*- coding: utf-8 -*-
"""build_index — 为 eu4cn 离线镜像构建 FTS5 全文索引 + titles.json。

用法:
    python build_index.py --root "D:/.../www.eu4cn.com" --out "D:/.../输出目录" [--workers N] [--limit N]

产出:
    search_index.db  (FTS5: title_spaced, body_spaced; UNINDEXED: title, ns, ord, path)
    titles.json      [{t: 标题, p: URL路径, n: 命名空间徽标, k: 排序}]
"""
import argparse
import json
import os
import sqlite3
import time
from multiprocessing import Pool
from urllib.parse import quote

import wikilib

PRAGMA_SET = "PRAGMA synchronous=OFF; PRAGMA journal_mode=MEMORY; PRAGMA temp_store=MEMORY;"


def iter_pages(root, limit=None):
    """产出 (绝对路径, 相对路径)。含 wiki/ 全部 .html 与根 首页.html。"""
    wiki_dir = os.path.join(root, "wiki")
    pages = []
    if os.path.isdir(wiki_dir):
        for dirpath, dirnames, filenames in os.walk(wiki_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.lower().endswith(".html"):
                    abs_p = os.path.join(dirpath, fn)
                    rel_p = "wiki/" + os.path.relpath(abs_p, wiki_dir).replace("\\", "/")
                    pages.append((abs_p, rel_p))
    home = os.path.join(root, "首页.html")
    if os.path.isfile(home):
        pages.append((home, "首页.html"))
    if limit:
        pages = pages[:limit]
    return pages


def parse_page(args):
    abs_p, rel_p = args
    try:
        with open(abs_p, "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    stem = os.path.basename(rel_p)[:-5]  # 去掉 .html
    title = wikilib.extract_title(raw, stem)
    badge, order = wikilib.ns_of(title)
    body = wikilib.extract_main(raw)
    return (title, wikilib.cjk_space(title), badge, order, rel_p, wikilib.cjk_space(body))


def page_url(title, rel_p):
    """磁盘相对路径 -> 站内 URL。

    标题与文件名一致（大多数页面）时用规范标题地址（URL 更可读，
    服务器端映射冒号/空格）；不一致的页面（Special 快照、链入页面等，
    其标题含引号/冒号且与磁盘名不同）直接用磁盘路径地址，保证可命中。
    """
    if rel_p == "首页.html":
        return "/"
    stem = rel_p.rsplit("/", 1)[-1]
    if stem.lower().endswith(".html"):
        stem = stem[:-5]
    if stem == title.replace(" ", "_").replace(":", "_"):
        return "/wiki/" + quote(title.replace(" ", "_"), safe="()!$&'*+;=@~#") + ".html"
    # 标题与磁盘名不一致：以磁盘路径为准（路径多已自带 .html，勿重复追加）
    path_part = rel_p[len("wiki/"):] if rel_p.startswith("wiki/") else rel_p
    if not path_part.lower().endswith(".html"):
        path_part += ".html"
    return "/wiki/" + quote(path_part, safe="/()!$&'*+;=@~#")


def run(root, out_dir=None, workers=6, limit=None, log=None, progress=None):
    """构建索引。progress(frac, done, total) 可选，用于首次运行进度页。"""
    log = log or (lambda s: print(s, flush=True))
    root = os.path.abspath(root)
    out_dir = os.path.abspath(out_dir) if out_dir else os.path.dirname(os.path.abspath(__file__))
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "search_index.db")
    titles_path = os.path.join(out_dir, "titles.json")

    pages = iter_pages(root, limit)
    total = len(pages)
    log("待索引页面: %d（镜像根: %s）" % (total, root))

    t0 = time.time()
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.executescript(PRAGMA_SET)
    con.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5(title_spaced, body_spaced, "
        "title UNINDEXED, ns UNINDEXED, ord UNINDEXED, path UNINDEXED)"
    )

    titles = []  # 仅统计用，最终清单以去重后的库为准
    done = 0
    batch = []
    workers = max(1, min(workers, os.cpu_count() or 4))
    log("启动 %d 个解析进程..." % workers)
    with Pool(workers) as pool:
        for rec in pool.imap(parse_page, pages, chunksize=32):
            done += 1
            if rec is not None:
                title, title_sp, badge, order, rel_p, body_sp = rec
                batch.append((title_sp, body_sp, title, badge, order, rel_p))
                titles.append({"t": title, "p": page_url(title, rel_p), "n": badge, "k": order})
                if len(batch) >= 500:
                    con.executemany("INSERT INTO wiki VALUES (?,?,?,?,?,?)", batch)
                    batch.clear()
            if done % 1000 == 0:
                con.commit()
                log("%d/%d (%.0f%%)  %.1fs" % (done, total, done * 100.0 / total, time.time() - t0))
                if progress:
                    progress(done / float(total), done, total)
    if batch:
        con.executemany("INSERT INTO wiki VALUES (?,?,?,?,?,?)", batch)
    con.commit()

    # 去重：重定向页保存了目标页内容，同标题多行。优先保留
    # "文件名 stem == 标题（空格/冒号转下划线）" 的规范页，其次保留正文最长者。
    con.execute("""
        DELETE FROM wiki WHERE rowid IN (
            SELECT rowid FROM (
                SELECT rowid,
                       row_number() OVER (
                           PARTITION BY title
                           ORDER BY CASE WHEN lower(path) IN (
                                             'wiki/' || lower(replace(replace(title,' ','_'),':','_')) || '.html',
                                             lower(replace(replace(title,' ','_'),':','_')) || '.html')
                                         THEN 0 ELSE 1 END,
                                    length(body_spaced) DESC
                       ) AS rn
                FROM wiki
            ) WHERE rn > 1
        )
    """)
    con.commit()
    dup_free = con.execute("SELECT count(*) FROM wiki").fetchone()[0]
    log("去重完成: %d -> %d 行" % (total, dup_free))

    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO meta VALUES ('built_at', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    con.execute("INSERT INTO meta VALUES ('pages', ?)", (str(dup_free),))
    con.commit()

    # titles.json 以去重后的库为准，按命名空间排序（条目优先）
    rows = con.execute("SELECT title, ns, ord, path FROM wiki").fetchall()
    con.close()
    titles = [
        {"t": t, "p": page_url(t, p), "n": ns, "k": k}
        for t, ns, k, p in sorted(rows, key=lambda r: (r[2], r[0]))
    ]
    with open(titles_path, "w", encoding="utf-8") as f:
        json.dump(titles, f, ensure_ascii=False, separators=(",", ":"))

    db_mb = os.path.getsize(db_path) / 1048576.0
    log("完成: %d 页, 用时 %.1f 秒, 索引 %.0f MB" % (len(titles), time.time() - t0, db_mb))
    if progress:
        progress(1.0, len(titles), total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="镜像根目录（含 wiki/ 与 首页.html）")
    ap.add_argument("--out", default=None, help="输出目录（默认脚本所在目录）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None, help="仅索引前 N 页（测试用）")
    args = ap.parse_args()
    run(args.root, args.out, args.workers, args.limit)


if __name__ == "__main__":
    main()
