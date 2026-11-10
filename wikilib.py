# -*- coding: utf-8 -*-
"""wikilib — build_index.py 与 app.py 共用的提取/检索工具。

核心约定：FTS5 使用 unicode61 分词器，索引与查询两侧都对 CJK 表意字符做
"逐字插空格"处理（unigram），中文短语查询即精确子串匹配，2 字查询也可毫秒级命中。
"""
import html as _html
import re

# CJK 表意字符（含扩展A/兼容/扩展B），逐字建索引
_CJK_RANGES = (
    "\u3400-\u4dbf"   # CJK 扩展 A
    "\u4e00-\u9fff"   # CJK 基本区
    "\uf900-\ufaff"   # CJK 兼容表意
    "\U00020000-\U0002a6df"  # CJK 扩展 B
)
_CJK_CHAR = re.compile("([" + _CJK_RANGES + "])")
# 显示时还原：仅移除两侧都是表意字符之间的空格
_CJK_GAP = re.compile("(?<=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]) +(?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])")

_TITLE_SUFFIX = " - 欧陆风云4百科，人人可以编辑的欧陆风云4中文百科全书"
_TITLE_TAG = re.compile(r"<title>(.*?)</title>", re.S | re.I)

# 命名空间识别：标题冒号前缀（英文 + 本站中文自定义命名空间）
# disk 文件名里冒号已替换为下划线，这里以 <title> 的冒号为准
_NS_BADGE = {
    "File": "文件", "文件": "文件",
    "Category": "分类", "分类": "分类",
    "Talk": "讨论", "讨论": "讨论",
    "Template": "模板", "模板": "模板",
    "User": "用户", "用户": "用户",
    "Special": "特殊", "特殊": "特殊",
    "Project": "项目", "项目": "项目",
    "MediaWiki": "MediaWiki",
    "Help": "帮助", "帮助": "帮助",
    "Module": "模块", "模块": "模块",
    "欧陆风云4百科": "站务",
    "国家理念": "国家理念",
    # 各种 talk 变体（"User talk:xxx" 等前缀带空格，先前漏识别为条目）
    "User talk": "讨论", "Template talk": "讨论", "File talk": "讨论",
    "Category talk": "讨论", "Help talk": "讨论", "Project talk": "讨论",
    "MediaWiki talk": "讨论", "Module talk": "讨论", "Talk": "讨论",
    "欧陆风云4百科 talk": "站务", "国家理念 talk": "讨论",
}
_NS_ORDER = ["条目", "国家理念", "模板", "分类", "文件", "讨论", "用户", "特殊", "站务", "帮助", "项目", "模块", "MediaWiki"]


def cjk_space(text):
    """索引侧处理：每个 CJK 表意字符前后插空格，使其成为独立 token。"""
    if not text:
        return text
    out = _CJK_CHAR.sub(r" \1 ", text)
    return re.sub(r" {2,}", " ", out)


def cjk_unspace(text):
    """显示侧处理：移除两个 CJK 表意字符之间的空格。"""
    return _CJK_GAP.sub("", text) if text else text


