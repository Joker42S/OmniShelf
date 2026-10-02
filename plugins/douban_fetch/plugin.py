"""豆瓣收藏抓取：把豆瓣的看过/读过/玩过抓成可直接导入 OmniShelf 的 JSON。

抓取之后有两种用法：

* **抓取并导入**（页面上的按钮 / `omnishelf plugin douban_fetch fetch_import`）——
  抓完直接调宿主的 `import` 接口入库，和上传页走同一条链路；
* **只抓取**，快照落在插件自己的数据目录，之后到**上传**页手动导入：

    plugins_data/douban_fetch/douban_<媒体>_<状态>.json   <- 直接在 /upload 页上传这个
    plugins_data/douban_fetch/douban_<媒体>_<状态>.detail.json

然后到 **上传** 页选对应的 importer（`douban_movies` / `douban_books` / `douban_games`），
把这个 json 传上去即可（上传页会先存档到 `data/imports/<来源>/` 再导入）。

实现上遵守插件规范：

* 继承 ``omnishelf.plugins.api.Plugin``（**插件唯一允许 import 的宿主模块**）；
* 只用标准库（``urllib``/``re``/``json``/``threading``/``pathlib``）+ 这个 SDK；
* 运行时数据只写 ``ctx.data_dir``；
* 抓取在**点按钮之后于后台线程**跑（页面靠轮询它刷新进度条），不在 ``start()`` 里做重活；
  点完按钮回的那一屏一律先画进度条，结果交给下一次轮询——秒级失败也不会把报告
  塞进 POST 响应后就把轮询链路绕过去；
* 出错自己兜住：配置缺项/填错都给默认值 + 明确提示，不让页面变成 500。

关于「抓到的条数比豆瓣报的少」：差额有两种，都**不是抓漏**——豆瓣里已被删除/隐藏的条目
（仍计入总数，列表页不渲染），以及标了**仅自己可见**的条目（游客视角连总数都会少报，
要在页面上填登录 Cookie 才抓得到）。页面会把差额和这一趟的认证方式单独标出来。
"""

from __future__ import annotations

# 豆瓣收藏抓取核心：HTTP、三个解析器、增量抓取。
#
# 只用标准库（urllib + re + json），**不 import 宿主内部模块**（插件规范红线）。
#
# 三个站的 HTML 结构实测并不一样，所以有三个解析器：
#
#     movie  <div class="item comment-item">   <li class="title"><a><em>标题</em>
#     book   <li class="subject-item">         <div class="info"><h2><a title="标题">
#     game   <div class="common-item">         <div class="content"><div class="title"><a>
#
# 星级 class 也不同：movie/book 是 ``rating5-t``（一位数 5 星制），game 是 ``allstar40``（两位数十星制）。
#
# 反爬要点（都是实测踩出来的，别改回去）：
#   * 拦截页（``请点击下方按钮继续浏览``，约 3.2KB）**绝不能当成「列表抓完了」**，
#     否则会静默少抓一大截（总量上千条的列表尤其明显）；
#   * 被拦时短间隔重试只会续期，必须长时间静默（默认 10/20/30 分钟）；
#   * book 站缺 ``Referer`` 更容易吃 nginx 403，所以每个请求都带站点 Referer。
#
# 关于「抓到的条数比豆瓣报的少」：差额有两种来源，都**不是抓漏**（readme 里有实测）：
#   * 条目在豆瓣上已被删除或下架——仍被计入站点总数，但列表页不渲染；
#   * 条目被设成了**仅自己可见**（私密标记）——游客视角根本看不到，站点报的总数
#     也会跟着变小（实测同一个列表：游客 1395 条 / 登录 1402 条）。
#     要抓全这一类必须带登录 Cookie：见下面的 ``normalize_cookie`` / ``http_get``。

# ---------------------------------------------------------------------------
# 以下是抓取库（原 douban.py），和入口放在同一个文件里：
# 宿主是按 `exec` 源码的方式加载入口文件的，插件目录不是 Python 包，
# 所以同目录的兄弟模块既不能 `from . import x`，动态加载也会绕开热重载指纹。
# ---------------------------------------------------------------------------


import html
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from omnishelf.plugins.api import Plugin, cmd

PAGE_SIZE = 15

#: 连翻这么多页就强制歇一会儿（再往前翻就是豆瓣开始限速的脸）
REST_EVERY_PAGES = 20
#: 歇多久（秒）。真被拦了要靠「等 30 分钟以上」那一档，这里只是别让节奏太密
REST_SECONDS = 60.0

MEDIA_CN = {"movie": "电影/剧集", "book": "读书", "game": "游戏"}
STATUS_CN = {
    "movie": {"collect": "看过", "do": "在看", "wish": "想看"},
    "book": {"collect": "读过", "do": "在读", "wish": "想读"},
    "game": {"collect": "玩过", "do": "在玩", "wish": "想玩"},
}
#: 豆瓣状态 -> OmniShelf 状态（和内置 importer 的 transforms 保持一致）
OMNISHELF_STATUS = {"collect": "done", "do": "doing", "wish": "wishlist"}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

STAR_RE = {
    "movie": re.compile(r"rating(\d)-t"),
    "book": re.compile(r"rating(\d)-t"),
    "game": re.compile(r"allstar(\d{2})"),
}


# --------------------------------------------------------------------------
# URL / HTTP
# --------------------------------------------------------------------------

def build_url(media: str, user: str, status: str, start: int = 0) -> str:
    if media == "game":
        return f"https://www.douban.com/people/{user}/games?action={status}&start={start}"
    host = "movie.douban.com" if media == "movie" else "book.douban.com"
    return (f"https://{host}/people/{user}/{status}"
            f"?start={start}&sort=time&rating=all&filter=all&mode=grid")


def referer_for(media: str) -> str:
    return {
        "movie": "https://movie.douban.com/",
        "book": "https://book.douban.com/",
        "game": "https://www.douban.com/",
    }[media]


#: Cookie 里代表「已经登录」的东西只有这一个；其余字段都是可有可无的伴生 cookie。
LOGIN_COOKIE = "dbcl2"
#: 浏览器 Cookie 面板里 dbcl2 的值的形状：``用户 ID:一串 token``。
#: 只贴裸值时用它把关——省得把「整页 HTML」当成 Cookie 发出去。
_BARE_LOGIN_VALUE = re.compile(r"^\d{5,}:[A-Za-z0-9._-]+$")
#: 粘贴版 Cookie 常见的名字分隔（浏览器扩展导出常是 `名字<TAB>值` 或 `名字 = 值`）。
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
#: 一份 Cookie 撑死几百字节；超过这个长度基本是把整页 HTML 粘进来了。
MAX_COOKIE_LENGTH = 4000


