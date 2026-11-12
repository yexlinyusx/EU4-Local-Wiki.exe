# -*- coding: utf-8 -*-
"""EU4百科离线版 — eu4cn.com 离线镜像阅读器。

免安装桌面应用：内置本地 HTTP 服务器 + FTS5 中文全文搜索。
镜像文件保持原样，所有链接修复在服务器端完成。

用法:
    python app.py                 # 图形窗口（pywebview / WebView2）
    python app.py --headless     # 仅启动服务器（调试用）
    python app.py --browser      # 启动服务器并打开系统浏览器
    python app.py --root <镜像根目录> --rebuild-index
"""
import argparse
import collections
import html as html_mod
import json
import logging
import logging.handlers
import mimetypes
import os
import random
import re
import socket
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import build_index
import wikilib

APP_TITLE = "EU4百科离线版"
APP_VERSION = "1.0"
DEFAULT_PORT = 26312
PAGE_SIZE = 50

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "EU4百科离线版.log")
CONFIG_PATH = os.path.join(BASE_DIR, "EU4百科离线版.ini")
# 数据文件目录：默认与 exe/脚本同目录，可用 --data-dir 覆盖（main() 里赋值）
DB_PATH = os.path.join(BASE_DIR, "search_index.db")
TITLES_PATH = os.path.join(BASE_DIR, "titles.json")
DATA_DIR = BASE_DIR
# 单文件直读模式：exe 尾部封有 wiki_data.zip 时启用（main() 里检测）
DATA_ZIP = None
ZIP_MODE = False


def setup_logging():
    logging.basicConfig(level=logging.INFO)
    try:
        fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024,
                                                  backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
    except OSError:
        pass


# ---------------------------------------------------------------- 配置与路径

def read_config():
    import configparser
    cfg = configparser.ConfigParser()
    try:
        cfg.read(CONFIG_PATH, encoding="utf-8")
        return cfg.get("paths", "wiki_root", fallback="")
    except Exception:
        return ""


def write_config(wiki_root):
    import configparser
    cfg = configparser.ConfigParser()
    cfg["paths"] = {"wiki_root": wiki_root}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)
    except OSError:
        pass


def looks_like_wiki_root(path):
    if not path or not os.path.isdir(path):
        return False
    return (os.path.isfile(os.path.join(path, "首页.html"))
            or os.path.isfile(os.path.join(path, "wiki", "首页.html")))


def find_wiki_root(explicit=None):
    """定位镜像根：命令行参数 > 配置文件 > 程序目录向上两层内的兄弟目录。"""
    if explicit and looks_like_wiki_root(explicit):
        return os.path.abspath(explicit)
    cfg = read_config()
    if cfg and looks_like_wiki_root(cfg):
        return cfg
    here = BASE_DIR
    for base in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        if not base or not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for name in entries:
            cand = os.path.join(base, name)
            if os.path.isdir(cand) and looks_like_wiki_root(cand):
                return cand
        if looks_like_wiki_root(base):
            return base
    return None


def ask_wiki_root_gui():
    """弹目录选择框。返回路径或 None。"""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_TITLE, "未找到百科镜像目录。\n请选择包含「首页.html」和「wiki」文件夹的目录。")
        picked = filedialog.askdirectory(title="选择镜像根目录")
        root.destroy()
        return picked or None
    except Exception:
        logging.error("目录选择框失败: %s", traceback.format_exc())
        return None


# ---------------------------------------------------------------- 全局状态

class App:
    def __init__(self, root=None):
        self.root = os.path.abspath(root) if root else None  # zip 模式无磁盘镜像根
        self.db = None
        self.db_lock = threading.Lock()  # 串行化共享连接的并发访问
        self.titles = []          # [{t,p,n,k}]
        self.lock = threading.Lock()
        self.build_state = {"running": False, "done": False, "error": "", "frac": 0.0,
                            "log": collections.deque(maxlen=6)}
        self.load_index()

    def load_index(self):
        with self.lock:
            try:
                if self.db:
                    self.db.close()
            except Exception:
                pass
            if os.path.isfile(DB_PATH):
                try:
                    self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
                    self.db.execute("SELECT 1 FROM wiki LIMIT 1")
                    logging.info("索引已加载")
                except sqlite3.Error:
                    self.db = None
            else:
                self.db = None
            try:
                with open(TITLES_PATH, "r", encoding="utf-8") as f:
                    self.titles = json.load(f)
            except (OSError, ValueError):
                self.titles = []

    def index_ready(self):
        return self.db is not None and self.titles

    def start_prepare(self):
        """首次准备：zip 模式从内嵌包提取索引数据；文件夹模式构建索引。"""
        if self.build_state["running"]:
            return
        self.build_state.update({"running": True, "done": False, "error": "", "frac": 0.0,
                                 "log": collections.deque(maxlen=6)})

        def work():
            def log(s):
                self.build_state["log"].appendleft(str(s))

            def progress(frac, done, total):
                self.build_state["frac"] = frac

            try:
                if ZIP_MODE:
                    self._extract_data(log)
                else:
                    build_index.run(self.root, DATA_DIR, workers=6, log=log, progress=progress)
                self.load_index()
                self.build_state["done"] = True
                if not self.index_ready():
                    self.build_state["error"] = "数据准备结束但未生成有效索引，请查看日志。"
            except Exception:
                logging.error("数据准备失败:\n%s", traceback.format_exc())
                self.build_state["error"] = "数据准备失败: %s" % traceback.format_exc(limit=1)

        threading.Thread(target=work, daemon=True).start()

    def _extract_data(self, log):
        """从内嵌数据包提取 search_index.db 与 titles.json 到 DATA_DIR。"""
        import zipfile as _zf
        os.makedirs(DATA_DIR, exist_ok=True)
        names = ("search_index.db", "titles.json")
        infos = [DATA_ZIP.getinfo(n) for n in names if n in DATA_ZIP.namelist()]
        total = sum(i.file_size for i in infos) or 1
        done = 0
        for info in infos:
            target = os.path.join(DATA_DIR, info.filename)
            if os.path.exists(target):
                done += info.file_size
                log("%s 已存在，跳过" % info.filename)
                continue
            log("提取 %s ..." % info.filename)
            part = target + ".part"
            with DATA_ZIP.open(info) as src, open(part, "wb") as dst:
                while True:
                    chunk = src.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    done += len(chunk)
                    self.build_state["frac"] = min(1.0, done / float(total))
            os.replace(part, target)
        log("数据提取完成")


# ---------------------------------------------------------------- HTTP 处理

EXTRA_MIME = {
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf",
    ".svg": "image/svg+xml", ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".webp": "image/webp",
}

INJECT_CSS = """
<style id="eu4-inject-css">
/* 离线下必然空黑的皮肤组件：隐藏"实时最近更改"与广告位 */
.live-recent-wrapper, .live-recent, ins.adsbygoogle{display:none !important}
#eu4-suggest{position:absolute;display:none;z-index:2147483000;background:#fff;border:1px solid #d5d9de;
border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.18);font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif;
overflow:hidden;max-width:380px;}
#eu4-suggest .eu4-si{padding:8px 12px;cursor:pointer;display:flex;justify-content:space-between;gap:8px;color:#1b2733}
#eu4-suggest .eu4-si:hover,#eu4-suggest .eu4-si.sel{background:#eef3fb}
#eu4-suggest .eu4-si .ns{color:#8a94a0;font-size:12px;flex:none}
#eu4-suggest .eu4-si .tt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
</style>
"""

