"""asmr.one 收藏抓取：把「播放列表」或「私人收藏」抓成可直接导入 OmniShelf 的 JSON。

三件事：

1. **抓取** 接口根地址（默认 ``https://api.asmr.one/api/``），尾巴按抓取目标自动补全：

   * **公开播放列表**（不用登录）：``playlist/get-playlist-works?id=<播放列表 id>``；
   * **私人收藏**（要登录）：``review?order=updated_at&sort=desc&page=…``，
     认证先用手上的 ``authorization``（Bearer 令牌），不行就用用户名+密码登录换一个令牌。

   接口一次给一页**作品完整信息**（title / circle / vas / tags / release / duration /
   language_editions / userRating / mainCoverUrl …），并且支持 ``page`` 与 ``pageSize``
   （``pageSize`` 上限 100，实测 >100 会被回 400），所以全量抓取是「翻页」而不是「逐条补详情」。
   三种抓法（两种目标都一样）：

   * **全量**：``pages = 0``、关掉增量 —— 从第 1 页翻到最后一页；
   * **增量**：``pages = 0``、打开增量 —— 从第 1 页往后翻，遇到**整页都已在快照里**就停
     （两种列表都是新的在最前面：播放列表按加入时间、私人收藏按评价更新时间倒序，
     所以这样不会漏）；
   * **指定范围**：``pages = 1`` / ``1-5`` / ``12-`` —— 只抓这几页（适合「只更新最新几页」）。

2. **落快照**：``plugins_data/asmr_playlist/asmr_playlist_<播放列表 id>.json``，
   形状就是内置 importer ``asmr_one`` 认的 ``{"works": [...]}``（私人收藏那份叫
   ``asmr_playlist_my_favorites.json``）。
   增量抓取是**累积**的：本次抓到的覆盖同 id 的老数据，其余保留，所以快照始终是
   「到目前为止见过的整个列表」。旁边还有一个小的 ``*.meta.json`` 给人看/给页面用
   （导入用的大文件写成紧凑 JSON：几千条约十几 MB，缩进写要多一倍）。

3. **导入**：调宿主的 ``import`` 接口（``self.import_file("asmr_one", ...)``），
   走的是和上传页/CLI 完全一样的链路：来源级判重、写 ``import_logs``、原始文件存档。
   判重靠 ``works[].id`` → ``external_ids.asmr_one``，所以重复导入同一批作品只会「未变化」。
   私人收藏里的 ``userRating``（本人打分）会被 importer 认成**评分**，播放列表里一般全是空。

配置（``plugins_data/asmr_playlist/config.toml``，页面上「保存默认值」或抓取时自动落盘）：

=================  ==========================================================
``base_url``       接口根地址，默认 ``https://api.asmr.one/api/``；尾巴按目标自动补全。
                   也可以整条写成带 ``id=…`` 的完整地址（此时 ``playlist_id`` 可以留空）
``target``         抓取目标：``playlist`` 公开播放列表 / ``favorites`` 私人收藏
``playlist_id``    公开播放列表 id（asmr.one 播放列表页 URL 里的那串 UUID）
``authorization``  私人收藏用的 Bearer 令牌（**会存进本地配置**，和 Cookie 一个性质）
``username``       私人收藏用的账号（配合页面上的密码框；密码本身**不落盘**）
``proxy``          本地代理，如 ``http://127.0.0.1:10809``；留空 = 直连。
                   代理连不上时会自动回退直连一次，并在结果里说明
``page_size``      每页条数（1~100，默认 100，越大越快）
``pages``          默认页码范围：``0`` 全抓 / ``1`` / ``1-5`` / ``12-``
``delay``          每次请求之间的间隔秒数（默认 0.5）
``incremental``    默认是否增量
``keep_raw``       默认 false：丢掉 API 里 importer 明确不用的几个大字段
                   （``translation_info`` / ``rank`` / ``rate_count_detail`` / ``work_attributes``），
                   快照能小三成；需要原始全字段就打开
``timeout``        单次请求超时秒数
=================  ==========================================================

实现上遵守插件规范：

* 继承 ``omnishelf.plugins.api.Plugin``（**插件唯一允许 import 的宿主模块**）；
* 只用标准库（``urllib`` / ``json`` / ``re`` / ``time`` / ``threading`` / ``pathlib``）+ 这个 SDK；
* 运行时数据只写 ``ctx.data_dir``；网络出口只有 :func:`open_response` 一个（方便测试替换）；
* 抓取在**点按钮之后于后台线程**跑（页面靠轮询它刷新进度条），不在 ``start()`` 里做重活；
  点完按钮回的那一屏一律先画进度条，结果交给下一次轮询——秒级失败也不会把报告
  塞进 POST 响应后就把轮询链路绕过去；
* 出错自己兜住：配置写坏了有默认值 + 明确提示，请求失败/返回不是 JSON 都不炸页面。

已知边界（和 douban_fetch 一样，是有意为之）：

* 列表里**被移除**的作品不会从库里删掉——导入接口只新增/更新，不删；
* 增量抓取遇到「整页已知」就停，所以**老作品的字段更新**（比如补了新标签）不会被发现，
  要刷新老数据就抓一次指定范围或全量；
* 私人收藏的令牌会过期：接口回 401 时，填了用户名密码就自动重新登录一次，
  只有令牌（没填密码）的话换一个再抓。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from omnishelf.plugins.api import Plugin, cmd

#: 接口根地址：尾巴（播放列表 / 私人收藏 / 登录）按抓取目标自动补全
DEFAULT_BASE_URL = "https://api.asmr.one/api/"
#: 抓取目标 → 接口尾巴 + 固定参数
TARGETS: dict[str, dict[str, Any]] = {
    "playlist": {"endpoint": "playlist/get-playlist-works", "params": {},
                 "label": "公开播放列表"},
    "favorites": {"endpoint": "review",
                  "params": {"order": "updated_at", "sort": "desc"},
                  "label": "私人收藏"},
}
#: 用户名密码登录换令牌用的地址尾巴（同一个接口：GET 是查自己，POST 是登录）
LOGIN_ENDPOINT = "auth/me"
#: 私人收藏的快照标识（一个账号一份快照，和播放列表共用同一套文件命名）
FAVORITES_ID = "my_favorites"
#: 内置 importer 的名字（kinds 里的 asmr）
IMPORTER_NAME = "asmr_one"
#: pageSize 上限（实测 100 可以，120 就 400 了）
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100
#: importer 明确不导入、又特别占体积的字段（keep_raw=false 时丢掉）
TRIM_FIELDS = ("translation_info", "rank", "rate_count_detail", "work_attributes")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "OmniShelf-asmr_playlist/1.0"
)

DEFAULTS: dict[str, Any] = {
    "base_url": DEFAULT_BASE_URL,
    "target": "playlist",
    "playlist_id": "",
    "authorization": "",
    "username": "",
    "proxy": "",
    "pages": "0",
    "page_size": DEFAULT_PAGE_SIZE,
    "delay": 0.5,
    "incremental": True,
    "keep_raw": False,
    "timeout": 30,
}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def normalize_base_url(value: Any) -> str:
    """接口地址：必须是 http(s) URL；空/不合法的返回空串（调用方再退回默认值）。"""
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    if not text.startswith(("http://", "https://")):
        return ""
    return text


def normalize_proxy(value: Any) -> str:
    """代理地址：空 = 直连；``127.0.0.1:10809`` 这种没写协议的自动补 ``http://``。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if any(ch.isspace() for ch in text):
        return ""
    if "://" not in text:
        text = "http://" + text
    if not text.startswith(("http://", "https://")):
        return ""
    return text


def normalize_target(value: Any) -> str:
    """抓取目标：只认 ``playlist`` / ``favorites``，其余（包括页面的开关值）都归到播放列表。"""
    text = str(value or "").strip().lower()
    if text in ("favorites", "favourite", "favorite", "on", "true", "1", "yes"):
        return "favorites"
    return "playlist"


