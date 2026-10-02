"""copymanga（copy4000.com）书架导出的 importer。

格式（顶层对象 + ``items`` 数组，每条约 23 个字段）::

    {"source":"https://copy4000.com/web/person/shujia","host":"https://copy4000.com",
     "schema_version":3,"total":392,"items":[{"collect_id":"287813","name":"...", ...}]}

字段取舍（对着真实导出定的，392 条）：

============================  ==================================================
源字段                         去处
============================  ==================================================
``name``                      ``title``（原样保留）
``authors``（list）           ``creators``（直接就是多值）
``themes``（list）            ``tags``（这份导出里都是空数组，仍然映射，兼容以后有值）
``comic_uuid``                ``external_ids.copymanga``（UUID，唯一 → 幂等的关键）
``url``                       写进该外部 ID 的 ``url`` 字段（详情页可点）
``cover``                     ``extra.cover_url``（约定名，和 picamanga 一致）
``status_label``              ``extra.status_label``（原文：短篇/連載中/已完結）
                              + ``extra.finished``（由它推导的布尔值）
``last_chapter.name``         ``extra.latest_chapter``
``last_browse.name``          ``extra.reading_progress``（读到第几话；可能为空 → 不写）
``comic_updated_at``          ``extra.remote_updated_at``（日期字符串）
其余（``collect_id`` ``popular`` ``shelf_position`` ``first_seen`` ``last_seen``
``removed`` ``removed_at`` ``free_type`` ``kind`` ``folder`` ``raw`` ``path_word``）
                              不导入：要么是同步工具的记账字段，要么是站点统计
============================  ==================================================

两点说明：

* 这个来源**没有 year / 评分 / 阅读状态**，所以留空不猜；``reading_progress`` 只是
  「读到第几话」的原始信息，不会自动推断成我们的 ``status``。
* 真实数据里 ``removed`` 全是 ``False``，所以没有特殊处理；若将来出现已移除的条目，
  它们会按普通条目导入。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .base import ItemDraft, RawRecord
from .json_file import JsonFileImporter
from .registry import register

#: 站点给的连载状态 -> 我们的 finished（未列出的新状态不猜，字段留空）
_FINISHED_BY_LABEL: dict[str, bool] = {"已完結": True, "短篇": True, "連載中": False}


def as_creator_list(value: Any) -> list[str]:
    """``authors`` → ``creators``。

    来源本来就是数组（所以**不要再拼成字符串**）；字符串也接受
    （老版本拼过的 ``"A / B"`` 会被 clean_creators 按同一个规则切开）。
    """
    from ..repository import clean_creators

    return clean_creators(value)


def chapter_name(value: Any) -> str | None:
    """``last_chapter`` / ``last_browse`` 是 ``{id, name, url}``，取 name。"""
    if isinstance(value, Mapping):
        name = value.get("name")
        return str(name).strip() if name else None
    return None


@register("copymanga")
class CopymangaImporter(JsonFileImporter):
    """copymanga 书架导出（``{"items": [...]}``）。"""

    source = "copymanga"
    kind = "manga"
    mapping: Mapping[str, Sequence[str]] = {
        "title": ["name"],
        "creators": ["authors"],
        "tags": ["themes"],
        "extra.cover_url": ["cover"],
        "extra.status_label": ["status_label"],
        "extra.finished": ["status_label"],
        "extra.latest_chapter": ["last_chapter"],
        "extra.reading_progress": ["last_browse"],
        "extra.remote_updated_at": ["comic_updated_at"],
        "external_ids.copymanga": ["comic_uuid"],
    }
    transforms: Mapping[str, Any] = {
        "creators": as_creator_list,
        "extra.finished": lambda value: _FINISHED_BY_LABEL.get(str(value)),
        "extra.latest_chapter": chapter_name,
        "extra.reading_progress": chapter_name,
    }

    def normalize(self, raw: RawRecord) -> ItemDraft:
        draft = super().normalize(raw)
        # 把漫画页地址挂到外部 ID 上（声明式 mapping 表达不了「给 ID 附 URL」这一层）
        page_url = raw.payload.get("url")
        if page_url:
            draft.external_ids = [
                link.model_copy(update={"url": str(page_url)})
                if link.source == self.source
                else link
                for link in draft.external_ids
            ]
        return draft