def normalize_cookie(value: Any) -> str:
    """把用户粘贴的 Cookie 整理成 ``k1=v1; k2=v2`` 一个字符串。

    认这三种贴法（都是从浏览器 Cookie 面板/导出里直接抄的形状）：

    * 一整串 Header 值：``dbcl2="12345:abcdef"; ck=xy``；
    * 一行一个 ``名字 值`` / ``名字 = 值`` / ``名字：值``（浏览器扩展的两列导出）；
    * 只贴了 ``dbcl2`` 的裸值 ``12345:abcdef``（带不带引号都认）——补上名字。
      裸值必须长得像 ``dbcl2``（``数字:token``），不然整页 HTML 也会被当成一个值贴进来。

    值两侧成对的引号会去掉（豆瓣自己下发的是 ``dbcl2="..."``，带引号发也合法，
    但去掉更整齐、也免得用户再抄一层引号）。解析不出任何 ``名字=值`` 就返回空串，
    调用方按「没配 Cookie」处理，绝不肯把一堆乱码当 Cookie 发出去。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    # 逗号在 Cookie 里不合法（值是 67021214:xxx 这种），但导出的 CSV 会拿它当分隔符
    parts: list[str] = []
    for chunk in re.split(r"[;\n\r,+]", text):
        chunk = chunk.replace("：", "=").replace("\t", " ").strip()
        if not chunk or chunk.startswith("#"):
            continue
        if "=" in chunk:
            name, _, raw = chunk.partition("=")
        else:
            head, sep, rest = chunk.partition(" ")
            if sep and _COOKIE_NAME_RE.match(head):
                name, raw = head, rest
            else:
                probe = chunk.strip("\"'").strip()
                if not _BARE_LOGIN_VALUE.match(probe):
                    continue                        # 既不是「名字=值」也不是 dbcl2 的值：不认
                name, raw = LOGIN_COOKIE, probe     # 只贴了裸值：就是 dbcl2
        name = name.strip().strip("\"'")
        raw = raw.strip().strip(",;")
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1].strip()
        if name and _COOKIE_NAME_RE.match(name) and raw:
            parts.append(f"{name}={raw}")
    joined = "; ".join(parts)
    return joined if len(joined) <= MAX_COOKIE_LENGTH else ""


def cookie_names(cookie: str) -> list[str]:
    """Cookie 里有哪些字段（只有**名字**，值一个都不露）。给页面/日志显示用。"""
    seen: list[str] = []
    for chunk in str(cookie or "").split(";"):
        name = chunk.split("=", 1)[0].strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def has_login_cookie(cookie: str) -> bool:
    """这份 Cookie 里有没有 ``dbcl2``（没有它就还是游客，带不带 ck 都一样）。"""
    return LOGIN_COOKIE in cookie_names(cookie)


def cookie_from_form(form: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    """表单里的 Cookie 框 → 这次要用的 Cookie。

    **留空 = 沿用配置里存着的**（和 copymanga/picamanga 的令牌框同一套语义：
    第一次粘对之后，后面几屏框里空着点不该变成「不认证」）；
    勾了「清除」才是真的把它忘掉。值本身不进日志，只报字段名。
    """
    if form.get("clear_cookie") is not None:
        return ""
    return normalize_cookie(form.get("cookie")) or str(config.get("cookie") or "")


def auth_label(cookie: str, logged_in: bool | None = None) -> str:
    """给页面/日志看的一句话认证状态：**只说有没有、成不成，绝不带值**。"""
    if not cookie:
        return "游客（未认证）"
    names = ", ".join(cookie_names(cookie))
    if logged_in is False:
        return f"带 Cookie（{names}）· 豆瓣按游客渲染，Cookie 可能已失效"
    if logged_in:
        return f"带 Cookie（{names}）· 登录态"
    return f"带 Cookie（{names}）"


def _secret_forms(value: str) -> list[str]:
    """一个敏感串可能露出来的各种形状：整串、里面每个 Cookie 值、以及 ``dbcl2`` 的 token 段。

    报错文本里往往只带其中一段（比如 urllib 把 ``Cookie: dbcl2=...`` 截半），
    只按整串去替换就会漏。
    """
    text = str(value or "").strip()
    if not text:
        return []
    forms = [text]
    for chunk in re.split(r"[;\n]", text):
        name, sep, raw = chunk.partition("=")
        if not sep:
            head, spaced, rest = chunk.partition(" ")
            raw = rest if spaced else chunk
            name = ""
        raw = raw.strip().strip("\"'").strip()
        if not raw:
            continue
        forms.append(raw)
        if ":" in raw:                              # dbcl2 的值是 `用户ID:token`
            forms.append(raw.split(":", 1)[1])
            forms.append(raw.split(":", 1)[0] + ":")
    return forms


def redact(text: Any, *secrets: str) -> str:
    """把回显/日志里的敏感值抹掉。

    插件规范红线：**进 ``ctx.log`` 之前先过这里**，宿主不会替我们兜底。
    豆瓣的 Cookie 会写进 ``data/logs/plugins/douban_fetch.log``（长期留在磁盘上、
    还会显示在 ``/logs`` 页），而 urllib 的报错偶尔会把请求 URL/响应片段带出来。
    """
    message = str(text if text is not None else "")
    for secret in secrets:
        for form in _secret_forms(secret):
            if len(form) >= 4:
                message = message.replace(form, "***")
    return message


def looks_logged_in(page_html: str) -> bool:
    """这一页是不是**登录态**渲染出来的。

    实测（三个站都验过）：游客页的导航里没有「退出」，登录后才有；
    自己的列表标题还会从「<昵称>看过的影视」变成「我看过的影视」。
    两个信号取其一即算登录——抓自己的列表时 Cookie 失效会静默退回游客视角、
    悄悄少掉那些「仅自己可见」的条目，所以这一条必须报给用户。
    """
    if not page_html:
        return False
    if "logout" in page_html:
        return True
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.S)
    title = re.search(r"<title>(.*?)</title>", page_html, re.S)
    for found in (heading, title):
        if found and _text(found.group(1)).startswith("我"):
            return True
    return False


@dataclass
class FetchResult:
    status: int | None
    text: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.text)


def http_get(
    url: str,
    media: str,
    *,
    retries: int = 2,
    timeout: int = 20,
    cookie: str = "",
) -> FetchResult:
    """取一页 HTML。``cookie`` 非空就带上——只有带上才看得见「仅自己可见」的条目。

    值本身**绝不进日志/报错文本**：错误信息一律过 :func:`redact`。
    """
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer_for(media),
        "Connection": "close",
    }
    if cookie:
        headers["Cookie"] = cookie
    last = ""
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return FetchResult(response.status, response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            last = redact(f"HTTP {exc.code}", cookie)
            if exc.code in (403, 418, 429) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            return FetchResult(exc.code, redact(body, cookie), last)
        except Exception as exc:  # URLError / timeout / ssl
            last = redact(f"{type(exc).__name__}: {exc}", cookie)
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            return FetchResult(None, "", last)
    return FetchResult(None, "", last)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

@dataclass
class Item:
    subject_id: str = ""            # 去重主键
    title: str = ""
    year: str = ""
    stars: int | None = None        # 1-5
    rating: int | None = None       # 0-10（豆瓣没给星级就是 None）
    marked_date: str = ""
    url: str = ""
    cover: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(raw: str) -> str:
    raw = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(re.sub(r"\s+", " ", raw)).strip()


def _stars(media: str, block: str) -> tuple[int | None, int | None]:
    match = STAR_RE[media].search(block)
    if not match:
        return None, None
    raw = int(match.group(1))
    stars = raw // 10 if media == "game" else raw   # 40 -> 4 星；5 -> 5 星
    return stars, stars * 2


def _total_pages(page_html: str) -> int | None:
    """总页数：优先 data-total-page，读书站没有这个属性就从分页链接推。"""
    match = re.search(r'data-total-page="(\d+)"', page_html)
    if match:
        return int(match.group(1))
    starts = [int(value) for value in re.findall(r"[?&]start=(\d+)", page_html)]
    if starts:
        return max(starts) // PAGE_SIZE + 1
    if re.search(r'class="(item comment-item|subject-item|common-item)"', page_html):
        return 1
    return None


def _total_count(page_html: str, words: str) -> int | None:
    """站点报告的条目总数。

    不能「全页取最大」：游戏页的 tab 同时列着 玩过(427)/在玩(36)/想玩(74)，
    取最大会让三个状态都变成 427。优先读页面大标题，其次读当前选中的 tab。
    """
    heading = (re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.S)
               or re.search(r"<title>(.*?)</title>", page_html, re.S))
    if heading:
        match = re.search(rf"(?:{words})[^（(]{{0,8}}[（(](\d+)[)）]", _text(heading.group(1)))
        if match:
            return int(match.group(1))
    for tab in re.finditer(r"<a[^>]*\bclass=[\"']?[^\"'>]*\bon\b[^\"'>]*[\"']?[^>]*>(.*?)</a>",
                           page_html, re.S):
        match = re.search(rf"(?:{words})[^（(]{{0,8}}[（(](\d+)[)）]", _text(tab.group(1)))
        if match:
            return int(match.group(1))
    found = [int(value) for value in
             re.findall(rf"(?:{words})[^（(]{{0,8}}[（(](\d+)[)）]", page_html)]
    return max(found) if found else None


def _reconcile_total_pages(
    known: int | None, parsed: int | None, reported: int | None
) -> int | None:
    """总页数**只增不减**：取「已知 / 这一页推出来的 / 站点报的条数换算的」里的最大者。

    两条理由：

    * 页面没渲染分页链接时 ``_total_pages`` 会退回「只有 1 页」，那是**猜的**；
      标题里的条数是豆瓣自己报的——报了 45 条就不可能只有一页。不加这条下限，
      ``页码=0`` 抓全量会只翻一页就停，报告还写着「已到最后一页（1）」，看着像抓完了。
    * 翻到中间某页时页码条可能只露出更小的数（比如只有「后一页」），
      后面的页不能把前面已经确认的总页数改小、把这次抓取提前收尾。

    代价：万一 ``reported`` 是别的 tab 的数（游戏页三个状态混排，解析时已经优先取
    当前状态），换算出的下限会偏大，多翻几页空页才停；不丢数据，只是多几个请求。
    """
    from_reported = -(-int(reported or 0) // PAGE_SIZE) if reported else 0
    candidates = [value for value in (known, parsed, from_reported) if value]
    return max(candidates) if candidates else None


def is_challenge(page_html: str) -> bool:
    """豆瓣的反爬拦截页。

    必须**先排除空列表页**：空的「在看」页也只有两三 KB、也没有分页，
    但它有标题、有 ``1-0`` 这种计数，是合法的空列表，不是拦截页
    （测试 test_detects_challenge_and_empty_pages 就是盯这个的）。
    """
    if "点我继续浏览" in page_html or "请点击下方按钮继续浏览" in page_html:
        return True
    if looks_empty(page_html, None):
        return False
    has_title = bool(re.search(r"<h1[^>]*>", page_html)
                     or re.search(r"og:title", page_html)
                     or re.search(r"<title>[^<]*[（(]\d", page_html))
    if has_title or 'class="paginator"' in page_html:
        return False
    return len(page_html) < 6000


def looks_empty(page_html: str, total_count: int | None) -> bool:
    """「这个状态确实是空的」——页面标题就是 看过(0)、分页 1-0。"""
    if total_count == 0:
        return True
    return bool(re.search(r'data-total-page="0"', page_html) or re.search(r"\b1-0\b", page_html))


def parse_movie_page(page_html: str) -> tuple[list[Item], int | None, int | None]:
    body = page_html
    match = re.search(r'<div class="grid-view">(.*?)<div class="paginator">', page_html, re.S)
    if match:
        body = match.group(1)

    items: list[Item] = []
    for block in re.split(r'<div class="item comment-item"[^>]*>', body)[1:]:
        alt = re.search(r'<img[^>]+alt="([^"]*)"', block)
        title = html.unescape(alt.group(1)).strip() if alt else ""
        url = ""
        em = re.search(r'<li class="title">\s*<a href="([^"]+)"[^>]*>\s*<em>(.*?)</em>', block, re.S)
        if em:
            url = em.group(1)
            title = _text(em.group(2)) or title
        if not url:
            anchor = re.search(r'<a[^>]+href="(https?://movie\.douban\.com/subject/\d+/?)"', block)
            url = anchor.group(1) if anchor else ""
        img = re.search(r'<img[^>]+src="([^"]+)"', block)
        intro = ""
        li = re.search(r'<li class="intro">(.*?)</li>', block, re.S)
        if li:
            intro = _text(li.group(1))
        date = re.search(r'<span class="date">\s*([\d-]+)', block)
        stars, rating = _stars("movie", block)
        sid = re.search(r"/subject/(\d+)", url)
        if not (title or url):
            continue
        parts = [part.strip() for part in intro.split("/")] if intro else []
        items.append(Item(
            subject_id=sid.group(1) if sid else "",
            title=title, stars=stars, rating=rating,
            marked_date=date.group(1) if date else "",
            url=url, cover=img.group(1) if img else "",
            extra={
                "intro": intro,
                "language": parts[-1] if parts and not re.search(r"\d", parts[-1]) else "",
                "playable": 'class="playable"' in block,
            },
        ))
    return items, _total_pages(page_html), _total_count(page_html, "看过|在看|想看")


def parse_book_page(page_html: str) -> tuple[list[Item], int | None, int | None]:
    body = page_html
    match = re.search(r'<ul class="interest-list">(.*?)(?:<div class="paginator">)', page_html, re.S)
    if match:
        body = match.group(1)

    items: list[Item] = []
    for block in re.split(r'<li class="subject-item"[^>]*>', body)[1:]:
        title = url = ""
        head = re.search(r'<h2>\s*<a href="([^"]+)"[^>]*title="([^"]*)"', block, re.S)
        if head:
            url, title = head.group(1), html.unescape(head.group(2)).strip()
        else:
            fallback = re.search(r'<h2>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if fallback:
                url, title = fallback.group(1), _text(fallback.group(2))
        img = re.search(r'<img[^>]+src="([^"]+)"', block)
        pub = ""
        pub_match = re.search(r'<div class="pub">(.*?)</div>', block, re.S)
        if pub_match:
            pub = _text(pub_match.group(1))
        note = ""
        note_match = re.search(r'<p class="comment comment-item"[^>]*>(.*?)</p>', block, re.S)
        if note_match:
            note = _text(note_match.group(1))
        date = re.search(r'<span class="date">\s*([\d-]+)', block)
        stars, rating = _stars("book", block)
        sid = re.search(r"/subject/(\d+)", url)

        # pub 形如：作者 / 译者 / 出版社 / 出版年 / 定价（可能缺项，所以按「年份前一段」取出版社）
        parts = [part.strip() for part in pub.split("/")] if pub else []
        year = publisher = ""
        for index, part in enumerate(parts):
            found = re.fullmatch(r"(\d{4})(?:-\d{1,2}(?:-\d{1,2})?)?", part)
            if found and not year:
                year = found.group(1)
                if index > 0:
                    publisher = parts[index - 1]
                break
        if not (title or url):
            continue
        items.append(Item(
            subject_id=sid.group(1) if sid else "",
            title=title, year=year, stars=stars, rating=rating,
            marked_date=date.group(1) if date else "",
            url=url, cover=img.group(1) if img else "",
            extra={"pub": pub, "publisher": publisher, "comment": note},
        ))
    return items, _total_pages(page_html), _total_count(page_html, "读过|在读|想读")


def parse_game_page(page_html: str) -> tuple[list[Item], int | None, int | None]:
    body = page_html
    match = re.search(r'<div class="game-list">(.*?)<div class="paginator">', page_html, re.S)
    if match:
        body = match.group(1)

    items: list[Item] = []
    for block in re.split(r'<div class="common-item">', body)[1:]:
        url = cover = ""
        pic = re.search(r'<div class="pic">(.*?)</div>', block, re.S)
        if pic:
            anchor = re.search(r'<a href="([^"]+)"', pic.group(1))
            if anchor:
                url = anchor.group(1)
            img = re.search(r'<img[^>]+src="([^"]+)"', pic.group(1))
            if img:
                cover = img.group(1)
        title = ""
        title_match = re.search(r'<div class="title">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if title_match:
            url = url or title_match.group(1)
            title = _text(title_match.group(2))
        info = ""
        info_match = re.search(r'<div class="desc">(.*?)<div class="rating-info">', block, re.S)
        if info_match:
            info = _text(info_match.group(1))
        date = re.search(r'<span class="date">\s*([\d-]+)', block)
        stars, rating = _stars("game", block)
        sid = re.search(r"/game/(\d+)", url)

        platforms: list[str] = []
        year = ""
        if info:
            head = re.split(r"(\d{4}-\d{2}-\d{2})", info)[0]
            platforms = [part.strip() for part in head.split("/") if part.strip()]
            found = re.search(r"(\d{4})-\d{2}-\d{2}", info)
            year = found.group(1) if found else ""
        if not (title or url):
            continue
        items.append(Item(
            subject_id=sid.group(1) if sid else "",
            title=title, year=year, stars=stars, rating=rating,
            marked_date=date.group(1) if date else "",
            url=url, cover=cover,
            # 豆瓣把「平台」和「类型」混在同一个 / 串里，列表页区分不了
            extra={"platforms": platforms, "info": info},
        ))
    return items, _total_pages(page_html), _total_count(page_html, "玩过|在玩|想玩")


PARSERS = {"movie": parse_movie_page, "book": parse_book_page, "game": parse_game_page}


# --------------------------------------------------------------------------
# 抓取（分页 + 增量）
# --------------------------------------------------------------------------

@dataclass
class Summary:
    media: str
    status: str
    user: str
    pages_requested: list[int]
    pages_fetched: int = 0
    total_pages: int | None = None
    reported_count: int | None = None
    highest_page: int = 0           # 快照覆盖到的最高页（判断是否抓全过）
    fetched_count: int = 0          # 本次请求抓到的条数
    item_count: int = 0             # 快照累计（含之前抓过的）
    new_count: int = 0              # 本次新增（本地没有的）
    deleted_or_hidden: int | None = None   # 只在快照看起来完整时才给
    stop_reason: str = ""
    #: 是否翻到了列表尽头（靠页码条认出的末页，或翻到空页）
    reached_end: bool = False
    #: 这一趟用什么身份抓的：``cookie``（带登录 Cookie）/ ``guest``（游客）
    auth: str = "guest"
    #: 页面渲染出来的是不是登录态（``None`` = 还没见过页面）。
    #: ``auth=cookie`` 却是 ``False`` => Cookie 已经失效，这一趟其实是游客视角。
    logged_in: bool | None = None
    elapsed: float = 0.0
    page_notes: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)

    def summary_dict(self) -> dict[str, Any]:
        """落盘用的概况。**不含 ``page_notes``**：逐页明细归宿主日志
        （``data/logs/plugins/douban_fetch.log``），详情文件只管「上次抓到什么」。"""
        data = asdict(self)
        data.pop("items", None)
        data.pop("page_notes", None)
        return data


def fetch_list(
    media: str,
    user: str,
    status: str = "collect",
    *,
    pages: tuple[int, int] = (1, 1),
    incremental: bool = False,
    delay: float = 2.5,
    known_ids: Iterable[str] = (),
    merge_with: Mapping[str, dict[str, Any]] | None = None,
    early_stop: bool = True,
    rest_every_pages: int = REST_EVERY_PAGES,
    rest_seconds: float = REST_SECONDS,
    cookie: str = "",
    progress: Any = None,
) -> Summary:
    """抓一个 (媒体, 状态) 列表。

    ``cookie`` 是登录态 Cookie（至少要有 ``dbcl2``）：**豆瓣把「仅自己可见」的条目
    藏起来，游客视角连站点总数都会变小**（实测同一列表：游客 1395 / 登录 1402），
    所以自己的列表要抓全就得带上它。留空就是游客抓，第一页会提示这一点。

    ``pages`` 是 1 基闭区间；``(0, 0)`` 表示抓到底（从 ``pages`` 的第一页开始）。
    ``incremental=True`` 时用 ``known_ids`` 判断：**抓到过新条目之后**再遇到整页已知就停
    （边界上的全已知页只跳过，否则续抓永远抓不到后面的页）。

    ``merge_with`` 是已有快照的 ``{subject_id: item}``：传进来就做**累积**
    （本次抓到的覆盖同 id，其余保留），这样分几次抓也能拼出完整快照。
    **没有单次页数上限**：填了全抓就得把数据抓完，中途停下就是静默少一批；
    豆瓣限速靠 ``delay``（每页之间）和 ``rest_every_pages`` / ``rest_seconds``
    （连翻若干页后强制歇一会儿）撑着，歇完接着翻，不中断。
    ``progress(message, summary)`` 每翻完一页回调一次，第二个参数是**跑到一半**的
    ``Summary``（``pages_fetched`` / ``total_pages`` / 条数都是实时值），页面用它画进度条。
    """
    if media not in PARSERS:
        raise ValueError(f"media 必须是 {sorted(PARSERS)}")
    if status not in ("collect", "do", "wish"):
        raise ValueError("status 必须是 collect / do / wish")
    user = (user or "").strip()
    if not user or not user.isdigit():
        raise ValueError("豆瓣用户 ID 必须是一串数字（URL 里那个，不是昵称）")

    known = {str(value) for value in known_ids}
    accumulated: dict[str, dict[str, Any]] = dict(merge_with or {})
    first_page, last_page = pages
    if first_page <= 0 or last_page <= 0:
        first_page, last_page = 1, 0            # 0 = 抓到没有下一页
    # 起页取 pages 的第一页（至少 1）：47- 这种续抓靠它，不再单开一个「从第几页开始」
    page_no = max(1, first_page)
    entry_page = page_no                        # 本次请求真正开始的那一页
    if last_page and last_page < entry_page:
        last_page = 0                           # 起页已经在末页之后 => 直接抓到底
    first_page = page_no

    summary = Summary(media=media, status=status, user=user,
                      pages_requested=[first_page, last_page],
                      auth="cookie" if cookie else "guest")
    started = time.time()
    merged: dict[str, dict[str, Any]] = {}
    got_new = False

    def note(message: str) -> None:
        summary.page_notes.append(message)
        if progress is not None:
            progress(message, summary)      # 第二个参数是**跑到一半**的 summary，给进度条用
    while True:
        if last_page and page_no > last_page:
            summary.stop_reason = f"到达指定末页 {last_page}"
            break
        # 连翻够 rest_every_pages 页就先歇一会儿：**不是中断**，歇完接着往下翻
        if (rest_every_pages and rest_seconds
                and summary.pages_fetched and summary.pages_fetched % rest_every_pages == 0):
            rest = float(rest_seconds)
            note(f"已连翻 {summary.pages_fetched} 页，按节奏强制休息 {rest:g} 秒再继续")
            time.sleep(rest)

        url = build_url(media, user, status, (page_no - 1) * PAGE_SIZE)
        result = http_get(url, media, cookie=cookie)
        if not result.ok:
            summary.stop_reason = f"第 {page_no} 页请求失败（{result.error or result.status}）"
            note(f"p{page_no} 请求失败：{result.error or result.status}")
            break

        if summary.logged_in is None:
            # 只认第一页给整趟定调：后面某页刚好没渲染出登录痕迹不该把结论翻来覆去
            summary.logged_in = looks_logged_in(result.text)
        if summary.pages_fetched == 0 and not summary.logged_in:
            note(("Cookie 没能换来登录态" if cookie else "游客身份（没配 Cookie）")
                 + "：这一趟看到的是游客视角，"
                   "标了「仅自己可见」的条目不会出现（豆瓣连总数都会少报）"
                 + ("——请换一个还有效的 Cookie 再抓" if cookie else "——要抓全请在上面填 Cookie"))
        if is_challenge(result.text):
            note(f"p{page_no} 被豆瓣反爬拦截页挡住，按插件规范**不再硬刚**，"
                 f"建议等 30 分钟以上再用增量继续")
            summary.stop_reason = f"第 {page_no} 页被反爬拦截，已停止（稍后增量续抓）"
            break

        items, total_pages, reported = PARSERS[media](result.text)
        summary.pages_fetched += 1
        summary.highest_page = max(summary.highest_page, page_no)
        if reported:
            summary.reported_count = reported
        # 总页数只增不减：见 _reconcile_total_pages
        summary.total_pages = _reconcile_total_pages(
            summary.total_pages, total_pages, summary.reported_count
        )

        if page_no == entry_page and not items:
            if looks_empty(result.text, reported):
                summary.stop_reason = f"该状态列表为空（{STATUS_CN[media][status]} 共 0 条）"
                summary.reached_end = True
            elif summary.total_pages is None:
                summary.stop_reason = "页面没有列表结构（可能被风控或页面改版）"
            else:
                summary.stop_reason = "第 1 页无条目"
                summary.reached_end = True
            break

        page_known = 0
        for item in items:
            data = item.to_dict()
            # 内置 importer 是**逐条**读 status 的（collect->done / do->doing / wish->wishlist），
            # 顶层那个 status 它不看：漏了这两行，导入后所有条目都会变成「未设置」。
            data["media"] = media
            data["status"] = status
            data["first_seen"] = data.get("first_seen") or time.strftime("%Y-%m-%d %H:%M:%S")
            data["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if data["subject_id"] and data["subject_id"] in known:
                page_known += 1
            else:
                got_new = True
            key = data["subject_id"] or f"_noid_{data.get('title', '')}"
            merged[key] = data
        # 进度条要显示「到目前为止」的数（循环结束还会按同样口径重算，值是一样的）
        summary.fetched_count = len(merged)
        summary.new_count = sum(1 for key in merged if key not in known)
        summary.item_count = len({**accumulated, **merged})
        note(f"p{page_no} 抓到 {len(items)} 条" + (f"（{page_known} 条本地已有）" if incremental else ""))

        if incremental and early_stop and items and page_known == len(items):
            summary.stop_reason = f"第 {page_no} 页整页都是已存在数据，增量停止"
            break
        if not items:
            summary.stop_reason = f"第 {page_no} 页无条目，视为已到末页"
            summary.reached_end = True
            break
        if summary.total_pages and page_no >= summary.total_pages:
            summary.stop_reason = f"已到最后一页（{summary.total_pages}）"
            summary.reached_end = True
            break

        page_no += 1
        time.sleep(delay + random.uniform(0, 1.5))   # 抖动，避免固定节奏被识别

    summary.fetched_count = len(merged)
    summary.new_count = sum(1 for key in merged if key not in known)
    accumulated.update(merged)
    summary.items = sorted(
        accumulated.values(),
        key=lambda row: (row.get("marked_date") or "", row.get("subject_id") or ""),
        reverse=True,
    )
    summary.item_count = len(summary.items)
    # 差额 = 豆瓣里已删除/隐藏的条目（仍计入站点总数，但列表页不渲染）。
    # 只有「这一次真的翻到了尽头」时这个数才有意义，抓到一半时给 None，免得看着像抓漏。
    complete = summary.reached_end
    if complete and summary.reported_count and summary.reported_count > summary.item_count:
        summary.deleted_or_hidden = summary.reported_count - summary.item_count
    summary.elapsed = round(time.time() - started, 1)
    return summary


# --------------------------------------------------------------------------
# 落盘：一份给 OmniShelf 导入用的 JSON
# --------------------------------------------------------------------------

def build_import_payload(summary: Summary) -> dict[str, Any]:
    """组装成内置 importer（douban_movies / douban_books / douban_games）认识的格式。"""
    return {
        "media": summary.media,
        "media_cn": MEDIA_CN[summary.media],
        "status": OMNISHELF_STATUS[summary.status],
        "status_cn": STATUS_CN[summary.media][summary.status],
        "user": summary.user,
        "source_url": build_url(summary.media, summary.user, summary.status, 0),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reported_count": summary.reported_count,
        "item_count": summary.item_count,
        "fetched_count": summary.fetched_count,
        "highest_page": summary.highest_page,
        "total_pages": summary.total_pages,
        "deleted_or_hidden": summary.deleted_or_hidden,
        "items": summary.items,
    }


def save_snapshot(data_dir: Path, summary: Summary) -> tuple[Path, Path]:
    """写快照文件，返回 (导入用 JSON, 详情 JSON)。文件名固定，重复抓取就是覆盖更新。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    stem = f"douban_{summary.media}_{summary.status}"
    import_path = data_dir / f"{stem}.json"
    detail_path = data_dir / f"{stem}.detail.json"

    payload = build_import_payload(summary)
    _atomic_write(import_path, json.dumps(payload, ensure_ascii=False, indent=2))
    detail = {
        **payload,
        "fetch": summary.summary_dict(),
        "status_raw": summary.status,
        "omnishelf_status": OMNISHELF_STATUS[summary.status],
    }
    _atomic_write(detail_path, json.dumps(detail, ensure_ascii=False, indent=2))
    return import_path, detail_path