def normalize_token(value: Any) -> str:
    """把用户粘的 ``authorization`` 变成裸令牌（``Bearer `` 前缀可有可无）。"""
    text = str(value or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def target_label(target: str) -> str:
    return str(TARGETS.get(target, TARGETS["playlist"])["label"])


def effective_config(ctx: Any) -> dict[str, Any]:
    """默认值 + 用户配置，每个字段都夹到合法范围（配置写坏了也不能炸）。"""
    config = dict(DEFAULTS)
    raw = ctx.config or {}
    for key, value in raw.items():
        if key in config:
            config[key] = value

    config["base_url"] = normalize_base_url(config.get("base_url")) or DEFAULT_BASE_URL
    config["target"] = normalize_target(config.get("target"))
    config["playlist_id"] = str(config.get("playlist_id") or "").strip()
    config["authorization"] = normalize_token(config.get("authorization"))
    config["username"] = str(config.get("username") or "").strip()
    config["proxy"] = normalize_proxy(config.get("proxy"))
    config["pages"] = str(config.get("pages") or DEFAULTS["pages"]).strip() or DEFAULTS["pages"]
    try:
        config["page_size"] = max(1, min(int(config.get("page_size") or DEFAULT_PAGE_SIZE),
                                         MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        config["page_size"] = DEFAULT_PAGE_SIZE
    try:
        config["delay"] = min(10.0, max(0.0, float(config.get("delay"))))
    except (TypeError, ValueError):
        config["delay"] = float(DEFAULTS["delay"])
    try:
        config["timeout"] = max(5, min(int(config.get("timeout") or DEFAULTS["timeout"]), 120))
    except (TypeError, ValueError):
        config["timeout"] = int(DEFAULTS["timeout"])
    config["incremental"] = bool(config.get("incremental", DEFAULTS["incremental"]))
    config["keep_raw"] = bool(config.get("keep_raw", DEFAULTS["keep_raw"]))
    return config


def parse_pages(text: str) -> tuple[tuple[int, int], str | None]:
    """把 "0" / "1" / "1-5" / "12-" 解析成 ``((起始页, 结束页), 错误)``；结束页 0 = 到底。"""
    text = (text or "").strip().replace(" ", "")
    if not text:
        return (0, 0), "页码范围不能为空（全抓填 0）"
    if text == "0":
        return (0, 0), None
    head, sep, tail = text.partition("-")
    if not head.isdigit():
        return (1, 1), f"页码范围看不懂：{text!r}（示例：1 / 1-5 / 12- / 0）"
    start = int(head)
    if not sep:
        end = start
    elif tail in ("", "0"):
        end = 0                       # "12-"：从第 12 页抓到底
    elif tail.isdigit():
        end = int(tail)
    else:
        return (1, 1), f"页码范围看不懂：{text!r}（示例：1 / 1-5 / 12- / 0）"
    if start < 1:
        return (1, 1), "起始页必须 ≥ 1（要全抓就填 0）"
    if end and end < start:
        return (1, 1), f"结束页（{end}）不能小于起始页（{start}）"
    return (start, end), None


def playlist_id_of(base_url: str, playlist_id: str = "") -> str:
    """播放列表 id：显式配置优先，其次从 base_url 的 ``id=`` 参数里取。"""
    explicit = str(playlist_id or "").strip()
    if explicit:
        return explicit
    return query_value(base_url, "id")


# --------------------------------------------------------------------------
# URL / HTTP
# --------------------------------------------------------------------------

def split_url(url: str) -> urllib.parse.SplitResult:
    return urllib.parse.urlsplit(url)


def query_value(url: str, key: str) -> str:
    """取 URL 查询串里的一个参数（没有就是空串）。"""
    for name, value in urllib.parse.parse_qsl(split_url(url).query, keep_blank_values=True):
        if name == key:
            return value
    return ""


def set_query_param(url: str, key: str, value: str) -> str:
    """设置（覆盖）URL 查询串里的一个参数，保留其它参数与顺序。"""
    parts = split_url(url)
    pairs = [(name, item) for name, item in
             urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if name != key]
    pairs.append((key, str(value)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(pairs)))


def drop_query_param(url: str, key: str) -> str:
    """去掉查询串里的一个参数（没写就当没写）。"""
    parts = split_url(url)
    pairs = [(name, item) for name, item in
             urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if name != key]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(pairs)))


def api_root(base_url: str) -> str:
    """接口根地址：把 base URL 里已经写好的接口尾巴去掉，只留到 ``…/api``（带尾斜杠）。

    认不出 ``api`` 段就把整条当根用（自建/镜像站点常见写法）；查询串原样留着，
    所以「直接粘一条带 ``id=…`` 的完整地址」这种用法不受影响。
    """
    url = normalize_base_url(base_url)
    if not url:
        return DEFAULT_BASE_URL
    parts = split_url(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    lowered = [segment.lower() for segment in segments]
    if "api" in lowered:
        kept = "/".join(segments[: lowered.index("api") + 1])
        return urllib.parse.urlunsplit(parts._replace(path="/" + kept + "/"))
    return urllib.parse.urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/"))


def endpoint_url(base_url: str, target: str) -> str:
    """把根地址补齐成「这次要抓的接口」，并带上这个目标的固定参数。"""
    endpoint = (LOGIN_ENDPOINT if target == "login"
                else str(TARGETS[normalize_target(target)]["endpoint"]))
    parts = split_url(api_root(base_url))
    path = parts.path.rstrip("/")          # api_root 出来的路径至少是 "/"，rstrip 后可能是空串
    # 已经写到这个接口了（自建/镜像站点整条粘过来）就别再拼一遍
    if path != "/" + endpoint and not path.endswith("/" + endpoint):
        path = f"{path}/{endpoint}"
    url = urllib.parse.urlunsplit(parts._replace(path=path))
    if target != "playlist":
        url = drop_query_param(url, "id")       # 私人收藏/登录都不认这个参数
    if target == "login":
        return url
    for key, value in (TARGETS[normalize_target(target)]["params"] or {}).items():
        if not query_value(url, key):
            url = set_query_param(url, key, str(value))
    return url


def build_page_url(
    base_url: str, playlist_id: str = "", page: int = 1, page_size: int = 0,
    target: str = "playlist",
) -> str:
    """拼出某一页的请求地址。

    * ``base_url`` 里写了 ``{page}`` 占位符就照用它（用户完全自己接管了地址），
      否则按抓取目标把接口尾巴补齐；
    * ``{page}`` / ``page=``：带占位符就替换，不然用 ``page=`` 参数；
    * ``id`` 参数：抓播放列表时才需要 —— ``base_url`` 里已经有就不动，没有就用
      ``playlist_id`` 补上（私人收藏接口认的是令牌，不认 id）；
    * ``page_size`` 给了就带 ``pageSize=``（调用方在服务端不认时会去掉再试）。
    """
    raw = normalize_base_url(base_url)
    url = raw if "{page}" in raw else endpoint_url(raw, target)
    if "{page}" in url:
        url = url.replace("{page}", str(int(page)))
    else:
        url = set_query_param(url, "page", str(int(page)))
    if page_size:
        url = set_query_param(url, "pageSize", str(int(page_size)))
    if (playlist_id and normalize_target(target) == "playlist"
            and not query_value(url, "id")):
        url = set_query_param(url, "id", playlist_id)
    return url


@dataclass
class FetchResult:
    status: int | None
    text: str = ""
    error: str = ""
    used_proxy: bool = False
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.text)


def open_response(request: urllib.request.Request, *, proxy: str = "", timeout: int = 30) -> Any:
    """**唯一的 HTTP 出口**：有代理走 ``ProxyHandler``，没有就用默认 opener。

    单独抽出来是为了让测试能整体替换（测试里同时替换 ``urlopen`` 与 ``build_opener``），
    抓列表的 GET 和登录的 POST 都从这一个口走。
    """
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _base_headers(auth: "Auth | None" = None) -> dict[str, str]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        "Connection": "close",
    }
    if auth is not None:
        headers.update(auth.headers())
    return headers