INJECT_JS = """
<script id="eu4-inject-js">
(function(){
  function $(s,c){return (c||document).querySelector(s)}
  var form=$('#searchform'), input=$('#searchInput')||document.querySelector('input[name="search"]');
  if(form&&input){
    form.addEventListener('submit',function(e){
      e.preventDefault();
      var q=input.value.trim();
      if(q){location.href='/search?q='+encodeURIComponent(q);}
    });
  }
  document.addEventListener('keydown',function(e){
    if((e.ctrlKey||e.metaKey)&&(e.key==='k'||e.key==='K')){
      e.preventDefault();
      if(input){input.focus();input.select();}
      else{location.href='/search';}
    }
  });
  if(!input) return;
  var box=document.createElement('div');
  box.id='eu4-suggest';
  document.body.appendChild(box);
  var items=[], sel=-1, timer=null;
  function hide(){box.style.display='none';sel=-1;}
  function place(){
    var r=input.getBoundingClientRect();
    box.style.left=(r.left+window.scrollX)+'px';
    box.style.top=(r.bottom+window.scrollY+4)+'px';
    box.style.width=Math.max(r.width,280)+'px';
  }
  function render(){
    if(!items.length){hide();return;}
    box.innerHTML=items.map(function(it,i){
      return '<div class="eu4-si'+(i===sel?' sel':'')+'" data-p="'+it.p+'">'+
        '<span class="tt">'+it.t+'</span><span class="ns">'+it.n+'</span></div>';
    }).join('');
    box.style.display='block';place();
    var kids=box.children;
    for(var i=0;i<kids.length;i++){
      (function(el){el.addEventListener('mousedown',function(e){e.preventDefault();location.href=el.getAttribute('data-p');});})(kids[i]);
    }
  }
  input.addEventListener('input',function(){
    clearTimeout(timer);
    var q=input.value.trim();
    if(!q){hide();return;}
    timer=setTimeout(function(){
      fetch('/api/suggest?q='+encodeURIComponent(q))
        .then(function(r){return r.json()})
        .then(function(d){items=d.r||[];sel=-1;render();})
        .catch(function(){});
    },120);
  });
  input.addEventListener('keydown',function(e){
    if(box.style.display==='block'){
      if(e.key==='ArrowDown'){e.preventDefault();sel=Math.min(sel+1,items.length-1);render();}
      else if(e.key==='ArrowUp'){e.preventDefault();sel=Math.max(sel-1,0);render();}
      else if(e.key==='Enter'&&sel>=0){e.preventDefault();location.href=items[sel].p;}
    }
    if(e.key==='Escape'){hide();}
  });
  document.addEventListener('click',function(e){if(!box.contains(e.target)&&e.target!==input)hide();});
  window.addEventListener('resize',function(){if(box.style.display==='block')place();});
})();
</script>
"""


def inject_into(html_text):
    idx = html_text.rfind("</body>")
    if idx < 0:
        idx = html_text.rfind("</html>")
    payload = INJECT_CSS + INJECT_JS
    if idx < 0:
        return html_text + payload
    return html_text[:idx] + payload + html_text[idx:]


