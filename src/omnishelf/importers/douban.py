"""豆瓣收藏导出（看过 / 读过 / 玩过）的三个 importer。

三个文件是**同一套 schema**（14 个字段，仅 media/kind 与 extra 内容不同）::

    {"media":"movie","media_cn":"电影/剧集","status":"collect","status_cn":"看过",
     "user":"12345678","list_url":...,"fetched_at":...,"total_pages":47,...,
     "items":[{"subject_id":"5996825","title":"...","year":"","rating":10,"stars":5,
               "marked_date":"2026-10-05","url":"https://movie.douban.com/subject/5996825/",
               "cover":"https://img9.doubanio.com/...","extra":{...},
               "media":"movie","status":"collect","page":1,
               "first_seen":"...","last_seen":"..."}]}

字段取舍（按真实导出的形状逐字段对齐）

============================  ====================================================
源字段                         去处
============================  ====================================================
``title``                     ``title``
``year``                      ``year``（字符串转 int；**影视导出里 year 全是空的** → 留空）
``rating``                    ``rating``（**本来就是 0-10**：豆瓣 1-5 星 × 2；未评分是 null）
``status``                    ``status``（``collect`` → ``done``；未知值不猜，留空）
``subject_id``                ``external_ids.douban_{movie,book,game}``（幂等的关键）
``url``                       写进该外部 ID 的 ``url``
``cover``                     ``extra.cover_url``（统一约定名）
``marked_date``               ``extra.marked_date``（豆瓣上标日期，2015~2026 都有）
``extra.language``            ``extra.language``（影视）
``extra.playable``            ``extra.playable``（影视，豆瓣是否可播放）
``extra.intro``               ``extra.intro`` 原样 + 拆出 ``extra.runtime``
``extra.pub``                 ``extra.pub`` 原样 + 拆出 ``creators``（首段是作者）
``extra.publisher``           ``extra.publisher``（图书）
``extra.platforms``           ``extra.platform``（只留平台 token，中文类型名丢掉）
``extra.info``                ``extra.info`` 原样 + 拆出 ``extra.release_date``
其余（``stars`` ``media`` ``page`` ``first_seen`` ``last_seen`` 与顶层元信息）
                              不导入：要么和 rating 重复，要么是抓取工具的记账字段
============================  ====================================================

三个需要说明的判断：

* **source 按媒体分开**（``douban_movie`` / ``douban_book`` / ``douban_game``）。豆瓣三种媒体的
  subject_id 是各自独立的编号空间，混用一个 source 有撞号风险（撞了就会被当成同一条更新掉）。
* **影视的创作者留空**：``extra.intro`` 是「演员 / 地区 / 导演 / 时长 / 类型」用 ``/`` 拼起来的
  **无标签**串，实测按位置猜会把人名当成地区，所以不猜——原串存在 ``extra.intro`` 里，
  ``extra.runtime`` 只取能确定的那一段（``N分钟``）。
* **图书的创作者取 ``extra.pub`` 的第一段**（实测 335/347 有效），若第一段就是出版社则不写。

另外会校验文件的 ``media``：拿电影列表喂给游戏 importer 会在抓取阶段直接报错，不会写脏数据。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .base import ItemDraft, NormalizeError, RawRecord
from .json_file import JsonFileImporter
from .registry import register

#: 豆瓣条目状态 -> 我们的 status。未知值返回 None（留空，不猜）。
_STATUS_BY_DOUBAN: dict[str, str] = {
    "collect": "done",
    "do": "doing",
    "doing": "doing",
    "wish": "wishlist",
}
_RUNTIME = re.compile(r"^(\d+)分钟$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: 平台 token：ASCII 字母数字（PC / PS4 / NS / IPHN …），中文的那些其实是类型名
_PLATFORM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 +.\-]*$")


def segments(value: Any) -> list[str]:
    """豆瓣把这些信息用 ``/`` 拼成一串，切成干净的分段。"""
    return [part.strip() for part in str(value or "").split("/") if part.strip()]


def to_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def douban_status(value: Any) -> str | None:
    return _STATUS_BY_DOUBAN.get(str(value or "").strip().lower())


def runtime_minutes(intro: Any) -> int | None:
    """从「… / 98分钟 / 剧情」里取片长（正常数据里只会有一段，取最后一段）。"""
    found: int | None = None
    for part in segments(intro):
        match = _RUNTIME.match(part)
        if match:
            found = int(match.group(1))
    return found


def release_date(info: Any) -> str | None:
    """游戏 ``info`` 的最后一段是发行日期（实测 423/423 都是 YYYY-MM-DD）。"""
    parts = segments(info)
    if parts and _DATE.match(parts[-1]):
        return parts[-1]
    return None


def platforms(value: Any) -> list[str] | None:
    """``platforms`` 里混着中文类型名（动作/角色扮演…），只留像平台的 token。

    过滤后为空时保留原样，宁可不干净也别丢数据。
    """
    if isinstance(value, str):
        raw = segments(value)
    elif isinstance(value, (list, tuple)):
        raw = [str(item).strip() for item in value if str(item).strip()]
    else:
        return None
    kept = [item for item in raw if _PLATFORM.match(item)]
    return kept or raw or None


def author_from_pub(pub: Any, publisher: Any) -> str | None:
    """图书作者：``pub`` 的第一段（``作者 / 译者 / 出版社 / 出版年月 / 定价``）。

    第一段就是出版社（或空）时返回 None，避免把出版社当作者。
    """
    parts = segments(pub)
    if not parts:
        return None
    first = parts[0]
    pub_name = str(publisher or "").strip()
    if pub_name and (first == pub_name or pub_name in first):
        return None
    return first


class DoubanImporter(JsonFileImporter):
    """豆瓣导出的公共部分：媒体校验 + 外部 ID 附 URL。"""

    #: 这个 importer 认哪个 ``media``（用于挡住串用）
    media: str = ""

    def declared_media(self) -> str | None:
        """读一下文件顶层的 ``media``（读不出就不校验）。"""
        try:
            payload = json.loads(self.path.read_text(encoding=self.encoding))
        except (OSError, ValueError):
            return None
        if isinstance(payload, dict):
            value = payload.get("media")
            return str(value) if value else None
        return None

    def fetch(self) -> Iterable[RawRecord]:
        declared = self.declared_media()
        if declared and self.media and declared != self.media:
            raise NormalizeError(
                f"这个文件是 {declared} 列表（media={declared}），不能用 {type(self).__name__} 导入"
                f"（它要 media={self.media}）"
            )
        return super().fetch()

    def normalize(self, raw: RawRecord) -> ItemDraft:
        draft = super().normalize(raw)
        page_url = raw.payload.get("url")
        if page_url:
            draft.external_ids = [
                link.model_copy(update={"url": str(page_url)}) if link.source == self.source else link
                for link in draft.external_ids
            ]
        return draft


@register("douban_games")
class DoubanGamesImporter(DoubanImporter):
    """豆瓣「玩过」导出。"""

    source = "douban_game"
    kind = "game"
    media = "game"
    mapping: Mapping[str, Sequence[str]] = {
        "title": ["title"],
        "year": ["year"],
        "rating": ["rating"],
        "status": ["status"],
        "extra.cover_url": ["cover"],
        "extra.marked_date": ["marked_date"],
        "extra.platform": ["extra.platforms"],
        "extra.info": ["extra.info"],
        "extra.release_date": ["extra.info"],
        "external_ids.douban_game": ["subject_id"],
    }
    transforms: Mapping[str, Any] = {
        "year": to_int,
        "status": douban_status,
        "extra.platform": platforms,
        "extra.release_date": release_date,
    }


@register("douban_movies")
class DoubanMoviesImporter(DoubanImporter):
    """豆瓣「看过」导出。"""

    source = "douban_movie"
    kind = "movie"
    media = "movie"
    mapping: Mapping[str, Sequence[str]] = {
        "title": ["title"],
        "year": ["year"],
        "rating": ["rating"],
        "status": ["status"],
        "extra.cover_url": ["cover"],
        "extra.marked_date": ["marked_date"],
        "extra.language": ["extra.language"],
        "extra.playable": ["extra.playable"],
        "extra.intro": ["extra.intro"],
        "extra.runtime": ["extra.intro"],
        "external_ids.douban_movie": ["subject_id"],
    }
    transforms: Mapping[str, Any] = {
        "year": to_int,
        "status": douban_status,
        "extra.runtime": runtime_minutes,
    }


@register("douban_books")
class DoubanBooksImporter(DoubanImporter):
    """豆瓣「读过」导出。"""

    source = "douban_book"
    kind = "book"
    media = "book"
    mapping: Mapping[str, Sequence[str]] = {
        "title": ["title"],
        "year": ["year"],
        "rating": ["rating"],
        "status": ["status"],
        "extra.cover_url": ["cover"],
        "extra.marked_date": ["marked_date"],
        "extra.pub": ["extra.pub"],
        "extra.publisher": ["extra.publisher"],
        "external_ids.douban_book": ["subject_id"],
    }
    transforms: Mapping[str, Any] = {
        "year": to_int,
        "status": douban_status,
    }

    def normalize(self, raw: RawRecord) -> ItemDraft:
        draft = super().normalize(raw)
        if not draft.creators:  # 作者要从 pub 里拆，且要跟 publisher 对照，所以在这里做
            extra = raw.payload.get("extra")
            if isinstance(extra, Mapping):
                author = author_from_pub(extra.get("pub"), extra.get("publisher"))
                if author:
                    draft.creators = [author]
        return draft
