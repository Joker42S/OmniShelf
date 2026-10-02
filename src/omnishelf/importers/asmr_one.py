"""asmr.one（DLsite 音声作品）的播放列表 / 收藏导出 importer。

数据形状（顶层是 ``{"works": [...]}``）::

    {"works": [{"id": 1072074, "title": "...", "name": "社团名", "release": "2023-07-23",
                "circle": {"id": 60242, "name": "Rad.Revel", ...},
                "vas": [{"id": "...", "name": "柚木つばめ"}],
                "tags": [{"name": "啊哦淫叫", "i18n": {"ja-jp": {"name": "オホ声"}}}],
                "language_editions": [{"label": "日本語", "workno": "RJ01072074"}],
                "userRating": null, "duration": 8509, "mainCoverUrl": "..."}]}

字段映射
--------
``title``                     ``title``
``name`` + ``circle.name`` +
``vas[].name``                ``creators``（**都是作者**：社团在前，声优按顺序跟在后面，多值）
``release``                   ``year``（取年份）+ ``extra.release_date``
``userRating``                ``rating``（**本人评分**：1~5 星 ×2 → 0~10；null 表示没打分）
``tags[].i18n.ja-jp.name``    ``tags``（日文原名；没有就用 ``tags[].name``）
``id``                        ``external_ids.asmr_one``（幂等的关键）
``source_id``（RJ…）           ``external_ids.dlsite``（同一个作品在 DLsite 的正身编号）
``mainCoverUrl``              ``extra.cover_url``（约定名，方便以后统一下封面）
``duration``（秒）             ``extra.duration_minutes``（四舍五入到分钟）
``language_editions[].label`` ``extra.languages``（语言版本列表）
``has_subtitle`` / ``nsfw`` /
``age_category_string`` /
``price`` / ``source_type`` /
``playlist_rel_created_at``   同名 ``extra`` 字段（``price`` → ``price_jpy``）
``review_text``               ``notes``

**不导入**：``rate_average_2dp`` / ``rate_count`` / ``rate_count_detail`` / ``rank``
（大众评分与排行，不是本人的打分）；``dl_count`` / ``review_count``（公开统计）；
``work_attributes`` / ``translation_info`` / ``other_language_editions_in_db``（内部数据）。
``other_language_editions_in_db`` 里的其他版本是**独立的作品**（各自有 asmr.one id），
按来源各自成条，这里不做关联。

**状态**：这份导出里没有「听过/在听」这种信息（播放列表 ≠ 已听），
所以 ``status`` 留空（未设置），需要的话在界面上批量改。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..kinds import get_kind
from ..repository import clean_creators
from .base import DraftExternalId, ItemDraft, RawRecord
from .json_file import JsonFileImporter
from .registry import register

__all__ = [
    "AsmrOneImporter",
    "edition_labels",
    "optional_editions",
    "ja_tag_names",
    "minutes_from_seconds",
    "rating_from_user",
    "year_from_release",
]


def ja_tag_names(value: Any) -> list[str]:
    """``tags`` 是对象数组：取 ``i18n['ja-jp'].name``（日文原名），没有就退回 ``name``。"""
    if not isinstance(value, (list, tuple)):
        return []
    names: list[str] = []
    for tag in value:
        if not isinstance(tag, Mapping):
            continue
        name = ""
        i18n = tag.get("i18n")
        if isinstance(i18n, Mapping):
            ja = i18n.get("ja-jp")
            if isinstance(ja, Mapping):
                name = str(ja.get("name") or "").strip()
        if not name:
            name = str(tag.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def year_from_release(value: Any) -> int | None:
    """``"2023-07-23"`` -> ``2023``。"""
    text = str(value or "").strip()
    head = text[:4]
    return int(head) if head.isdigit() else None


def rating_from_user(value: Any) -> float | None:
    """``userRating`` 是本人评分：1~5 星 → 0~10；空值就是没打分。"""
    if value is None or value == "":
        return None
    try:
        stars = float(value)
    except (TypeError, ValueError):
        return None
    if stars <= 0:
        return None
    return round(min(stars, 5.0) * 2, 2)


def minutes_from_seconds(value: Any) -> int | None:
    """``duration`` 是秒 → 分钟（四舍五入）。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return round(seconds / 60)


def edition_labels(value: Any) -> list[str]:
    """``language_editions`` -> ``["日本語", "英語", "簡体中文", ...]``（按 display_order）。"""
    if not isinstance(value, (list, tuple)):
        return []
    rows = [row for row in value if isinstance(row, Mapping)]
    rows.sort(key=lambda row: row.get("display_order") or 0)
    labels: list[str] = []
    for row in rows:
        label = str(row.get("label") or row.get("lang") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def optional_editions(value: Any) -> list[str] | None:
    """空的语言版本列表不写进 extra（``None`` = 这个字段没内容）。"""
    return edition_labels(value) or None


def creator_names(payload: Mapping[str, Any]) -> list[str]:
    """社团名（顶层 ``name`` 或 ``circle.name``）+ 所有声优名，去重保序。"""
    names: list[str] = []
    for key in ("name", "circle.name"):
        _found, value = JsonFileImporter.lookup(payload, key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    vas = payload.get("vas")
    if isinstance(vas, (list, tuple)):
        for va in vas:
            if isinstance(va, Mapping):
                name = str(va.get("name") or "").strip()
                if name:
                    names.append(name)
    return clean_creators(names)


@register("asmr_one")
class AsmrOneImporter(JsonFileImporter):
    """asmr.one 的播放列表 / 收藏导出（``{"works": [...]}``）。"""

    source = "asmr_one"
    kind = "asmr"

    mapping: Mapping[str, Sequence[str]] = {
        "title": ["title"],
        "year": ["release"],
        "rating": ["userRating"],
        "notes": ["review_text"],
        "tags": ["tags"],
        "external_ids.asmr_one": ["id"],
        "extra.release_date": ["release"],
        "extra.duration_minutes": ["duration"],
        "extra.languages": ["language_editions"],
        "extra.has_subtitle": ["has_subtitle"],
        "extra.nsfw": ["nsfw"],
        "extra.age_category": ["age_category_string"],
        "extra.price_jpy": ["price"],
        "extra.source_type": ["source_type"],
        "extra.playlist_added_at": ["playlist_rel_created_at"],
        "extra.cover_url": ["mainCoverUrl", "samCoverUrl"],
    }

    transforms = {
        "year": year_from_release,
        "rating": rating_from_user,
        "tags": ja_tag_names,
        "extra.duration_minutes": minutes_from_seconds,
        "extra.languages": optional_editions,
    }

    def normalize(self, raw: RawRecord) -> ItemDraft:
        draft = super().normalize(raw)
        payload = raw.payload
        # 创作者是多值：社团 + 声优（顶层 name 和 circle.name 是同一个社团名，去重归 clean_creators）
        draft.creators = creator_names(payload)
        # 正身编号也记一条：以后从 DLsite 直接导同一作品时能对上（同源判重按编号）
        source_type = str(payload.get("source_type") or "").strip().lower()
        workno = str(payload.get("source_id") or "").strip()
        page_url = str(payload.get("source_url") or "").strip()
        if source_type and workno and not any(link.source == source_type for link in draft.external_ids):
            draft.external_ids.append(
                DraftExternalId(source=source_type, external_id=workno, url=page_url or None)
            )
        if page_url:
            draft.external_ids = [
                link.model_copy(update={"url": page_url}) if not link.url else link
                for link in draft.external_ids
            ]
        return draft


# kinds 注册表里必须有 asmr（import 时就检查，早于运行时）
get_kind("asmr")
