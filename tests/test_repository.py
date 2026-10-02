"""repository：CRUD、过滤、软删除、时间戳、external_ids、tags、pending_merges。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.models import STATUSES


def test_new_id_is_ulid() -> None:
    item_id = repo.new_id()
    assert len(item_id) == 26
    assert item_id.isalnum()
    assert repo.new_id() != repo.new_id()


def test_utc_now_iso_is_utc_iso8601() -> None:
    value = repo.utc_now_iso()
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)
    assert value.endswith("+00:00")


def test_create_and_get_item(session: Session) -> None:
    item = repo.create_item(
        session,
        kind="movie",
        title="Blade Runner",
        year=1982,
        status="done",
        rating=9.0,
        extra={"director": "Ridley Scott"},
        tags=["sci-fi", "noir"],
        external_ids=[("tmdb", "78", "https://www.themoviedb.org/movie/78")],
    )
    fetched = repo.get_item(session, item.id)
    assert fetched is not None
    assert fetched.title == "Blade Runner"
    assert fetched.created_at == fetched.updated_at
    assert [link.source for link in repo.list_external_ids(session, item.id)] == ["tmdb"]
    assert repo.list_tags(session, item.id) == ["noir", "sci-fi"]


def test_create_item_rejects_unknown_kind(session: Session) -> None:
    with pytest.raises(KeyError):
        repo.create_item(session, kind="nope", title="x")


@pytest.mark.parametrize("status", STATUSES)
def test_status_validation(session: Session, status: str) -> None:
    item = repo.create_item(session, kind="movie", title=f"t-{status}", status=status)
    assert item.status == status
    with pytest.raises(ValueError):
        repo.create_item(session, kind="movie", title="bad", status="finished")


def test_rating_range(session: Session) -> None:
    assert repo.create_item(session, kind="movie", title="a", rating=0.0).rating == 0.0
    assert repo.create_item(session, kind="movie", title="b", rating=10).rating == 10.0
    with pytest.raises(ValueError):
        repo.create_item(session, kind="movie", title="c", rating=11)
    with pytest.raises(ValueError):
        repo.update_item(session, repo.create_item(session, kind="movie", title="d").id, rating=-1)


def test_update_item_touches_updated_at(session: Session, make_item) -> None:
    item = make_item(now="2020-01-01T00:00:00+00:00")
    assert item.updated_at == "2020-01-01T00:00:00+00:00"
    updated = repo.update_item(session, item.id, title="Blade Runner (Final Cut)", status="done")
    assert updated.title == "Blade Runner (Final Cut)"
    assert updated.status == "done"
    assert updated.updated_at > item.created_at
    assert updated.created_at == item.created_at


def test_update_item_rejects_unknown_field(session: Session, make_item) -> None:
    item = make_item()
    with pytest.raises(ValueError):
        repo.update_item(session, item.id, nope=1)


def test_require_item_missing(session: Session) -> None:
    with pytest.raises(repo.ItemNotFoundError):
        repo.require_item(session, "01HZZZZZZZZZZZZZZZZZZZZZZZ")


def test_list_filters(session: Session, make_item) -> None:
    make_item(kind="movie", title="Blade Runner", year=1982, status="done", tags=["sci-fi"])
    make_item(kind="movie", title="Arrival", year=2016, status="wishlist", tags=["sci-fi"])
    make_item(kind="book", title="Dune", year=1965, status="done", tags=["novel"])

    assert len(repo.list_items(session)) == 3
    assert [i.title for i in repo.list_items(session, kind="book")] == ["Dune"]
    assert len(repo.list_items(session, status="done")) == 2
    assert [i.title for i in repo.list_items(session, tag="novel")] == ["Dune"]
    assert [i.title for i in repo.list_items(session, kind="movie", tag="sci-fi")] != []
    assert len(repo.list_items(session, kind="movie", tag="sci-fi")) == 2
    assert [i.title for i in repo.list_items(session, title_prefix="bla")] == ["Blade Runner"]
    assert repo.list_items(session, title_prefix="zzz") == []


def test_list_prefix_escapes_like_wildcards(session: Session, make_item) -> None:
    make_item(title="100% Real")
    make_item(title="1000 Fake")
    assert [i.title for i in repo.list_items(session, title_prefix="100%")] == ["100% Real"]


def test_soft_delete_hides_from_list_but_keeps_row(session: Session, make_item) -> None:
    keep = make_item(title="Keep")
    gone = make_item(title="Gone")
    repo.soft_delete_item(session, gone.id)

    titles = [i.title for i in repo.list_items(session)]
    assert titles == ["Keep"]
    assert repo.get_item(session, gone.id) is None
    hidden = repo.get_item(session, gone.id, include_deleted=True)
    assert hidden is not None and hidden.deleted_at is not None
    assert repo.count_items(session) == 1
    assert repo.count_items(session, include_deleted=True) == 2
    assert len(repo.list_items(session, include_deleted=True)) == 2

    repo.restore_item(session, gone.id)
    assert {i.title for i in repo.list_items(session)} == {"Keep", "Gone"}
    assert keep.deleted_at is None


def test_external_id_upsert_and_reassign(session: Session, make_item) -> None:
    first = make_item(title="One")
    second = make_item(title="Two")
    assert repo.add_external_id(session, first.id, source="tmdb", external_id="78") is True
    assert repo.add_external_id(session, first.id, source="tmdb", external_id="78") is False
    assert repo.get_item_by_external_id(session, "tmdb", "78").id == first.id  # type: ignore[union-attr]

    repo.add_external_id(session, second.id, source="tmdb", external_id="78")
    assert repo.get_item_by_external_id(session, "tmdb", "78").id == second.id  # type: ignore[union-attr]

    repo.remove_external_id(session, "tmdb", "78")
    assert repo.get_item_by_external_id(session, "tmdb", "78") is None


def test_tags_helpers(session: Session, make_item) -> None:
    item = make_item(tags=["b", "a", "a"])
    assert repo.list_tags(session, item.id) == ["a", "b"]
    assert repo.add_tag(session, item.id, "c") is True
    assert repo.add_tag(session, item.id, "c") is False
    assert repo.add_tag(session, item.id, "  ") is False
    assert repo.remove_tag(session, item.id, "a") is True
    assert repo.remove_tag(session, item.id, "a") is False
    assert repo.set_tags(session, item.id, ["x", "y", "x"]) == ["x", "y"]
    assert repo.all_tags(session) == ["x", "y"]


def test_all_tags_excludes_deleted(session: Session, make_item) -> None:
    item = make_item(tags=["sci-fi"])
    assert repo.all_tags(session) == ["sci-fi"]
    repo.soft_delete_item(session, item.id)
    assert repo.all_tags(session) == []
    assert repo.all_tags(session, include_deleted=True) == ["sci-fi"]


def test_normalize_title() -> None:
    assert repo.normalize_title("Spirited Away") == "spiritedaway"
    assert repo.normalize_title("spirited-away ") == "spiritedaway"
    assert repo.normalize_title("《千与千寻》") == "千与千寻"
    assert repo.normalize_title("Your Name.") == "yourname"


def test_find_by_title_year(session: Session, make_item) -> None:
    make_item(title="Arrival", year=2016)
    assert repo.find_by_title_year(session, kind="movie", title="Arrival", year=2016) is not None
    assert repo.find_by_title_year(session, kind="movie", title="Arrival", year=2017) is None
    assert repo.find_by_title_year(session, kind="book", title="Arrival", year=2016) is None
    make_item(title="No Year")
    assert repo.find_by_title_year(session, kind="movie", title="No Year", year=None) is not None


def test_find_fuzzy_title_matches(session: Session, make_item) -> None:
    make_item(title="Spirited Away", year=2001)
    make_item(title="Spirited  Away!", year=2003)
    make_item(title="Arrival", year=2016)
    matches = repo.find_fuzzy_title_matches(session, kind="movie", title="spirited-away")
    assert len(matches) == 2
    assert repo.find_fuzzy_title_matches(session, kind="book", title="Spirited Away") == []
    assert repo.find_fuzzy_title_matches(session, kind="movie", title="   ") == []


def test_import_logs(session: Session) -> None:
    log = repo.create_import_log(session, source="unit")
    assert log.id is not None
    repo.finish_import_log(
        session,
        log.id,
        total=3,
        inserted=1,
        updated=1,
        skipped=1,
        errors=[{"level": "error", "record": 2, "message": "boom"}],
    )
    logs = repo.list_import_logs(session)
    assert len(logs) == 1
    assert logs[0].total == 3
    assert logs[0].errors and logs[0].errors[0]["message"] == "boom"
    assert logs[0].finished_at is not None


def test_pending_merge_dedup_and_delete(session: Session, make_item) -> None:
    item = make_item(title="A")
    candidate = make_item(title="B")
    first = repo.add_pending_merge(session, item_id=item.id, candidate_id=candidate.id, reason="x")
    second = repo.add_pending_merge(session, item_id=item.id, candidate_id=candidate.id, reason="y")
    assert first.id == second.id
    assert repo.count_pending_merges(session) == 1
    assert len(repo.list_pending_merges(session)) == 1
    assert first.id is not None
    repo.delete_pending_merge(session, first.id)
    assert repo.count_pending_merges(session) == 0
    assert repo.get_pending_merge(session, 999) is None
    with pytest.raises(repo.PendingMergeNotFoundError):
        repo.require_pending_merge(session, 999)


def test_stats(session: Session, make_item) -> None:
    make_item(kind="movie", title="A", status="done", tags=["x"])
    make_item(kind="movie", title="B", status="wishlist")
    book = make_item(kind="book", title="C", status="done")
    repo.soft_delete_item(session, book.id)

    result = repo.stats(session)
    assert result["total"] == 2
    assert result["deleted"] == 1
    assert result["by_kind"] == {"movie": 2}
    assert result["by_status"]["done"] == 1
    assert result["tags"] == 1
    assert result["pending_merges"] == 0
    assert result["with_cover"] == 0


STAMP = "2030-01-01T00:00:00+00:00"


def test_update_item_with_same_values_does_not_bump_updated_at(
    session: Session, make_item: Any
) -> None:
    """原样再保存一次不该产生新版本（data 仓库里就不会有噪音改动）。"""
    item = make_item(kind="movie", title="Same", rating=8.0)
    session.commit()
    before = item.updated_at

    again = repo.update_item(session, item.id, title="Same", rating=8.0)
    session.commit()
    assert again.updated_at == before

    # 真的改了才推进（updated_at 是秒精度，用显式 now 避免同一秒的假阴性）
    changed = repo.update_item(session, item.id, title="Same", rating=9.0, now=STAMP)
    session.commit()
    assert changed.updated_at == STAMP and changed.rating == 9.0


def test_set_tags_with_same_tags_does_not_bump_updated_at(
    session: Session, make_item: Any
) -> None:
    item = make_item(kind="movie", title="Tagged")
    repo.set_tags(session, item.id, ["科幻", "经典"])
    session.commit()
    before = repo.require_item(session, item.id).updated_at

    repo.set_tags(session, item.id, ["经典", "科幻", " 科幻 "])  # 同样的集合
    session.commit()
    assert repo.require_item(session, item.id).updated_at == before

    repo.set_tags(session, item.id, ["科幻"])
    session.commit()
    assert repo.list_tags(session, item.id) == ["科幻"]  # 真的改了标签
    assert repo.require_item(session, item.id).updated_at >= before