def _atomic_write(path: Path, text: str) -> None:
    """先写 .part 再改名，避免宿主/用户读到写了一半的文件。"""
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_snapshot(data_dir: Path, media: str, status: str) -> dict[str, dict[str, Any]]:
    """读已有快照，返回 ``{subject_id: item}``：既用于增量判断，也用于累积合并。"""
    items, _meta = load_snapshot_document(data_dir, media, status)
    return items


def load_snapshot_document(
    data_dir: Path, media: str, status: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """读快照，同时返回顶层元信息（highest_page / total_pages 用来判断是否抓全过）。"""
    path = data_dir / f"douban_{media}_{status}.json"
    if not path.is_file():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}
    rows = payload.get("items")
    items = {str(row["subject_id"]): row for row in rows
             if isinstance(row, dict) and row.get("subject_id")} if isinstance(rows, list) else {}
    return items, payload


def snapshot_is_complete(meta: Mapping[str, Any]) -> bool:
    """快照是否已经抓到最后：highest_page 追平 total_pages（或没有更多页）。"""
    total = meta.get("total_pages")
    highest = meta.get("highest_page")
    if not total or not highest:
        return False
    try:
        return int(highest) >= int(total)
    except (TypeError, ValueError):
        return False


def snapshot_index(data_dir: Path) -> list[dict[str, Any]]:
    """列出所有已抓快照的概况（页面上显示）。"""
    rows: list[dict[str, Any]] = []
    for path in sorted(data_dir.glob("douban_*.json")):
        if path.name.endswith(".detail.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        rows.append({
            "file": path.name,
            "media": payload.get("media", ""),
            "status": payload.get("status", ""),
            "status_cn": payload.get("status_cn", ""),
            "item_count": payload.get("item_count") or len(payload.get("items") or []),
            "reported_count": payload.get("reported_count"),
            "deleted_or_hidden": payload.get("deleted_or_hidden"),
            "fetched_at": payload.get("fetched_at", ""),
            "importer": f"douban_{payload.get('media', '')}s",
            "size_kb": round(path.stat().st_size / 1024, 1),
        })
    rows.sort(key=lambda row: row.get("fetched_at") or "", reverse=True)
    return rows



