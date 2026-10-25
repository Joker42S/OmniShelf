"""数据模型：全部 frozen dataclass，并保留 ``raw`` 原始响应。

``raw`` 让调用方能读取这里没建模的字段，不必为每个新字段改模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["PlayerSummary", "OwnedGame", "AppDetail"]


@dataclass(frozen=True)
class PlayerSummary:
    steamid: str
    persona_name: str | None = None
    profile_url: str | None = None
    avatar: str | None = None
    #: 3=公开, 1=私密
    communityvisibilitystate: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OwnedGame:
    appid: int
    name: str | None = None
    #: 累计游玩时长（分钟）
    playtime_forever: int = 0
    #: 近两周游玩时长（分钟）；Steam 没给就是 None
    playtime_2weeks: int | None = None
    img_icon_url: str | None = None


@dataclass(frozen=True)
class AppDetail:
    appid: int
    name: str | None = None
    type: str | None = None
    is_free: bool = False
    #: 价格（分）。无价格信息（如区域无售）时 None
    price_cents: int | None = None
    currency: str | None = None
    header_image: str | None = None
    release_date: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
