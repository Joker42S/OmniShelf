"""Steam 收藏 importer（单一数据结构，游戏库 / 愿望单通用）。

插件抓取时**总会**给每款补全商店信息，两份快照（``steam_owned.json`` /
``steam_wishlist.json``）是同一个 item schema，只有顶层 ``list`` 与 item
``status``（done / wishlist）不同——所以这里只有一个 importer，两个文件都能导。

两份快照共用 **同一条外部 ID 空间** ``external_ids("steam", appid)``：
愿望单先入库、后来买了，再导游戏库时走「本源命中」更新到 ``done``，不会长出第二条。

统一 item schema（``plugins/steam_fetch`` 产出）::

    {"appid":730,"name":"Counter-Strike 2","status":"done",
     "playtime_forever":640,"playtime_2weeks":null,
     "cover_url":"https://…/header.jpg","release_date":"21 Aug, 2012",
     "developers":["Valve"],"genres":["Action","免费游玩"],
     "short_description":"…纯文本简介…","url":"https://store.steampowered.com/app/730/"}

字段去处：``appid`` -> 外部 ID（幂等的关键）；``name`` -> title；
``playtime_forever``（分钟）-> ``extra.playtime_hours``（小时，1 位小数）；
``release_date`` 里抽 4 位年份 -> ``year``，原串留在 ``extra.release_date``；
``cover_url`` -> ``extra.cover_url``（只存链接，不下载图片）；
``short_description`` -> notes（备注）；``developers`` -> creators（创作者）；
``genres`` -> tags（标签）。价格不进库：商店价格随区域/促销常变，记下来很快就过期。

旧快照的 ``header_image`` 键名仍被接受（映射候选里保留）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from .base import ItemDraft, RawRecord
from .json_file import JsonFileImporter
from .registry import register

__all__ = ["SteamImporter"]

_YEAR = re.compile(r"\d{4}")


def year_from_text(value: Any) -> int | None:
    """Steam 的发行日期是「21 Aug, 2012」这种本地化串，只认里面第一个 4 位数字。"""
    match = _YEAR.search(str(value or ""))
    return int(match.group()) if match else None


def minutes_to_hours(value: Any) -> float | None:
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return None
    return round(minutes / 60.0, 1)


@register("steam")
class SteamImporter(JsonFileImporter):
    """Steam 收藏统一 importer：游戏库（done）与愿望单（wishlist）同一 schema。"""

    source = "steam"
    kind = "game"
    mapping: "dict[str, Sequence[str]]" = {
        "title": ["name", "title"],
        "status": ["status"],
        "year": ["release_date"],
        "notes": ["short_description"],
        "creators": ["developers"],
        "tags": ["genres"],
        "extra.cover_url": ["cover_url", "header_image"],
        "extra.playtime_hours": ["playtime_forever"],
        "extra.release_date": ["release_date"],
        "external_ids.steam": ["appid"],
    }
    transforms = {
        "year": year_from_text,
        "extra.playtime_hours": minutes_to_hours,
    }

    def normalize(self, raw: RawRecord) -> ItemDraft:
        draft = super().normalize(raw)
        page_url = raw.payload.get("url")
        if page_url:
            draft.external_ids = [
                link.model_copy(update={"url": str(page_url)}) if link.source == self.source else link
                for link in draft.external_ids
            ]
        return draft