import time
from typing import Any, Mapping

MEDIA_CHOICES = ("movie", "book", "game")
STATUS_CHOICES = ("collect", "do", "wish")
IMPORTER_NAME = {
    ("movie", "collect"): "douban_movies",
    ("book", "collect"): "douban_books",
    ("game", "collect"): "douban_games",
}

#: 页面表单的默认值（也是配置默认值）
DEFAULTS: dict[str, Any] = {
    "user": "",
    "media": "movie",
    "status": "collect",
    #: 登录态 Cookie（含 ``dbcl2``）。和 copymanga/picamanga 的 token 一个性质：
    #: **会存进本地 config.toml**（那份文件不进 git），但绝不进日志、页面回显和快照。
    "cookie": "",
    # 默认抓全量：少翻几页可能永远少一批数据，多翻几页只是慢一点
    "pages": "0",
    "incremental": True,
    "delay": 2.5,
    "overrides": {},          # 每个 (媒体, 状态) 各自的用户 ID
}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def effective_config(ctx: Any) -> dict[str, Any]:
    """默认值 + 用户配置，并且把每个字段都夹到合法范围（配置写坏了也不能炸）。"""
    config = dict(DEFAULTS)
    raw = ctx.config or {}
    for key, value in raw.items():
        if key in config:
            config[key] = value

    if str(config.get("media")) not in MEDIA_CHOICES:
        config["media"] = DEFAULTS["media"]
    if str(config.get("status")) not in STATUS_CHOICES:
        config["status"] = DEFAULTS["status"]
    config["user"] = str(config.get("user") or "").strip()
    config["cookie"] = normalize_cookie(config.get("cookie"))
    try:
        config["delay"] = min(30.0, max(1.0, float(config.get("delay") or DEFAULTS["delay"])))
    except (TypeError, ValueError):
        config["delay"] = float(DEFAULTS["delay"])
    config["pages"] = (str(config.get("pages") or DEFAULTS["pages"]).strip()
                      or DEFAULTS["pages"])
    config["incremental"] = bool(config.get("incremental", True))
    overrides = config.get("overrides")
    config["overrides"] = overrides if isinstance(overrides, dict) else {}
    return config


