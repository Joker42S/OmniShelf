"""统一请求层：GET + format=json + 超时 + 429/5xx 指数退避重试 + key 脱敏 + 可选代理。

只用标准库 ``urllib``（仓库惯例：插件与其随附工具包不新增第三方依赖，
设计文档里的 requests 依赖按此口径替换，对外行为不变）。

红线：**api_key 绝不出现在异常信息里**，一律替换为 ``***``；
所有会带出 URL/响应内容的报错都必须先过 :meth:`SteamWebAPI.redact`。
代理地址如果带 ``user:pass@`` 口令，同样只在这道关卡里被抹掉，日志/报错只留 ``协议://主机:端口``。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import (
    SteamAuthError,
    SteamDataUnavailableError,
    SteamError,
    SteamNetworkError,
    SteamNotFoundError,
    SteamRateLimitError,
)

__all__ = [
    "SteamWebAPI",
    "normalize_proxy",
    "proxy_credentials",
    "proxy_without_credentials",
    "require_steamid64",
    "unwrap_response",
]

API_BASE = "https://api.steampowered.com"
STORE_BASE = "https://store.steampowered.com"

#: 固定标识本工具，便于 Steam 侧排查
USER_AGENT = "OmniShelf-steam-client/0.1 (public data queries)"

_STEAMID64_RE = re.compile(r"^\d{17}$")


def normalize_proxy(value: Any) -> str:
    """代理地址归一：``127.0.0.1:10809`` 这种没写协议的补 ``http://``，空值原样空。

    本地代理软件（Clash/V2Ray 的 HTTP 混合端口）通常只给 ``IP:端口``，而 urllib 的
    ``ProxyHandler`` 拿到没有协议头的串会直接忽略——不补的效果是「悄悄没走代理」。
    所以 SOCKS 端口要自己写全 ``socks5://IP:端口``。合法性（协议白名单、带空格）
    由调用方（插件层）判，这里只做归一。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return text if "://" in text else f"http://{text}"


def proxy_without_credentials(value: Any) -> str:
    """去掉代理地址里的 ``user:pass@``，用于日志和报错文案（口令绝不出现在这里）。"""
    text = normalize_proxy(value)
    if not text:
        return ""
    parts = urllib.parse.urlsplit(text)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, "", "", ""))


def proxy_credentials(value: Any) -> str:
    """代理地址里的口令部分（``user:pass``）；没有则空串。用来把原文里的它抹掉。"""
    text = normalize_proxy(value)
    if not text or "@" not in text:
        return ""
    userinfo = urllib.parse.urlsplit(text).netloc.rsplit("@", 1)[0]
    return userinfo


def require_steamid64(value: Any) -> str:
    """校验 17 位数字的 steamid64；格式错误抛 ``ValueError``（不是网络错误）。"""
    text = str(value or "").strip()
    if not _STEAMID64_RE.match(text):
        raise ValueError(f"steamid64 必须是 17 位数字字符串，收到 {text!r}")
    return text


def unwrap_response(data: dict[str, Any]) -> dict[str, Any]:
    """取 Steam Web API 响应里的 ``response`` 壳；缺失即视为数据不可用。"""
    response = data.get("response")
    if not isinstance(response, dict):
        raise SteamDataUnavailableError()
    return response


def _query_value(value: Any) -> Any:
    """urlencode 会把 Python 的 True/False 拼成 True/False，Steam 接口要 1/0。"""
    if isinstance(value, bool):
        return 1 if value else 0
    return value


