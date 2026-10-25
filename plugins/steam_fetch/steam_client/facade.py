"""整合门面：只做组装与转发，不写业务逻辑。"""

from __future__ import annotations

from pathlib import Path

from .game import SteamGameClient
from .user import SteamUserClient

__all__ = ["SteamClient"]


class SteamClient:
    def __init__(
        self,
        api_key: str,
        cache_path: str | Path = "./.steam_cache",
        app_ttl: int = 604800,
        timeout: float = 10.0,
        proxy: str = "",
    ) -> None:
        #: 两个子模块共用同一份超时与代理：愿望单走 api 域名、商店信息走 store 域名，
        #: 直连不通时两个都得走代理，只配一个等于没配。
        self.user = SteamUserClient(api_key, timeout=timeout, proxy=proxy)
        self.game = SteamGameClient(api_key, cache_path=cache_path, app_ttl=app_ttl,
                                    timeout=timeout, proxy=proxy)
