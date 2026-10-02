"""pending_merges 的 merge / skip 流程。"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.services import review as review_service


def _make_pair(session: Session, make_item):
    """造一对模糊匹配的 item + 一条待复核记录。"""
    existing = make_item(kind="movie", title="Spirited Away", year=2001, rating=9.5, tags=["ghibli"])
    candidate = make_item(
        kind="movie",
        title="Spirited Away",
        year=2003,
        status="wishlist",
        tags=["ghibli", "new"],
        external_ids=[("tmdb", "129")],
        extra={"director": "Hayao Miyazaki"},
    )
    pending = repo.add_pending_merge(
        session, item_id=existing.id, candidate_id=candidate.id, reason="模糊标题匹配"
    )
    return existing, candidate, pending


def test_list_pending_view(session: Session, make_item) -> None:
    existing, candidate, pending = _make_pair(session, make_item)
    views = review_service.list_pending(session)
    assert len(views) == 1
    view = views[0]
    assert view.id == pending.id
    assert view.item is not None and view.item.id == existing.id
    assert view.candidate is not None and view.candidate.id == candidate.id
    assert view.reason == "模糊标题匹配"
    assert view.dangling is False
    assert view.created_at


def test_merge_keep_existing(session: Session, make_item) -> None:
    existing, candidate, pending = _make_pair(session, make_item)
    outcome = review_service.merge(session, pending.id, existing.id)

    assert outcome.kept.id == existing.id
    assert outcome.dropped.id == candidate.id
    assert outcome.moved_external_ids == 1
    assert outcome.moved_tags == 1  # "new" 被带过来，"ghibli" 已存在
    assert "status" in outcome.moved_fields

    kept = repo.require_item(session, existing.id)
    assert kept.status == "wishlist"  # 只补空字段
    assert kept.rating == 9.5  # 原有评分不被覆盖
    assert sorted(repo.list_tags(session, kept.id)) == ["ghibli", "new"]
    assert [link.external_id for link in repo.list_external_ids(session, kept.id)] == ["129"]

    # 被放弃的一侧被软删除，但行还在
    dropped = repo.get_item(session, candidate.id)
    assert dropped is None
    assert repo.get_item(session, candidate.id, include_deleted=True) is not None
    # 待复核记录被清掉
    assert repo.count_pending_merges(session) == 0


def test_merge_keep_candidate(session: Session, make_item) -> None:
    existing, candidate, pending = _make_pair(session, make_item)
    outcome = review_service.merge(session, pending.id, candidate.id)

    assert outcome.kept.id == candidate.id
    assert outcome.dropped.id == existing.id
    kept = repo.get_item(session, candidate.id)
    assert kept is not None
    assert kept.rating == 9.5  # 从 existing 补过来的
    assert kept.deleted_at is None
    assert repo.get_item(session, existing.id) is None
    assert repo.count_pending_merges(session) == 0


def test_merge_clears_sibling_pending_rows(session: Session, make_item) -> None:
    existing, candidate, first = _make_pair(session, make_item)
    repo.add_pending_merge(
        session, item_id=candidate.id, candidate_id=existing.id, reason="反向重复"
    )
    assert repo.count_pending_merges(session) == 2

    review_service.merge(session, first.id, existing.id)
    assert repo.count_pending_merges(session) == 0  # 同一对 item 的记录一起清掉


def test_merge_rejects_keep_not_in_pair(session: Session, make_item) -> None:
    _, _, pending = _make_pair(session, make_item)
    outsider = make_item(title="Arrival", year=2016)
    with pytest.raises(review_service.MergeConflictError):
        review_service.merge(session, pending.id, outsider.id)


def test_merge_unknown_pending(session: Session) -> None:
    with pytest.raises(repo.PendingMergeNotFoundError):
        review_service.merge(session, 4242, "whatever")


def test_skip_keeps_both_items(session: Session, make_item) -> None:
    existing, candidate, pending = _make_pair(session, make_item)
    view = review_service.skip(session, pending.id)

    assert view.id == pending.id
    assert repo.count_pending_merges(session) == 0
    assert repo.get_item(session, existing.id) is not None
    assert repo.get_item(session, candidate.id) is not None
    assert len(repo.list_items(session, kind="movie")) == 2


def test_skip_unknown_pending(session: Session) -> None:
    with pytest.raises(repo.PendingMergeNotFoundError):
        review_service.skip(session, 4242)


def test_dangling_pending_view(session: Session, make_item) -> None:
    existing, candidate, pending = _make_pair(session, make_item)
    session.delete(repo.require_item(session, candidate.id, include_deleted=True))
    session.flush()
    view = review_service.get_pending_view(session, pending.id)
    assert view.dangling is True


def test_full_import_to_review_flow(session: Session, movies_path) -> None:
    """导入 fixtures -> 复核 -> 合并，端到端走一遍。"""
    from omnishelf.services.import_ import import_file

    import_file(session, importer_name="json_movies", path=movies_path)
    views = review_service.list_pending(session)
    assert len(views) == 1
    view = views[0]
    assert view.item is not None and view.candidate is not None
    assert view.item.title == "Spirited Away"
    assert view.item.year == 2001
    assert view.candidate.year == 2003

    review_service.merge(session, view.id, view.item.id)
    assert repo.count_pending_merges(session) == 0
    assert repo.count_items(session, include_deleted=True) == len(repo.list_items(session)) + 1
