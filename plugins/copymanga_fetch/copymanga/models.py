"""数据模型：一条收藏、以及一次同步的报告。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: 站点连载状态码 -> 显示文案
STATUS_LABEL: Dict[int, str] = {
    0: "連載中",
    1: "已完結",
    2: "短篇",
}

#: 远端可能变化的字段，用于判断「这一条有没有更新」
MUTABLE_FIELDS = (
    "name",
    "comic_uuid",
    "path_word",
    "url",
    "cover",
    "authors",
    "themes",
    "status",
    "status_label",
    "popular",
    "last_chapter_id",
    "last_chapter_name",
    "last_chapter_url",
    "last_browse_id",
    "last_browse_name",
    "comic_updated_at",
    "folder",
)


def now_iso() -> str:
    """当前时间（带本地时区偏移）的 ISO 字符串。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class CollectItem:
    """一条收藏记录（已从接口原始结构规整过）。

    用 :meth:`from_api` 从接口响应构造，用 :meth:`to_dict` 序列化。
    """

    collect_id: str
    name: str = ""
    comic_uuid: Optional[str] = None
    path_word: str = ""
    url: str = ""
    cover: str = ""
    authors: List[str] = field(default_factory=list)
    themes: List[str] = field(default_factory=list)
    status: Optional[int] = None
    status_label: str = ""
    popular: Optional[int] = None
    last_chapter_id: str = ""
    last_chapter_name: str = ""
    last_chapter_url: str = ""
    last_browse_id: str = ""
    last_browse_name: str = ""
    comic_updated_at: str = ""
    folder: Optional[str] = None
    free_type: int = 1
    kind: str = "comics"

    # —— 书架位次（本地按 collect_id 计算，0 = 最新加入書架）——
    #: 站点「加到書架」顺序的稳定位次。``collect_id`` 是与书架顺序严格同构的
    #: 自增序列，因此 0 = 最新添加，数字越大加入越早。
    shelf_position: int = 0

    # —— 本地状态 ——
    first_seen: str = ""
    last_seen: str = ""
    removed: bool = False
    removed_at: Optional[str] = None

    raw: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------

    @classmethod
    def from_api(
        cls,
        row: Dict[str, Any],
        *,
        host: str,
        free_type: int = 1,
        kind: str = "comics",
        shelf_position: int = 0,
    ) -> "CollectItem":
        """把 ``/api/v3/member/collect/*`` 返回的一条记录转成对象。"""
        comic = row.get("comic") or {}
        base = host.rstrip("/")
        path_word = comic.get("path_word") or ""
        status = comic.get("status")
        chapter_id = comic.get("last_chapter_id") or ""
        browse = row.get("last_browse") or {}

        return cls(
            collect_id=str(row.get("uuid")),
            name=comic.get("name") or "",
            comic_uuid=comic.get("uuid"),
            path_word=path_word,
            url="%s/comic/%s" % (base, path_word) if path_word else "",
            cover=comic.get("cover") or "",
            authors=[a.get("name") for a in (comic.get("author") or []) if a.get("name")],
            themes=[t.get("name") for t in (comic.get("theme") or []) if t.get("name")],
            status=status,
            status_label=STATUS_LABEL.get(status, "" if status is None else "未知(%s)" % status),
            popular=comic.get("popular"),
            last_chapter_id=chapter_id,
            last_chapter_name=comic.get("last_chapter_name") or "",
            last_chapter_url=(
                "%s/comic/%s/chapter/%s" % (base, path_word, chapter_id)
                if path_word and chapter_id
                else ""
            ),
            last_browse_id=browse.get("last_browse_id") or "",
            last_browse_name=browse.get("last_browse_name") or "",
            comic_updated_at=comic.get("datetime_updated") or "",
            folder=row.get("name"),
            free_type=free_type,
            kind=kind,
            shelf_position=shelf_position,
            first_seen=now_iso(),
            last_seen=now_iso(),
            raw=row,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CollectItem":
        """从状态库的 JSON 条目还原对象。

        JSON 里章节信息是嵌套的（读起来更顺），这里把它摊平回字段；
        未知字段直接忽略，方便以后加字段做向后兼容。
        """
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}

        chapter = data.get("last_chapter")
        if isinstance(chapter, dict):
            if not payload.get("last_chapter_id"):
                payload["last_chapter_id"] = chapter.get("id") or ""
            if not payload.get("last_chapter_name"):
                payload["last_chapter_name"] = chapter.get("name") or ""
            if not payload.get("last_chapter_url"):
                payload["last_chapter_url"] = chapter.get("url") or ""

        browse = data.get("last_browse")
        if isinstance(browse, dict):
            if not payload.get("last_browse_id"):
                payload["last_browse_id"] = browse.get("id") or ""
            if not payload.get("last_browse_name"):
                payload["last_browse_name"] = browse.get("name") or ""

        return cls(**payload)

    # ------------------------------------------------------------------

    def to_dict(self, *, include_raw: bool = True) -> Dict[str, Any]:
        """序列化为可写进 JSON 的 dict。"""
        data: Dict[str, Any] = {
            "collect_id": self.collect_id,
            "name": self.name,
            "comic_uuid": self.comic_uuid,
            "path_word": self.path_word,
            "url": self.url,
            "cover": self.cover,
            "authors": list(self.authors),
            "themes": list(self.themes),
            "status": self.status,
            "status_label": self.status_label,
            "popular": self.popular,
            "last_chapter": {
                "id": self.last_chapter_id,
                "name": self.last_chapter_name,
                "url": self.last_chapter_url,
            },
            "last_browse": {
                "id": self.last_browse_id,
                "name": self.last_browse_name,
            },
            "comic_updated_at": self.comic_updated_at,
            "folder": self.folder,
            "free_type": self.free_type,
            "kind": self.kind,
            "shelf_position": self.shelf_position,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "removed": self.removed,
            "removed_at": self.removed_at,
        }
        if include_raw:
            data["raw"] = self.raw
        return data

    def signature(self) -> tuple:
        """可变字段的指纹，用来判断内容是否变化。"""
        return tuple(
            tuple(v) if isinstance(v, list) else v for v in (getattr(self, f) for f in MUTABLE_FIELDS)
        )

    def merge_from(self, other: "CollectItem") -> bool:
        """用远端最新数据刷新自己。

        :return: 内容是否发生变化（``shelf_position`` 是本地算的，不计入）。
        """
        changed = self.signature() != other.signature()
        for name in MUTABLE_FIELDS:
            setattr(self, name, getattr(other, name))
        self.raw = other.raw
        return changed

    # ------------------------------------------------------------------

    def to_row(self, index: int, *, kind_label: str = "") -> Dict[str, Any]:
        """拍平成表格一行（CSV/Excel 用）。"""
        return {
            "序号": index,
            "书架序": self.shelf_position + 1,
            "收藏ID": self.collect_id,
            "漫画名称": self.name,
            "作者": "、".join(self.authors),
            "题材": "、".join(self.themes),
            "状态": self.status_label,
            "人气": self.popular if self.popular is not None else "",
            "最新话": self.last_chapter_name,
            "上次读到": self.last_browse_name,
            "更新时间": self.comic_updated_at,
            "链接": self.url,
            "最新话链接": self.last_chapter_url,
            "封面": self.cover,
            "类型": kind_label,
            "已取消收藏": "是" if self.removed else "",
        }


