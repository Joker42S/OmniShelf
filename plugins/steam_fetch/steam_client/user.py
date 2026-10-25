"""玩家资料模块：**任何方法都不读写缓存**（设计文档硬约束 1）。

相同参数连续调用必须产生两次远端请求——玩家资料是实时状态，落盘就是错的。
"""

from __future__ import annotations

from typing import Any

from .errors import SteamAuthError, SteamDataUnavailableError, SteamNotFoundError
from .models import PlayerSummary
from .transport import SteamWebAPI, require_steamid64, unwrap_response

__all__ = ["SteamUserClient"]

#: GetPlayerSummaries 单次上限，超出自动分片
_MAX_IDS_PER_REQUEST = 100


def _to_summary(player: dict[str, Any]) -> PlayerSummary:
    return PlayerSummary(
        steamid=str(player.get("steamid") or ""),
        persona_name=player.get("personaname"),
        profile_url=player.get("profileurl"),
        avatar=player.get("avatarfull") or player.get("avatarmedium") or player.get("avatar"),
        communityvisibilitystate=player.get("communityvisibilitystate"),
        raw=player,
    )


class SteamUserClient:
    def __init__(self, api_key: str, *, timeout: float = 10.0, proxy: str = "") -> None:
        if not api_key:
            raise SteamAuthError("api_key 不能为空：用户模块的每个调用都必须带 key")
        self.api = SteamWebAPI(api_key, timeout=timeout, proxy=proxy)

    # ------------------------------------------------------------------ 解析
    def resolve_vanity_url(self, vanity: str) -> str:
        """个性签名（自定义 URL）-> steamid64。success != 1 抛 ``SteamNotFoundError``。"""
        text = str(vanity or "").strip()
        if not text:
            raise ValueError("vanity 不能为空")
        data = self.api.request("/ISteamUser/ResolveVanityURL/v1/", {"vanityurl": text})
        response = unwrap_response(data)
        if response.get("success") != 1:
            raise SteamNotFoundError(f"vanity 解析失败（success={response.get('success')!r}）: {text}")
        steamid = str(response.get("steamid") or "")
        if not steamid:
            raise SteamNotFoundError(f"vanity 解析结果里没有 steamid: {text}")
        return require_steamid64(steamid)

    # ------------------------------------------------------------------ 资料
    def get_player_summary(self, steamid64: str, *, api_key: str | None = None) -> PlayerSummary:
        """单个玩家资料。``api_key`` 覆盖本次调用使用的 key。

        私密/受限档案 Steam 会回空 players——按「不可用或未公开」抛错，不返回假数据。
        """
        sid = require_steamid64(steamid64)
        players = self._fetch_players([sid], api_key=api_key)
        if not players:
            raise SteamDataUnavailableError()
        return _to_summary(players[0])

    def get_player_summaries(self, steamid64s: list[str]) -> list[PlayerSummary]:
        """批量资料；单次最多 100 个 ID，超出自动分片。"""
        ids = [require_steamid64(value) for value in steamid64s]
        players: list[dict[str, Any]] = []
        for start in range(0, len(ids), _MAX_IDS_PER_REQUEST):
            players.extend(self._fetch_players(ids[start:start + _MAX_IDS_PER_REQUEST]))
        return [_to_summary(player) for player in players]

    # ------------------------------------------------------------------ 内部
    def _fetch_players(self, steamids: list[str], *, api_key: str | None = None) -> list[dict[str, Any]]:
        data = self.api.request(
            "/ISteamUser/GetPlayerSummaries/v2/",
            {"steamids": ",".join(steamids)},
            key=api_key,
        )
        response = unwrap_response(data)
        players = response.get("players")
        return players if isinstance(players, list) else []
