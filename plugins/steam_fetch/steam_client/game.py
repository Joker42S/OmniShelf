"""游戏模块：游戏库 / 愿望单**直接请求 Steam**，只有 app 详情走本地缓存。

硬约束（设计文档 §4）：

* ``get_owned_games`` / ``get_wishlist`` 不接受 ``force_refresh``，实现不 import cache；
* 缓存目录只出现 ``apps/{cc}/{appid}.json``（价格随商店区域变化，按区域分目录），
  不出现任何用户维度文件；
* app 详情失败（请求异常或 success=false）**不删除、不覆盖**已有缓存。

商店信息**不能批量取**：``appdetails`` 的逗号多 appid 写法现在会被 Steam 直接回
HTTP 400（2026-10-18 实测：``appids=730,570`` → 400，``appids=730`` → 200），
所以这里按 appid 单条请求，靠**并发（≤8 路）+ 网络熔断**压时间。

熔断按**连续失败**判定（成功一次就清零）：偶发一两个超时只影响那一条，不会让后面
几百个 id 集体留空（2026-10-30 愿望单尾部 100 多条全变数字标题就是这么来的）。
真熔断或单条网络失败后，隔一小会儿用低并发**补跑（最多两轮）**——链路只是抖了一下
就能把名字/封面捞回来，链路真断了也只会再撞有限的几次墙。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from .cache import FileCache
from .errors import (
    SteamAuthError,
    SteamDataUnavailableError,
    SteamError,
    SteamNetworkError,
    SteamRateLimitError,
)
from .models import AppDetail, OwnedGame
from .transport import STORE_BASE, SteamWebAPI, require_steamid64, unwrap_response

__all__ = ["SteamGameClient"]

#: 并发补全的线程上限
_MAX_ENRICH_WORKERS = 8
#: 连续这么多次网络失败才认定链路断了（单次抖动不该连坐后面的 id）
_BREAKER_STREAK = 5
#: 熔断/单条失败后隔多久补跑一轮（秒）
_RECOVERY_PAUSE = 2.0
#: 补跑用的低并发：链路刚抖过，别再一股脑挤上去
_RECOVERY_WORKERS = 4
#: 最多补跑几轮：链路抖一下通常一轮就够，抖得久一点给第二轮
_RECOVERY_ROUNDS = 2


class _CircuitBreaker:
    """连续 N 次网络失败后熔断；任何一次成功都把连续计数清零。"""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._streak = 0
        self._tripped = threading.Event()

    @property
    def tripped(self) -> bool:
        return self._tripped.is_set()

    def record_success(self) -> None:
        with self._lock:
            self._streak = 0

    def record_failure(self) -> bool:
        """记一次网络失败，返回熔断是否已经打开。"""
        with self._lock:
            self._streak += 1
            if self._streak >= self.limit:
                self._tripped.set()
            return self._tripped.is_set()


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
        #: 熔断/单条失败后补跑前的等待（秒）；测试里置 0
        self.recovery_pause = float(_RECOVERY_PAUSE)

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

    def _fetch_details(
        self,
        ids: list[int],
        *,
        force_refresh: bool,
        cc: str,
        workers: int,
        breaker: _CircuitBreaker,
    ) -> tuple[dict[int, AppDetail | None], list[int], list[int]]:
        """跑一轮 appdetails，返回 (拿到的结果, 可补跑的 id, 熔断后没问的 id)。"""
        fetched: dict[int, AppDetail | None] = {}
        retryable: list[int] = []
        skipped: list[int] = []
        lock = threading.Lock()

        def one(appid: int) -> None:
            if breaker.tripped:
                with lock:
                    skipped.append(appid)
                return
            try:
                detail = self.get_app_details(appid, force_refresh=force_refresh, cc=cc)
            except SteamNetworkError:
                tripped = breaker.record_failure()
                with lock:
                    (skipped if tripped else retryable).append(appid)
            except SteamRateLimitError:
                with lock:
                    retryable.append(appid)     # 限流是暂时的，补跑时再问
            except SteamError:
                with lock:
                    fetched[appid] = None       # 单条 401/400/success=false：不是链路问题
            else:
                breaker.record_success()
                with lock:
                    fetched[appid] = detail

        with ThreadPoolExecutor(max_workers=min(workers, len(ids))) as pool:
            list(pool.map(one, ids))
        return fetched, retryable, skipped

    def get_app_details_map(self, appids: Iterable[int], *, force_refresh: bool = False,
                            cc: str = "US",
                            stats: dict[str, int] | None = None) -> dict[int, AppDetail | None]:
        """按 appid 补全（去重、≤8 路并发），命中缓存的不发请求。

        ``stats`` 传一个 dict 进来可拿到本次的分类计数（total/cached/resolved/
        unavailable/failed），插件据此把「商店说不给」和「网络没取回」分开报告。

        失败口径：

        * 单条 ``success=false`` 只影响那一条，照旧不写不删缓存；
        * 网络类失败按**连续**次数熔断：偶发一两次只补跑那几条，不会连坐后面的 id；
          连续撞墙（链路真断了）才停手，避免每个 id 都付 4 次超时；
        * 熔断/网络失败/限流的 id 隔一会儿低并发补跑（最多两轮），链路抖一下也能把名字捞回来。
        """
        ids = list(dict.fromkeys(int(value) for value in appids))
        if stats is not None:
            stats.clear()
        details: dict[int, AppDetail | None] = {}
        pending: list[int] = []
        for appid in ids:
            cached = None if force_refresh else self.cache.get(appid, cc, self.app_ttl)
            if cached is not None:
                details[appid] = _to_detail(cached, appid)
            else:
                pending.append(appid)
        unavailable_ids: set[int] = set()

        if pending:
            fetched, retryable, skipped = self._fetch_details(
                pending,
                force_refresh=force_refresh,
                cc=cc,
                workers=_MAX_ENRICH_WORKERS,
                breaker=_CircuitBreaker(_BREAKER_STREAK),
            )
            details.update(fetched)
            unavailable_ids.update(appid for appid, detail in fetched.items() if detail is None)

            for _ in range(_RECOVERY_ROUNDS):
                retry_set = set(retryable) | set(skipped)
                retry_ids = [appid for appid in pending if appid in retry_set]
                if not retry_ids:
                    break
                if self.recovery_pause:
                    time.sleep(self.recovery_pause)
                fetched, retryable, skipped = self._fetch_details(
                    retry_ids,
                    force_refresh=force_refresh,
                    cc=cc,
                    workers=_RECOVERY_WORKERS,
                    breaker=_CircuitBreaker(_BREAKER_STREAK),
                )
                details.update(fetched)
                unavailable_ids.update(appid for appid, detail in fetched.items() if detail is None)

        for appid in pending:
            details.setdefault(appid, None)

        if stats is not None:
            unresolved = [appid for appid in pending if details[appid] is None]
            unavailable = sum(1 for appid in unresolved if appid in unavailable_ids)
            stats.update({
                "total": len(ids),
                "cached": len(ids) - len(pending),
                "resolved": sum(1 for appid in ids if details.get(appid) is not None),
                "unavailable": unavailable,
                "failed": len(unresolved) - unavailable,
            })
        return details

    def enrich_apps(
        self, appids: list[int], *, force_refresh: bool = False, cc: str = "US",
        stats: dict[str, int] | None = None,
    ) -> list[AppDetail | None]:
        """并发补全，结果与入参**下标一一对齐**（重复的 appid 也对得上），单条失败返回 None 不中断。"""
        ids = [int(value) for value in appids]
        if not ids:
            if stats is not None:
                stats.clear()
                stats.update({"total": 0, "cached": 0, "resolved": 0,
                              "unavailable": 0, "failed": 0})
            return []
        try:
            details = self.get_app_details_map(ids, force_refresh=force_refresh, cc=cc, stats=stats)
        except SteamError:
            if stats is not None:
                total = len(set(ids))
                stats.clear()
                stats.update({"total": total, "cached": 0, "resolved": 0,
                              "unavailable": 0, "failed": total})
            return [None] * len(ids)        # 缓存目录出问题也不把抓取带崩
        return [details.get(appid) for appid in ids]

    # ------------------------------------------------------------------ 清理
    def clear_cache(self) -> int:
        """只清 ``apps/`` 下的缓存文件，返回删除数量。"""
        return self.cache.clear()