def parse_pages(text: str) -> tuple[tuple[int, int], str | None]:
    """把 "1" / "1-5" / "0" / "47-" 解析成 (pages, 错误信息)。"""
    text = (text or "").strip().replace(" ", "")
    if not text:
        return (1, 1), "页码范围不能为空"
    if text == "0":
        return (0, 0), None
    head, sep, tail = text.partition("-")
    if not head.isdigit():
        return (1, 1), f"页码范围看不懂：{text!r}（示例：1 / 1-5 / 47- / 0）"
    start = int(head)
    if not sep:
        end = start
    elif tail == "" or tail == "0":
        end = 0                      # "47-"：从第 47 页抓到底
    elif tail.isdigit():
        end = int(tail)
    else:
        return (1, 1), f"页码范围看不懂：{text!r}（示例：1 / 1-5 / 47- / 0）"
    if start < 1 and end != 0:
        return (1, 1), "起始页必须 ≥ 1（要全抓就填 0）"
    if end and end < start:
        return (1, 1), f"结束页（{end}）不能小于起始页（{start}）"
    return (start, end), None


def user_for(ctx: Any, config: Mapping[str, Any], media: str) -> str:
    """用户 ID：先看这个媒体有没有单独设置，再看全局设置。"""
    overrides = config.get("overrides") or {}
    if isinstance(overrides, Mapping):
        value = str(overrides.get(media) or "").strip()
        if value:
            return value
    return str(config.get("user") or "").strip()


