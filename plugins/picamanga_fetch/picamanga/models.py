"""数据模型：一条收藏、以及一次同步的报告。

字段命名分两套：状态库里用 snake_case（给人读），导入文件用来源原本的 camelCase
（内置 importer ``picamanga`` 认的那 19 个字段，见 :meth:`ComicItem.to_export_dict`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

#: 远端可能变化的字段，用于判断「这一条有没有更新」
MUTABLE_FIELDS: Tuple[str, ...] = (
    "title",
    "author",
    "categories",
    "tags",
    "likes",
    "total_likes",
    "total_views",
    "pages_count",
    "eps_count",
    "finished",
    "thumb_file_server",
    "thumb_path",
    "thumb_original_name",
    "released",
)

#: 内置 importer ``picamanga`` 认的字段集合（顺序即写出顺序）
EXPORT_FIELDS: Tuple[str, ...] = (
    "comicId",
    "title",
    "author",
    "thumbFileServer",
    "thumbPath",
    "thumbOriginalName",
    "categories",
    "groupId",
    "knownEpsCount",
    "knownLatestOrder",
    "remoteUpdatedAt",
    "finished",
    "checkEnabled",
    "hasNewChapter",
    "latestChapterTitle",
    "latestChapterOrder",
    "pinnedAt",
    "addedAt",
    "updatedAt",
)

#: 本工具只有一个收藏夹分组（哔咔的收藏接口不给分组）
DEFAULT_GROUP = {"id": "default", "name": "默认", "displayOrder": 0}


def now_iso() -> str:
    """当前时间（带本地时区偏移）的 ISO 字符串。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def iso_to_millis(value: Optional[str]) -> Optional[int]:
    """ISO 字符串 -> 毫秒时间戳（来源格式里的 ``addedAt``/``updatedAt`` 用毫秒）。"""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def object_id_date(comic_id: str) -> str:
    """从 MongoDB ObjectId 的前 4 字节取出**漫画自身的上传日期**（``YYYY-MM-DD``）。

    .. warning::
       这是漫画上架的时间，**不是**你收藏它的时间——收藏列表接口不返回收藏时间，
       收藏顺序只体现在 ``dd`` 排序的位置上。别拿它当「标记日期」。
    """
    try:
        timestamp = int(str(comic_id)[:8], 16)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


