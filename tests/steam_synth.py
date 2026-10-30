"""steam_client 测试替身：假请求层 + 合成响应（假 steamid、假 appid 数据）。

``steam_client`` 随 ``plugins/steam_fetch`` 分发（宿主按 exec 加载插件入口，
插件目录不是包），所以这里先把插件目录补进 ``sys.path`` 再 import——
和 ``plugins/copymanga_fetch/plugin.py`` 用 vendored 工具包的方式一模一样。

约定：**测试绝不出真请求**（conftest 的 ``no_outbound_requests`` 兜底），
所有远端行为都换成 :class:`FakeAPI`（替换 ``client.user.api`` / ``client.game.api``）。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

from omnishelf.config import repo_root

PLUGIN_DIR = repo_root() / "plugins" / "steam_fetch"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from steam_client import SteamClient  # noqa: E402
from steam_client.errors import SteamError  # noqa: E402
from steam_client.transport import API_BASE  # noqa: E402

#: 合成凭据：只用来断言「谁进请求、谁不进日志」，别换成真实 key
KEY = "SYNTHETICKEY000000000000000000aaaa"
OTHER_KEY = "SYNTHETICOTHER0000000000000000000bbbb"
SID = "76561197960287998"                       # 17 位，形状合法即可
SID2 = "76561198000000001"


def player(steamid: str = SID, **overrides: Any) -> dict[str, Any]:
    row = {
        "steamid": steamid,
        "personaname": "SyntheticGuy",
        "profileurl": f"https://steamcommunity.com/profiles/{steamid}/",
        "avatarfull": "https://cdn.invalid/alpha_full.jpg",
        "communityvisibilitystate": 3,
        "primarystate": "Synthetic Land",
    }
    row.update(overrides)
    return row


def owned_entry(appid: int = 730, name: str = "Counter-Strike 2", **overrides: Any) -> dict[str, Any]:
    row = {
        "appid": appid,
        "name": name,
        "playtime_forever": 640,
        "img_icon_url": "f7fa/invalid_icon.jpg",
    }
    row.update(overrides)
    return row


def app_data(appid: int = 730, name: str = "Counter-Strike 2", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "steam_appid": appid,
        "name": name,
        "type": "game",
        "is_free": False,
        "header_image": f"https://cdn.invalid/{appid}_header.jpg",
        "release_date": {"date": "21 Aug, 2012", "coming_soon": False},
        "price_overview": {"currency": "USD", "final": 999, "initial": 1999},
        "developers": ["Synthetic Valve"],
        "genres": [{"description": "Action"}, {"description": "免费游玩"}],
        "short_description": "Buy <b>wisely</b>.<br>No refunds.",
    }
    row.update(overrides)
    return row


class FakeAPI:
    """``SteamWebAPI`` 的替身：按接口路径返回合成响应，并记录每次调用。

    失败注入：

    * ``owned_error`` / ``app_error``：给异常（模拟 transport 抛错）；
    * ``app_errors``：按 appid 给异常（每次都抛，模拟那条一直坏）；
    * ``app_error_once``：按 appid 抛**一次**异常（模拟那条抖了一下，重试就好）；
    * ``app_fail_calls`` + ``app_fail_error``：前 N 次 appdetails 调用失败，之后恢复
      （模拟一段几秒的链路抖动，和 appid 无关）；
    * ``app_missing``：返回 ``success=false`` 的正常响应（不是异常）。

    ``appdetails`` **只认单 appid**：逗号列表一律回 HTTP 400，这里照抄真机行为。
    """

    def __init__(
        self,
        *,
        owned: list[dict[str, Any]] | None = None,
        owned_error: Exception | None = None,
        wishlist: list[dict[str, Any]] | None = None,
        app_data: dict[int, dict[str, Any]] | None = None,
        app_missing: set[int] | None = None,
        app_error: Exception | None = None,
        app_errors: dict[int, Exception] | None = None,
        app_error_once: dict[int, Exception] | None = None,
        app_fail_calls: int = 0,
        app_fail_error: Exception | None = None,
        players: dict[str, dict[str, Any]] | None = None,
        vanity: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.owned = owned if owned is not None else [owned_entry()]
        self.owned_error = owned_error
        self.wishlist = wishlist if wishlist is not None else [{"appid": 730}]
        self.app_data = app_data if app_data is not None else {730: app_data_row()}
        self.app_missing = app_missing or set()
        self.app_error = app_error
        self.app_errors = dict(app_errors or {})
        self.app_error_once = dict(app_error_once or {})
        self.app_fail_calls = int(app_fail_calls)
        self.app_fail_error = app_fail_error or SteamError("simulated outage")
        self._fail_lock = threading.Lock()
        self.players = players if players is not None else {SID: player()}
        self.vanity = vanity if vanity is not None else {"gabelogannewell": SID}

    # ------------------------------------------------------------------ 记录
    def calls_to(self, needle: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if needle in call["path"]]

    @property
    def call_count(self) -> int:
        return len(self.calls)

    # ------------------------------------------------------------------ 协议
    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        base: str = API_BASE,
    ) -> dict[str, Any]:
        self.calls.append({"path": path, "params": dict(params or {}), "key": key, "base": base})
        if "GetOwnedGames" in path:
            if self.owned_error is not None:
                raise self.owned_error
            return {"response": {"game_count": len(self.owned), "games": list(self.owned)}}
        if "GetWishlist" in path:
            # 真实形状：IWishlistService 把结果包在 response 里，参数名是 steamid
            assert params and "steamid" in params, "GetWishlist 应传 steamid"
            return {"response": {"items": list(self.wishlist)}}
        if "GetPlayerSummaries" in path:
            ids = [s for s in str(params.get("steamids", "")).split(",") if s]
            return {"response": {"players": [self.players[s] for s in ids if s in self.players]}}
        if "ResolveVanityURL" in path:
            name = str(params.get("vanityurl"))
            if name in self.vanity:
                return {"response": {"success": 1, "steamid": self.vanity[name]}}
            return {"response": {"success": 42}}
        if "appdetails" in path:
            raw = str(params.get("appids", ""))
            if "," in raw:
                # 真机 2026-10-18 实测：逗号多 appid 直接回 HTTP 400（transport 映射成 SteamError）。
                # 假请求层照抄这条，谁写回批量拉取谁就在这里红。
                raise SteamError(f"HTTP 400: /api/appdetails（appdetails 不支持多 appid: {raw!r}）")
            appid = int(raw)
            with self._fail_lock:
                fail_call = self.app_fail_calls > 0
                if fail_call:
                    self.app_fail_calls -= 1
            if fail_call:
                raise self.app_fail_error
            if appid in self.app_error_once:
                raise self.app_error_once.pop(appid)
            if appid in self.app_errors:
                raise self.app_errors[appid]
            if self.app_error is not None and appid not in self.app_missing:
                raise self.app_error
            if appid in self.app_missing or appid not in self.app_data:
                return {str(appid): {"success": False}}
            return {str(appid): {"success": True, "data": dict(self.app_data[appid])}}
        raise AssertionError(f"FakeAPI 不认识的接口: {path}")


def app_data_row(appid: int = 730) -> dict[str, Any]:
    return app_data(appid)


def make_client(fake: FakeAPI, cache_path: Path, **kwargs: Any) -> SteamClient:
    """建一个真 SteamClient，然后把两个子模块的请求层换成 ``fake``。"""
    client = SteamClient(api_key=KEY, cache_path=cache_path, **kwargs)
    client.user.api = fake
    client.game.api = fake
    return client