# --------------------------------------------------------------------------
# 抓取任务：进度条要实时更新，所以抓取放在后台线程里跑
# --------------------------------------------------------------------------
#
# 一次抓取短则几秒、长则几分钟（一页 15 条，豆瓣限速又要 2~5 秒一页）。
# 要是让它占着请求线程等，浏览器只能转圈，用户看不出「现在抓到第几页」。所以：
#
#   handle_post  → 校验表单 → 登记任务 + 起后台线程 → **立刻**返回带进度条的页面
#   fetch_list(progress=…) 每翻完一页把页数/条数回写进任务状态
#   页面用 HTMX 每隔 POLL_SECONDS 秒重新请求自己一次，只替换进度条那一块
#
# 状态放在模块全局（进程内）：热重载会换掉模块 globals，正在跑的线程会变成孤儿
# 继续把快照写完，但内存态不会留下「永远显示运行中」的脏文件。

_JOBS_LOCK = threading.Lock()
_JOB: dict[str, Any] | None = None

#: 在跑的任务超过这个时长就当作卡住，允许用户再点一次
JOB_TIMEOUT = 1800.0

#: 进度条刷新间隔（秒）。豆瓣本来就慢，轮询太密只是白费请求。
POLL_SECONDS = 1.5


def _expected_pages(summary: Any) -> int | None:
    """这一轮**打算翻几页**（进度条的分母）：写了末页就用它，抓到底要等站点总页数。"""
    first, last = summary.pages_requested
    if last:
        return max(1, last - first + 1)
    total = summary.total_pages
    return max(1, int(total) - first + 1) if total else None


def _start_job(*, label: str, do_import: bool, plan_pages: int | None) -> dict[str, Any] | None:
    """登记一次抓取任务；已经有在跑的就返回 ``None``（同一插件同时只跑一个）。"""
    global _JOB
    with _JOBS_LOCK:
        current = _JOB
        if (current is not None and current["state"] == "running"
                and time.time() - current["started_at"] < JOB_TIMEOUT):
            return None
        _JOB = {
            "id": uuid.uuid4().hex[:12],
            "label": label,
            "do_import": do_import,
            "state": "running",
            "viewed": False,
            "started_at": time.time(),
            "elapsed": 0.0,
            "pages_fetched": 0,
            "expected_pages": plan_pages,
            "total_pages": None,
            "reported_count": None,
            "fetched_count": 0,
            "new_count": 0,
            "item_count": 0,
            "note": "正在读列表第一页…",
            "notes": [],
            "report": None,
            "import_report": None,
            "error": "",
        }
        return dict(_JOB)


def _job_view() -> dict[str, Any] | None:
    """给页面看的任务快照（进度条 + 最近几行明细）。"""
    with _JOBS_LOCK:
        job = _JOB
        if job is None:
            return None
        view = dict(job)
        view["notes"] = list(job["notes"])
    if view["state"] == "running":
        view["elapsed"] = round(time.time() - view["started_at"], 1)
    return view


def _mark_job_viewed(job_id: str) -> None:
    with _JOBS_LOCK:
        if _JOB is not None and _JOB["id"] == job_id:
            _JOB["viewed"] = True


def _progress_sink(job_id: str) -> Any:
    """``fetch_list(progress=…)``：把每一页的进度写回任务状态。"""

    def update(message: str, summary: Any = None) -> None:
        with _JOBS_LOCK:
            job = _JOB
            if job is None or job["id"] != job_id:
                return
            job["note"] = message
            job["notes"].append(message)
            del job["notes"][:-40]
            job["elapsed"] = round(time.time() - job["started_at"], 1)
            if summary is None:
                return
            job["pages_fetched"] = summary.pages_fetched
            job["total_pages"] = summary.total_pages
            job["reported_count"] = summary.reported_count
            job["fetched_count"] = summary.fetched_count
            job["new_count"] = summary.new_count
            job["item_count"] = summary.item_count
            job["expected_pages"] = _expected_pages(summary)
    return update


def _logging_progress(sink: Any, log: Any) -> Any:
    """把「每翻完一页」那一行**同时**送给页面进度条和宿主的统一日志。

    进度条只看最近 40 行、页面关掉就没了；宿主那份写进
    ``data/logs/plugins/douban_fetch.log``，重启也还在、``/logs`` 页能实时看。
    """

    def update(message: str, summary: Any = None) -> None:
        log(message)
        if sink is not None:
            sink(message, summary)

    return update


