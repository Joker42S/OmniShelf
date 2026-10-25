"""steam_client —— Steam 公开数据查询库（纯标准库实现）。

对外只有一个可实例化门面 ``SteamClient``，内部持有两个子模块::

    client = SteamClient(api_key=KEY, cache_path="/data/steam_cache")
    client = SteamClient(api_key=KEY, cache_path="/data/steam_cache",
                         proxy="http://127.0.0.1:10809")      # 直连不通时走本地代理

    sid = client.user.resolve_vanity_url("gabelogannewell")
    profile = client.user.get_player_summary(sid)          # 实时，不缓存
    games = client.game.get_owned_games(sid)               # 实时，不缓存
    wish = client.game.get_wishlist(sid)                   # 实时，不缓存
    details = client.game.enrich_apps(wish)                # app 详情，走缓存 + 逐款并发补全
    details = client.game.enrich_apps(wish, force_refresh=True)
    by_id = client.game.get_app_details_map(wish)          # 只要 appid -> 详情这个映射

边界：不做登录/会话/交易/库存/市场；不绕过隐私设置（空数据 ≠ 无数据，
统一抛 ``SteamDataUnavailableError``）；api_key 不落日志、不落缓存、不出现在异常文本，
带口令的代理地址同理只在请求路由里出现原文（日志与报错只留 ``协议://主机:端口``）。
``proxy`` 留空走 urllib 默认（``HTTPS_PROXY`` 等环境变量照旧生效），填了就只走这一个出口。
"""

from __future__ import annotations

from .errors import (
    CacheError,
    SteamAuthError,
    SteamDataUnavailableError,
    SteamError,
    SteamNetworkError,
    SteamNotFoundError,
    SteamRateLimitError,
)
from .facade import SteamClient
from .game import SteamGameClient
from .models import AppDetail, OwnedGame, PlayerSummary
from .user import SteamUserClient

__all__ = [
    "SteamClient",
    "SteamUserClient",
    "SteamGameClient",
    "PlayerSummary",
    "OwnedGame",
    "AppDetail",
    "SteamError",
    "SteamAuthError",
    "SteamNotFoundError",
    "SteamRateLimitError",
    "SteamDataUnavailableError",
    "SteamNetworkError",
    "CacheError",
]
