"""HTTP 客户端：请求签名、登录、翻页拉收藏夹（只用标准库）。

哔咔接口的关键点（官方 App 的行为，各家开源实现一致）：

* 每个请求都要带一套 HMAC-SHA256 签名头（``api-key`` / ``time`` / ``nonce`` /
  ``signature`` 加上几个 ``app-*``），值来自 :mod:`picamanga.config`；
* **签名用的是去掉前导斜杠的路径**（含查询串）。带着 ``/`` 去签，服务端会回一个
  假的 ``{"code":200,"message":"success"}`` 空响应而不是报错，非常难查；
* 认证靠请求头 ``authorization: <token>``，``token`` 由 ``/auth/sign-in`` 用
  邮箱 + 口令换来；
* 单页固定 20 条，``limit`` 参数会被忽略；
* 所有请求都从 :meth:`PicamangaClient._open` 这一个口出去，配了代理（``config.proxy``）
  就在这一处换成代理服务器，签名头、令牌、重试逻辑都不用管路由。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import (
    ACCEPT_JSON,
    API_KEY,
    APP_BUILD_VERSION,
    APP_CHANNEL,
    APP_PLATFORM,
    APP_UUID,
    APP_VERSION,
    AUTH_ERROR_CODES,
    FAVOURITE_PATH,
    NONCE,
    PAGE_SIZE,
    SECRET_KEY,
    SIGN_IN_PATH,
    USER_AGENT,
    PicamangaConfig,
)
from .errors import ApiError, AuthError
from .models import ComicItem


class PicamangaClient:
    """哔咔接口客户端（只做收藏夹备份用到的那两件事）。

    典型用法::

        from picamanga import PicamangaClient, PicamangaConfig

        client = PicamangaClient(PicamangaConfig(token="<你的令牌>"))
        docs, total, pages = client.fetch_favourite_page(1)
        print(total, len(docs))

    :param config: :class:`~picamanga.config.PicamangaConfig`，省略则用默认值。
    :param overrides: 直接覆盖配置字段，例如 ``PicamangaClient(token="xx")``。
    """

    def __init__(self, config: Optional[PicamangaConfig] = None, **overrides: Any) -> None:
        cfg = config or PicamangaConfig()
        if overrides:
            cfg = cfg.merge(**overrides)
        self.config = cfg.validate()
        self._token: Optional[str] = (self.config.token or "").strip() or None
        #: 这次运行里是否由本客户端登录换来的令牌（插件那一侧据此决定要不要存）
        self.logged_in: bool = False

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    def __enter__(self) -> "PicamangaClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        """释放资源（当前用 urllib，无连接池，留作扩展）。"""

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------

    @property
    def token(self) -> Optional[str]:
        """当前使用的令牌（可能为 ``None``）。"""
        return self._token

    def login(self, email: Optional[str] = None, password: Optional[str] = None) -> str:
        """用邮箱和口令换取令牌。

        :return: 新换来的令牌。
        :raises AuthError: 缺少凭据，或登录响应里没有令牌。
        """
        email = (email or self.config.email or "").strip()
        password = password or self.config.password
        if not email or not password:
            raise AuthError("登录需要邮箱账号和口令")
        result = self.request(
            "POST",
            SIGN_IN_PATH,
            data={"email": email, "password": password},
            token=None,
        )
        found = ((result.get("data") or {}).get("token") or "")
        if not found:
            raise AuthError("登录失败：响应里没有令牌（%s）" % str(result.get("message") or "")[:120])
        self._token = str(found)
        self.logged_in = True
        return self._token

    def ensure_authenticated(self) -> str:
        """确保手里有一个令牌：有就直接用，没有就用邮箱口令登录换一个。

        和拷贝漫画那份不同，这里**不发一次「校验令牌」的请求**：哔咔的收藏夹接口
        本身就是认证接口，第一个请求就会把坏令牌顶出来（然后由 :meth:`request`
        自动重新登录一次）。

        :return: 可用的令牌。
        :raises AuthError: 既没有令牌，也没有可用的邮箱 + 口令。
        """
        if self._token:
            return self._token
        if self.config.email and self.config.password:
            return self.login()
        raise AuthError(
            "缺少认证信息：请提供 token，或 email + password。"
            "令牌可以从浏览器/抓包请求头里的 authorization 拿到。"
        )

    # ------------------------------------------------------------------
    # 签名与请求头
    # ------------------------------------------------------------------

    @staticmethod
    def sign(method: str, path: str, timestamp: str) -> str:
        """算 HMAC-SHA256 签名。``path`` 是**去掉前导斜杠**、含查询串的路径。"""
        raw = "%s%s%s%s%s" % (path, timestamp, NONCE, method, API_KEY)
        digest = hmac.new(SECRET_KEY.encode("utf-8"), raw.lower().encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()

    def build_headers(self, method: str, path: str, token: Optional[str]) -> Dict[str, str]:
        """构造一套与官方 App 一致的请求头。"""
        timestamp = str(int(time.time()))
        headers = {
            "api-key": API_KEY,
            "accept": ACCEPT_JSON,
            "app-channel": APP_CHANNEL,
            "time": timestamp,
            "nonce": NONCE,
            # 签的是去掉前导斜杠的路径，带斜杠会被服务端回一个假的空成功响应
            "signature": self.sign(method, path.lstrip("/"), timestamp),
            "app-version": APP_VERSION,
            "app-uuid": APP_UUID,
            "app-platform": APP_PLATFORM,
            "app-build-version": APP_BUILD_VERSION,
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["authorization"] = token
        return headers

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------

    def _open(
        self, request: "urllib.request.Request", *, timeout: float, context: Any
    ) -> Any:
        """**唯一的 HTTP 出口**：配了代理就走代理服务器，没配就走 urllib 的默认路径。

        抽成一个口子有两个用处：

        * 测试可以整体替换（假站点同时换 ``urlopen`` 和 ``build_opener``），
          签名、令牌、重试那套完全不需要知道请求是怎么路由的；
        * 代理只在这里生效一次——显式的 ``ProxyHandler`` 会顶掉 urllib 从环境变量
          读来的那份，所以填了代理就**只走这一个**，不会被系统环境半路换掉。
        """
        proxy = self.config.proxy_target
        if not proxy:
            return urllib.request.urlopen(request, timeout=timeout, context=context)
        handlers: List[Any] = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
        if context is not None:                 # 关掉证书校验时，代理那条路也不校验
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers).open(request, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        token: Any = "__current__",
        retries: Optional[int] = None,
        timeout: Optional[float] = None,
        _after_relogin: bool = False,
    ) -> Dict[str, Any]:
        """发一个请求并返回解析后的 JSON。

        :param path: 以 ``/`` 开头的接口路径（可以带查询串，签名会一起算）。
        :param data: 请求体（非 GET），按 JSON 发送。
        :param token: 传 ``None`` 表示匿名请求；默认用当前令牌。
        :raises AuthError: HTTP 401 或哔咔的认证错误码；自动重新登录一次仍失败。
        :raises ApiError: 网络错误、非 JSON 响应、业务错误码、重试耗尽。
        """
        if token == "__current__":
            token = self._token
        verb = method.upper()
        url = self.config.base_url + path
        body = json.dumps(data).encode("utf-8") if data else None
        retries = self.config.retries if retries is None else retries
        attempts = max(1, int(retries))
        timeout = self.config.timeout if timeout is None else timeout
        context = None if self.config.verify_ssl else ssl._create_unverified_context()
        headers = self.build_headers(verb, path, token)

        def auth_failed(payload: Dict[str, Any], code: Any) -> Dict[str, Any]:
            """令牌失效：配了邮箱口令就重新登录一次，并重放这个请求。"""
            message = str(payload.get("message") or "令牌可能已失效")
            if (
                _after_relogin
                or token is None                    # 匿名请求（登录本身）不触发重登
                or not (self.config.email and self.config.password)
            ):
                raise AuthError("认证失败: %s (%s)" % (message, code))
            return self.request(
                verb, path, data=data, token=self.login(),
                retries=retries, timeout=timeout, _after_relogin=True,
            )

        last_err: Optional[Exception] = None
        # 代理地址进错误信息：哔咔这类接口「连不上」最常见的原因就是代理没配好，
        # 光一句 URLError 分不清是代理不通还是直连被墙。
        via = ("（经代理 %s）" % self.config.proxy_target) if self.config.proxy_target else ""
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(
                url, data=body, method=verb, headers=headers
            )
            try:
                with self._open(req, timeout=timeout, context=context) as resp:
                    text = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                try:
                    text = exc.read().decode("utf-8", "replace")
                except Exception:                     # 响应体读不出来
                    text = ""
                if exc.code == 401:
                    return auth_failed(_parse(text) if text else {}, exc.code)
                if exc.code >= 500:
                    last_err = ApiError("HTTP %s %s" % (exc.code, _brief(text)))
                else:
                    raise _http_error(exc, text)
            except Exception as exc:                 # 网络抖动 / 超时 / DNS
                last_err = exc
            else:
                payload = _parse(text)
                code = _as_int(payload.get("code"))
                if code == 200:
                    return payload
                if code == 401 or str(payload.get("error") or "") in AUTH_ERROR_CODES:
                    return auth_failed(payload, payload.get("code"))
                if code >= 500:
                    last_err = ApiError(
                        "服务端出错：%s（%s）" % (payload.get("message"), code)
                    )
                else:
                    raise ApiError(
                        "请求失败: %s (%s)" % (payload.get("message") or "未知错误", code)
                    )

            if attempt < attempts:
                wait = self.config.retry_backoff * attempt
                if not self.config.quiet:
                    print("    ! 请求失败(%s)，%.1fs 后重试…" % (last_err, wait))
                time.sleep(wait)

        raise ApiError("请求失败%s：%s" % (via, last_err))

    # ------------------------------------------------------------------
    # 收藏夹数据
    # ------------------------------------------------------------------

    def fetch_favourite_page(
        self, page: int = 1, *, sort: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], int, int]:
        """取一页收藏夹。

        :param page: 页码，从 1 开始。
        :param sort: 排序值，默认用配置里的（``dd`` = 按收藏日期新到旧）。
        :return: ``(本页原始漫画对象列表, 远端总数, 远端总页数)``。
        """
        self.ensure_authenticated()
        chosen = sort or self.config.sort
        query = urllib.parse.urlencode({"s": chosen, "page": str(page)})
        result = self.request("GET", "%s?%s" % (FAVOURITE_PATH, query))
        comics = ((result.get("data") or {}).get("comics") or {})
        if not isinstance(comics, dict):
            raise ApiError("收藏夹返回格式异常：%s" % str(comics)[:200])
        raw_docs = comics.get("docs")
        docs = [doc for doc in raw_docs if isinstance(doc, dict)] if isinstance(raw_docs, list) else []
        total = _as_int(comics.get("total"))
        pages = _as_int(comics.get("pages")) or (
            max(1, -(-total // PAGE_SIZE)) if total else 0
        )
        return docs, total, pages

    def pages(
        self,
        *,
        sort: Optional[str] = None,
        max_pages: Optional[int] = None,
        delay: Optional[float] = None,
    ) -> Iterator[Tuple[List[Dict[str, Any]], int, int]]:
        """逐页产出 ``(本页漫画对象, 远端总数, 总页数)``，从第 1 页翻到最后一页。

        调用方可以在循环里 ``break``（增量模式就是这么提前收手的）。
        某一页抓取失败会直接抛出 :class:`~picamanga.errors.ApiError`；
        要「失败也继续翻」的是 :class:`~picamanga.exporter.FavoritesExporter`。
        """
        limit_pages = self.config.max_pages if max_pages is None else max_pages
        gap = self.config.request_delay if delay is None else delay

        page = 1
        total_pages = 1
        while page <= total_pages:
            if limit_pages is not None and page > int(limit_pages):
                return
            docs, _total, reported = self.fetch_favourite_page(page, sort=sort)
            if reported > 0:
                total_pages = reported
            yield docs, _total, total_pages
            if not docs:
                return
            page += 1
            if gap and page <= total_pages:
                time.sleep(gap)

    def iter_comics(
        self, *, sort: Optional[str] = None, max_pages: Optional[int] = None
    ) -> Iterator[ComicItem]:
        """遍历收藏夹并产出 :class:`~picamanga.models.ComicItem`（按编号去重）。"""
        seen: set = set()
        for docs, _total, _pages in self.pages(sort=sort, max_pages=max_pages):
            for doc in docs:
                item = ComicItem.from_api(doc)
                if not item.comic_id or item.comic_id in seen:
                    continue
                seen.add(item.comic_id)
                yield item


def _parse(text: str) -> Dict[str, Any]:
    """把响应体解析成 dict；不是 JSON（被网关/CDN 拦了之类）就抛 ApiError。"""
    try:
        payload = json.loads(text)
    except ValueError:
        raise ApiError("返回内容不是 JSON：%s" % (text[:200] or "(空响应)"))
    return payload if isinstance(payload, dict) else {"code": -1, "message": str(payload)[:200]}


def _brief(text: str) -> str:
    return (text or "").strip().replace("\n", " ")[:200]


def _http_error(exc: urllib.error.HTTPError, text: str) -> ApiError:
    """非 401 / 非 5xx 的 HTTP 错误（403、404……）。"""
    message = _brief(text) or exc.reason or ""
    return ApiError("HTTP %s %s" % (exc.code, message))


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