@dataclass
class ComicItem:
    """一条收藏夹里的漫画（已从接口原始结构规整过）。

    用 :meth:`from_api` 从响应构造，:meth:`to_dict` 写进状态库，
    :meth:`to_export_dict` 拍平成导入文件里的一条记录。
    """

    comic_id: str
    title: str = ""
    author: str = ""
    categories: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    #: ``likesCount`` 是「爱心」数，``totalLikes`` 是热度，两个都留
    likes: int = 0
    total_likes: int = 0
    total_views: int = 0
    pages_count: int = 0
    eps_count: int = 0
    finished: bool = False
    #: 漫画上架日期（由 ``comic_id`` 算出，见 :func:`object_id_date`）
    released: str = ""
    thumb_file_server: str = ""
    thumb_path: str = ""
    thumb_original_name: str = ""

    # —— 本地状态 ——
    first_seen: str = ""
    last_seen: str = ""
    removed: bool = False
    removed_at: Optional[str] = None

    raw: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------

    @classmethod
    def from_api(cls, row: Dict[str, Any]) -> "ComicItem":
        """把 ``/users/favourite`` 返回的一条漫画对象转成条目。

        ``thumb`` 是 ``{originalName, path, fileServer}`` 三个字段拼起来的封面信息，
        这里摊平成三个字段存着——本工具不下载任何图片，只把地址带着走。
        """
        comic_id = str(row.get("_id") or "")
        thumb = row.get("thumb") if isinstance(row.get("thumb"), dict) else {}
        stamp = now_iso()
        return cls(
            comic_id=comic_id,
            title=row.get("title") or "",
            author=row.get("author") or "",
            categories=[str(v) for v in (row.get("categories") or [])],
            tags=[str(v) for v in (row.get("tags") or [])],
            likes=int(row.get("likesCount") or 0),
            total_likes=int(row.get("totalLikes") or 0),
            total_views=int(row.get("totalViews") or 0),
            pages_count=int(row.get("pagesCount") or 0),
            eps_count=int(row.get("epsCount") or 0),
            finished=bool(row.get("finished", False)),
            released=object_id_date(comic_id),
            thumb_file_server=str(thumb.get("fileServer") or ""),
            thumb_path=str(thumb.get("path") or ""),
            thumb_original_name=str(thumb.get("originalName") or ""),
            first_seen=stamp,
            last_seen=stamp,
            raw=row,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComicItem":
        """从状态库的 JSON 条目还原对象（未知字段忽略，方便以后加字段）。"""
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}

        thumb = data.get("thumb")
        if isinstance(thumb, dict):
            payload.setdefault(
                "thumb_file_server", str(thumb.get("file_server") or "")
            )
            payload.setdefault("thumb_path", str(thumb.get("path") or ""))
            payload.setdefault(
                "thumb_original_name", str(thumb.get("original_name") or "")
            )
        return cls(**payload)

    # ------------------------------------------------------------------

    def to_dict(self, *, include_raw: bool = True) -> Dict[str, Any]:
        """序列化为写进状态库的 dict（snake_case，thumb 嵌套起来更好读）。"""
        data: Dict[str, Any] = {
            "comic_id": self.comic_id,
            "title": self.title,
            "author": self.author,
            "categories": list(self.categories),
            "tags": list(self.tags),
            "likes": self.likes,
            "total_likes": self.total_likes,
            "total_views": self.total_views,
            "pages_count": self.pages_count,
            "eps_count": self.eps_count,
            "finished": self.finished,
            "released": self.released,
            "thumb": {
                "file_server": self.thumb_file_server,
                "path": self.thumb_path,
                "original_name": self.thumb_original_name,
            },
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "removed": self.removed,
            "removed_at": self.removed_at,
        }
        if include_raw:
            data["raw"] = self.raw
        return data

    def cover_url(self) -> Optional[str]:
        """封面绝对地址（``fileServer`` + ``path`` 拼出来，缺任何一段就没有）。"""
        if not self.thumb_file_server or not self.thumb_path:
            return None
        return "%s/%s" % (
            self.thumb_file_server.rstrip("/"),
            self.thumb_path.lstrip("/"),
        )

    def to_export_dict(self) -> Dict[str, Any]:
        """拍平成内置 importer ``picamanga`` 认的那 19 个字段。

        收藏列表接口只给漫画本体的信息，**不给**「最新话 / 源站更新时间 / 是否有新话」
        这些要逐本再请求章节接口才知道的字段，所以这里一律留空（``None``）——
        importer 认得空值，会直接跳过不写。``addedAt``/``updatedAt`` 是本地记账：
        「第一次见到这条收藏」和「最近一次见到」的毫秒时间戳，**不是收藏时间**
        （接口不提供收藏时间）。
        """
        return {
            "comicId": self.comic_id,
            "title": self.title,
            "author": self.author,
            "thumbFileServer": self.thumb_file_server or None,
            "thumbPath": self.thumb_path or None,
            "thumbOriginalName": self.thumb_original_name or None,
            "categories": list(self.categories),
            "groupId": DEFAULT_GROUP["id"],
            "knownEpsCount": self.eps_count,
            "knownLatestOrder": None,
            "remoteUpdatedAt": None,
            "finished": self.finished,
            "checkEnabled": None,
            "hasNewChapter": None,
            "latestChapterTitle": None,
            "latestChapterOrder": None,
            "pinnedAt": None,
            "addedAt": iso_to_millis(self.first_seen),
            "updatedAt": iso_to_millis(self.last_seen),
        }

    # ------------------------------------------------------------------

    def signature(self) -> tuple:
        """可变字段的指纹，用来判断内容是否变化。"""
        return tuple(
            tuple(v) if isinstance(v, list) else v
            for v in (getattr(self, name) for name in MUTABLE_FIELDS)
        )

    def merge_from(self, other: "ComicItem") -> bool:
        """用远端最新数据刷新自己。

        :return: 内容是否发生变化。
        """
        changed = self.signature() != other.signature()
        for name in MUTABLE_FIELDS:
            setattr(self, name, getattr(other, name))
        self.raw = other.raw
        return changed