def _finish_job(
    job_id: str,
    *,
    state: str = "done",
    report: dict[str, Any] | None = None,
    import_report: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    with _JOBS_LOCK:
        job = _JOB
        if job is None or job["id"] != job_id:
            return
        job["state"] = state
        job["report"] = report
        job["import_report"] = import_report
        job["error"] = error
        job["elapsed"] = round(time.time() - job["started_at"], 1)
        job["note"] = (report or {}).get("stop_reason") or error or "抓取结束"


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

def _render(
    ctx: Any,
    request: Any,
    *,
    report: dict[str, Any] | None = None,
    json_text: str = "",
    error: str = "",
    import_report: dict[str, Any] | None = None,
    just_started: bool = False,
) -> str:
    config = effective_config(ctx)
    query = request.query_params if request is not None else {}
    importer = IMPORTER_NAME.get(
        (str(query.get("media") or config["media"]), str(query.get("status") or config["status"]))
    )
    job = _job_view()
    if job is not None and just_started:
        # 刚点下按钮的这一屏一律先给进度条：哪怕后台已经抢先跑完，结果也留给
        # 进度条的下一次轮询，不然「点一次只看到一次结果」就说不准了。
        job["state"] = "running"
    if job is not None and job["state"] != "running":
        if job["viewed"]:
            job = None                    # 结束而且显示过了，就别再占一块地方
        else:
            # 抓完了：把进度条那一块换成正式报告（和以前同步返回时看到的是同一份东西）
            # 出错也走同一块地方，轮询才能把「失败」这条信息换进页面。
            _mark_job_viewed(job["id"])
            if job["state"] == "done" and job["report"]:
                report = job["report"]
                import_report = job["import_report"]
                json_text = _snapshot_preview(report)
    return ctx.render(
        "page.html",
        config=config,
        media_choices=[(name, MEDIA_CN[name]) for name in MEDIA_CHOICES],
        status_choices=[(name, STATUS_CN[config["media"]][name]) for name in STATUS_CHOICES],
        status_labels={name: labels for name, labels in STATUS_CN.items()},
        snapshots=snapshot_index(ctx.data_dir),
        data_dir=str(ctx.data_dir),
        report=report,
        json_text=json_text,
        error=error,
        import_report=import_report,
        importer=importer,
        # 抓取表单里的用户 ID 要按「当前选中的媒体」取（overrides 才有意义）
        fetch_user=user_for(ctx, config, str(query.get("media") or config["media"])),
        imported=bool(query.get("imported")),
        job=job,
        poll_seconds=POLL_SECONDS,
        # 只报「有没有、有哪些字段名」，Cookie 的值一律不回显（留空=沿用已存的）
        auth_display=auth_label(str(config.get("cookie") or "")),
        has_cookie=bool(config.get("cookie")),
    )


def _snapshot_preview(report: Mapping[str, Any]) -> str:
    """报告里的快照预览（只在快照不大的时候给，避免为了一张页面读几十 MB）。"""
    path = report.get("saved")
    if not path or int(report.get("item_count") or 0) > 400:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _handle_post(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    """表单：保存默认配置，或者执行一次抓取。"""
    action = str(form.get("action") or "save").strip()
    if action == "save":
        return _save_config(ctx, request, form)
    if action == "fetch":
        return _do_fetch(ctx, request, form)
    if action == "fetch_import":
        return _do_fetch(ctx, request, form, do_import=True)
    return _render(ctx, request, error=f"不认识的 action: {action!r}")


def _save_config(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    """保存默认值。

    语义：**以当前配置为底，只覆盖这个表单真的提交了的字段**。
    这样「只改用户 ID」不会把媒体、页码、间隔打回默认值
    （渲染时会把没画成表单控件的项放进隐藏域，所以提交一定是完整的）。
    """
    base = effective_config(ctx)
    render_media = str(form.get("render_media") or base["media"])
    render_status = str(form.get("render_status") or base["status"])
    render_base = dict(base)
    render_base["media"] = render_media
    render_base["status"] = render_status
    if isinstance(base.get("overrides"), Mapping):
        render_base["overrides"] = dict(base["overrides"])
        render_base["user"] = base["overrides"].get(render_media, base["user"])

    values: dict[str, Any] = dict(base)
    for field in ("user", "media", "status", "pages", "delay"):
        if field in form:
            values[field] = form[field]
    # Cookie 不在这张表单里：它跟着抓取走（见 _do_fetch 的 _remember_cookie），
    # 这里只是把已有值原样带过去，不会被这屏打回空。
    # incremental：这个表单里是复选框，所以只认「表单被提交了」的情况
    if str(form.get("form") or "") == "save":
        values["incremental"] = form.get("incremental") is not None
    # 每个媒体单独的 user：带该前缀的字段出现了才动
    if any(f"user_{media}" in form for media in MEDIA_CHOICES):
        overrides = dict(base.get("overrides") or {})
        for media in MEDIA_CHOICES:
            key = f"user_{media}"
            if key not in form:
                continue
            value = str(form.get(key) or "").strip()
            if value:
                overrides[media] = value
            else:
                overrides.pop(media, None)
        values["overrides"] = overrides

    checked = effective_config(_FakeContext(ctx, values))
    user = str(checked["user"])
    if user and not user.isdigit():
        return _render(ctx, request, error=f"用户 ID 必须是一串数字，收到 {user!r}")
    ctx.save_config(checked)
    ctx.log(f"默认配置已保存：user={checked['user'] or '(未设置)'} "
            f"media={checked['media']} status={checked['status']} pages={checked['pages']} "
            f"delay={checked['delay']} "
            f"incremental={checked['incremental']} overrides={checked['overrides']} "
            f"认证={auth_label(str(checked['cookie']))}")
    return _render(ctx, request, report={"saved": True})


class _FakeContext:
    """只为复用 effective_config 的夹取逻辑（不碰磁盘）。"""

    def __init__(self, ctx: Any, config: Mapping[str, Any]) -> None:
        self.config = dict(config)


def _fetch_worker(
    ctx: Any,
    *,
    job_id: str,
    media: str,
    status: str,
    user: str,
    pages: tuple[int, int],
    incremental: bool,
    delay: float,
    do_import: bool,
    cookie: str = "",
) -> None:
    """后台线程里跑的那一遍抓取：抓 → 落快照 →（可选）导入 → 把结果写回任务。"""
    try:
        previous, previous_meta = load_snapshot_document(ctx.data_dir, media, status)
        known_ids = set(previous) if incremental else set()
        # 只有「已有快照本来就抓全过」才允许遇到整页已知就停；
        # 否则（只抓了前半段）必须把指定页码走完，不然第一页就停住、后面永远抓不到。
        complete = snapshot_is_complete(previous_meta)
        early_stop = bool(incremental and complete)
        ctx.log(f"开始抓取：{media}/{status} user={user} pages={pages} "
                f"增量={incremental}（本地快照 {len(previous)} 条，"
                f"{'已完整' if complete else '尚未抓全'}，early_stop={early_stop}）"
                f" 认证={auth_label(cookie)}")

        summary = fetch_list(
            media, user, status,
            pages=pages, incremental=incremental,
            delay=delay, known_ids=known_ids, merge_with=previous, early_stop=early_stop,
            progress=_logging_progress(_progress_sink(job_id), ctx.log),
            cookie=cookie,
        )
        import_path, detail_path = save_snapshot(ctx.data_dir, summary)
        report = summary.summary_dict()
        report.update(
            saved=str(import_path),
            detail=str(detail_path),
            saved_kb=round(import_path.stat().st_size / 1024, 1),
            notes=list(summary.page_notes[-40:]),
            importer=IMPORTER_NAME.get((media, status), f"douban_{media}s"),
            status_cn=STATUS_CN[media][status],
            media_cn=MEDIA_CN[media],
            snapshot_complete=snapshot_is_complete({
                "highest_page": summary.highest_page, "total_pages": summary.total_pages}),
            previous_count=len(previous),
            auth_display=auth_label(cookie, summary.logged_in),
        )
        ctx.log(f"抓取完成：{summary.item_count} 条（新增 {summary.new_count}，"
                f"豆瓣报 {summary.reported_count}，差额 {summary.deleted_or_hidden}），"
                f"认证={auth_label(cookie, summary.logged_in)}，已存 {import_path.name}")

        import_report: dict[str, Any] | None = None
        if do_import:
            import_report = import_snapshot(ctx, import_path, media)
            if import_report.get("error"):
                ctx.log(f"抓取成功但导入失败：{import_report['error']}")
            else:
                ctx.log(f"已导入：新增 {import_report['inserted']}，更新 {import_report['updated']}，"
                        f"错误 {import_report['error_count']}，日志 #{import_report['log_id']}")
        _finish_job(job_id, report=report, import_report=import_report)
    except Exception as exc:                      # 网络/解析/写盘意外都不能让任务卡在运行中
        message = redact(f"抓取失败：{type(exc).__name__}: {exc}", cookie)
        ctx.log(message)
        _finish_job(job_id, state="error", error=message)


def _do_fetch(
    ctx: Any, request: Any, form: Mapping[str, Any], *, do_import: bool = False
) -> str:
    """表单校验（同步）→ 起后台抓取 → 立刻返回带进度条的页面。"""
    config = effective_config(ctx)
    media = str(form.get("media") or config["media"])
    status = str(form.get("status") or config["status"])
    user = str(form.get("user") or user_for(ctx, config, media)).strip()
    pages_text = str(form.get("pages") or config["pages"])
    incremental = form.get("incremental") is not None
    cookie = cookie_from_form(form, config)

    if media not in MEDIA_CHOICES:
        return _render(ctx, request, error=f"媒体类型不认识：{media!r}")
    if status not in STATUS_CHOICES:
        return _render(ctx, request, error=f"状态不认识：{status!r}")
    if not user:
        return _render(ctx, request, error="请先填豆瓣用户 ID（URL 里那串数字）")
    if not user.isdigit():
        return _render(ctx, request, error=f"用户 ID 必须是一串数字，收到 {user!r}")

    pages, pages_error = parse_pages(pages_text)
    if pages_error:
        return _render(ctx, request, error=pages_error)

    try:
        delay = min(30.0, max(1.0, float(form.get("delay") or config["delay"])))
    except (TypeError, ValueError):
        delay = float(config["delay"])

    # 粘了 Cookie 却一串都没解析出来：不能当「没配 Cookie」悄悄抓，那正好是少数据的身份
    raw_cookie = str(form.get("cookie") or "").strip()
    if raw_cookie and not form.get("clear_cookie"):
        if not cookie:
            return _render(ctx, request,
                           error="这串 Cookie 没解析出任何「名字=值」（粘进来的是不是整页 HTML？）")
        if not has_login_cookie(cookie):
            return _render(ctx, request,
                           error="Cookie 里没有 dbcl2——那才是豆瓣的登录凭证，"
                                 "只有 ck / ll 之类的话抓回来还是游客视角")

    first_page, last_page = pages
    entry = max(1, first_page)
    plan_pages = max(1, last_page - entry + 1) if last_page and last_page >= entry else None
    job = _start_job(
        label=f"{MEDIA_CN[media]} · {STATUS_CN[media][status]}（用户 {user}）",
        do_import=do_import,
        plan_pages=plan_pages,
    )
    if job is None:
        return _render(ctx, request,
                       error="已经有一个抓取在跑了，等它的进度条走完再点。")

    _remember_cookie(ctx, cookie, config)
    ctx.log(f"这次的认证方式：{auth_label(cookie)}")

    threading.Thread(
        target=_fetch_worker,
        kwargs={
            "ctx": ctx, "job_id": job["id"], "media": media, "status": status,
            "user": user, "pages": pages,
            "incremental": incremental, "delay": delay, "do_import": do_import,
            "cookie": cookie,
        },
        name=f"douban-fetch-{job['id']}",
        daemon=True,
    ).start()
    return _render(ctx, request, just_started=True)


def _remember_cookie(ctx: Any, cookie: str, config: Mapping[str, Any]) -> None:
    """把这次真的在用的 Cookie 记回配置（只动这一个键）。

    和兄弟插件的「抓取时就记住连接设置」同一套语义：用户在表单里粘对一次，
    下一屏框里空着点「开始抓取」也还是登录态。落盘的就是本地
    ``plugins_data/douban_fetch/config.toml``（在 ``.gitignore`` 里），
    日志只报字段名。
    """
    if cookie == str(config.get("cookie") or ""):
        return
    values = dict(config)
    values["cookie"] = cookie
    try:
        ctx.save_config(values)
    except OSError as exc:
        ctx.log(f"Cookie 没能保存：{redact(exc, cookie)}")


def import_snapshot(ctx: Any, import_path: Path, media: str) -> dict[str, Any]:
    """把快照交给宿主的 **import 接口** 入库。

    走的是和上传页/CLI 完全一样的链路（来源判重、写 import_logs、原始文件存档），
    所以这里不需要（也不应该）自己去碰数据库。出错也**不抛给页面**：
    抓取已经成功了，返回带 ``error`` 的摘要让页面提示用户手动导入即可。
    """
    importer = IMPORTER_NAME.get((media, "collect"), f"douban_{media}s")
    try:
        result = ctx.host.import_file(importer, import_path)
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "path": str(import_path),
            "importer": importer,
        }
    return dict(result) | {"importer": importer, "path": str(import_path)}


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------

class DoubanFetch(Plugin):
    """豆瓣收藏抓取插件。

    只实现契约要求的 ``start()`` / ``stop()``，外加页面与表单两个可选钩子。
    抓取的实现都在上面那些纯函数里（``fetch_list`` / ``save_snapshot`` …），
    它们同样只用标准库，便于单测直接调用。
    """

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        config = effective_config(self.ctx)
        stored = snapshot_index(self.ctx.data_dir)
        self.log(f"{self.title} 已启动：默认 {MEDIA_CN[config['media']]} / "
                 f"{STATUS_CN[config['media']][config['status']]}，用户 "
                 f"{config['user'] or '(未设置)'}，已有快照 {len(stored)} 份")

    def stop(self) -> None:
        self.log("已停止（没有常驻资源需要释放）")

    # ------------------------------------------------------------------ 页面
    def render_page(self, request: Any = None) -> str:
        return _render(self.ctx, request)

    def handle_post(self, request: Any = None, form: Mapping[str, Any] | None = None) -> str:
        return _handle_post(self.ctx, request, form or {})

    # ------------------------------------------------------------------ 命令行
    @cmd("fetch_import", help="抓取豆瓣列表并导入 OmniShelf")
    def fetch_import(
        self,
        media: str = "movie",
        pages: str = "",
        status: str = "collect",
        user: str = "",
        delay: float = 0.0,
        incremental: bool = True,
        dry_run: bool = False,
        cookie: str = "",
    ) -> str:
        """抓取豆瓣的收藏列表，并（默认）直接导入 OmniShelf。

        等价于页面上的「抓取并导入」按钮：抓完调宿主的 import 接口入库，
        和上传页走同一条链路（来源判重、写 import_logs、原始文件存档）。

        media: 媒体类型：movie（电影/剧集）/ book（读书）/ game（游戏）
        pages: 页码范围：1 只抓第一页；1-5 抓 1~5 页；47- 从第 47 页抓到底；0 全抓；
            留空用配置里的默认（默认就是 0，全抓）。续抓就写 47- 这样的起点
        status: 列表：collect（看过/读过/玩过）/ do（在看/在读/在玩）/ wish（想看/想读/想玩）
        user: 豆瓣用户 ID（个人主页 URL 里那串数字）；留空则用插件配置里的
        delay: 每次请求间隔秒数（1~30）；留空/0 用插件配置里的值（默认 2.5 秒，豆瓣限速敏感）
        incremental: 增量抓取（默认开）；关掉就是 --no-incremental，整段重抓
        dry_run: 只抓取不导入；快照照样落盘，之后可以到上传页手动导入
        cookie: 登录态 Cookie（至少要含 dbcl2）；留空用插件配置里存的那个。
            不填就是游客视角，豆瓣上标了「仅自己可见」的条目会抓不到、总数也会少报。
            命令行参数会留在 shell 历史里，长期用建议在页面「保存为默认值」那一栏存好
        """
        config = effective_config(self.ctx)
        pages_text = str(pages or config["pages"])
        parsed_pages, pages_error = parse_pages(pages_text)
        if pages_error:
            raise ValueError(pages_error)
        chosen_user = (user or user_for(self.ctx, config, media)).strip()
        if not chosen_user:
            raise ValueError("要指定豆瓣用户 ID：--user <个人主页 URL 里那串数字>"
                             "（先在页面上保存默认值也可以）")
        chosen_delay = float(delay) if delay else float(config["delay"])
        chosen_status = status if status in STATUS_CHOICES else config["status"]
        pasted = normalize_cookie(cookie)
        if str(cookie or "").strip() and not pasted:
            raise ValueError("--cookie 没解析出任何「名字=值」"
                             "（要的是 dbcl2=... 这样的「名字=值」，不是整页 HTML）")
        chosen_cookie = pasted or str(config.get("cookie") or "")

        previous, previous_meta = load_snapshot_document(self.data_dir, media, chosen_status)
        summary = fetch_list(
            media, chosen_user, chosen_status,
            pages=parsed_pages,
            incremental=incremental,
            delay=min(30.0, max(1.0, chosen_delay)),
            known_ids=set(previous) if incremental else set(),
            merge_with=previous,
            early_stop=bool(incremental and snapshot_is_complete(previous_meta)),
            progress=_logging_progress(None, self.log),
            cookie=chosen_cookie,
        )
        if pasted:
            _remember_cookie(self.ctx, chosen_cookie, config)
        import_path, _detail = save_snapshot(self.data_dir, summary)

        lines = [
            f"抓取完成：{MEDIA_CN[media]} / {STATUS_CN[media][chosen_status]}（用户 {chosen_user}）",
            f"  快照      {import_path}",
            f"  认证      {auth_label(chosen_cookie, summary.logged_in)}"
            + ("" if summary.logged_in is not False else
               "\n            （豆瓣按游客渲染：Cookie 可能已过期，"
               "「仅自己可见」的条目这次抓不到）"),
            f"  条目      {summary.item_count} 条（本次新增 {summary.new_count}，"
            f"抓取 {summary.fetched_count}）",
            f"  页数      抓了 {summary.pages_fetched} 页 · {summary.stop_reason}",
            f"  豆瓣报告  {summary.reported_count or '—'}"
            + (f"（差额 {summary.deleted_or_hidden} 是已删除/隐藏条目）" if summary.deleted_or_hidden else ""),
        ]
        if dry_run:
            lines.append("  导入      已跳过（--dry-run）；到上传页选 importer 导入这个文件即可")
            return "\n".join(lines)

        report = import_snapshot(self.ctx, import_path, media)
        if report.get("error"):
            lines.append(f"  导入      失败：{report['error']}")
            lines.append(f"            （快照没丢，仍可到上传页手动导入）")
            return "\n".join(lines)
        lines.append(
            f"  导入      共 {report['total']} 条 · 新增 {report['inserted']} · "
            f"更新 {report['updated']} · 跳过 {report['skipped']} · "
            f"未变化 {report['unchanged']} · 错误 {report['error_count']} · 警告 {report['warning_count']}"
        )
        lines.append(f"            importer {report['importer']} · 导入日志 #{report['log_id']}"
                     + (f" · 已存档 {report['archive_path']}" if report.get("archive_path") else ""))
        return "\n".join(lines)