def recover_utf8(s):
    """http.server 按 latin-1 解码请求行/查询串；若原始字节是 UTF-8，恢复原文。

    浏览器发送的是百分号编码（纯 ASCII），此函数不改变它们；
    直接发送原始 UTF-8 字节的客户端（curl 等）则在此还原。
    """
    if all(ord(c) < 128 for c in s):
        return s
    try:
        return s.encode("latin-1").decode("utf-8", errors="replace")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "EU4WikiOffline/" + APP_VERSION
    app = None  # 注入

    # ---- 基础应答 ----
    def send_body(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass

    def send_html(self, status, text):
        self.send_body(status, "text/html; charset=utf-8", text.encode("utf-8"))

    def redirect(self, location):
        body = b""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass

    def log_message(self, fmt, *args):
        logging.debug("%s %s", self.address_string(), fmt % args)

    # ---- 路由 ----
    def do_GET(self):
        self._qs = {}
        self._path = ""
        try:
            parsed = urlparse(self.path)
            path = unquote(recover_utf8(parsed.path))
            qs = parse_qs(recover_utf8(parsed.query), keep_blank_values=True)
            self._qs = qs
            self._path = path
            if path in ("/", "/index.html"):
                return self.serve_html("首页.html")
            if path == "/search":
                return self.serve_search(qs)
            if path == "/api/suggest":
                return self.api_suggest(qs)
            if path == "/favicon.ico":
                return self.serve_favicon()
            if path == "/index.php":
                return self.handle_index_php(qs)
            if path.startswith("/wiki/"):
                return self.serve_wiki(path[len("/wiki/"):])
            if path.startswith("/skins/Liberty/img/"):
                return self.serve_static("/resources/poweredby_mediawiki_88x31.png")
            if path == "/first-run":
                return self.serve_first_run()
            if path == "/api/first-run-status":
                return self.api_first_run_status()
            if path == "/api/toggle-dark":
                PREFS["dark"] = not PREFS.get("dark", False)
                try:
                    with open(PREF_FILE, "w", encoding="utf-8") as f:
                        json.dump(PREFS, f)
                except OSError:
                    pass
                body = json.dumps({"dark": PREFS["dark"]}, ensure_ascii=False).encode("utf-8")
                return self.send_body(200, "application/json; charset=utf-8", body)
            return self.serve_static(path)
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception:
            logging.error("处理 %s 出错:\n%s", self.path, traceback.format_exc())
            try:
                self.send_html(500, error_page(APP_TITLE, "服务器内部错误，详情见日志文件。"))
            except Exception:
                pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- VFS 读取（zip 直读 / 磁盘读取 双模式） ----
    def read_asset(self, rel):
        """按镜像相对路径读取文件字节；不存在返回 None。"""
        rel = rel.replace("\\", "/").lstrip("/")
        while rel.startswith("./") or rel.startswith("../"):
            rel = rel[3:] if rel.startswith("../") else rel[2:]
        if not rel:
            return None
        if ZIP_MODE and DATA_ZIP is not None:
            try:
                return DATA_ZIP.read(rel)
            except KeyError:
                return None
        root = os.path.abspath(self.app.root or ".")
        fs_path = os.path.abspath(os.path.join(root, rel.replace("/", os.sep)))
        if not (fs_path.startswith(root + os.sep) or fs_path == root):
            return None
        # 超长路径（>240 字符）走 \\?\ 前缀，规避 Windows 260 限制
        if len(fs_path) > 240:
            fs_path = "\\\\?\\" + fs_path
        if not os.path.isfile(fs_path):
            return None
        with open(fs_path, "rb") as f:
            return f.read()

    # ---- 页面文件伺服 ----
    def serve_page(self, rel):
        """纯静态文件伺服（CSS/JS/图片等）。"""
        rel = rel.replace("\\", "/").lstrip("/")
        if not rel or contains_parent_dir(rel):
            return self.send_html(404, error_page("404", "文件不存在"))
        raw = self.read_asset(rel)
        if raw is None:
            return self.send_html(404, error_page("404", "文件不存在"))
        self.send_body(200, guess_type(rel), raw)

    def serve_html(self, rel):
        """HTML 页面伺服：全新阅读界面；正文提取失败时回退为原样 + 搜索注入。"""
        rel = rel.replace("\\", "/").lstrip("/")
        if contains_parent_dir(rel):
            return self.serve_missing("路径无效")
        raw = self.read_asset(rel)
        if raw is None:
            return self.serve_missing(os.path.basename(rel))
        text = raw.decode("utf-8", errors="replace")
        content, catlinks, _is_main, ambox = wikilib.extract_content_blocks(text)
        if content:
            content = re.sub(r"<script\b.*?</script\s*>", " ", content, flags=re.S | re.I)
            stem = os.path.basename(rel)[:-5]
            title = wikilib.extract_title(text, stem)
            # 首页：模块解析 + 杂志式重排版（失败回退普通阅读视图）
            if _is_main:
                try:
                    banner, pairs = parse_homepage(content)
                    if len(pairs) >= 5:
                        page = render_homepage(title, banner, pairs)
                        html_out = (html_open(title)
                                    + _util_style() + READER_CSS + INJECT_CSS
                                    + '</head><body>'
                                    + app_header()
                                    + '<div class="rmain wide"><div class="rmain-inner">'
                                    + '<h1 class="ptitle" id="eu4-page-top">%s</h1>' % html_mod.escape(title)
                                    + '<p class="pmeta">EU4百科离线版 · 数据快照 2024-11</p>'
                                    + page
                                    + '</div></div>'
                                    + '<button id="topBtn" onclick="window.scrollTo({top:0})" title="回到顶部">↑</button>'
                                    + INJECT_JS + _READER_JS
                                    + '</body></html>')
                        self.send_body(200, "text/html; charset=utf-8", html_out.encode("utf-8"))
                        return
                except Exception:
                    logging.warning("首页重排版失败，回退阅读视图:\n%s", traceback.format_exc(limit=1))
            toc_items, content = wikilib.extract_toc(content)
            self.send_body(200, "text/html; charset=utf-8",
                           render_reader(title, content, catlinks, toc_items,
                                         ambox_html=ambox).encode("utf-8"))
        else:
            self.send_body(200, "text/html; charset=utf-8",
                           inject_into(text).encode("utf-8"))

    def serve_static(self, path):
        rel = path.lstrip("/")
        if not rel or contains_parent_dir(rel):
            return self.send_html(404, error_page("404", "文件不存在"))
        if os.path.isdir(os.path.join(self.app.root or ".", rel.replace("/", os.sep))):
            rel = rel.rstrip("/") + "/index.html"
        self.serve_page(rel)

    def serve_favicon(self):
        ico = os.path.join(BASE_DIR, "app.ico")
        if os.path.isfile(ico):
            with open(ico, "rb") as f:
                self.send_body(200, "image/x-icon", f.read())
        else:
            self.send_body(204, "image/x-icon", b"")

    # ---- wiki 页面路由（链接修复核心） ----
    def serve_wiki(self, raw_name):
        name = raw_name.replace(":", "_").replace(" ", "_")
        if not name or contains_parent_dir(name):
            return self.serve_missing(raw_name)
        stem = name[:-5] if name.lower().endswith(".html") else name

        # Special 动态页拦截
        if stem == "Special_随机页面" or stem.startswith("Special_随机页面/"):
            return self.redirect(self.random_page_url())
        if stem == "Special_搜索" or stem.startswith("Special_搜索"):
            # 旧快照里的站内搜索链接统一转成本地搜索；Special:搜索/词 当作关键词
            rest = raw_name.split("/search/", 1)[-1] if "/search/" in raw_name else ""
            rest = rest.split(".html")[0].replace("_", " ").strip()
            return self.redirect("/search?q=" + quote(rest) if rest else "/search")
        if stem in ("Special_用户登录", "Special_创建账号", "Special_用户登录/signup",
                    "Special_最近更改", "Special_链入页面", "Special_链出更改",
                    "Special_用户贡献", "Special_统计"):
            return self.serve_special_notice(stem)

        asset = "wiki/" + stem + ".html"
        if self.read_asset(asset) is not None:
            return self.serve_html(asset)
        return self.serve_missing(stem)

    def random_page_url(self):
        """随机条目：只用真正的百科条目，排除"链接至/链接到"快照、
        Special 子页、讨论页等导航型页面。"""
        mains = [t for t in self.app.titles
                 if t.get("k") == 0
                 and not t["t"].startswith(("链接至", "链接到"))
                 and not t["p"].startswith(("/wiki/Special_", "/wiki/Talk_"))]
        pick = random.choice(mains) if mains else {"p": "/"}
        return pick["p"]

    def serve_special_notice(self, stem):
        zh = stem.replace("Special_", "Special:")
        self.send_html(200, error_page(
            "功能不可用",
            "<p>「%s」属于 MediaWiki 动态功能（登录、统计、更改记录等），"
            "离线版无法提供。</p><p><a href='/'>返回首页</a> · "
            "<a href='/search'>搜索文章</a></p>" % html_mod.escape(zh)))

    def handle_index_php(self, qs):
        title = (qs.get("title") or [""])[0]
        search = (qs.get("search") or [""])[0]
        if title:
            return self.redirect("/wiki/" + quote(title.replace(" ", "_")) + ".html")
        if search:
            return self.redirect("/search?q=" + quote(search))
        return self.serve_special_notice("Special_首页")

    # ---- 搜索 ----
    def serve_search(self, qs):
        t_start = time.time()
        q_raw = (qs.get("q") or [""])[0].strip()
        ns_filter = (qs.get("ns") or [""])[0]
        page_no = 1
        try:
            page_no = max(1, int((qs.get("page") or ["1"])[0]))
        except ValueError:
            pass

        rows, total, err = [], 0, None
        fts_q = wikilib.build_fts_query(q_raw)
        if self.app.db and fts_q:
            try:
                where, params = "wiki MATCH ?", [fts_q]
                if ns_filter and ns_filter != "全部":
                    where += " AND ns = ?"
                    params.append(ns_filter)
                with self.app.db_lock:  # 共享连接串行化，杜绝并发竞态
                    total = self.app.db.execute(
                        "SELECT count(*) FROM wiki WHERE " + where, params).fetchone()[0]
                    rows = self.app.db.execute(
                        "SELECT highlight(wiki,0,char(1),char(2)),"
                        " snippet(wiki,1,char(1),char(2),'…',28),"
                        " ns, path FROM wiki WHERE " + where +
                        " ORDER BY (CASE WHEN path LIKE 'wiki/Special\\_%' ESCAPE '\\'"
                        " OR path LIKE 'wiki/Talk\\_%' ESCAPE '\\' THEN 3.0 ELSE 0 END)"
                        " + bm25(wiki, 10.0, 1.0) LIMIT ? OFFSET ?",
                        params + [PAGE_SIZE, (page_no - 1) * PAGE_SIZE]).fetchall()
            except sqlite3.Error as e:
                err = str(e)
        elif not fts_q:
            err = None
        ms = (time.time() - t_start) * 1000

        esc_q = html_mod.escape(q_raw)
        parts = [html_open("搜索: %s" % esc_q)]
        parts.append(SEARCH_PAGE_CSS)
        parts.append(INJECT_CSS)
        parts.append(app_header(esc_q))
        parts.append('<div class="searchwrap">')
        if not q_raw:
            parts.append(SEARCH_TIPS)
        else:
            # 标题精确/前缀匹配置顶（bm25 对长文有长度惩罚，标题命中须单独处理）
            pinned = []
            if self.app.db:
                like_q = q_raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                try:
                    pinned = self.app.db.execute(
                        "SELECT title, ns, path FROM wiki WHERE title = ? "
                        "UNION ALL SELECT title, ns, path FROM wiki "
                        "WHERE title LIKE ? ESCAPE '\\' AND title <> ? LIMIT 10",
                        (q_raw, like_q + "%", q_raw)).fetchall()
                except sqlite3.Error:
                    pinned = []
            if pinned:
                parts.append('<p class="sec">标题匹配</p><ol class="res pinned">')
                for t, ns, path in pinned:
                    badge = "" if ns == "条目" else '<span class="badge">%s</span>' % ns
                    parts.append('<li><a class="rt" href="/%s">%s%s</a></li>'
                                 % (quote(path, safe="/:_()!$&'*+;=@~#"), badge,
                                    html_mod.escape(t)))
                parts.append('</ol>')
            chips = ['<span class="chip"><a href="/search?q=%s">全部</a></span>' % quote(q_raw)]
            for ns in wikilib._NS_ORDER:
                cls = " chip on" if ns == ns_filter else " chip"
                chips.append('<span class="%s"><a href="/search?q=%s&amp;ns=%s">%s</a></span>'
                             % (cls, quote(q_raw), quote(ns), ns))
            parts.append('<div class="chips">%s</div>' % "".join(chips))
            if err:
                parts.append('<p class="meta">搜索出错: %s</p>' % html_mod.escape(err))
            elif not rows and not pinned:
                parts.append('<p class="meta">没有找到与「<b>%s</b>」匹配的页面。</p>' % esc_q)
                parts.append(SEARCH_TIPS)
            elif rows:
                parts.append('<p class="sec">全文匹配（共 %d 条，用时 %.0f ms，第 %d 页 / 共 %d 页）</p>'
                             % (total, ms, page_no, max(1, -(-total // PAGE_SIZE))))
                parts.append('<ol class="res">')
                for t_hl, snip, ns, path in rows:
                    t_disp = wikilib.tidy_display(wikilib.display_title(t_hl))
                    snip_disp = wikilib.tidy_display(wikilib.display_title(snip))
                    t_disp = t_disp.replace("\x01", "<mark>").replace("\x02", "</mark>")
                    snip_disp = snip_disp.replace("\x01", "<mark>").replace("\x02", "</mark>")
                    badge = "" if ns == "条目" else '<span class="badge">%s</span>' % ns
                    parts.append('<li><a class="rt" href="/%s">%s%s</a><p class="sn">%s</p></li>'
                                 % (quote(path, safe="/:_()!$&'*+;=@~#"), badge, t_disp, snip_disp))
                parts.append('</ol>')
                parts.append(pager_html(q_raw, ns_filter, page_no, total))
        parts.append('</div><footer class="ft">EU4百科离线版 · 数据快照 2024-11（游戏 1.36/1.37 时代）· Ctrl+K 聚焦搜索</footer>')
        parts.append(INJECT_JS + _READER_JS + '</body></html>')
        self.send_html(200, "".join(parts))

    def api_suggest(self, qs):
        q_raw = (qs.get("q") or [""])[0].strip()
        out = []
        if q_raw:
            ql = q_raw.lower()
            scored = []
            for it in self.app.titles:
                tl = it["t"].lower()
                pen = 1 if it["t"].startswith("链接至") else 0
                if tl.startswith(ql):
                    scored.append((0, pen, it.get("k", 99), len(it["t"]), it))
                elif ql in tl:
                    scored.append((1, pen, it.get("k", 99), len(it["t"]), it))
            scored.sort(key=lambda x: x[:4])
            out = [{"t": it["t"], "p": it["p"], "n": it["n"]} for _, _, _, _, it in scored[:10]]
        body = json.dumps({"r": out}, ensure_ascii=False).encode("utf-8")
        self.send_body(200, "application/json; charset=utf-8", body)

    def api_first_run_status(self):
        st = self.app.build_state
        body = json.dumps({"running": st["running"], "done": st["done"], "frac": st["frac"],
                           "error": st["error"],
                           "log": list(st["log"])}, ensure_ascii=False).encode("utf-8")
        self.send_body(200, "application/json; charset=utf-8", body)

    # ---- 未收录 / 404 ----
    def serve_missing(self, name):
        shown = name.replace("_", " ")
        esc = html_mod.escape(shown)
        sug = self.title_suggestions(name)
        sug_html = ""
        if sug:
            sug_html = '<p class="sug-t">你是不是想找：</p><ul class="sug">' + "".join(
                '<li><a href="%s">%s</a></li>' % (s["p"], html_mod.escape(s["t"])) for s in sug) + "</ul>"
        self.send_html(404, error_page(
            "离线版未收录该页面",
            "<p>页面「<b>%s</b>」不在这份离线镜像里（可能是未抓取的红链、"
            "在线专属页面或快照后新增的内容）。</p>%s"
            "<p><a href='/'>返回首页</a> · <a href='/search?q=%s'>搜索「%s」</a></p>"
            % (esc, sug_html, quote(shown), esc)))

    def title_suggestions(self, name):
        tokens = [t.lower() for t in re.split(r"[\s_()/]+", name) if len(t) >= 2]
        if not tokens:
            return []
        scored = []
        for it in self.app.titles:
            tl = it["t"].lower()
            hit = sum(1 for tk in tokens if tk in tl)
            if hit:
                scored.append((-hit, it.get("k", 99), len(it["t"]), it))
        scored.sort(key=lambda x: x[:3])
        return [it for _, _, _, it in scored[:8]]


def contains_parent_dir(rel):
    parts = rel.replace("\\", "/").split("/")
    return ".." in parts


def guess_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in EXTRA_MIME:
        return EXTRA_MIME[ext]
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


# ---------------------------------------------------------------- 页面模板

HTML_HEAD = ('<!doctype html><html lang="zh-CN"{dark}><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>{title} - EU4百科离线版</title>')

PREFS = {"dark": False}
PREF_FILE = None  # main() 里按数据目录赋值


def _dark_attr():
    return ' class="dark"' if PREFS.get("dark") else ""


_UTIL_CSS = {"css": None}


def wiki_utility_css():
    """抽取镜像 skinliberty.css 中的纯类选择器工具样式（bg-红 / bd-黑 等）。

    原站这些内容模板工具类由皮肤 CSS 提供，阅读界面未整份引入皮肤
    （其 html/body/h1 等全局规则会干扰自建排版），这里只取类选择器规则。
    """
    if _UTIL_CSS["css"] is not None:
        return _UTIL_CSS["css"]
    css = ""
    try:
        raw = None
        if ZIP_MODE and DATA_ZIP is not None:
            try:
                raw = DATA_ZIP.read("skinliberty.css")
            except KeyError:
                raw = None
        elif WIKI_ROOT:
            p = os.path.join(WIKI_ROOT, "skinliberty.css")
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    raw = f.read()
        if raw:
            text = raw.decode("utf-8", errors="replace")
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            keep = []
            for m in re.finditer(r"([^{}@]+)\{([^{}]*)\}", text):
                sels = [s.strip() for s in m.group(1).split(",") if s.strip()]
                if sels and all(s.startswith(".") for s in sels):
                    keep.append(m.group(0).strip())
            css = "\n".join(keep)
    except Exception:
        logging.warning("工具样式提取失败:\n%s", traceback.format_exc(limit=1))
    _UTIL_CSS["css"] = css
    return css


def _util_style():
    css = wiki_utility_css()
    return ('<style id="eu4-wiki-util">\n' + css + '\n</style>') if css else ""


def html_open(title):
    """统一的 <html> 开头（含深浅色 class）。"""
    return ('<!doctype html><html lang="zh-CN"%s><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s - EU4百科离线版</title>' % (_dark_attr(), html_mod.escape(title)))

SEARCH_PAGE_CSS = """
<style>
/* ===== 共享设计语言：EU4 深蓝+金 ===== */
:root{--bg:#f3f5f8;--surface:#fff;--fg:#1f2733;--muted:#66717f;--link:#1a5dad;
--border:#dfe5ec;--navy:#1b2a3f;--navy2:#243654;--gold:#d9a514;--gold2:#e6b31e;
--mark:#ffe27a;--markfg:#1f2733;--thead:#eef2f7;--hover:#f2f6fb;
--scroll:#c3cad3;--scrollh:#98a3b0}
html.dark{--bg:#11151c;--surface:#1a212c;--fg:#d6dce4;--muted:#8d97a3;--link:#7fb0ea;
--border:#2a3440;--navy:#0d1219;--navy2:#182230;--gold:#d9a514;--gold2:#e6b31e;
--mark:#8a6d00;--markfg:#fff;--thead:#1f2733;--hover:#202a37;
--scroll:#39434f;--scrollh:#4e5b6a}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--fg);
font:15.5px/1.78 "Segoe UI","Microsoft YaHei","PingFang SC",sans-serif}
/* 细滚动条（WebView2/Chromium），主题自适应 */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--scroll);border-radius:6px}
::-webkit-scrollbar-thumb:hover{background:var(--scrollh)}
::-webkit-scrollbar-corner{background:transparent}
.rh{position:sticky;top:0;z-index:999;display:flex;gap:14px;align-items:center;height:58px;
padding:0 22px;background:linear-gradient(180deg,var(--navy),var(--navy2));
box-shadow:0 2px 10px rgba(0,0,0,.25)}
.rlogo{color:#fff;font-size:17px;font-weight:700;text-decoration:none;white-space:nowrap;line-height:1.15}
.rlogo small{display:block;font-size:10.5px;font-weight:400;color:#9fb0c2;letter-spacing:2px}
.sf{flex:1;display:flex;max-width:620px}
.sf input{flex:1;height:38px;padding:0 14px;border:1px solid #3d5273;border-radius:7px 0 0 7px;
font-size:14.5px;outline:none;background:#fff;color:#1f2733}
html.dark .sf input{background:#232c39;color:#d6dce4}
.sf button{height:38px;padding:0 22px;border:0;border-radius:0 7px 7px 0;background:var(--gold);
color:#fff;font-size:14.5px;cursor:pointer;font-family:inherit}
.sf button:hover{background:var(--gold2)}
.hbtn{color:#cdd8e6;text-decoration:none;font-size:13.5px;padding:7px 12px;border-radius:7px;
white-space:nowrap;border:0;background:none;cursor:pointer;font-family:inherit}
.hbtn:hover{background:rgba(255,255,255,.14)}
.notice{background:var(--surface);border:1px solid var(--border);border-left:6px solid var(--gold);
border-radius:10px;padding:12px 18px;margin:0 0 20px;font-size:14.5px}
.ambox2020{border:1px solid var(--border);border-left:6px solid var(--gold);
background:rgba(217,165,20,.06);padding:8px 14px;margin:14px 0;font-size:14px}
mark{background:var(--mark);color:var(--markfg);padding:0 2px;border-radius:2px}
#topBtn{position:fixed;right:24px;bottom:24px;width:42px;height:42px;border-radius:50%;
background:var(--navy);color:#fff;border:0;font-size:18px;cursor:pointer;display:none;
box-shadow:0 6px 16px rgba(0,0,0,.3);z-index:998}
/* ===== 搜索页 ===== */
.searchwrap{max-width:940px;margin:32px auto 90px;padding:0 26px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 10px}
.chip a{display:inline-block;padding:4px 14px;border-radius:16px;background:var(--surface);
color:var(--muted);font-size:13px;text-decoration:none;border:1px solid var(--border)}
.chip.on a{background:var(--navy);color:#fff;border-color:var(--navy)}
.chip a:hover{border-color:var(--link);color:var(--link)}
.chip.on a:hover{color:#fff}
.sec{margin:20px 0 10px;font-weight:700;color:var(--fg);font-size:15px}
.pinned li{border-left:4px solid var(--gold)}
.meta{color:var(--muted);font-size:13px;margin:8px 0 18px}
ol.res{list-style:none;margin:0;padding:0}
ol.res li{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:16px 20px;margin-bottom:14px}
ol.res li:hover{border-color:#c4cfdb}
.rt{font-size:16.5px;font-weight:600;color:var(--link);text-decoration:none}
.rt:hover{text-decoration:underline}
.badge{display:inline-block;margin-right:8px;padding:1px 9px;border-radius:4px;
background:rgba(26,93,173,.12);color:var(--link);font-size:12px;font-weight:400;vertical-align:2px}
html.dark .badge{background:rgba(127,176,234,.15)}
.sn{margin:7px 0 0;color:var(--muted);font-size:13.5px;overflow-wrap:break-word}
.pager{display:flex;justify-content:space-between;margin-top:22px}
.pager a{color:var(--link);text-decoration:none;padding:9px 16px;background:var(--surface);
border:1px solid var(--border);border-radius:8px}
.pager a:hover{border-color:var(--link)}
.ft{text-align:center;color:var(--muted);font-size:12.5px;padding:18px 0 26px}
.tips{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:16px 22px;color:var(--muted);font-size:14px}
.tips h3{margin:0 0 10px;font-size:15px;color:var(--fg)}
.errpage{max-width:640px;margin:70px auto;padding:30px 38px;background:var(--surface);
border:1px solid var(--border);border-radius:12px}
.errpage h1{font-size:21px;margin:0 0 14px;color:#c8483c}
html.dark .errpage h1{color:#e07060}
.errpage a{color:var(--link)}
.sug-t{margin-top:18px;color:var(--fg);font-weight:600}
ul.sug{margin:6px 0 0;padding-left:22px}
</style>
"""

SEARCH_TIPS = """
<div class="tips"><h3>搜索提示</h3>
输入国家名（如 大明、奥斯曼）、机制（如 厌战度、正统性）、决议 / 事件 / 理念名称，
或任意出现在页面正文里的关键词。支持同时输入多个词（取交集）。</div>
"""


def pager_html(q_raw, ns_filter, page_no, total):
    last = max(1, -(-total // PAGE_SIZE))
    parts = ['<div class="pager">']
    if page_no > 1:
        parts.append('<a href="/search?q=%s&amp;ns=%s&amp;page=%d">← 上一页</a>'
                     % (quote(q_raw), quote(ns_filter), page_no - 1))
    else:
        parts.append('<span></span>')
    if page_no < last:
        parts.append('<a href="/search?q=%s&amp;ns=%s&amp;page=%d">下一页 →</a>'
                     % (quote(q_raw), quote(ns_filter), page_no + 1))
    else:
        parts.append('<span></span>')
    parts.append('</div>')
    return "".join(parts)


def error_page(h1, body_html):
    return (html_open(h1) + SEARCH_PAGE_CSS +
            '<div class="errpage"><h1>%s</h1>%s</div>' % (h1, body_html))


def app_header(q_value=""):
    """阅读页/搜索页共用的顶栏：品牌 + 搜索 + 返回/前进 + 随机 + 深浅色。"""
    val_attr = ' value="%s"' % html_mod.escape(q_value) if q_value else ""
    return (
        '<header class="rh">'
        '<a class="rlogo" href="/">EU4百科<small>离线版</small></a>'
        '<form class="sf" id="searchform" action="/search" method="get">'
        '<input id="searchInput" name="q" type="search"%s placeholder="搜索国家、机制、决议…" autocomplete="off">'
        '<button type="submit">搜索</button></form>'
        '<button class="hbtn" onclick="history.back()" title="返回（Alt+←）">← 返回</button>'
        '<button class="hbtn" onclick="history.forward()" title="前进（Alt+→）">前进 →</button>'
        '<a class="hbtn" href="/wiki/Special:随机页面.html" title="随机条目">🎲 随机</a>'
        '<button class="hbtn" onclick="window.eu4ToggleDark()" title="深浅色切换">🌓</button>'
        '</header>' % val_attr
    )


# ---------------------------------------------------------------- 阅读模式（全新 UI）

READER_CSS = SEARCH_PAGE_CSS + """
<style>
/* ===== 阅读布局：左侧目录 + 宽幅正文 ===== */
.rlayout{display:grid;grid-template-columns:248px minmax(0,1fr)}
.tocbar{position:sticky;top:58px;align-self:start;height:calc(100vh - 58px);overflow-y:auto;
overflow-x:hidden;scrollbar-width:thin;scrollbar-color:var(--scroll) transparent;
padding:24px 14px 40px 20px;border-right:1px solid var(--border);font-size:13.5px}
.tocbar::-webkit-scrollbar{width:6px}
.tocbar::-webkit-scrollbar-thumb{background:var(--scroll);border-radius:3px}
.tocbar::-webkit-scrollbar-thumb:hover{background:var(--scrollh)}
.tocbar-t{font-weight:700;color:var(--muted);font-size:12px;letter-spacing:2px;margin-bottom:12px}
.tocbar ol{list-style:none;margin:0;padding:0}
.tocbar .l2{margin:2px 0}
.tocbar .l3{margin:2px 0 2px 16px}
.tocbar .l4{margin:2px 0 2px 32px}
.tocbar a{color:var(--fg);text-decoration:none;display:block;padding:3px 9px;border-radius:6px;line-height:1.45}
.tocbar a:hover{background:var(--hover)}
.tocbar a.on{background:rgba(217,165,20,.18);color:var(--link);font-weight:700;
border-left:3px solid var(--gold);padding-left:6px}
.tocbar .none{color:var(--muted);font-size:13px}
.rmain{padding:30px 46px 90px;min-width:0}
.rmain-inner{max-width:1240px;margin:0 auto}
.rmain.wide{padding:30px 40px 90px}
.rmain.wide .rmain-inner{max-width:1760px}
h1.ptitle{font-size:30px;line-height:1.25;margin:0 0 8px;font-weight:800;
padding-bottom:12px;border-bottom:3px solid var(--gold);scroll-margin-top:76px}
p.pmeta{color:var(--muted);font-size:13px;margin:10px 0 26px}
.content h2{font-size:22px;margin:38px 0 14px;padding-bottom:8px;
border-bottom:2px solid var(--border);font-weight:700}
.content h3{font-size:18.5px;margin:26px 0 10px;font-weight:700}
.content h4{font-size:16.5px;margin:20px 0 8px;font-weight:700}
.content h5{margin-top:18px}
.content p{margin:0.85em 0}
.content ul,.content ol{margin:0.7em 0;padding-left:1.7em}
/* plainlist：原版无圆点紧凑列表 */
ul.plainlist,.plainlist ul,.plainlist ol{list-style:none;margin:0.4em 0;padding-left:0.5em}
.plainlist li{margin:0;line-height:1.45}
/* Bootstrap 栅格：镜像模板大量使用 col-sm-* 排版（col-sm-4 占 44871 处），
   原版由 Bootstrap 实现三模块并排；缺失这些类时模块会堆叠成整行 */
/* Bootstrap 栅格：同一行模块 flex 等高拉伸，底部对齐 */
.rmain .row{display:flex;flex-wrap:wrap;align-items:stretch}
.rmain .col-sm-4,.rmain .col-sm-5,.rmain .col-sm-6,.rmain .col-sm-7,
.rmain .col-sm-8,.rmain .col-sm-12,.rmain .col-xs-12{display:flex;
flex-direction:column;padding:0 8px;box-sizing:border-box;min-width:0}
.rmain .col-xs-12{width:100%}
.rmain .col-sm-4{width:33.3333%}
.rmain .col-sm-5{width:41.6667%}
.rmain .col-sm-6{width:50%}
.rmain .col-sm-7{width:58.3333%}
.rmain .col-sm-8{width:66.6667%}
.rmain .col-sm-12{width:100%}
/* 模块盒子（栏内最后一个 div）纵向填满，使同排盒子底边对齐 */
.rmain .col-sm-4 > div:last-child,.rmain .col-sm-5 > div:last-child,
.rmain .col-sm-6 > div:last-child,.rmain .col-sm-7 > div:last-child,
.rmain .col-sm-8 > div:last-child,.rmain .col-sm-12 > div:last-child,
.rmain .col-xs-12 > div:last-child{flex:1 1 auto}
/* 首页栏目内堆叠多个模块（标题+正文成对）：正文盒等分栏高，
   使左右两列逐模块对齐、底边整齐 */
.rmain .col-sm-4 > .section-template-border,.rmain .col-sm-5 > .section-template-border,
.rmain .col-sm-6 > .section-template-border,.rmain .col-sm-7 > .section-template-border,
.rmain .col-sm-8 > .section-template-border,.rmain .col-xs-12 > .section-template-border,
.rmain .col-sm-12 > .section-template-border{flex:1 1 auto;display:flex !important;
flex-direction:column}
/* 导航模块词条：列宽自适应（宽窗口 3 列 ~195px，长词条不折行）；
   词条块随盒高等距分布，短模块不留大片空白 */
.rmain .col-sm-4 > .section-template-border > div[style*="columns"],
.rmain .col-sm-5 > .section-template-border > div[style*="columns"],
.rmain .col-sm-6 > .section-template-border > div[style*="columns"],
.rmain .col-sm-7 > .section-template-border > div[style*="columns"],
.rmain .col-sm-8 > .section-template-border > div[style*="columns"],
.rmain .col-sm-12 > .section-template-border > div[style*="columns"]{
flex:1 1 auto;columns:150px auto !important;-webkit-columns:150px auto !important}
.rmain .col-sm-4 > .section-template-border > div[style*="columns"] li,
.rmain .col-sm-5 > .section-template-border > div[style*="columns"] li,
.rmain .col-sm-6 > .section-template-border > div[style*="columns"] li,
.rmain .col-sm-7 > .section-template-border > div[style*="columns"] li,
.rmain .col-sm-8 > .section-template-border > div[style*="columns"] li,
.rmain .col-sm-12 > .section-template-border > div[style*="columns"] li{margin:4px 0}
.rmain .col-sm-4 > .section-template-border > div[style*="columns"] li img,
.rmain .col-sm-5 > .section-template-border > div[style*="columns"] li img,
.rmain .col-sm-6 > .section-template-border > div[style*="columns"] li img,
.rmain .col-sm-7 > .section-template-border > div[style*="columns"] li img,
.rmain .col-sm-8 > .section-template-border > div[style*="columns"] li img,
.rmain .col-sm-12 > .section-template-border > div[style*="columns"] li img{
vertical-align:middle;margin-right:2px}
/* col-xs-* 仅窄屏生效，桌面端由 col-sm-* 决定宽度；只有 col-xs 的元素
   保持块级全宽即可，无需专门定义 */
.content a{color:var(--link)}
.content a.new{color:#d24b3f}
.content a.new:visited{color:#d24b3f}
.content a:hover{text-decoration:underline}
.mw-headline{scroll-margin-top:80px}
.content img{max-width:100%;height:auto}
.content table{border-collapse:collapse}
table.wikitable{width:auto;max-width:100%;margin:16px 0;background:var(--surface);
border:1px solid var(--border);font-size:14px;border-radius:6px;overflow:hidden}
table.wikitable th{background:var(--thead);padding:7px 12px;border:1px solid var(--border);
text-align:left;font-weight:600}
table.wikitable td{padding:7px 12px;border:1px solid var(--border)}
table[class*="countrybox"],table.infobox{float:right;margin:8px 0 18px 24px;width:340px;max-width:100%}
table.ambox{border:1px solid var(--border);border-left:8px solid var(--gold);
background:rgba(217,165,20,.07);padding:4px 12px;margin:14px 0;font-size:14px}
/* EU4 事件选项按钮：原站用 27px 横幅图 + 负 margin 叠文字，字号一大文字就
   溢出框外。改为 grid 同格叠放：文字决定高度，横幅自动拉伸到实际尺寸 */
.content div[style*="width: 468px"],.content div[style*="width:468px"],
.content div[style*="width: 488px"],.content div[style*="width:488px"],
.content div[style*="width: 547px"],.content div[style*="width:547px"]{
    display:inline-grid !important;width:auto !important;max-width:100%;min-width:240px;
    text-align:center;vertical-align:middle;margin:6px 0;
}
.content .option_button{grid-area:1/1}
.content .option_button img{width:100% !important;height:100% !important;
object-fit:fill;border-radius:4px}
.content .option_title{grid-area:1/1;align-self:center;margin:0 !important;
padding:4px 14px;line-height:1.6;position:relative !important;z-index:2;
text-shadow:0 1px 3px rgba(0,0,0,.85)}
#toc{display:none}
.thumb{border:1px solid var(--border);border-radius:8px;background:var(--surface);
padding:8px;margin:12px 0;display:inline-block}
.thumbcaption{font-size:12.5px;color:var(--muted);padding:5px 2px 0}
ul.gallery,.gallery{list-style:none;padding:0}
.gallery li{display:inline-block;margin:6px;vertical-align:top}
.gallerybox .thumb{display:block}
pre{background:var(--bg);border:1px solid var(--border);border-radius:8px;
padding:14px;overflow:auto;font-size:13.5px}
.catbox{margin-top:36px;padding:14px 20px;background:var(--surface);
border:1px solid var(--border);border-radius:10px;font-size:13.5px;color:var(--muted)}
.catbox a{color:var(--link);text-decoration:none}
.clear{clear:both}
.reflist{font-size:13px;color:var(--muted)}
@media (max-width:960px){.rlayout{grid-template-columns:1fr}.tocbar{display:none}.rmain{padding:22px 18px 80px}}
</style>
"""

_READER_JS = """
<script>
(function(){
  window.eu4ToggleDark=function(){
    fetch('/api/toggle-dark').then(function(){location.reload();});
  };
  var btn=document.getElementById('topBtn');
  window.addEventListener('scroll',function(){
    if(btn){btn.style.display=window.scrollY>600?'block':'none';}
  },{passive:true});
  // Alt+← / Alt+→ 前进后退（独立窗口无浏览器工具栏）
  document.addEventListener('keydown',function(e){
    if(e.altKey&&e.key==='ArrowLeft'){e.preventDefault();history.back();}
    else if(e.altKey&&e.key==='ArrowRight'){e.preventDefault();history.forward();}
  });
  // 目录滚动高亮（实时取各标题视口位置，图片加载导致的布局变化不影响判断）
  var links={},alist=document.querySelectorAll('.tocbar a[data-h]'),i;
  for(i=0;i<alist.length;i++){links[alist[i].getAttribute('data-h')]=alist[i];}
  var keys=[],k;
  for(k in links){if(links.hasOwnProperty(k))keys.push(k);}
  if(keys.length){
    var heads=[];
    for(i=0;i<keys.length;i++){
      var el=document.getElementById(keys[i]);
      if(el)heads.push(el);
    }
    if(heads.length){
      var upd=function(){
        var cur=null;
        for(var j=0;j<heads.length;j++){
          if(heads[j].getBoundingClientRect().top<=110)cur=heads[j].id;
          else break;
        }
        for(var m=0;m<keys.length;m++){links[keys[m]].classList.remove('on');}
        if(cur&&links[cur])links[cur].classList.add('on');
      };
      window.addEventListener('scroll',upd,{passive:true});
      upd();
    }
  }
})();
</script>
"""


# ---------------------------------------------------------------- 首页专属重排版

def _balanced_element(html_text, start):
    """从某开标签起返回 (完整元素 HTML, 结束位置)。"""
    m = re.match(r'<(\w+)', html_text[start:])
    if not m:
        return html_text[start:], len(html_text)
    tag = m.group(1).lower()
    open_end = html_text.find('>', start) + 1
    if open_end == 0:
        return html_text[start:], len(html_text)
    depth = 1
    for mm in re.finditer(r'<%s\b[^>]*>|</%s\s*>' % (tag, tag), html_text[open_end:], re.I):
        if mm.group(0)[1] == '/':
            depth -= 1
            if depth == 0:
                end = open_end + mm.end()
                return html_text[start:end], end
        else:
            depth += 1
    return html_text[start:], len(html_text)


def parse_homepage(content):
    """解析首页：返回 (横幅HTML, [(标题, 标题元素HTML, 正文元素HTML), ...])。"""
    marks = []
    for m in re.finditer(r'<div[^>]+class="[^"]*section-template[^"]*"[^>]*>', content, re.I):
        kind = 'body' if 'section-template-border' in m.group(0) else 'head'
        marks.append((m.start(), kind))
    pairs = []
    cursor = 0
    pending = None
    for start, kind in marks:
        if start < cursor:
            continue  # 嵌套在已消费元素内部，跳过
        el, end = _balanced_element(content, start)
        cursor = end
        if kind == 'head':
            t = re.split(r'<a[\s>]', el, 1)[0]
            title = wikilib.cjk_unspace(re.sub(r'<[^>]+>', '', t)).strip()
            pending = (title, el)
        else:
            if pending:
                pairs.append((pending[0], pending[1], el))
                pending = None
            else:
                pairs.append(('', '', el))
    banner = None
    first_pos = marks[0][0] if marks else len(content)
    mb = re.search(r'<table[^>]*header-template[^>]*>', content[:first_pos], re.I)
    if mb:
        banner, _ = _balanced_element(content, mb.start())
    return banner, pairs


def _fix_home_links(html_text):
    """首页模块内的裸相对内容链接（如 1.37_版本.html，镜像保存时缺
    wiki/ 前缀）补上 /wiki/；已指向镜像目录（wiki/、images/ 等）的
    链接保持原样——根路径页面上它们本就能直达。"""
    top_dirs = ("wiki", "images", "resources", "skins")

    def rep(m):
        href = m.group(1)
        h = href
        while h.startswith("./"):
            h = h[2:]
        if h.startswith(("http://", "https://", "/", "#", "mailto:", "javascript:")):
            return m.group(0)
        if h.split("/", 1)[0].lower() in top_dirs:
            return m.group(0)
        return 'href="/wiki/' + h + '"'
    return re.sub(r'href="([^"]+)"', rep, html_text)


def render_homepage(title, banner_html, pairs):
    """首页杂志式版面：横幅 / 新闻+右侧信息列 / 3×3 导航卡片 / 帮助行。

    各行复用 col-sm-* 栅格与等高拉伸规则，右侧信息列按内容量配平，
    不再出现大块空洞。
    """
    def block(pair):
        t, head, body = pair
        return _fix_home_links((head or '') + (body or ''))

    def card(pair):
        return '<div class="col-sm-4">' + block(pair) + '</div>'

    by = {}
    used = set()
    for p in pairs:
        if p[0] in ('欧陆时报', '关于本站', '你知道吗', '历史上的今天',
                    '开始游戏', '内政', '经济', '扩展', '外交与战争', '额外',
                    '扩展包内容'):
            by[p[0]] = p

    rows = []
    if banner_html:
        rows.append('<div class="row"><div class="col-sm-12">' + _fix_home_links(banner_html) + '</div></div>')
    # 新闻行：欧陆时报(7) | 右侧信息列(5)：关于本站 + 你知道吗
    news = by.get('欧陆时报')
    stack_titles = ('关于本站', '你知道吗')
    if news:
        used.add('欧陆时报')
        stack = [by.get(k) for k in stack_titles if by.get(k)]
        used.update(stack_titles)
        rows.append('<div class="row">'
                    '<div class="col-sm-7">' + block(news) + '</div>'
                    '<div class="col-sm-5">' + ''.join(block(p) for p in stack) + '</div>'
                    '</div>')
    # 导航卡片行
    r3 = [by.get(k) for k in ('开始游戏', '内政', '经济') if by.get(k)]
    used.update(k for k in ('开始游戏', '内政', '经济') if by.get(k))
    if r3:
        rows.append('<div class="row">' + ''.join(card(p) for p in r3) + '</div>')
    r4 = [by.get(k) for k in ('扩展', '外交与战争', '额外') if by.get(k)]
    used.update(k for k in ('扩展', '外交与战争', '额外') if by.get(k))
    if r4:
        rows.append('<div class="row">' + ''.join(card(p) for p in r4) + '</div>')
    # 历史上的今天整行
    today = by.get('历史上的今天')
    if today:
        used.add('历史上的今天')
        rows.append('<div class="row"><div class="col-sm-12">' + block(today) + '</div></div>')
    # 扩展包内容：通栏大横框（离线版去掉参与编辑类模块）
    dlc = by.get('扩展包内容')
    if dlc:
        used.add('扩展包内容')
        rows.append('<div class="row"><div class="col-sm-12">' + block(dlc) + '</div></div>')
    # 未被上面行使用的模块按 3 列卡片排布（"协助完善百科"等在线编辑类直接略去）
    rest = [p for p in pairs if p[0] not in used and '协助完善' not in p[0]]
    for i in range(0, len(rest), 3):
        chunk = rest[i:i + 3]
        rows.append('<div class="row">' + ''.join(card(p) for p in chunk) + '</div>')
    return ''.join(rows)


def render_reader(title, content_html, catlinks_html, toc_items, ambox_html=None):
    """全新阅读界面：左侧目录栏 + 宽幅正文 + 深浅主题，不含原皮肤任何元素。"""
    esc_title = html_mod.escape(title)
    ambox = ('<div class="notice">' + ambox_html + "</div>") if ambox_html else ""
    cat = ('<div class="catbox">' + catlinks_html + "</div>") if catlinks_html else ""
    if toc_items:
        rows = "".join(
            '<li class="l%d"><a href="#%s" data-h="%s">%s</a></li>'
            % (lv, html_mod.escape(hid, quote=True), html_mod.escape(hid, quote=True),
               html_mod.escape(tx))
            for lv, hid, tx in toc_items)
        layout_open = ('<div class="rlayout">'
                       '<nav class="tocbar"><div class="tocbar-t">本页目录</div><ol>'
                       '<li class="l2"><a href="#eu4-page-top" data-h="eu4-page-top">页面开头</a></li>'
                       + rows + '</ol></nav>'
                       '<div class="rmain"><div class="rmain-inner">')
        layout_close = '</div></div></div>'
    else:
        # 无章节页面（如首页）：不显示目录栏，宽幅渲染
        layout_open = '<div class="rmain wide"><div class="rmain-inner">'
        layout_close = '</div></div>'
    return (
        html_open(title)
        + _util_style() + READER_CSS + INJECT_CSS
        + '</head><body>'
        + app_header()
        + layout_open
        + '<h1 class="ptitle" id="eu4-page-top">%s</h1>' % esc_title
        + '<p class="pmeta">EU4百科离线版 · 数据快照 2024-11</p>'
        + ambox
        + '<div class="content">' + content_html + '<div class="clear"></div></div>'
        + cat
        + layout_close
        + '<button id="topBtn" onclick="window.scrollTo({top:0})" title="回到顶部">↑</button>'
        + INJECT_JS + _READER_JS
        + '</body></html>'
    )


def first_run_page():
    return (html_open("首次启动 · 准备数据") + SEARCH_PAGE_CSS + """
<div class="errpage"><h1 style="color:#243447">首次启动：正在准备数据</h1>
<p>只需进行一次（提取搜索索引），预计 1 分钟左右。完成后将自动进入百科。</p>
<div style="background:#eef1f4;border-radius:6px;height:14px;overflow:hidden;margin:16px 0">
  <div id="bar" style="height:100%;width:0%;background:#d9a514;transition:width .3s"></div>
</div>
<pre id="log" style="font:12px/1.6 Consolas,monospace;color:#4d5865;white-space:pre-wrap;margin:0"></pre>
</div>
<script>
function poll(){
  fetch('/api/first-run-status').then(function(r){return r.json()}).then(function(d){
    document.getElementById('bar').style.width=Math.round((d.frac||0)*100)+'%';
    document.getElementById('log').textContent=(d.log||[]).slice().reverse().join('\\n');
    if(d.done&&!d.error){location.href='/';}
    else if(d.error){document.getElementById('log').textContent=d.error;}
    else{setTimeout(poll,400);}
  }).catch(function(){setTimeout(poll,800);});
}
poll();
</script>""")


# ---------------------------------------------------------------- 启动

def pick_free_port(start):
    for p in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("无可用端口")


def main():
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--root", default=None, help="镜像根目录")
    ap.add_argument("--headless", action="store_true", help="仅启动服务器，不开窗口")
    ap.add_argument("--browser", action="store_true", help="打开系统浏览器而非内嵌窗口")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--data-dir", default=None, help="索引数据目录（默认与程序同目录）")
    ap.add_argument("--rebuild-index", action="store_true", help="强制重建搜索索引")
    args = ap.parse_args()

    global DB_PATH, TITLES_PATH, DATA_DIR, DATA_ZIP, ZIP_MODE, PREF_FILE, WIKI_ROOT
    DATA_DIR = os.path.abspath(args.data_dir) if args.data_dir else BASE_DIR
    DB_PATH = os.path.join(DATA_DIR, "search_index.db")
    TITLES_PATH = os.path.join(DATA_DIR, "titles.json")
    PREF_FILE = os.path.join(DATA_DIR, "eu4_prefs.json")
    try:
        with open(PREF_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                PREFS.update(loaded)
    except (OSError, ValueError):
        pass

    setup_logging()
    logging.info("%s v%s 启动（基目录 %s）", APP_TITLE, APP_VERSION, BASE_DIR)

    for ext, mt in EXTRA_MIME.items():
        mimetypes.add_type(mt, ext)

    # 单文件直读模式检测：exe 尾部封有 wiki_data.zip
    try:
        import zipfile as _zf
        _z = _zf.ZipFile(sys.executable, "r")
        _names = set(_z.namelist())
        if "search_index.db" in _names and any(n.startswith("wiki/") for n in _names):
            DATA_ZIP = _z
            ZIP_MODE = True
            logging.info("检测到内嵌数据包（单文件直读模式）")
        else:
            _z.close()
    except Exception:
        pass

    if ZIP_MODE:
        # 直读模式：数据封在 exe 内，索引缓存放 LOCALAPPDATA（纯 ASCII 路径）
        if not args.data_dir:
            DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "EU4WikiOffline")
            DB_PATH = os.path.join(DATA_DIR, "search_index.db")
            TITLES_PATH = os.path.join(DATA_DIR, "titles.json")
        os.makedirs(DATA_DIR, exist_ok=True)
        logging.info("数据缓存目录: %s", DATA_DIR)
        root = None
    else:
        root = find_wiki_root(args.root)
        if not root:
            root = ask_wiki_root_gui()
            if not root:
                print("未找到镜像目录，退出。", file=sys.stderr)
                sys.exit(1)
        write_config(root)
        WIKI_ROOT = root
        logging.info("镜像根: %s", root)

    if args.rebuild_index:
        for p in (DB_PATH, TITLES_PATH):
            try:
                os.remove(p)
            except OSError:
                pass

    app = App(root)
    Handler.app = app

    port = pick_free_port(args.port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    logging.info("服务器就绪: %s", url)

    start_url = url if app.index_ready() else url + "first-run"
    if not app.index_ready():
        app.start_prepare()

    if args.headless:
        print("headless 模式运行中: %s （Ctrl+C 退出）" % url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    elif args.browser:
        webbrowser.open(start_url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        try:
            import webview
            # InPrivate 模式：不落盘浏览历史——"已访问"变色只在本次会话内有效，
            # 重启即清空；暗色偏好改由服务端偏好文件保存（不受隐私模式影响）。
            # 用户数据目录固定到 LOCALAPPDATA（纯 ASCII），规避中文路径问题
            udf = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "EU4WikiOffline")
            os.makedirs(udf, exist_ok=True)
            webview.create_window(APP_TITLE, start_url, width=1280, height=840,
                                  min_size=(980, 620))
            webview.start(private_mode=True, storage_path=udf)
            logging.info("窗口已关闭，退出。")
        except Exception:
            logging.error("内嵌窗口不可用，改用系统浏览器:\n%s", traceback.format_exc())
            webbrowser.open(start_url)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    httpd.shutdown()


if __name__ == "__main__":
    main()