#: 状态扫描的停止原因
STOP_COMPLETE = "complete"
STOP_KNOWN_BOUNDARY = "known-boundary"
STOP_MAX_PAGES = "max-pages"

#: 一次扫描对远端确认到什么程度
VERIFY_BASELINE = "baseline"          # 冷启动，第一次把整份列表落下来
VERIFY_CHANGED_SCAN = "changed-scan"  # 增量：连续命中本地条目就停手
VERIFY_FULL_SCAN = "full-scan"        # 完整翻过整份列表


@dataclass
class SyncReport:
    """一次同步的结果摘要。"""

    mode: str = "incremental"
    host: str = ""
    sort: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    #: ``ok`` / ``partial``（有页没抓到但整体完成了）
    status: str = "ok"
    total_remote: int = 0
    total_local: int = 0
    fetched: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    restored: int = 0
    removed: int = 0
    pages: int = 0
    stop_reason: str = ""
    #: 见 ``VERIFY_*``：这次到底把远端确认到哪一步
    verification: str = ""
    #: 增量判定「后面都已同步」所用的连续命中数
    consecutive_known_stop: int = 0
    #: 是否确认了「新收藏在头部」这个前提
    sort_verified: bool = True
    #: 是否因为前提被打破而自动改跑全量
    fell_back_to_full: bool = False
    #: 扫描是否被 ``max_pages`` 截断
    truncated: bool = False
    store_path: str = ""
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "host": self.host,
            "sort": self.sort,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 2),
            "status": self.status,
            "counts": {
                "total_remote": self.total_remote,
                "total_local": self.total_local,
                "fetched": self.fetched,
                "added": self.added,
                "updated": self.updated,
                "unchanged": self.unchanged,
                "restored": self.restored,
                "removed": self.removed,
                "pages": self.pages,
            },
            "scan": {
                "verification": self.verification,
                "consecutive_known_stop": self.consecutive_known_stop,
                "truncated": self.truncated,
                "sort_verified": self.sort_verified,
                "fell_back_to_full": self.fell_back_to_full,
            },
            "stop_reason": self.stop_reason,
            "store_path": self.store_path,
            "errors": list(self.errors),
        }

    def __str__(self) -> str:
        # 只有「增量模式却没能确认排序」才是需要提醒的情况；
        # 全量模式本来就会翻完，用别的排序也无害。
        actionable = self.fell_back_to_full or (
            self.mode == "incremental" and not self.sort_verified
        )
        return (
            "[{mode}] 远端 {total_remote} / 本地 {total_local} | "
            "新增 {added} 更新 {updated} 未变 {unchanged} 恢复 {restored} 取消 {removed} | "
            "{pages} 页 / {duration_s:.1f}s ({stop}){warn}".format(
                mode=self.mode,
                total_remote=self.total_remote,
                total_local=self.total_local,
                added=self.added,
                updated=self.updated,
                unchanged=self.unchanged,
                restored=self.restored,
                removed=self.removed,
                pages=self.pages,
                duration_s=self.duration_s,
                stop=self.stop_reason,
                warn="  ⚠ 没确认住「新收藏在头部」，已降级全量" if actionable else "",
            )
        )