class SteamWebAPI:
    """所有远端请求的唯一出口（测试替身就换 :meth:`request`）。"""

    def __init__(
        self,
        api_key: str | None,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff: float = 1.0,
        proxy: str = "",
    ) -> None:
        self.api_key = str(api_key or "")
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff = float(backoff)
        #: 代理地址。留空 = 不在这里指定，走 urllib 默认（认 ``HTTPS_PROXY`` 等环境变量）
        self.proxy = normalize_proxy(proxy)

    @property
    def proxy_target(self) -> str:
        """交给 ``ProxyHandler`` 的代理地址；空串表示「不指定代理」。"""
        return self.proxy

    @property
    def proxy_label(self) -> str:
        """给人看的代理地址，口令已去掉。"""
        return proxy_without_credentials(self.proxy)

    # ------------------------------------------------------------------ 脱敏
    def redact(self, text: Any) -> str:
        """把回显文本里出现的明文 key 换成 ``***``（代理口令同理）。"""
        message = str(text if text is not None else "")
        if self.api_key:
            message = message.replace(self.api_key, "***")
        secret = proxy_credentials(self.proxy)
        if secret:
            message = message.replace(secret, "***")
        return message

    # ------------------------------------------------------------------ 请求
    def sleep(self, seconds: float) -> None:
        """退避等待。测试里替换成记录调用的替身。"""
        time.sleep(seconds)

    # --------------------------------------------------------------- HTTP 出口
    def _open(self, request: "urllib.request.Request") -> Any:
        """**唯一的 HTTP 出口**：配了代理就只走它，没配走 urllib 默认路径。

        抽成一个口子有两个用处（和 ``picamanga`` 的 ``_open`` 同一套路）：

        * 测试可以整体替换，上层完全不需要知道请求是怎么路由的；
        * 代理只在这里生效一次——显式的 ``ProxyHandler`` 会顶掉 urllib 从环境变量
          读来的那份，所以填了代理就**只走这一个**，不会被系统环境半路换掉。
          反过来，留空时不建 opener，``HTTPS_PROXY`` 之类的环境变量照旧生效。
        """
        if not self.proxy:
            return urllib.request.urlopen(request, timeout=self.timeout)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
        )
        return opener.open(request, timeout=self.timeout)

    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        base: str = API_BASE,
    ) -> dict[str, Any]:
        """发一次 GET，返回解析后的 JSON dict。

        ``key`` 覆盖本次调用使用的 api_key；传空串表示这次匿名请求。
        429 / 5xx / 超时按 ``backoff * 2**n`` 指数退避，最多重试 ``max_retries`` 次；
        其余 4xx 不重试。错误一律映射到 errors.py 的异常类型。
        """
        query: dict[str, Any] = {"format": "json"}
        effective_key = self.api_key if key is None else str(key)
        if effective_key:
            query["key"] = effective_key
        if params:
            query.update({name: _query_value(value) for name, value in params.items()})
        url = f"{base}{path}?{urllib.parse.urlencode(query)}"
        # 连不上时最常见的原因就是代理没配好或代理不通，光一句 URLError 分不清走的哪条路
        via = f"（经代理 {self.proxy_label}）" if self.proxy_label else ""

        last_network: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
                )
                with self._open(request) as response:
                    body = response.read()
                try:
                    data = json.loads(body)
                except ValueError as exc:
                    raise SteamNetworkError(self.redact(f"响应不是合法 JSON: {exc}")) from exc
                if not isinstance(data, dict):
                    raise SteamNetworkError(self.redact(f"响应顶层不是对象: {type(data).__name__}"))
                return data
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise SteamAuthError(self.redact(f"HTTP {exc.code}：api_key 无效或权限不足")) from exc
                if exc.code == 404:
                    raise SteamNotFoundError(self.redact(f"HTTP {exc.code}: {path}")) from exc
                if exc.code == 429:
                    retry_after = _retry_after(exc)
                    if attempt < self.max_retries:
                        self.sleep(max(float(retry_after or 0), self.backoff * 2 ** attempt))
                        continue
                    raise SteamRateLimitError(retry_after=retry_after) from exc
                if 500 <= exc.code < 600 and attempt < self.max_retries:
                    self.sleep(self.backoff * 2 ** attempt)
                    continue
                if 500 <= exc.code < 600:
                    raise SteamNetworkError(
                        self.redact(f"HTTP {exc.code}（已重试 {self.max_retries} 次）{via}")
                    ) from exc
                raise SteamError(self.redact(f"HTTP {exc.code}: {path}")) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_network = exc
                if attempt < self.max_retries:
                    self.sleep(self.backoff * 2 ** attempt)
                    continue
                raise SteamNetworkError(
                    self.redact(f"{type(exc).__name__}: {exc}{via}")
                ) from exc
        raise SteamNetworkError(self.redact(f"请求失败: {last_network}{via}"))  # pragma: no cover


def _retry_after(exc: urllib.error.HTTPError) -> int | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None
