"""游戏模块：游戏库 / 愿望单**直接请求 Steam**，只有 app 详情走本地缓存。

硬约束（设计文档 §4）：

* ``get_owned_games`` / ``get_wishlist`` 不接受 ``force_refresh``，实现不 import cache；
* 缓存目录只出现 ``apps/{cc}/{appid}.json``（价格随商店区域变化，按区域分目录），
  不出现任何用户维度文件；
* app 详情失败（请求异常或 success=false）**不删除、不覆盖**已有缓存。

商店信息**不能批量取**：``appdetails`` 的逗号多 appid 写法现在会被 Steam 直接回
HTTP 400（2026-10-18 实测：``appids=730,570`` → 400，``appids=730`` → 200），
所以这里按 appid 单条请求，靠**并发（≤8 路）+ 网络熔断**压时间：
一次超时/断网之后，本次排队的其余 id 直接留空，不再一个个去撞 4 次超时。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from .cache import FileCache
from .errors import (
    SteamAuthError,
    SteamDataUnavailableError,
    SteamError,
    SteamNetworkError,
)
from .models import AppDetail, OwnedGame
from .transport import STORE_BASE, SteamWebAPI, require_steamid64, unwrap_response

__all__ = ["SteamGameClient"]

#: 并发补全的线程上限
_MAX_ENRICH_WORKERS = 8


def _payload_from_entry(appid: int, entry: Any) -> dict[str, Any] | None:
    """单个 appid 的响应条目 -> 可缓存的 payload；``success=false``/结构不对返回 None。"""
    if not isinstance(entry, dict) or not entry.get("success") or not isinstance(entry.get("data"), dict):
        return None
    payload = dict(entry["data"])
    payload.setdefault("appid", appid)
    return payload


def _to_detail(payload: dict[str, Any], appid: int) -> AppDetail:
    price = payload.get("price_overview")
    price = price if isinstance(price, dict) else {}
    release = payload.get("release_date")
    release_date = release.get("date") if isinstance(release, dict) else release
    return AppDetail(
        appid=int(payload.get("appid") or appid),
        name=payload.get("name"),
        type=payload.get("type"),
        is_free=bool(payload.get("is_free")),
        price_cents=price.get("final"),
        currency=price.get("currency"),
        header_image=payload.get("header_image"),
        release_date=release_date,
        raw=payload,
    )


class SteamGameClient:
    def __init__(
        self,
        api_key: str | None,
        cache_path: str | Path = "./.steam_cache",
        app_ttl: int = 604800,
        timeout: float = 10.0,
        proxy: str = "",
    ) -> None:
        self.api = SteamWebAPI(api_key, timeout=timeout, proxy=proxy)
        self.api_key = str(api_key or "")
        self.cache = FileCache(cache_path)
        self.app_ttl = int(app_ttl)

    # ------------------------------------------------------ 用户数据：直接请求
    def get_owned_games(
        self,
        steamid64: str,
        *,
        include_free: bool = True,
        include_appinfo: bool = True,
    ) -> list[OwnedGame]:
        """IPlayerService/GetOwnedGames/v1。必须带 key；空响应抛 SteamDataUnavailableError。"""
        if not self.api_key:
            raise SteamAuthError("get_owned_games 需要 api_key（构造时注入或另行提供）")
        sid = require_steamid64(steamid64)
        data = self.api.request(
            "/IPlayerService/GetOwnedGames/v1/",
            {
                "steamid": sid,
                "include_played_free_games": include_free,
                "include_appinfo": include_appinfo,
                # 显式传 false：不依赖接口默认值，未审核的 DLC 也要出现在结果里
                "skip_unvetted_apps": "false",
            },
        )
        response = unwrap_response(data)
        games = response.get("games")
        if not isinstance(games, list) or not games:
            raise SteamDataUnavailableError()
        return [
            OwnedGame(
                appid=int(game["appid"]),
                name=game.get("name"),
                playtime_forever=int(game.get("playtime_forever") or 0),
                playtime_2weeks=(
                    int(game["playtime_2weeks"]) if game.get("playtime_2weeks") is not None else None
                ),
                img_icon_url=game.get("img_icon_url") or None,
            )
            for game in games
            if game.get("appid") is not None
        ]

    def get_wishlist(self, steamid64: str) -> list[int]:
        """IWishlistService/GetWishlist/v1，返回 appid 列表。空列表抛 SteamDataUnavailableError。

        wishlistdata 旧路径 2024-11 起已不可用，这里只走 IWishlistService。
        """
        sid = require_steamid64(steamid64)
        data = self.api.request("/IWishlistService/GetWishlist/v1/", {"steamid": sid})
        items = unwrap_response(data).get("items")
        if not isinstance(items, list) or not items:
            raise SteamDataUnavailableError()
        return [int(item["appid"]) for item in items if isinstance(item, dict) and item.get("appid") is not None]

    # ------------------------------------------------------ 游戏元数据：走缓存
    def get_app_details(self, appid: int, *, force_refresh: bool = False, cc: str = "US") -> AppDetail | None:
        """store/api/appdetails，无需 key。**只带单个 appid**（多 appid 的逗号写法会被回 400）。

        ``success=false``/data 为空 → 返回 None（正常缺失，不抛异常、不写不删缓存）；
        ``force_refresh=True`` 跳过读、强制请求，但请求失败时旧缓存原样保留。
        """
        appid = int(appid)
        if not force_refresh:
            cached = self.cache.get(appid, cc, self.app_ttl)
            if cached is not None:
                return _to_detail(cached, appid)
        data = self.api.request("/api/appdetails", {"appids": appid, "cc": cc}, base=STORE_BASE)
        payload = _payload_from_entry(appid, data.get(str(appid)))
        if payload is None:
            return None
        try:
            self.cache.set(appid, cc, payload, self.app_ttl)
        except SteamError:
            pass                        # 缓存写不进去（磁盘满之类）不能把手上这份数据也丢掉
        return _to_detail(payload, appid)

    def get_app_details_map(self, appids: Iterable[int], *, force_refresh: bool = False,
                            cc: str = "US") -> dict[int, AppDetail | None]:
        """按 appid 补全（去重、≤8 路并发），命中缓存的不发请求。

        失败口径：

        * 单条 ``success=false`` 只影响那一条，照旧不写不删缓存；
        * **网络类失败（超时/断网/5xx 重试用尽）熔断本次剩下的 id**：每个未命中的
          id 最坏要付 4 次超时，链路断了还硬排 300 个队，抓取就会像 2026-10-18 那次
          一样挂半小时、结果全是空值。
        """
        ids = list(dict.fromkeys(int(value) for value in appids))
        details: dict[int, AppDetail | None] = {}
        pending: list[int] = []
        for appid in ids:
            cached = None if force_refresh else self.cache.get(appid, cc, self.app_ttl)
            if cached is not None:
                details[appid] = _to_detail(cached, appid)
            else:
                pending.append(appid)
        if not pending:
            return details

        down = threading.Event()

        def one(appid: int) -> tuple[int, AppDetail | None]:
            if down.is_set():
                return appid, None
            try:
                return appid, self.get_app_details(appid, force_refresh=force_refresh, cc=cc)
            except SteamNetworkError:
                down.set()               # 链路已经不通了，后面排队的别再去撞超时
                return appid, None
            except SteamError:
                return appid, None

        with ThreadPoolExecutor(max_workers=min(_MAX_ENRICH_WORKERS, len(pending))) as pool:
            for appid, detail in pool.map(one, pending):
                details[appid] = detail
        return details

    def enrich_apps(
        self, appids: list[int], *, force_refresh: bool = False, cc: str = "US"
    ) -> list[AppDetail | None]:
        """并发补全，结果与入参**下标一一对齐**（重复的 appid 也对得上），单条失败返回 None 不中断。"""
        ids = [int(value) for value in appids]
        if not ids:
            return []
        try:
            details = self.get_app_details_map(ids, force_refresh=force_refresh, cc=cc)
        except SteamError:
            return [None] * len(ids)        # 缓存目录出问题也不把抓取带崩
        return [details.get(appid) for appid in ids]

    # ------------------------------------------------------------------ 清理
    def clear_cache(self) -> int:
        """只清 ``apps/`` 下的缓存文件，返回删除数量。"""
        return self.cache.clear()