def _http_get_with_retries(
    url: str, *, proxy: str, timeout: int, retries: int, auth: "Auth | None" = None
) -> FetchResult:
    headers = _base_headers(auth)
    last = ""
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with open_response(request, proxy=proxy, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read().decode("utf-8", "replace")
                return FetchResult(status, body, used_proxy=bool(proxy))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            last = f"HTTP {exc.code}"
            if exc.code in (408, 425, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return FetchResult(exc.code, body, last, used_proxy=bool(proxy))
        except Exception as exc:                      # URLError / 超时 / SSL / 代理拒绝
            last = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return FetchResult(None, "", last, used_proxy=bool(proxy))
    return FetchResult(None, "", last, used_proxy=bool(proxy))


def http_get(
    url: str, *, proxy: str = "", timeout: int = 30, retries: int = 2,
    auth: "Auth | None" = None,
) -> FetchResult:
    """GET 一个 JSON 接口。

    配了代理但**代理连不上**（连不上/超时，``status is None``）时自动回退直连再试一次；
    服务端真的回了 4xx/5xx 就不回退——那是接口在说话，换条链路也一样。
    带了认证信息时，401/403 先换一个令牌再试一次（令牌过期的常见情况）。
    """
    result = _http_get_with_retries(url, proxy=proxy, timeout=timeout, retries=retries, auth=auth)
    if auth is not None and result.status in (401, 403) and auth.relogin():
        result = _http_get_with_retries(
            url, proxy=proxy, timeout=timeout, retries=0, auth=auth)
    if result.ok or not proxy or result.status is not None:
        return result
    fallback = _http_get_with_retries(url, proxy="", timeout=timeout, retries=1, auth=auth)
    if fallback.ok:
        fallback.note = (f"代理 {proxy} 不可用（{result.error or result.status}），"
                         f"已回退直连成功")
        return fallback
    return result


def http_post_json(
    url: str, payload: Mapping[str, Any], *, proxy: str = "", timeout: int = 30
) -> FetchResult:
    """POST 一段 JSON（登录用），返回和 GET 一样形状的 ``FetchResult``，出错不往外抛。"""
    body = json.dumps(payload).encode("utf-8")
    headers = _base_headers()
    headers["Content-Type"] = "application/json"
    routes = [proxy, ""] if proxy else [""]
    last = ""
    for use_proxy in routes:
        try:
            request = urllib.request.Request(url, data=body, headers=headers)
            with open_response(request, proxy=use_proxy, timeout=timeout) as response:
                return FetchResult(getattr(response, "status", 200),
                                   response.read().decode("utf-8", "replace"),
                                   used_proxy=bool(use_proxy))
        except urllib.error.HTTPError as exc:
            return FetchResult(exc.code, exc.read().decode("utf-8", "replace"),
                               f"HTTP {exc.code}", used_proxy=bool(use_proxy))
        except Exception as exc:                      # 连不上/超时：换直连再试一次
            last = f"{type(exc).__name__}: {exc}"
    return FetchResult(None, "", last, used_proxy=bool(proxy))


#: 接口把请求原样回显时也不能让密码进日志/报告
def redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def token_from_login(payload: Mapping[str, Any]) -> str:
    """从登录返回里找令牌：``{"token": …}`` 和 ``{"data": {"token": …}}`` 都认。"""
    rows: list[Mapping[str, Any]] = [payload]
    nested = payload.get("data")
    if isinstance(nested, Mapping):
        rows.append(nested)
    for row in rows:
        for key in ("token", "access_token", "jwt"):
            value = str(row.get(key) or "").strip()
            if value:
                return normalize_token(value)
    return ""


def login(
    base_url: str,
    *,
    username: str,
    password: str,
    proxy: str = "",
    timeout: int = 30,
) -> tuple[str, str]:
    """``POST {根地址}auth/me`` 用用户名密码换一个 Bearer 令牌，返回 ``(令牌, 错误)``。"""
    if not (username and password):
        return "", "要登录就得同时填用户名和密码"
    result = http_post_json(
        endpoint_url(base_url, "login"),
        {"name": username, "password": password},
        proxy=proxy, timeout=timeout,
    )
    if not result.ok:
        detail = f"：{redact(result.text[:120], password)}" if result.text else ""
        reason = redact(result.error or f"HTTP {result.status}", password)
        return "", f"登录失败（{reason}{detail}）——核对一下用户名/密码，或者改用 authorization"
    try:
        payload = json.loads(result.text)
    except ValueError as exc:
        return "", f"登录返回的不是 JSON（{exc}）——改用 authorization 粘令牌"
    if not isinstance(payload, Mapping):
        return "", "登录返回的 JSON 顶层不是对象——改用 authorization 粘令牌"
    token = token_from_login(payload)
    if not token:
        return "", f"登录返回里没有令牌字段：{redact(str(payload)[:120], password)}"
    return token, ""


#: 私人收藏的认证提示（页面和 CLI 共用同一句话）
AUTH_REQUIRED = "抓私人收藏要先认证：粘一个 authorization（Bearer 令牌），或者填用户名和密码"


@dataclass
class Auth:
    """私人收藏要的 Bearer 令牌：先用手上的 authorization，不行就用户名+密码登录。

    令牌被接口拒了（401/403）而表单里填了用户名密码，就重新登录换一个再试
    （一次抓取里只换一次，免得账号是错的却每页都登录）。
    """

    base_url: str = ""
    token: str = ""
    username: str = ""
    password: str = ""
    proxy: str = ""
    timeout: int = 30
    note: str = ""
    error: str = ""
    relogin_attempted: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.token) or bool(self.username and self.password)

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def prepare(self) -> None:
        """抓之前的那一步认证：有令牌就用，没有就登录。"""
        if self.token:
            self.note = "用 authorization 里的令牌认证"
            return
        if self.username and self.password:
            token, error = login(
                self.base_url, username=self.username, password=self.password,
                proxy=self.proxy, timeout=self.timeout,
            )
            self.token, self.error = token, error
            self.note = "用户名密码登录换的令牌" if token else error
            return
        self.error = AUTH_REQUIRED

    def relogin(self) -> bool:
        """换一个令牌；换成功返回 ``True``（调用方会重试这一页）。"""
        if self.relogin_attempted or not (self.username and self.password):
            return False
        self.relogin_attempted = True
        token, error = login(
            self.base_url, username=self.username, password=self.password,
            proxy=self.proxy, timeout=self.timeout,
        )
        if not token:
            self.error = error
            return False
        self.token, self.error = token, ""
        self.note = "令牌被接口拒了，已用用户名密码重新登录"
        return True


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------

@dataclass
class Summary:
    """一次抓取的结果摘要（``works`` 是累积后的完整快照，不进 summary_dict）。"""

    playlist_id: str = ""
    target: str = "playlist"             # playlist / favorites
    base_url: str = ""
    source_url: str = ""
    proxy: str = ""
    auth_note: str = ""                  # 私人收藏用哪种方式认证的（不含令牌）
    mode: str = ""                       # full / incremental / range
    pages_requested: list[int] = field(default_factory=list)
    pages_fetched: int = 0
    total_pages: int | None = None
    reported_count: int | None = None
    page_size: int = 0
    highest_page: int = 0
    next_page: int | None = None     # 只有中途失败时才非空：下次从这一页接着抓
    fetched_count: int = 0               # 本次请求真的抓到的条数
    item_count: int = 0                  # 快照累计
    new_count: int = 0                   # 本次新增（快照里原本没有的）
    stop_reason: str = ""
    elapsed: float = 0.0
    proxy_fallback: bool = False
    trimmed_fields: list[str] = field(default_factory=list)
    reached_end: bool = False            # 本次是不是真的抓到了末页
    coverage_complete: bool = False      # 快照（含以前几次）是不是已经覆盖整个列表
    notes: list[str] = field(default_factory=list)
    works: list[dict[str, Any]] = field(default_factory=list)

    def summary_dict(self) -> dict[str, Any]:
        """落盘用的概况。

        **不含 ``notes``**：逐页明细归宿主日志（``data/logs/plugins/<插件 id>.log``），
        快照和 meta 只管「上次抓到什么」的状态，不留一份每次覆盖、又没人看的历史副本。
        """
        data = asdict(self)
        data.pop("works", None)
        data.pop("notes", None)
        return data


def work_key(work: Mapping[str, Any]) -> str:
    """去重主键：asmr.one 的 ``id``（导入后就是 ``external_ids.asmr_one``）。"""
    value = str(work.get("id") or "").strip()
    if value:
        return value
    return f"_noid_{str(work.get('title') or '')[:80]}"


def normalize_work(work: Mapping[str, Any], *, keep_raw: bool = False) -> dict[str, Any]:
    """整理一条作品：``keep_raw=False`` 时丢掉 importer 不用的几个大字段。"""
    data = dict(work)
    if not keep_raw:
        for key in TRIM_FIELDS:
            data.pop(key, None)
    return data


def snapshot_is_complete(meta: Mapping[str, Any]) -> bool:
    """快照是不是已经覆盖整个播放列表。

    优先看快照自己记的 ``coverage_complete``（**单调**：抓到过末页就一直算抓全，
    后来的增量抓取不会把它抹掉——否则「增量 → 下次又全量」）。老快照没有这个字段时，
    退回「最高页 >= 总页数」的判断。
    """
    if meta.get("coverage_complete") is not None:
        return bool(meta["coverage_complete"])
    total = meta.get("total_pages")
    highest = meta.get("highest_page")
    if not total or not highest:
        return False
    try:
        return int(highest) >= int(total)
    except (TypeError, ValueError):
        return False


def fetch_playlist(
    base_url: str,
    playlist_id: str,
    *,
    pages: tuple[int, int] = (0, 0),
    page_size: int = DEFAULT_PAGE_SIZE,
    incremental: bool = False,
    known_ids: Iterable[str] = (),
    merge_with: Mapping[str, Mapping[str, Any]] | None = None,
    delay: float = 0.5,
    proxy: str = "",
    timeout: int = 30,
    early_stop: bool = True,
    previous_complete: bool = False,
    keep_raw: bool = False,
    progress: Any = None,
    target: str = "playlist",
    auth: Auth | None = None,
) -> Summary:
    """翻页抓一个播放列表 / 私人收藏。

    ``pages`` 是 1 基闭区间，``(0, 0)`` 表示抓到底。
    ``incremental=True`` 时用 ``known_ids`` 判断：某页**整页都已在快照里**就停
    （两种列表都按加入/更新时间的倒序，新的在最前面，所以这样不会漏）。
    ``merge_with`` 是已有快照的 ``{key: work}``，传进来就做**累积**。
    ``previous_complete`` 是「之前的快照已经抓全过」，用来把 ``coverage_complete``
    做成**单调**的：增量抓取只翻了一页，也不能把「已抓全」抹掉。
    **没有单次页数上限**：填了全抓就一路翻到接口报的末页，中途不中断（间隔靠 ``delay``）。
    ``target`` 决定接口尾巴，``auth`` 只在私人收藏时给（播放列表不用）。
    ``progress(message, summary)`` 每翻完一页回调一次，第二个参数是**跑到一半**的
    ``Summary``（页数、条数都是实时值），页面用它画进度条。
    """
    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    known = {str(value) for value in known_ids}
    accumulated: dict[str, dict[str, Any]] = {str(k): dict(v) for k, v in (merge_with or {}).items()}
    seen_keys: set[str] = set()

    first_page, last_page = pages
    if first_page <= 0 or last_page <= 0:
        first_page, last_page = 1, 0                     # 0 = 抓到没有下一页
    # 起页取 pages 的第一页（至少 1）：``12-`` 这种续抓靠它，不再单开一个「起始页」
    page_no = max(1, first_page)
    entry_page = page_no
    if last_page and last_page < entry_page:
        last_page = 0                                    # 起页已在末页之后 => 抓到底
    first_page = page_no

    target = normalize_target(target)

    summary = Summary(
        playlist_id=playlist_id,
        target=target,
        base_url=base_url,
        source_url=build_page_url(base_url, playlist_id, 1, 0, target),
        proxy=proxy,
        auth_note=(auth.note if auth is not None else ""),
        mode=("incremental" if incremental
              else ("full" if (last_page == 0 and first_page == 1) else "range")),
        pages_requested=[first_page, last_page],
        page_size=page_size,
        trimmed_fields=[] if keep_raw else list(TRIM_FIELDS),
    )
    started = time.time()
    server_page_size = page_size
    use_page_size = page_size

    def note(message: str) -> None:
        summary.notes.append(message)
        if progress is not None:
            progress(message, summary)     # 第二个参数是**跑到一半**的 Summary，给进度条用

    while True:
        if last_page and page_no > last_page:
            summary.stop_reason = f"到达指定末页 {last_page}"
            summary.next_page = None
            # 指定末页就是最后一页，才算抓到末尾
            summary.reached_end = bool(summary.total_pages and last_page >= summary.total_pages)
            break

        url = build_page_url(base_url, playlist_id, page_no, use_page_size, target)
        result = http_get(url, proxy=proxy, timeout=timeout, auth=auth)
        if result.status == 400 and use_page_size:
            # 服务端不认 pageSize（不同版本上限不一样）：去掉它，用它自己的每页数量
            note(f"p{page_no} 带 pageSize={use_page_size} 被服务端拒绝（400），"
                 f"改用服务端默认每页数量重试")
            use_page_size = 0
            url = build_page_url(base_url, playlist_id, page_no, 0, target)
            result = http_get(url, proxy=proxy, timeout=timeout, auth=auth)
        if not result.ok:
            reason = result.error or f"HTTP {result.status}"
            extra = ""
            if result.status in (400, 401, 403) and result.text:
                extra = f"：{result.text[:160]}"
            if result.status in (401, 403):
                extra += "（认证没过：换一个 authorization，或者填用户名密码再抓）"
            summary.stop_reason = f"第 {page_no} 页请求失败（{reason}{extra}）"
            summary.next_page = page_no
            note(f"p{page_no} 请求失败：{reason}")
            break
        if result.note:
            summary.proxy_fallback = True
            note(result.note)

        try:
            payload = json.loads(result.text)
        except ValueError as exc:
            summary.stop_reason = f"第 {page_no} 页返回的不是 JSON（{exc}）"
            summary.next_page = page_no
            note(f"p{page_no} 不是 JSON：{exc}")
            break
        if not isinstance(payload, Mapping):
            summary.stop_reason = f"第 {page_no} 页返回的 JSON 顶层不是对象"
            summary.next_page = page_no
            break

        works = [row for row in (payload.get("works") or []) if isinstance(row, Mapping)]
        if not works and payload.get("error"):
            summary.stop_reason = f"接口报错：{payload['error']}"
            summary.next_page = None
            note(f"接口报错：{payload['error']}")
            break

        pagination = payload.get("pagination")
        pagination = pagination if isinstance(pagination, Mapping) else {}
        reported = pagination.get("totalCount")
        if isinstance(reported, int) and reported >= 0:
            summary.reported_count = reported
        reported_page_size = pagination.get("pageSize")
        if isinstance(reported_page_size, int) and reported_page_size > 0:
            server_page_size = reported_page_size
        if server_page_size:
            summary.page_size = server_page_size
            summary.total_pages = max(1, -(-summary.reported_count // server_page_size)) \
                if summary.reported_count else 0

        summary.pages_fetched += 1
        summary.highest_page = max(summary.highest_page, page_no)

        if page_no == entry_page and not works:
            if summary.reported_count == 0:
                summary.stop_reason = f"{target_label(summary.target)}是空的（0 个作品）"
            else:
                summary.stop_reason = "第 1 页没有作品"
            summary.next_page = None
            summary.reached_end = True
            break

        page_known = 0
        for work in works:
            key = work_key(work)
            if key in known:
                page_known += 1
            seen_keys.add(key)
            accumulated[key] = normalize_work(work, keep_raw=keep_raw)
        # 进度条要显示「到目前为止」的数（循环结束还会按同样口径重算，值是一样的）
        summary.fetched_count = len(seen_keys)
        summary.new_count = sum(1 for key in seen_keys if key not in known)
        summary.item_count = len(accumulated)
        note(f"p{page_no} 抓到 {len(works)} 条"
             + (f"（{page_known} 条本地已有）" if incremental else ""))

        if incremental and early_stop and works and page_known == len(works):
            summary.stop_reason = f"第 {page_no} 页整页都是已有数据，增量停止"
            summary.next_page = None
            # 能在这里停，本身就说明之前的快照已经抓全过
            summary.reached_end = previous_complete
            break
        if not works:
            summary.stop_reason = f"第 {page_no} 页无作品，视为已到末页"
            summary.next_page = None
            summary.reached_end = True
            break
        if summary.total_pages and page_no >= summary.total_pages:
            summary.stop_reason = f"已到最后一页（{summary.total_pages}）"
            summary.next_page = None
            summary.reached_end = True
            break

        page_no += 1
        if delay:
            time.sleep(delay)

    summary.fetched_count = len(seen_keys)
    summary.new_count = sum(1 for key in seen_keys if key not in known)
    # 新的排在快照前面：播放列表看加入时间，私人收藏没有这个字段、就看评价的更新时间
    summary.works = sorted(
        accumulated.values(),
        key=lambda row: (str(row.get("playlist_rel_created_at")
                          or row.get("updated_at") or ""),
                         str(row.get("id") or "")),
        reverse=True,
    )
    summary.item_count = len(summary.works)
    # 「已抓全」是**单调**的：这次抓到了末页，或者以前抓到过末页并且这次是从头接着抓的，
    # 都算覆盖了整个列表。否则一次只翻一页的增量抓取会把「已抓全」抹掉，
    # 下次就变成全量重抓（实测踩过）。
    summary.coverage_complete = bool(summary.reached_end or previous_complete)
    summary.elapsed = round(time.time() - started, 1)
    return summary


# --------------------------------------------------------------------------
# 快照落盘
# --------------------------------------------------------------------------

def safe_slug(text: str) -> str:
    """把播放列表 id 变成能当文件名的短标识（UUID 原样保留）。"""
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(text or "").strip())
    return slug.strip("._-")[:80] or "default"


def snapshot_path(data_dir: Path, playlist_id: str) -> Path:
    """导入用的快照文件（``{"works": [...]}``，直接喂给 importer asmr_one）。"""
    return data_dir / f"asmr_playlist_{safe_slug(playlist_id)}.json"


def meta_path(data_dir: Path, playlist_id: str) -> Path:
    """给人看 / 给页面用的小元信息文件（不读大 JSON 就能列快照）。"""
    return data_dir / f"asmr_playlist_{safe_slug(playlist_id)}.meta.json"


def _atomic_write(path: Path, text: str) -> None:
    """先写 .part 再改名，避免宿主/用户读到写了一半的文件。"""
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_snapshot(data_dir: Path, summary: Summary) -> tuple[Path, Path]:
    """写快照 + 元信息，返回 ``(导入用 JSON, 元信息 JSON)``。同名就是覆盖更新。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = time.strftime("%Y-%m-%d %H:%M:%S")
    info = summary.summary_dict() | {"fetched_at": fetched_at}
    payload = {
        "works": summary.works,
        "pagination": {
            "page": 1,
            "pageSize": len(summary.works),
            "totalCount": len(summary.works),
        },
        "playlist": {
            "id": summary.playlist_id,
            "base_url": summary.base_url,
            "source_url": summary.source_url,
            "reported_count": summary.reported_count,
        },
        "fetch_info": info,
    }
    import_file = snapshot_path(data_dir, summary.playlist_id)
    meta_file = meta_path(data_dir, summary.playlist_id)
    # 导入用的大文件写**紧凑 JSON**：5000 多条的作品列表缩进写要 37 MB，紧凑写 18 MB；
    # 它是机器读的，人看结构有旁边的 meta 文件和页面预览。
    _atomic_write(import_file, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    _atomic_write(meta_file, json.dumps(
        {"playlist": payload["playlist"], "fetch_info": info},
        ensure_ascii=False, indent=2,
    ))
    return import_file, meta_file


def load_snapshot(
    data_dir: Path, playlist_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """读快照，返回 ``({key: work}, fetch_info)``：既用于增量判断，也用于累积合并。"""
    path = snapshot_path(data_dir, playlist_id)
    if not path.is_file():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(payload, Mapping):
        return {}, {}
    rows = payload.get("works")
    works: dict[str, dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping):
                works[work_key(row)] = dict(row)
    info = payload.get("fetch_info")
    return works, (dict(info) if isinstance(info, Mapping) else {})


def snapshot_index(data_dir: Path) -> list[dict[str, Any]]:
    """列出所有快照的概况（页面上显示；只读小元信息文件）。"""
    rows: list[dict[str, Any]] = []
    for path in sorted(data_dir.glob("asmr_playlist_*.meta.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, Mapping):
            continue
        playlist = payload.get("playlist")
        playlist = playlist if isinstance(playlist, Mapping) else {}
        info = payload.get("fetch_info")
        info = info if isinstance(info, Mapping) else {}
        playlist_id = str(playlist.get("id") or info.get("playlist_id") or "")
        import_file = snapshot_path(data_dir, playlist_id)
        rows.append({
            "file": import_file.name,
            "meta_file": path.name,
            "playlist_id": playlist_id,
            "item_count": info.get("item_count") or 0,
            "reported_count": info.get("reported_count"),
            "highest_page": info.get("highest_page"),
            "total_pages": info.get("total_pages"),
            "next_page": info.get("next_page"),
            "mode": info.get("mode", ""),
            "target": normalize_target(info.get("target", "playlist")),
            "fetched_at": info.get("fetched_at", ""),
            "complete": snapshot_is_complete(info),
            "size_mb": round(import_file.stat().st_size / 1048576, 2) if import_file.is_file() else 0,
            "importer": IMPORTER_NAME,
        })
    rows.sort(key=lambda row: row.get("fetched_at") or "", reverse=True)
    return rows


# --------------------------------------------------------------------------
# 导入
# --------------------------------------------------------------------------

def import_snapshot(ctx: Any, import_file: Path) -> dict[str, Any]:
    """把快照交给宿主的 **import 接口** 入库（importer ``asmr_one``）。

    走的是和上传页/CLI 完全一样的链路（来源判重、写 import_logs、原始文件存档）。
    出错**不抛给页面**：抓取已经成功了，返回带 ``error`` 的摘要让页面提示手动导入。
    """
    importer = IMPORTER_NAME
    try:
        available = list(ctx.host.importer_names())
        if importer not in available:
            return {
                "error": f"宿主没有注册 {importer} importer（可选：{', '.join(available) or '无'}）",
                "path": str(import_file),
                "importer": importer,
            }
        result = ctx.host.import_file(importer, import_file)
    except Exception as exc:                      # FileNotFoundError / ValueError / 写库错误
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "path": str(import_file),
            "importer": importer,
        }
    return dict(result) | {"importer": importer, "path": str(import_file)}


def asmr_count(ctx: Any) -> int | None:
    """库里现有多少条 asmr（页面上的参考数字；查询接口出问题就不显示）。"""
    try:
        return int(ctx.host.count_items(kind="asmr"))
    except Exception:
        return None


# --------------------------------------------------------------------------
# 抓取任务：进度条要实时更新，所以抓取放在后台线程里跑
# --------------------------------------------------------------------------
#
# 全量抓一个几千条的播放列表要翻几十页、配了代理更慢；让它占着请求线程等，
# 浏览器只能转圈。所以 ``handle_post`` 只做校验 + 起线程 + 立刻返回带进度条的页面，
# ``fetch_playlist(progress=…)`` 每翻完一页把进度回写到这里，页面用 HTMX 定时轮询自己。
#
# 状态放在模块全局（进程内）：插件热重载会换掉模块 globals，正在跑的线程继续把快照
# 写完，但内存态不会留下「永远显示运行中」的脏文件。

_JOBS_LOCK = threading.Lock()
_JOB: dict[str, Any] | None = None

#: 在跑的任务超过这个时长就当作卡住，允许用户再点一次
JOB_TIMEOUT = 1800.0

#: 进度条刷新间隔（秒）
POLL_SECONDS = 1.0


def _expected_pages(summary: Any) -> int | None:
    """这一轮**打算翻几页**（进度条的分母）：写了末页就用它，抓到底要等接口报总条数。"""
    first, last = summary.pages_requested
    if last:
        return max(1, last - first + 1)
    total = summary.total_pages
    return max(1, int(total) - first + 1) if total else None


def _start_job(
    *, label: str, do_import: bool, plan_pages: int | None,
    first_note: str = "正在读播放列表第一页…",
) -> dict[str, Any] | None:
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
            "note": first_note,
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
    """``fetch_playlist(progress=…)``：把每一页的进度写回任务状态。"""

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
# 表单 / 页面
# --------------------------------------------------------------------------

def _validate_connection(
    base_url: str, playlist_id: str, proxy: str, *, target: str = "playlist"
) -> str:
    """连接项的校验：返回错误信息（空串 = 通过）。"""
    if not base_url:
        return "接口地址（base URL）必须以 http:// 或 https:// 开头，不能为空"
    if normalize_target(target) == "playlist":
        if not playlist_id and not query_value(base_url, "id"):
            return ("请填播放列表 ID（asmr.one 播放列表页 URL 里那串 UUID），"
                    "或在 base URL 里带上 id=")
        if playlist_id and any(ch.isspace() for ch in playlist_id):
            return "播放列表 ID 不能包含空白字符"
    if proxy and not proxy.startswith(("http://", "https://")):
        return "代理地址要写成 http://127.0.0.1:10809 这样（留空 = 直连）"
    return ""


def _connection_from_form(
    form: Mapping[str, Any], config: Mapping[str, Any]
) -> tuple[str, str, str, str, str]:
    """从表单读连接四项，返回 ``(base_url, playlist_id, proxy, target, 错误)``。

    只有表单**真的提交了**那个字段才动它（没提交 = 用配置里的）；
    提交了但写错就报错，**不能默默退回默认值**——否则用户以为自己改了。
    """
    target = normalize_target(config.get("target", "playlist"))
    # 复选框不勾就压根不提交，所以模板里跟一个同名标记（有标记才认这次表里的开关）
    if str(form.get("has_favorites") or ""):
        target = "favorites" if form.get("favorites") else "playlist"

    if "base_url" in form:
        raw_base = str(form.get("base_url") or "").strip()
        if raw_base and not normalize_base_url(raw_base):
            return "", "", "", target, "接口地址（base URL）必须以 http:// 或 https:// 开头"
        base_url = normalize_base_url(raw_base) or config["base_url"]
    else:
        base_url = str(config["base_url"])

    if "proxy" in form:
        raw_proxy = str(form.get("proxy") or "").strip()
        if raw_proxy and not normalize_proxy(raw_proxy):
            return "", "", "", target, "代理地址要写成 http://127.0.0.1:10809 这样（留空 = 直连）"
        proxy = normalize_proxy(raw_proxy)
    else:
        proxy = str(config["proxy"])

    raw_playlist = (str(form.get("playlist_id") or "").strip()
                    if "playlist_id" in form else str(config["playlist_id"]))
    playlist_id = playlist_id_of(base_url, raw_playlist)
    return base_url, playlist_id, proxy, target, ""


def _auth_from_form(
    form: Mapping[str, Any], config: Mapping[str, Any], *, base_url: str, proxy: str, timeout: int
) -> Auth:
    """认证信息：``authorization`` 优先，其次用户名 + 密码（密码**只在本次表单里**，不落盘）。"""
    token = (normalize_token(form.get("authorization")) if "authorization" in form
             else str(config.get("authorization") or ""))
    username = (str(form.get("username") or "").strip() if "username" in form
                else str(config.get("username") or ""))
    return Auth(
        base_url=base_url,
        token=token,
        username=username,
        password=str(form.get("password") or "").strip(),
        proxy=proxy,
        timeout=timeout,
    )


def _persist_connection(
    ctx: Any, base_url: str, playlist_id: str, proxy: str, *,
    target: str = "playlist", authorization: str = "", username: str = "",
) -> None:
    """把这次用的连接项固化进配置（用户不用每次重填）。

    刻意**不保存密码**：配置文件是给「下次点开还在」用的，口令这种东西一次一填。
    """
    values = dict(effective_config(ctx))
    values.update({
        "base_url": base_url,
        "playlist_id": playlist_id,
        "proxy": proxy,
        "target": normalize_target(target),
        "authorization": normalize_token(authorization),
        "username": str(username or "").strip(),
    })
    try:
        ctx.save_config(values)
    except OSError as exc:                        # 写不进磁盘不影响这次抓取
        ctx.log(f"连接配置没能保存：{exc}")


def _persist_token(ctx: Any, token: str) -> None:
    """登录换来的 authorization 存回配置：下次直接用它，不用再登一次。

    只补这一个字段——密码照样不落盘。
    """
    values = dict(effective_config(ctx))
    values["authorization"] = normalize_token(token)
    try:
        ctx.save_config(values)
    except OSError as exc:
        ctx.log(f"authorization 没能保存：{exc}")


def _importer_available(ctx: Any) -> bool:
    try:
        return IMPORTER_NAME in list(ctx.host.importer_names())
    except Exception:
        return False


def _render(
    ctx: Any,
    request: Any,
    *,
    report: dict[str, Any] | None = None,
    import_report: dict[str, Any] | None = None,
    error: str = "",
    notice: str = "",
    json_text: str = "",
    just_started: bool = False,
) -> str:
    config = effective_config(ctx)
    query = getattr(request, "query_params", None) or {}

    def form_value(key: str) -> Any:
        if key in query:
            return query.get(key)
        return config.get(key)

    playlist_id = str(query.get("playlist_id") or config["playlist_id"])
    snapshot_key = FAVORITES_ID if config["target"] == "favorites" else playlist_id
    snapshots = snapshot_index(ctx.data_dir)
    latest = next((row for row in snapshots
                   if row.get("playlist_id") == snapshot_key), None)

    # 续抓提示：上次中途失败停在第几页，就建议「页码范围」填 N- 接着抓
    resume_hint = None
    if latest and latest.get("next_page"):
        resume_hint = int(latest["next_page"])

    form = {
        "base_url": form_value("base_url"),
        "playlist_id": playlist_id,
        "proxy": form_value("proxy"),
        "favorites": config["target"] == "favorites",
        "username": config["username"],
        "authorization": config["authorization"],
        "pages": form_value("pages"),
        "page_size": form_value("page_size"),
        "delay": form_value("delay"),
        "incremental": config["incremental"],
        "keep_raw": config["keep_raw"],
    }
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
            _mark_job_viewed(job["id"])
            if job["state"] == "done" and job["report"]:
                report = job["report"]
                import_report = job["import_report"]
                json_text = _preview_text(
                    Path(report["saved"]), int(report.get("item_count") or 0))
    return ctx.render(
        "page.html",
        config=config,
        form=form,
        snapshots=snapshots,
        latest=latest,
        resume_hint=resume_hint,
        data_dir=str(ctx.data_dir),
        default_base_url=DEFAULT_BASE_URL,
        importer=IMPORTER_NAME,
        importer_available=_importer_available(ctx),
        asmr_total=asmr_count(ctx),
        report=report,
        import_report=import_report,
        error=error,
        notice=notice,
        json_text=json_text,
        job=job,
        poll_seconds=POLL_SECONDS,
    )


def _handle_post(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    action = str(form.get("action") or "save").strip()
    if action == "save":
        return _save_config(ctx, request, form)
    if action == "fetch":
        return _do_fetch(ctx, request, form)
    if action == "fetch_import":
        return _do_fetch(ctx, request, form, do_import=True)
    return _render(ctx, request, error=f"不认识的 action: {action!r}")


def _save_config(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    """保存默认值：以当前配置为底，只覆盖这个表单真的提交了的字段。"""
    base = effective_config(ctx)
    values: dict[str, Any] = dict(base)

    base_url, playlist_id, proxy, target, connection_error = _connection_from_form(form, base)
    if connection_error:
        return _render(ctx, request, error=connection_error)
    values.update({"base_url": base_url, "playlist_id": playlist_id,
                   "proxy": proxy, "target": target})
    if "authorization" in form:
        values["authorization"] = normalize_token(form.get("authorization"))
    if "username" in form:
        values["username"] = str(form.get("username") or "").strip()

    for key in ("pages",):
        if key in form:
            values[key] = str(form.get(key) or "").strip() or base[key]
    for key in ("page_size", "timeout"):
        if key in form:
            values[key] = form.get(key)
    if "delay" in form:
        values["delay"] = form.get("delay")
    if str(form.get("form") or "") == "save":
        # 复选框：只有这个表单真的提交了才认（否则「保存别的字段」会把开关打回默认）
        values["incremental"] = form.get("incremental") is not None
        values["keep_raw"] = form.get("keep_raw") is not None

    checked = effective_config(_FakeContext(values))
    pages_error = parse_pages(checked["pages"])[1]
    if pages_error:
        return _render(ctx, request, error=pages_error)
    connection_error = _validate_connection(
        checked["base_url"], checked["playlist_id"], checked["proxy"],
        target=checked["target"])
    if connection_error:
        return _render(ctx, request, error=connection_error)

    ctx.save_config(checked)
    ctx.log(f"默认配置已保存：目标={target_label(checked['target'])} "
            f"playlist_id={checked['playlist_id'] or '(未设置)'} "
            f"base_url={checked['base_url']} proxy={checked['proxy'] or '(直连)'} "
            f"认证={'有' if (checked['authorization'] or checked['username']) else '无'} "
            f"pages={checked['pages']} page_size={checked['page_size']} "
            f"delay={checked['delay']} "
            f"incremental={checked['incremental']} keep_raw={checked['keep_raw']}")
    return _render(ctx, request, notice="默认配置已保存")


class _FakeContext:
    """只为复用 effective_config 的夹取逻辑（不碰磁盘）。"""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)


def _pick_int(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _pick_float(value: Any, default: float, *, low: float, high: float) -> float:
    try:
        return max(low, min(float(value), high))
    except (TypeError, ValueError):
        return default


def _run_fetch(
    ctx: Any,
    *,
    base_url: str,
    playlist_id: str,
    pages_text: str,
    incremental: bool,
    page_size: int,
    delay: float,
    proxy: str,
    timeout: int,
    keep_raw: bool,
    on_progress: Any = None,
    target: str = "playlist",
    auth: Auth | None = None,
) -> tuple[Summary | None, str, int]:
    """真正干活的那一步：认证 → 解析范围 → 读旧快照 → 翻页。

    返回 ``(摘要, 错误信息, 抓之前快照里的条数)``；出错时摘要为 ``None``。
    ``on_progress(message, summary)`` 每翻完一页给一次（页面的进度条用它）。
    """
    pages, pages_error = parse_pages(pages_text)
    if pages_error:
        return None, pages_error, 0

    notes: list[str] = []
    # 日志文件是要长期留着看的：令牌和这次填的密码都不能进去。
    secrets = ((auth.token, auth.password) if auth is not None else ("", ""))

    def sink(message: str, summary: Any = None) -> None:
        notes.append(message)
        ctx.log(redact(message, *secrets))
        if on_progress is not None:
            on_progress(message, summary)

    snapshot_id = FAVORITES_ID if normalize_target(target) == "favorites" else playlist_id
    token_at_start = auth.token if auth is not None else ""
    token_saved = token_at_start

    def keep_token() -> None:
        """登录/重登换来的令牌**当场**存回配置：抓取哪怕中途出事也不白登一次。"""
        nonlocal token_saved
        if auth is None or not auth.token or auth.token == token_saved:
            return
        token_saved = auth.token
        _persist_token(ctx, auth.token)

    if auth is not None:
        auth.prepare()
        if not auth.token:
            return None, auth.error or AUTH_REQUIRED, 0
        keep_token()
        sink(auth.note)

    previous, previous_meta = load_snapshot(ctx.data_dir, snapshot_id)
    complete = snapshot_is_complete(previous_meta)
    early_stop = bool(incremental and complete)
    ctx.log(f"开始抓取：{target_label(target)} {snapshot_id} pages={pages_text} "
            f"增量={incremental}（本地快照 {len(previous)} 条，"
            f"{'已抓全' if complete else '尚未抓全'}，early_stop={early_stop}）")

    try:
        summary = fetch_playlist(
            base_url, snapshot_id,
            pages=pages, page_size=page_size,
            incremental=incremental, known_ids=set(previous) if incremental else set(),
            merge_with=previous, delay=delay,
            proxy=proxy, timeout=timeout, early_stop=early_stop,
            previous_complete=complete,
            keep_raw=keep_raw, progress=sink,
            target=target, auth=auth,
        )
    except Exception as exc:                      # 解析/网络意外都不能炸页面
        ctx.log(f"抓取失败：{type(exc).__name__}: {exc}")
        return None, f"抓取失败：{type(exc).__name__}: {exc}", len(previous)
    finally:
        # 抓取中途换过令牌（被接口拒了又重登一次）也照样存下来
        keep_token()
        if token_saved != token_at_start:
            sink("登录换的 authorization 已存进本地配置，下次不用再登录")
    if auth is not None:
        # 认证方式可能在抓取中途换过（令牌被拒后重新登录），报告里说最后这次用的哪种
        summary.auth_note = auth.note
    summary.notes = notes[-60:]
    return summary, "", len(previous)


def _fetch_worker(
    ctx: Any,
    *,
    job_id: str,
    base_url: str,
    playlist_id: str,
    pages_text: str,
    incremental: bool,
    page_size: int,
    delay: float,
    proxy: str,
    timeout: int,
    keep_raw: bool,
    do_import: bool,
    target: str = "playlist",
    auth: Auth | None = None,
) -> None:
    """后台线程里跑的那一遍抓取：认证 → 翻页 → 落快照 →（可选）导入 → 把结果写回任务。"""
    summary, fetch_error, previous_count = _run_fetch(
        ctx, base_url=base_url, playlist_id=playlist_id, pages_text=pages_text,
        incremental=incremental, page_size=page_size,
        delay=delay, proxy=proxy, timeout=timeout, keep_raw=keep_raw,
        on_progress=_progress_sink(job_id), target=target, auth=auth,
    )
    if summary is None:
        _finish_job(job_id, state="error", error=fetch_error)
        return
    try:
        import_file, meta_file = save_snapshot(ctx.data_dir, summary)
        report = summary.summary_dict()
        report.update({
            "saved": str(import_file),
            "meta_saved": str(meta_file),
            "saved_mb": round(import_file.stat().st_size / 1048576, 2),
            "notes": list(summary.notes),
            "importer": IMPORTER_NAME,
            "previous_count": previous_count,
            "complete": summary.coverage_complete,
            "rated_count": sum(1 for row in summary.works
                               if row.get("userRating") not in (None, "", 0)),
        })
        ctx.log(f"抓取完成：{target_label(summary.target)} 快照累计 {summary.item_count} 条"
                f"（本次新增 {summary.new_count}，抓到 {summary.fetched_count}，"
                f"接口报 {summary.reported_count}），{summary.stop_reason}，"
                f"已存 {import_file.name}"
                + (f"（{report['saved_mb']} MB）" if report["saved_mb"] else ""))

        import_report: dict[str, Any] | None = None
        if do_import:
            import_report = import_snapshot(ctx, import_file)
            if import_report.get("error"):
                ctx.log(f"抓取成功但导入失败：{import_report['error']}")
            else:
                ctx.log(f"已导入 {IMPORTER_NAME}：新增 {import_report['inserted']}，"
                        f"更新 {import_report['updated']}，未变化 {import_report['unchanged']}，"
                        f"错误 {import_report['error_count']}，日志 #{import_report['log_id']}")
        _finish_job(job_id, report=report, import_report=import_report)
    except Exception as exc:                      # 写盘/导入意外也得把任务结掉
        ctx.log(f"抓取结果落盘失败：{type(exc).__name__}: {exc}")
        _finish_job(job_id, state="error",
                    error=f"抓取结果落盘失败：{type(exc).__name__}: {exc}")


def _do_fetch(
    ctx: Any, request: Any, form: Mapping[str, Any], *, do_import: bool = False
) -> str:
    """表单校验（同步）→ 起后台抓取 → 立刻返回带进度条的页面。"""
    config = effective_config(ctx)
    base_url, playlist_id, proxy, target, connection_error = _connection_from_form(form, config)
    if connection_error:
        return _render(ctx, request, error=connection_error)
    pages_text = str(form.get("pages") or config["pages"]).strip() or config["pages"]
    incremental = form.get("incremental") is not None
    page_size = _pick_int(form.get("page_size"), config["page_size"], low=1, high=MAX_PAGE_SIZE)
    delay = _pick_float(form.get("delay"), float(config["delay"]), low=0.0, high=10.0)
    timeout = _pick_int(form.get("timeout"), config["timeout"], low=5, high=120)
    keep_raw = config["keep_raw"]

    connection_error = _validate_connection(base_url, playlist_id, proxy, target=target)
    if connection_error:
        return _render(ctx, request, error=connection_error)
    pages, pages_error = parse_pages(pages_text)
    if pages_error:
        return _render(ctx, request, error=pages_error)

    auth: Auth | None = None
    if target == "favorites":
        auth = _auth_from_form(form, config, base_url=base_url, proxy=proxy, timeout=timeout)
        if not auth.usable:
            return _render(ctx, request, error=AUTH_REQUIRED)

    _persist_connection(
        ctx, base_url, playlist_id, proxy,
        target=target,
        authorization=auth.token if auth else str(config["authorization"]),
        username=auth.username if auth else str(config["username"]),
    )

    first_page, last_page = pages
    entry = max(1, first_page)
    plan_pages = max(1, last_page - entry + 1) if last_page and last_page >= entry else None
    private = target == "favorites"
    job = _start_job(
        label=f"{'私人收藏' if private else f'播放列表 {playlist_id}'}"
              f"（第 {entry} 页起 · {'增量' if incremental else '整段重抓'}）",
        do_import=do_import,
        plan_pages=plan_pages,
        first_note="正在认证并读私人收藏第一页…" if private
                   else "正在读播放列表第一页…",
    )
    if job is None:
        return _render(ctx, request,
                       error="已经有一个抓取在跑了，等它的进度条走完再点。")

    threading.Thread(
        target=_fetch_worker,
        kwargs={
            "ctx": ctx, "job_id": job["id"], "base_url": base_url,
            "playlist_id": playlist_id, "pages_text": pages_text,
            "incremental": incremental,
            "page_size": page_size, "delay": delay,
            "proxy": proxy, "timeout": timeout, "keep_raw": keep_raw,
            "do_import": do_import, "target": target, "auth": auth,
        },
        name=f"asmr-playlist-{job['id']}",
        daemon=True,
    ).start()
    return _render(ctx, request, just_started=True)


def _preview_text(import_file: Path, item_count: int, *, limit: int = 300) -> str:
    """快照预览：小快照才读，且缩进后给人看（文件本身是紧凑 JSON）。"""
    if item_count > limit:
        return ""
    try:
        payload = json.loads(import_file.read_text(encoding="utf-8"))
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (OSError, ValueError):
        return ""


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def run_cli_fetch(
    ctx: Any,
    *,
    playlist_id: str = "",
    base_url: str = "",
    pages: str = "",
    proxy: str = "",
    page_size: int = 0,
    delay: float = 0.0,
    timeout: int = 0,
    incremental: bool = True,
    dry_run: bool = False,
    target: str = "",
    authorization: str = "",
    username: str = "",
    password: str = "",
) -> str:
    """命令行的主体：抓取（+ 导入），返回给人看的多行文本。"""
    config = effective_config(ctx)
    chosen_base = normalize_base_url(base_url) or config["base_url"]
    chosen_pages = (pages or config["pages"]).strip() or config["pages"]
    if proxy.strip().lower() in ("-", "none", "direct"):
        chosen_proxy = ""                        # 显式要求直连
    elif proxy:
        chosen_proxy = normalize_proxy(proxy)
    else:
        chosen_proxy = config["proxy"]
    chosen_target = normalize_target(target) if str(target or "").strip() \
        else config["target"]
    chosen_playlist = playlist_id_of(chosen_base, playlist_id or config["playlist_id"])

    connection_error = _validate_connection(
        chosen_base, chosen_playlist, chosen_proxy, target=chosen_target)
    if connection_error:
        raise ValueError(connection_error)

    chosen_page_size = int(page_size) if page_size else config["page_size"]
    chosen_delay = float(delay) if delay else float(config["delay"])
    chosen_timeout = int(timeout) if timeout else config["timeout"]

    auth: Auth | None = None
    if chosen_target == "favorites":
        auth = Auth(
            base_url=chosen_base,
            token=normalize_token(authorization) or str(config["authorization"]),
            username=(str(username or "").strip() or str(config["username"])),
            password=str(password or ""),
            proxy=chosen_proxy,
            timeout=chosen_timeout,
        )
        if not auth.usable:
            raise ValueError(AUTH_REQUIRED)

    _persist_connection(
        ctx, chosen_base, chosen_playlist, chosen_proxy,
        target=chosen_target,
        authorization=auth.token if auth else str(config["authorization"]),
        username=auth.username if auth else str(config["username"]),
    )

    summary, fetch_error, _previous = _run_fetch(
        ctx, base_url=chosen_base, playlist_id=chosen_playlist, pages_text=chosen_pages,
        incremental=incremental, page_size=chosen_page_size,
        delay=min(10.0, max(0.0, chosen_delay)),
        proxy=chosen_proxy, timeout=chosen_timeout, keep_raw=config["keep_raw"],
        target=chosen_target, auth=auth,
    )
    if summary is None:
        raise ValueError(fetch_error)

    import_file, _meta = save_snapshot(ctx.data_dir, summary)
    lines = [
        f"抓取完成：{target_label(summary.target)} {summary.playlist_id}"
        f"（{'增量' if incremental else summary.mode}，{chosen_pages}）",
        f"  快照      {import_file}"
        f"（{round(import_file.stat().st_size / 1048576, 2)} MB，累计 {summary.item_count} 条）",
        f"  本次      抓到 {summary.fetched_count} 条 · 新增 {summary.new_count} 条"
        f" · 之前 {summary.item_count - summary.new_count} 条",
        f"  页数      抓了 {summary.pages_fetched} 页"
        + (f" / 共 {summary.total_pages} 页" if summary.total_pages else "")
        + f" · {summary.stop_reason}",
        f"  接口报告  {summary.reported_count if summary.reported_count is not None else '—'} 个作品",
        f"  代理      {summary.proxy or '直连'}",
    ]
    if summary.auth_note:
        lines.append(f"  认证      {summary.auth_note}")
    if summary.next_page:
        lines.append(f"  续抓      这次没抓完，下次 --pages {summary.next_page}- 从那页接着抓")
    if summary.notes:
        lines.append("  明细      " + "；".join(summary.notes[-6:]))

    if dry_run:
        lines.append(f"  导入      已跳过（--dry-run）；到上传页选 importer {IMPORTER_NAME} 导入上面这个文件")
        return "\n".join(lines)

    report = import_snapshot(ctx, import_file)
    if report.get("error"):
        lines.append(f"  导入      失败：{report['error']}")
        lines.append("            （快照没丢，仍可到上传页手动导入）")
        return "\n".join(lines)
    lines.append(
        f"  导入      共 {report['total']} 条 · 新增 {report['inserted']} · "
        f"更新 {report['updated']} · 跳过 {report['skipped']} · "
        f"未变化 {report['unchanged']} · 错误 {report['error_count']} · "
        f"警告 {report['warning_count']}"
    )
    lines.append(f"            importer {report['importer']} · 导入日志 #{report['log_id']}"
                 + (f" · 已存档 {report['archive_path']}" if report.get("archive_path") else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------

class AsmrPlaylist(Plugin):
    """asmr.one 收藏抓取插件（公开播放列表 + 需要认证的私人收藏）。

    只实现契约要求的 ``start()`` / ``stop()``，外加页面与表单两个可选钩子。
    抓取/落盘/导入的实现都在上面那些纯函数里（``fetch_playlist`` / ``save_snapshot`` …），
    它们同样只用标准库，便于单测直接调用。
    """

    def start(self) -> None:
        config = effective_config(self.ctx)
        stored = snapshot_index(self.ctx.data_dir)
        self.log(f"{self.title} 已启动：抓取目标 {target_label(config['target'])}，"
                 f"接口 {config['base_url']}，"
                 + (f"播放列表 {config['playlist_id'] or '(未设置)'}，"
                    if config["target"] == "playlist"
                    else f"认证 {'已配' if (config['authorization'] or config['username']) else '未配'}，")
                 + f"代理 {config['proxy'] or '(直连)'}，"
                 f"默认 {'增量' if config['incremental'] else '全量'}/页码 {config['pages']}，"
                 f"已有快照 {len(stored)} 份"
                 + ("" if _importer_available(self.ctx)
                    else f"（⚠ 宿主没有注册 importer {IMPORTER_NAME}）"))

    def stop(self) -> None:
        self.log("已停止（没有常驻资源需要释放）")

    def render_page(self, request: Any = None) -> str:
        return _render(self.ctx, request)

    def handle_post(self, request: Any = None, form: Mapping[str, Any] | None = None) -> str:
        return _handle_post(self.ctx, request, form or {})

    @cmd("fetch_import", help="抓取 asmr.one 播放列表/私人收藏并导入 OmniShelf")
    def fetch_import(
        self,
        playlist_id: str = "",
        pages: str = "",
        proxy: str = "",
        base_url: str = "",
        page_size: int = 0,
        delay: float = 0.0,
        timeout: int = 0,
        incremental: bool = True,
        dry_run: bool = False,
        target: str = "",
        authorization: str = "",
        username: str = "",
        password: str = "",
    ) -> str:
        """抓取 asmr.one 的播放列表或私人收藏，并（默认）直接导入 OmniShelf。

        等价于页面上的「抓取并导入」按钮：抓完调宿主的 import 接口入库，走的是
        importer ``asmr_one``，和上传页/CLI 同一条链路（来源判重、写 import_logs、原始文件存档）。
        判重靠 ``works[].id``，所以重复导入同一批作品只会「未变化」。

        playlist_id: 公开播放列表 id（asmr.one 播放列表页 URL 里那串 UUID）；留空用插件配置里的
        pages: 页码范围：0 全抓（翻到末页，中途不中断）；1 只抓第一页；1-5 抓 1~5 页；
               12- 从第 12 页抓到底（续抓就用这个）
        proxy: 本地代理，如 http://127.0.0.1:10809；留空 = 用插件配置；填 - 表示强制直连
        base_url: 接口根地址；留空用插件配置里的（默认 https://api.asmr.one/api/，尾巴按目标自动补全）
        page_size: 每页条数 1~100（越大请求越少）；0 = 用插件配置（默认 100）
        delay: 每次请求间隔秒数 0~10；0 = 用插件配置（默认 0.5）
        timeout: 单次请求超时秒数 5~120；0 = 用插件配置（默认 30）
        incremental: 增量抓取（默认开）：从第 1 页往后翻，遇到整页都已在快照里就停；
                     关掉就是 --no-incremental，按页码范围整段重抓（全量）
        dry_run: 只抓取不导入；快照照样落盘，之后可以到上传页手动导入
        target: 抓取目标：playlist 公开播放列表 / favorites 私人收藏；留空用插件配置里的
        authorization: 私人收藏用的 Bearer 令牌；留空用插件配置里的
        username: 私人收藏用的账号（没有可用令牌时用它登录）
        password: 私人收藏用的密码（只在这次命令里用，不落盘；命令行会留在 shell 历史里，
                  介意的话改用 authorization）
        """
        return run_cli_fetch(
            self.ctx,
            playlist_id=playlist_id, base_url=base_url, pages=pages, proxy=proxy,
            page_size=page_size, delay=delay, timeout=timeout,
            incremental=incremental, dry_run=dry_run,
            target=target, authorization=authorization, username=username, password=password,
        )
