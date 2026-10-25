"""Steam 数据查询的错误模型。

映射规则（设计文档 §7）::

    401/403                              -> SteamAuthError
    404 / ResolveVanityURL success != 1  -> SteamNotFoundError
    429（读 Retry-After）                 -> SteamRateLimitError
    200 但数据为空/缺失                   -> SteamDataUnavailableError
    超时 / 连接异常                       -> SteamNetworkError
    缓存路径不可写                        -> CacheError

``SteamDataUnavailableError`` 的文案固定为「游戏信息不可用或未公开」：
空数据不等于无数据，可能是私密、被限制或接口抖动，不能对用户宣称「没有游戏」。
"""

from __future__ import annotations

__all__ = [
    "SteamError",
    "SteamAuthError",
    "SteamNotFoundError",
    "SteamRateLimitError",
    "SteamDataUnavailableError",
    "SteamNetworkError",
    "CacheError",
]

#: 空响应的固定文案。禁止输出「该用户没有游戏 / 愿望单」之类的推断。
UNAVAILABLE_MESSAGE = "游戏信息不可用或未公开"


class SteamError(Exception):
    """本库所有可预期异常的基类。"""


class SteamAuthError(SteamError):
    """401/403，或需要 key 的接口在缺 key 时被调用。"""


class SteamNotFoundError(SteamError):
    """404，或 vanity 解析失败（success != 1）。"""


class SteamRateLimitError(SteamError):
    """429。``retry_after`` 是 Retry-After 头的秒数（没给就是 None）。"""

    def __init__(self, message: str = "请求被 Steam 限流（429）", *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SteamDataUnavailableError(SteamError):
    """200 但响应为空/缺失：不可用或未公开，不代表「没有数据」。"""

    def __init__(self, message: str = UNAVAILABLE_MESSAGE):
        super().__init__(message)


class SteamNetworkError(SteamError):
    """超时、连接错误，或响应不是合法 JSON。"""


class CacheError(SteamError):
    """缓存目录不可创建/不可写。"""
