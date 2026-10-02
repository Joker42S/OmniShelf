"""本地 JSON 状态库：增量合并、变更追踪。

状态库是一份可读的 JSON，既是增量同步的依据，也是导入文件的取材处::

    {
      "schema_version": 1,
      "host": "https://picaapi.picacomic.com",
      "sort": "dd",
      "server_total": 4093,
      "total": 4093,
      "updated_at": "...",
      "items": [ {...}, ... ]
    }

和拷贝漫画那份不一样的一点：这里**没有「水位线」**。哔咔返回的是漫画自己的
ObjectId，前 4 字节是这本漫画的上传时间，跟你什么时候收藏它毫无关系
（实测把「``_id`` 时间戳是否单调」当排序键会把账号批量整理收藏夹留下的痕迹
误判成排序规则）。所以增量靠「按收藏日期倒序翻页 + 连续命中本地已有条目」判定边界，
本地条目的顺序就是扫描顺序（新收藏在前）。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set

from .errors import ConfigError
from .models import ComicItem, now_iso


class JsonStore:
    """一个 key 为漫画编号（``comic_id``）的 JSON 数据仓库。

    :param path: JSON 文件路径，不存在时视为空库。
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str) -> None:
        self.path = path
        self.items: Dict[str, ComicItem] = {}
        self.meta: Dict[str, Any] = {}
        self.loaded = False

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def load(self) -> "JsonStore":
        """载入磁盘上的 JSON；文件不存在时是空库，读坏了会明确报错。

        「坏了」不当成「空」处理：那样会把本地状态静默清零，下一次全量同步
        又把每一条都当成新收藏重导一遍，还得不到任何提示。
        """
        self.items = {}
        self.meta = {}
        self.loaded = True
        if not self.path or not os.path.isfile(self.path):
            return self

        try:
            with open(self.path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except ValueError as exc:
            raise ConfigError(
                "状态库 %s 读坏了（%s）：改名或删掉它再重新同步一次" % (self.path, exc)
            ) from exc
        if not isinstance(payload, dict):
            raise ConfigError("状态库 %s 顶层必须是对象" % self.path)

        for entry in payload.get("items") or []:
            if not isinstance(entry, dict) or not entry.get("comic_id"):
                continue
            item = ComicItem.from_dict(entry)
            self.items[item.comic_id] = item

        self.meta = {k: v for k, v in payload.items() if k != "items"}
        return self

    def to_payload(self, *, include_raw: bool = True) -> Dict[str, Any]:
        """组装将要写盘的完整 JSON 结构。

        顺序：未取消收藏的在前、保持扫描顺序（新收藏在前），已取消的沉到最后。
        """
        ordered = sorted(self.items.values(), key=lambda it: it.removed)
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
        """写盘（自动建目录）。先写临时文件再原子替换，中断不会留下半个 JSON。

        :return: 实际写入路径。
        """
        target = path or self.path
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = target + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_payload(include_raw=include_raw), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return target

    # ------------------------------------------------------------------
    # 变更
    # ------------------------------------------------------------------

    def get(self, comic_id: str) -> Optional[ComicItem]:
        return self.items.get(str(comic_id))

    def __contains__(self, comic_id: object) -> bool:
        return str(comic_id) in self.items

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[ComicItem]:
        return iter(self.items.values())

    def ids(self) -> Set[str]:
        """已记录的漫画编号集合。"""
        return set(self.items)

    def active(self) -> List[ComicItem]:
        """未取消收藏的条目（导入文件取材于此）。"""
        return [it for it in self.items.values() if not it.removed]

    def upsert(self, item: ComicItem) -> str:
        """插入或更新一条收藏。

        :return: ``"added"`` / ``"updated"`` / ``"unchanged"`` / ``"restored"``
            （``restored`` 表示之前标记为已取消收藏、这次又出现了）。
        """
        stamp = now_iso()
        existing = self.items.get(item.comic_id)

        if existing is None:
            item.first_seen = item.first_seen or stamp
            item.last_seen = stamp
            item.removed = False
            item.removed_at = None
            self.items[item.comic_id] = item
            return "added"

        changed = existing.merge_from(item)
        existing.last_seen = stamp

        if existing.removed:
            existing.removed = False
            existing.removed_at = None
            return "restored"
        return "updated" if changed else "unchanged"

    def mark_removed(self, seen_ids: Iterable[str], *, stamp: Optional[str] = None) -> int:
        """完整扫描收尾：把本次没在远端出现的条目标记为已取消收藏。

        只在**确实翻完了整份列表**时调用（见 :meth:`sync` 里的 ``complete``）——
        「这次没抓到」和「用户取消了收藏」是两件事，弄混会把整个收藏夹标记成已取消。

        :param seen_ids: 本次远端出现过的漫画编号。
        :return: 新标记的数量（本来就已经标记的不重复计）。
        """
        seen = {str(i) for i in seen_ids}
        stamp = stamp or now_iso()
        count = 0
        for comic_id, item in self.items.items():
            if comic_id in seen or item.removed:
                continue
            item.removed = True
            item.removed_at = stamp
            count += 1
        return count

    def reorder(self, order: Sequence[str]) -> None:
        """按本次扫描顺序重排条目：扫到的在前（新收藏在头部），其余保持原顺序。

        哔咔这边没有可用的「书架位次」编号（见模块文档），本地条目的排列顺序
        就是「离列表头部多远」，下次同步的连续命中判定和导入文件都靠它。
        """
        seen: Dict[str, ComicItem] = {}
        for comic_id in order:
            item = self.items.get(str(comic_id))
            if item is not None and str(comic_id) not in seen:
                seen[str(comic_id)] = item
        rest = {cid: it for cid, it in self.items.items() if cid not in seen}
        self.items = {}
        self.items.update(seen)
        self.items.update(rest)

    def server_total(self) -> Optional[int]:
        """上次记录的远端收藏总数（排序不变量校验用），没记录过返回 ``None``。"""
        value = self.meta.get("server_total")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
