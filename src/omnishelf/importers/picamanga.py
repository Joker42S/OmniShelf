"""picamanga 导出文件的 importer（一个来源 = 一个 importer）。

格式（顶层对象，`items` 数组，每条约 19 个字段）::

    {"schemaVersion":1,"exportedAt":1790951158770,"groups":[{"id":"default","name":"默认"}],
     "items":[{"comicId":"6aaf...","title":"...","author":"...", ...}]}

字段取舍（对着真实导出定的）：

===========================  ==========================================================
源字段                        去处
===========================  ==========================================================
``title``                    ``title``（原样保留：常见 ``日文 | 中文 [中国翻訳] [DL版]``）
``author``                   ``creators``（通用列，单值也是列表）
``categories``               ``tags``（可直接用 ``omnishelf list --tag 長篇`` 过滤）
``comicId``                  ``external_ids.picamanga``（24 位 hex，唯一 → 幂等的关键）
``knownEpsCount``            ``extra.chapter_count``
``latestChapterTitle``       ``extra.latest_chapter``（可能为 null → 不写）
``remoteUpdatedAt``          ``extra.remote_updated_at``（ISO8601，可能为 null → 不写）
``finished``                 ``extra.finished``（连载状态，**不是**你的阅读状态）
``hasNewChapter``            ``extra.has_new_chapter``
``thumbFileServer``+``thumbPath``  ``extra.cover_url``（两个字段拼起来，见 ``normalize``）
其余                         不导入（``groupId``/``checkEnabled``/``pinnedAt``/
                             ``thumbOriginalName``/``addedAt``/``updatedAt`` 是导出工具的记账字段）
===========================  ==========================================================

**这个来源没有 year / rating / 阅读状态**，所以这三项留空，不猜；
要补就在 Web 详情页手动填。

将来接别的来源：复制本文件改 ``source`` / ``mapping`` / ``external_ids.<来源>`` 即可
（copymanga 那份就是这么来的），不需要动框架。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .base import ItemDraft, RawRecord
from .json_file import JsonFileImporter
from .registry import register


def join_cover_url(server: Any, path: Any) -> str | None:
    """把 ``thumbFileServer`` + ``thumbPath`` 拼成可下载的绝对 URL。"""
    if not server or not path:
        return None
    return f"{str(server).rstrip('/')}/{str(path).lstrip('/')}"


@register("picamanga")
class PicamangaImporter(JsonFileImporter):
    """picamanga 导出的漫画库（``{"items": [...]}``）。"""

    source = "picamanga"
    kind = "manga"
    mapping: Mapping[str, Sequence[str]] = {
        "title": ["title"],
        "creators": ["author"],
        "extra.finished": ["finished"],
        "extra.chapter_count": ["knownEpsCount"],
        "extra.latest_chapter": ["latestChapterTitle"],
        "extra.remote_updated_at": ["remoteUpdatedAt"],
        "extra.has_new_chapter": ["hasNewChapter"],
        "external_ids.picamanga": ["comicId"],
        "tags": ["categories"],
    }

    def normalize(self, raw: RawRecord) -> ItemDraft:
        draft = super().normalize(raw)
        # cover_url 需要拼接两个字段，声明式 mapping 表达不了，所以这里 override。
        cover_url = join_cover_url(raw.payload.get("thumbFileServer"), raw.payload.get("thumbPath"))
        if cover_url:
            draft.extra["cover_url"] = cover_url
        return draft