#: 表格列顺序
ROW_FIELDS = [
    "序号", "书架序", "漫画名称", "作者", "题材", "状态", "人气", "最新话",
    "上次读到", "更新时间", "收藏ID", "链接", "最新话链接", "封面",
    "类型", "已取消收藏",
]


@dataclass
class SyncReport:
    """一次同步的结果摘要。"""

    mode: str = "incremental"
    host: str = ""
    ordering: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    user: Dict[str, Any] = field(default_factory=dict)
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
    #: 本次开始前记录的书架水位线（现存条目中最大的收藏 ID）
    watermark: Optional[int] = None
    #: 是否验证了「按加到書架時間降序」这一不变量
    ordering_verified: bool = True
    #: 是否因为不变量被打破而自动降级成全量扫描
    fell_back_to_full: bool = False
    store_path: str = ""
    files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "host": self.host,
            "ordering": self.ordering,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 2),
            "user": self.user,
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
            "ordering_check": {
                "ordering": self.ordering,
                "watermark": self.watermark,
                "verified": self.ordering_verified,
                "fell_back_to_full": self.fell_back_to_full,
            },
            "stop_reason": self.stop_reason,
            "store_path": self.store_path,
            "files": self.files,
        }

    def __str__(self) -> str:
        # 只有「增量模式却没能确认排序」才是需要提醒的情况；
        # 全量模式下用别的排序是无害的，不必报警。
        actionable = self.fell_back_to_full or (
            self.mode == "incremental" and not self.ordering_verified
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
                warn="  ⚠ 排序校验失败，已降级全量" if actionable else "",
            )
        )