def extract_main(html_text):
    """取 <div id="mw-content-text"> 到 <footer 之间的正文，剥标签得到纯文本。"""
    m = re.search(r'<div id="mw-content-text"', html_text)
    start = m.start() if m else 0
    m2 = re.search(r"<footer", html_text[start:], re.I)
    body = html_text[start:start + m2.start()] if m2 else html_text[start:]

    # 剔除脚本/样式/注释/隐藏元素
    body = re.sub(r"<script\b.*?</script\s*>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<style\b.*?</style\s*>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<noscript\b.*?</noscript\s*>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    # 剥标签
    body = re.sub(r"<[^>]+>", " ", body)
    text = _html.unescape(body)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def extract_title(html_text, fallback_stem):
    """从 <title> 提取页面标题，去掉站点后缀；失败则用文件名还原。"""
    m = _TITLE_TAG.search(html_text)
    if m:
        t = _html.unescape(m.group(1)).strip()
        idx = t.find(_TITLE_SUFFIX)
        if idx > 0:
            t = t[:idx]
        t = t.strip()
        if t:
            return t
    return fallback_stem.replace("_", " ")


def ns_of(title):
    """根据标题冒号前缀判别命名空间，返回 (徽标名, 排序序号)。"""
    i = title.find(":")
    if i > 0:
        prefix = title[:i]
        if prefix in _NS_BADGE:
            badge = _NS_BADGE[prefix]
            return badge, _NS_ORDER.index(badge) if badge in _NS_ORDER else 99
        # 大小写不敏感再试一次（如 file:xxx）
        for k in _NS_BADGE:
            if prefix.lower() == k.lower():
                badge = _NS_BADGE[k]
                return badge, _NS_ORDER.index(badge) if badge in _NS_ORDER else 99
    return "条目", 0


def build_fts_query(q):
    """把用户输入转成 FTS5 MATCH 表达式：CJK 连续段→短语，ASCII 词→普通 term。"""
    if not q:
        return None
    tokens = []
    for run in re.findall(r"[" + _CJK_RANGES + r"]+|[0-9A-Za-z]+", q):
        if re.match(r"[" + _CJK_RANGES + r"]+$", run):
            tokens.append('"' + " ".join(run) + '"')
        else:
            tokens.append(run)
    return " ".join(tokens) if tokens else None


def display_title(spaced_or_raw_title):
    """无论存的是插空格标题还是原始标题，都还原成可显示形式。"""
    return cjk_unspace(spaced_or_raw_title)


# 剥标签时产生的全角标点前/后多余空格（显示层清理）
_PUNCT_BEFORE = re.compile(" +(?=[\uff0c\uff1a\uff1b\uff01\uff1f\u3001\u3002\uff09\uff5d\u201d\u2019%])")
_PUNCT_AFTER = re.compile("(?<=[\uff08\u201c\u2019{]) +")


def tidy_display(text):
    """片段显示优化：清掉全角标点两侧因剥标签留下的空格。"""
    if not text:
        return text
    return _PUNCT_AFTER.sub("", _PUNCT_BEFORE.sub("", text))


# ---------------------------------------------------------------- 阅读模式提取

_DIV_OPEN = re.compile(r"<div\b[^>]*>|</div\s*>", re.I)


def _balanced_div_inner(html_text, open_tag_start):
    """从某个 <div> 开标签起，按配对关系取出其内部 HTML（不含外层标签）。"""
    open_end = html_text.find(">", open_tag_start) + 1
    if open_end == 0:
        return None
    depth = 1
    for m in _DIV_OPEN.finditer(html_text, open_end):
        if m.group(0)[1] == "/":  # </div>
            depth -= 1
            if depth == 0:
                return html_text[open_end:m.start()]
        else:
            depth += 1
    return html_text[open_end:]  # 未闭合则取到结尾


def extract_content_blocks(html_text):
    """提取阅读模式所需区块。

    先剥掉注释与 script 块再配对 div，避免广告注释 / JS 字符串里的
    '<div'、'</div>' 干扰配对计数（正文结构不受影响，样式与图片保留）。
    页面存在两个 mw-parser-output（页头一个只装提示框），必须以
    id="mw-content-text" 为锚点取第二个。

    返回 (正文 HTML 或 None, 分类链接 HTML 或 None, 是否首页, 页头 ambox HTML 或 None)。
    """
    is_main = "wgIsMainPage" in html_text
    cleaned = re.sub(r"<!--.*?-->", "", html_text, flags=re.S)
    cleaned = re.sub(r"<script\b.*?</script\s*>", "", cleaned, flags=re.S | re.I)
    cleaned = re.sub(r"<noscript\b.*?</noscript\s*>", "", cleaned, flags=re.S | re.I)

    anchor = cleaned.find('id="mw-content-text"')
    if anchor < 0:
        anchor = cleaned.find("id='mw-content-text'")
    search_from = anchor if anchor >= 0 else 0

    content = None
    m = re.search(r'<div class="mw-parser-output"[^>]*>', cleaned[search_from:], re.I)
    if m:
        content = _balanced_div_inner(cleaned, search_from + m.start())

    # 页头提示框（"版本可能过时，最后更新于 x.x" 等）：锚点前最后一个
    # parser-output 的内容，文本量小才视为提示，防止误吞大块结构
    ambox = None
    if anchor > 0:
        last = None
        for m in re.finditer(r'<div class="mw-parser-output"[^>]*>', cleaned[:anchor], re.I):
            last = m
        if last:
            inner = _balanced_div_inner(cleaned, last.start())
            if inner and len(re.sub(r"<[^>]+>", " ", inner).strip()) < 1500:
                ambox = inner

    catlinks = None
    mc = re.search(r'<div[^>]+class="catlinks"[^>]*>', cleaned, re.I)
    if mc:
        catlinks = _balanced_div_inner(cleaned, mc.start())
    return content, catlinks, is_main, ambox


# ---------------------------------------------------------------- 目录生成

_H_BLOCK = re.compile(r"<h([2-4])[^>]*>(.*?)</h\1\s*>", re.S | re.I)
_ID_ATTR = re.compile(r'id="([^"]+)"', re.I)
# 标题里有两个 span：空锚点 span（MediaWiki 老式 dot 编码 id，如 ".E9.A9.AC…"）
# 和 mw-headline span（原始中文 id）。目录必须取 mw-headline 的 id。
_HEAD_SPAN_ID = re.compile(
    r'<span[^>]*class="[^"]*mw-headline[^"]*"[^>]*id="([^"]+)"'
    r'|<span[^>]*id="([^"]+)"[^>]*class="[^"]*mw-headline[^"]*"', re.I)


def _strip_toc_div(html_text):
    """移除内联目录框 <div id="toc">…</div>（含其'目录'标题）。"""
    m = re.search(r'<div[^>]+id="toc"[^>]*>', html_text, re.I)
    if not m:
        return html_text
    inner = _balanced_div_inner(html_text, m.start())
    if inner is None:
        return html_text
    open_end = html_text.find(">", m.start()) + 1
    end = open_end + len(inner)
    m2 = re.match(r"\s*</div\s*>", html_text[end:], re.I)
    if m2:
        end += m2.end()
    return html_text[:m.start()] + html_text[end:]


def extract_toc(content_html):
    """从正文中提取 h2-h4 标题生成目录。

    返回 (条目列表 [(级别, 锚点id, 文本)], 处理后的正文)。
    先剥掉内联目录框（侧栏取代它），标题缺锚点 id 时就地补一个。
    """
    items = []
    counter = [0]
    html_text = _strip_toc_div(content_html)

    def _fix(m):
        level, inner = m.group(1), m.group(2)
        hid = None
        idm = _HEAD_SPAN_ID.search(inner)
        if idm:
            hid = idm.group(1) or idm.group(2)
        if not hid:
            idm = _ID_ATTR.search(inner)
            if idm:
                hid = idm.group(1)
        if not hid:
            counter[0] += 1
            hid = "euid-%d" % counter[0]
            inner = '<span id="%s">%s</span>' % (hid, inner)
        text = _html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if text:
            items.append((int(level), hid, text))
        return "<h%s>%s</h%s>" % (level, inner, level)

    out = _H_BLOCK.sub(_fix, html_text)
    return items, out
