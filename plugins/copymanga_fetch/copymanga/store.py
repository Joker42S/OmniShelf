"""本地 JSON 状态库：增量合并、变更追踪。

状态库是一份可读的 JSON，既是本地数据源，也是最终交付文件::

    {
      "schema_version": 2,
      "host": "https://copy4000.com",
      "user": {...},
      "created_at": "...",
      "updated_at": "...",
      "last_full_sync": "...",
      "last_incremental_sync": "...",
      "total": 392,
      "items": [ {...}, ... ]
    }
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

from .models import CollectItem, now_iso


class JsonStore:
    """一个 key 为收藏 ID 的 JSON 数据仓库。

    :param path: JSON 文件路径，不存在时视为空库。
    """

    SCHEMA_VERSION = 3

    def __init__(self, path: str) -> None:
        self.path = path
        self.items: Dict[str, CollectItem] = {}
        self.meta: Dict[str, Any] = {}
        self.loaded = False

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def load(self) -> "JsonStore":
        """载入磁盘上的 JSON；文件不存在或损坏时退化为空库。"""
        self.items = {}
        self.meta = {}
        self.loaded = True
        if not self.path or not os.path.isfile(self.path):
            return self

        with open(self.path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            return self

        for entry in payload.get("items") or []:
            if not isinstance(entry, dict) or not entry.get("collect_id"):
                continue
            item = CollectItem.from_dict(entry)
            self.items[item.collect_id] = item

        self.meta = {k: v for k, v in payload.items() if k != "items"}
        return self

    def to_payload(self, *, include_raw: bool = True) -> Dict[str, Any]:
        """组装将要写盘的完整 JSON 结构。"""
        # 排序：未取消收藏的在前，按书架位次（0 = 最新加入書架）；已取消的沉到最后
        ordered = sorted(
            self.items.values(),
            key=lambda it: (it.removed, it.shelf_position, it.name),
        )
        payload = dict(self.meta)
        payload.update(
            {
                "schema_version": self.SCHEMA_VERSION,
                "total": len(ordered),
                "active_total": sum(1 for it in ordered if not it.removed),
                "removed_total": sum(1 for it in ordered if it.removed),
                "items": [it.to_dict(include_raw=include_raw) for it in ordered],
            }
        )
        return payload

    def save(self, *, include_raw: bool = True, path: Optional[str] = None) -> str:
        """写盘（自动建目录）。返回实际写入路径。"""
        target = path or self.path
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(self.to_payload(include_raw=include_raw), fh, ensure_ascii=False, indent=2)
        return target

    # ------------------------------------------------------------------
    # 变更
    # ------------------------------------------------------------------

    def get(self, collect_id: str) -> Optional[CollectItem]:
        return self.items.get(str(collect_id))

    def __contains__(self, collect_id: object) -> bool:
        return str(collect_id) in self.items

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[CollectItem]:
        return iter(self.items.values())

    def ids(self) -> Set[str]:
        """已记录的收藏 ID 集合。"""
        return set(self.items)

    def active(self) -> List[CollectItem]:
        """未取消收藏的条目。"""
        return [it for it in self.items.values() if not it.removed]

    def upsert(self, item: CollectItem) -> str:
        """插入或更新一条收藏。

        :return: ``"added"`` / ``"updated"`` / ``"unchanged"`` / ``"restored"``
            （``restored`` 表示之前标记为已取消收藏、这次又出现了）。
        """
        stamp = now_iso()
        existing = self.items.get(item.collect_id)

        if existing is None:
            item.first_seen = item.first_seen or stamp
            item.last_seen = stamp
            item.removed = False
            item.removed_at = None
            self.items[item.collect_id] = item
            return "added"

        changed = existing.merge_from(item)
        existing.last_seen = stamp

        if existing.removed:
            existing.removed = False
            existing.removed_at = None
            return "restored"
        return "updated" if changed else "unchanged"

    def mark_removed(self, seen_ids: Iterable[str], *, stamp: Optional[str] = None) -> int:
        """全量同步收尾：把本次没在远端出现的条目标记为已取消收藏。

        :param seen_ids: 本次远端出现过的收藏 ID。
        :return: 新标记的数量（本来就已经标记的不重复计）。
        """
        seen = {str(i) for i in seen_ids}
        stamp = stamp or now_iso()
        count = 0
        for collect_id, item in self.items.items():
            if collect_id in seen or item.removed:
                continue
            item.removed = True
            item.removed_at = stamp
            count += 1
        return count

    # ------------------------------------------------------------------
    # 书架顺序（排序不变量）
    # ------------------------------------------------------------------

    @staticmethod
    def _shelf_key(item: CollectItem) -> tuple:
        """书架顺序的排序键：收藏 ID 降序，0 位是最新加入書架的。

        收藏 ID（接口字段 ``uuid``）是站点侧与「加到書架時間」严格同构的自增序列，
        实测全部条目上严格单调，因此可以直接当书架顺序用，比依赖接口返回顺序更稳。
        万一将来 ID 不再是纯数字，退化为字符串比较，仍然保持稳定排序。
        """
        raw = str(item.collect_id)
        try:
            return (0, -int(raw))
        except (TypeError, ValueError):
            return (1, raw)  # 回退：非数字 ID 排在数字之后，按字符串升序

    def reindex_shelf(self) -> int:
        """按收藏 ID 重算每条的书架位次（0 = 最新加入書架）。

        :return: 位次最大的一条的 ``shelf_position``（即条目数 - 1）。
        """
        ordered = sorted(self.items.values(), key=self._shelf_key)
        for position, item in enumerate(ordered):
            item.shelf_position = position
        return len(ordered) - 1

    def watermark(self, *, include_removed: bool = False) -> Optional[int]:
        """书架水位线：现存条目中最大的收藏 ID。

        增量同步的依据 —— 排序正确时，**新加入書架的收藏 ID 一定大于水位线**，
        所以只要翻到一页「全部 ID 都不超过水位线」就可以安全停止。

        :param include_removed: 为 ``False``（默认）时忽略已取消收藏的条目，
            避免「取消了最新的收藏」被误判成排序异常。
        """
        best: Optional[int] = None
        for item in self.items.values():
            if item.removed and not include_removed:
                continue
            try:
                value = int(item.collect_id)
            except (TypeError, ValueError):
                continue
            if best is None or value > best:
                best = value
        return best
