"""待复核合并（pending_merges）的编排。

语义（与 ``services/import_`` 的规则 3 配套）：
  * ``item_id``      —— 命中的既有 item
  * ``candidate_id`` —— 新导入、被标记为候选的 item（已落库，未自动合并）
  * ``merge``        —— 把「被放弃的一侧」的 external_ids / tags / 空字段并入保留侧，
                        然后软删除被放弃的一侧，并清掉这条待复核记录。
  * ``skip``         —— 判定「不是同一条」，只删待复核记录；两个 item 都保留，
                        由用户自行决定后续处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from .. import repository as repo
from ..models import Item, PendingMerge


class MergeConflictError(ValueError):
    """merge 的 --keep 既不是 item_id 也不是 candidate_id。"""


@dataclass(slots=True)
class PendingMergeView:
    """给 CLI / Web 用的只读视图。"""

    pending: PendingMerge
    item: Item | None
    candidate: Item | None

    @property
    def id(self) -> int:
        return int(self.pending.id or 0)

    @property
    def reason(self) -> str:
        return self.pending.reason or ""

    @property
    def created_at(self) -> str:
        return self.pending.created_at or ""

    @property
    def dangling(self) -> bool:
        return self.item is None or self.candidate is None


@dataclass(slots=True)
class MergeOutcome:
    pending_id: int
    kept: Item
    dropped: Item
    moved_external_ids: int
    moved_tags: int
    moved_fields: list[str]


def list_pending(session: Session) -> list[PendingMergeView]:
    views: list[PendingMergeView] = []
    for row in repo.list_pending_merges(session):
        item = repo.get_item(session, row.item_id, include_deleted=True) if row.item_id else None
        candidate = (
            repo.get_item(session, row.candidate_id, include_deleted=True)
            if row.candidate_id
            else None
        )
        views.append(PendingMergeView(pending=row, item=item, candidate=candidate))
    return views


def get_pending_view(session: Session, pending_id: int) -> PendingMergeView:
    row = repo.require_pending_merge(session, pending_id)
    item = repo.get_item(session, row.item_id, include_deleted=True) if row.item_id else None
    candidate = (
        repo.get_item(session, row.candidate_id, include_deleted=True)
        if row.candidate_id
        else None
    )
    return PendingMergeView(pending=row, item=item, candidate=candidate)


def merge_item_into(
    session: Session,
    *,
    keep: Item,
    drop: Item,
    now: str | None = None,
) -> tuple[int, int, list[str]]:
    """把 ``drop`` 并入 ``keep``（external_ids/tags 取并集，空字段补全），软删 ``drop``。"""
    moved_external_ids = 0
    for link in repo.list_external_ids(session, drop.id):
        repo.add_external_id(
            session, keep.id, source=link.source, external_id=link.external_id, url=link.url
        )
        moved_external_ids += 1

    moved_tags = 0
    for tag in repo.list_tags(session, drop.id):
        if repo.add_tag(session, keep.id, tag):
            moved_tags += 1

    changes: dict[str, Any] = {}
    moved_fields: list[str] = []
    for name in ("subtitle", "year", "status", "rating", "notes", "cover_path"):
        if getattr(keep, name) is None and getattr(drop, name) is not None:
            changes[name] = getattr(drop, name)
            moved_fields.append(name)
    merged_extra = dict(keep.extra or {})
    for key, value in (drop.extra or {}).items():
        if merged_extra.get(key) is None:
            merged_extra[key] = value
            moved_fields.append(f"extra.{key}")
    if merged_extra != (keep.extra or {}):
        changes["extra"] = merged_extra
    if changes:
        repo.update_item(session, keep.id, now=now, **changes)

    repo.soft_delete_item(session, drop.id, now=now)
    return moved_external_ids, moved_tags, moved_fields


def merge(session: Session, pending_id: int, keep_item_id: str, *, now: str | None = None) -> MergeOutcome:
    """处理一条待复核记录：保留 ``keep_item_id``，另一侧被合并并软删除。"""
    view = get_pending_view(session, pending_id)
    sides = [view.pending.item_id, view.pending.candidate_id]
    if keep_item_id not in sides:
        raise MergeConflictError(
            f"--keep {keep_item_id} 不是这条待复核记录的任何一侧: {sides}"
        )
    drop_id = sides[0] if sides[1] == keep_item_id else sides[1]
    if drop_id is None:
        raise MergeConflictError(f"待复核记录 {pending_id} 缺少另一侧 id，无法合并")

    keep = repo.require_item(session, keep_item_id, include_deleted=True)
    drop = repo.require_item(session, drop_id, include_deleted=True)
    moved_external_ids, moved_tags, moved_fields = merge_item_into(
        session, keep=keep, drop=drop, now=now
    )
    if keep.deleted_at is not None:
        repo.restore_item(session, keep.id, now=now)

    # 清掉这条以及所有涉及同一对 item 的待复核记录，避免悬挂引用。
    for row in repo.list_pending_merges(session):
        pair = {row.item_id, row.candidate_id}
        if row.id == pending_id or pair == {keep.id, drop.id}:
            repo.delete_pending_merge(session, int(row.id or 0))

    return MergeOutcome(
        pending_id=pending_id,
        kept=keep,
        dropped=drop,
        moved_external_ids=moved_external_ids,
        moved_tags=moved_tags,
        moved_fields=moved_fields,
    )


def skip(session: Session, pending_id: int) -> PendingMergeView:
    """判定「不是同一条」：只删除待复核记录，两个 item 都保留。"""
    view = get_pending_view(session, pending_id)
    repo.delete_pending_merge(session, pending_id)
    return view
