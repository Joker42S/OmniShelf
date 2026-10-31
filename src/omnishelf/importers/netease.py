"""网易云音乐「我喜欢的音乐」importer（``netease_music``）。

快照由 ``plugins/netease_music`` 插件生成；为了让人也能直接拿
``ncm-cli playlist tracks`` 的原始输出来导入，这里同时认两种行：

* 插件快照行：``song_id`` / ``name`` / ``artists``（字符串数组）/ ``album`` /
  ``duration_seconds``；
* ncm-cli 原始行：``originalId`` / ``name`` / ``artists``（对象数组，取 ``name``）/
  ``album``（对象，取 ``name``）/ ``duration``（毫秒）。

落库口径沿用音乐那套 11 项（见 :mod:`omnishelf.importers.music`）：

==============  ==========================================
标题            ``title``（``name``）
艺术家          ``creators`` + ``extra.artists``
专辑            ``extra.album``
时长            ``extra.duration_seconds``（毫秒换算成秒）
编号            ``extra.track_number``（有就收）
判重            ``external_ids.netease_music`` = 歌曲数字 id
==============  ==========================================

判重特意用 ``originalId``（网易云全局的歌曲数字 id），**不用**请求参数里的加密 ``id``：
同一首歌在歌单里出现两次（网易云允许）会落到同一条记录上，重复导入只会「未变化」。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import DraftExternalId, ItemDraft, NormalizeError, RawRecord
from .json_file import JsonFileImporter
from .music import build_music_draft
from .registry import register

__all__ = ["NeteaseMusicImporter", "artist_names_of", "song_id_of"]

SOURCE = "netease_music"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> int | None:
    """整数收整数，字符串只认纯数字；其它一律留空（不猜）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    return None


def artist_names_of(value: Any) -> list[str]:
    """艺术家字段 -> 名字列表；对象数组（``{"name": ...}``）与字符串数组都认。"""
    if value is None:
        return []
    pieces = value if isinstance(value, (list, tuple)) else [value]
    names: list[str] = []
    for piece in pieces:
        name = _text(piece.get("name")) if isinstance(piece, Mapping) else _text(piece)
        if name and name not in names:
            names.append(name)
    return names


def song_id_of(row: Mapping[str, Any]) -> str | None:
    """歌曲稳定 id：优先 ``originalId``（数字 id），没有才退回加密 ``id``。"""
    for key in ("song_id", "originalId", "original_id"):
        value = row.get(key)
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return _text(row.get("id"))


def album_name_of(row: Mapping[str, Any]) -> str | None:
    album = row.get("album")
    if isinstance(album, Mapping):
        return _text(album.get("name"))
    return _text(album)


def duration_seconds_of(row: Mapping[str, Any]) -> int | None:
    """快照行的 ``duration_seconds`` 直接用；原始行的 ``duration`` 是毫秒。"""
    if "duration_seconds" in row:
        return _number(row.get("duration_seconds"))
    millis = _number(row.get("duration")) or _number(row.get("dt"))
    return round(millis / 1000) if millis and millis > 0 else None


@register(SOURCE)
class NeteaseMusicImporter(JsonFileImporter):
    """导入网易云音乐歌单快照（``{"items": [...]}`` 或 ``[...]``）。"""

    source = SOURCE
    kind = "music"

    def normalize(self, raw: RawRecord) -> ItemDraft:
        row = dict(raw.payload)
        title = _text(row.get("name")) or _text(row.get("title"))
        if not title:
            raise NormalizeError("歌曲缺少 name（标题），无法入库")

        song_id = song_id_of(row)
        external_ids = (
            [DraftExternalId(source=SOURCE, external_id=song_id)] if song_id else []
        )
        track_number = _number(row.get("track_number")) or _number(row.get("no"))
        return build_music_draft(
            title=title,
            artists=artist_names_of(row.get("artists") or row.get("ar")),
            album=album_name_of(row),
            track_number=track_number,
            duration_seconds=duration_seconds_of(row),
            external_ids=external_ids,
        )
