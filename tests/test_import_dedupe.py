"""导入去重三分支 + 错误隔离 + import_logs。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.kinds import KINDS, KindDef, register
from omnishelf.services.import_ import import_file


def test_rule1_external_id_hit_updates(session: Session, import_records) -> None:
    first = import_records(
        [
            {
                "kind": "movie",
                "title": "Arrival",
                "year": 2016,
                "external_ids": [("tmdb", "329865")],
            }
        ]
    )
    assert (first.total, first.inserted, first.updated, first.skipped) == (1, 1, 0, 0)

    second = import_records(
        [
            {
                "kind": "movie",
                "title": "Arrival",
                "year": 2016,
                "rating": 9.0,
                "status": "done",
                "external_ids": [("tmdb", "329865")],
            }
        ]
    )
    assert (second.inserted, second.updated) == (0, 1)
    items = repo.list_items(session, kind="movie")
    assert len(items) == 1
    # 规则 1 的更新语义：外部数据权威，覆盖非空字段
    assert items[0].rating == 9.0
    assert items[0].status == "done"


def test_rule2_title_year_merges_and_keeps_data(session: Session, import_records) -> None:
    import_records(
        [
            {
                "kind": "movie",
                "title": "Arrival",
                "year": 2016,
                "rating": 8.5,
                "external_ids": [("tmdb", "329865")],
            }
        ]
    )
    result = import_records(
        [
            {
                "kind": "movie",
                "title": "Arrival",
                "year": 2016,
                "status": "wishlist",
                "external_ids": [("douban", "42")],
            }
        ]
    )
    assert (result.inserted, result.updated) == (0, 1)
    items = repo.list_items(session, kind="movie")
    assert len(items) == 1
    item = items[0]
    # 合并语义：只补空字段，不覆盖已有评分
    assert item.rating == 8.5
    assert item.status == "wishlist"
    sources = {link.source for link in repo.list_external_ids(session, item.id)}
    assert sources == {"tmdb", "douban"}


def test_rule2_year_mismatch_is_not_an_exact_match(session: Session, import_records) -> None:
    import_records([{"kind": "movie", "title": "Arrival", "year": 2016}])
    result = import_records([{"kind": "movie", "title": "Arrival", "year": 2017}])
    # 年份不同 -> 走模糊匹配（规则 3），不覆盖原 item
    assert result.inserted == 0
    assert result.pending == 1
    titles = [i.title for i in repo.list_items(session, kind="movie")]
    assert titles.count("Arrival") == 2


def test_rule3_fuzzy_match_never_auto_merges(session: Session, import_records) -> None:
    import_records([{"kind": "movie", "title": "Spirited Away", "year": 2001, "rating": 9.5}])
    result = import_records([{"kind": "movie", "title": "spirited-away ", "year": 2003}])

    assert result.updated == 0
    assert result.inserted == 0
    assert result.skipped == 1
    assert result.pending == 1
    assert result.warning_count >= 1

    items = repo.list_items(session, kind="movie")
    assert len(items) == 2
    original = next(i for i in items if i.year == 2001)
    assert original.rating == 9.5

    pending = repo.list_pending_merges(session)
    assert len(pending) == 1
    assert pending[0].item_id == original.id
    assert pending[0].candidate_id is not None
    assert pending[0].candidate_id != original.id
    assert "模糊" in (pending[0].reason or "")


def test_rule3_pending_is_idempotent_on_reimport(session: Session, import_records) -> None:
    import_records([{"kind": "movie", "title": "Spirited Away", "year": 2001}])
    first = import_records([{"kind": "movie", "title": "Spirited Away", "year": 2003}])
    assert first.pending == 1
    assert first.candidates == 1
    # 第二次导入：候选已经作为 item 存在（title+year 精确命中），因此走规则 2
    second = import_records([{"kind": "movie", "title": "Spirited Away", "year": 2003}])
    assert second.pending == 0
    assert second.inserted == 0
    assert second.updated + second.unchanged == 1
    assert repo.count_pending_merges(session) == 1
    assert len(repo.list_items(session, kind="movie")) == 2


def test_unchanged_records_are_not_counted_as_updates(session: Session, import_records) -> None:
    """命中既有 item 但没有任何变化 -> unchanged，不算 updated，也不动 updated_at。"""
    first = import_records([{"kind": "movie", "title": "Arrival", "year": 2016, "rating": 8.5}])
    assert first.inserted == 1
    item = repo.list_items(session, kind="movie")[0]
    updated_at = item.updated_at

    second = import_records([{"kind": "movie", "title": "Arrival", "year": 2016}])
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 1)
    session.refresh(item)
    assert item.updated_at == updated_at

    third = import_records([{"kind": "movie", "title": "Arrival", "year": 2016, "tags": ["x"]}])
    assert (third.updated, third.unchanged) == (1, 0)
    assert "更新 1" in third.summary()
    assert "未变化 0" in third.summary()
    assert "候选已落库 0" in third.summary()


def test_rule3_multiple_matches_create_multiple_pending(
    session: Session, make_item, import_records
) -> None:
    make_item(title="Spirited Away", year=2001)
    make_item(title="Spirited  Away!", year=2002)
    result = import_records([{"kind": "movie", "title": "spiritedaway", "year": 2003}])
    assert result.pending == 2
    assert repo.count_pending_merges(session) == 2
    assert len(repo.list_items(session, kind="movie")) == 3


def test_unknown_extra_field_warns_but_inserts(session: Session, import_records) -> None:
    result = import_records(
        [{"kind": "movie", "title": "Solaris", "year": 1972, "extra": {"not_declared": 1}}]
    )
    assert result.inserted == 1
    assert result.warning_count == 1
    assert "not_declared" in result.errors[0]["message"]
    item = repo.list_items(session, kind="movie")[0]
    assert item.extra["not_declared"] == 1


def test_extra_type_mismatch_rejects_record(session: Session, import_records) -> None:
    result = import_records(
        [
            {"kind": "movie", "title": "Good", "extra": {"runtime": 100}},
            {"kind": "movie", "title": "Bad", "extra": {"runtime": "很久"}},
        ]
    )
    assert result.inserted == 1
    assert result.skipped == 1
    assert result.error_count == 1
    assert [i.title for i in repo.list_items(session, kind="movie")] == ["Good"]


def test_unknown_kind_rejects_record(session: Session, import_records) -> None:
    result = import_records([{"kind": "nope", "title": "X"}])
    assert result.skipped == 1
    assert result.error_count == 1


def test_record_error_does_not_stop_batch(session: Session, import_records) -> None:
    result = import_records(
        [
            {"kind": "movie", "title": "One"},
            {"kind": "movie"},  # 缺 title
            {"kind": "movie", "title": "Two"},
            {"kind": "movie", "title": "Three", "status": "bogus"},
            {"kind": "movie", "title": "Four"},
        ]
    )
    assert result.total == 5
    assert result.inserted == 3
    assert result.skipped == 2
    assert result.error_count == 2


def test_import_log_is_written(session: Session, import_records) -> None:
    result = import_records(
        [
            {"kind": "movie", "title": "A"},  # 新增
            {"kind": "movie"},  # 缺 title -> 跳过
            {"kind": "movie", "title": "A", "tags": ["new"]},  # 命中并真有变化 -> 更新
            {"kind": "movie", "title": "A"},  # 命中但没有变化 -> unchanged
        ]
    )
    logs = repo.list_import_logs(session)
    assert len(logs) == 1
    log = logs[0]
    assert log.id == result.log_id
    assert log.source == "unit"
    assert log.total == 4
    assert log.inserted == 1
    assert log.updated == 1
    assert log.skipped == 1
    assert result.unchanged == 1
    assert log.started_at and log.finished_at
    assert isinstance(log.errors, list)
    assert log.errors and log.errors[0]["level"] == "error"


def test_tags_are_unioned_and_deduped(session: Session, import_records) -> None:
    import_records([{"kind": "movie", "title": "A", "tags": ["x", "y"]}])
    import_records([{"kind": "movie", "title": "A", "tags": ["y", "z"]}])
    item = repo.list_items(session, kind="movie")[0]
    assert repo.list_tags(session, item.id) == ["x", "y", "z"]


def test_import_file_with_fixtures(session: Session, movies_path: Path) -> None:
    result = import_file(session, importer_name="json_movies", path=movies_path)
    assert result.total == 8
    assert result.inserted == 5
    assert result.updated == 2
    assert result.skipped == 1
    assert result.pending == 1
    assert result.error_count == 0
    # 3 条有 tmdb_id
    tmdb_items = [
        item
        for item in repo.list_items(session, kind="movie")
        if any(link.source == "tmdb" for link in repo.list_external_ids(session, item.id))
    ]
    assert len(tmdb_items) == 3
    # 未声明的 extra 字段给出 warning
    assert any("letterboxd_uri" in w for w in result.warnings)


def test_import_movies_twice_is_stable(session: Session, movies_path: Path) -> None:
    import_file(session, importer_name="json_movies", path=movies_path)
    before = len(repo.list_items(session))
    second = import_file(session, importer_name="json_movies", path=movies_path)
    assert second.inserted == 0
    assert len(repo.list_items(session)) == before
    assert repo.count_pending_merges(session) == 1


def test_import_books_fixture(session: Session, books_path: Path) -> None:
    result = import_file(session, importer_name="json_books", path=books_path)
    assert result.total == 5
    assert result.inserted == 5
    assert result.error_count == 0
    assert repo.stats(session)["by_kind"] == {"book": 5}
    dune = next(i for i in repo.list_items(session) if i.title == "Dune")
    assert dune.creators == ["Frank Herbert"]
    assert dune.extra["pages"] == 412
    sources = {link.source for link in repo.list_external_ids(session, dune.id)}
    assert sources == {"douban", "isbn"}


def test_import_malformed_does_not_crash(session: Session, malformed_path: Path) -> None:
    result = import_file(session, importer_name="json_movies", path=malformed_path)
    assert result.total == 4
    assert result.inserted == 1
    assert result.skipped == 3
    assert result.error_count == 3
    items = repo.list_items(session, kind="movie")
    assert [i.title for i in items] == ["Ghost in the Shell"]
    assert items[0].year == 1995  # 数字字符串被宽松转换


def test_import_missing_file_records_fetch_error(session: Session, tmp_path: Path) -> None:
    result = import_file(session, importer_name="json_movies", path=tmp_path / "nope.json")
    assert result.total == 0
    assert result.error_count == 1
    assert "fetch 失败" in result.errors[0]["message"]


def test_unknown_importer_name(session: Session, movies_path: Path) -> None:
    with pytest.raises(LookupError):
        import_file(session, importer_name="nope", path=movies_path)


def test_import_file_with_kwargs_override(session: Session, tmp_path: Path, import_records) -> None:
    register(KindDef("boardgame", "桌游"))
    try:
        path = tmp_path / "games.json"
        path.write_text('[{"name": "Brass"}]', encoding="utf-8")
        result = import_file(
            session,
            importer_name="json_movies",
            path=path,
            kind="boardgame",
            mapping={"title": ["name"]},
            source="unit_games",
        )
        assert result.inserted == 1
        assert result.source == "unit_games"
        assert repo.list_items(session, kind="boardgame")[0].title == "Brass"
    finally:
        KINDS.pop("boardgame", None)


def test_rule1_reimport_without_changes_touches_nothing(session: Session, import_records) -> None:
    """带外部 ID 的记录重复导入：数据没变就不写库、不动 updated_at、算「未变化」。"""
    record = {
        "kind": "movie",
        "title": "Arrival",
        "year": 2016,
        "rating": 8.5,
        "tags": ["sci-fi"],
        "external_ids": [("tmdb", "329865")],
    }
    first = import_records([record])
    assert (first.inserted, first.updated, first.unchanged) == (1, 0, 0)
    item = repo.list_items(session, kind="movie")[0]
    created_at, updated_at = item.created_at, item.updated_at

    second = import_records([record])
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 1)
    session.refresh(item)
    assert item.updated_at == updated_at == created_at
    assert len(repo.list_items(session, kind="movie")) == 1

    # 真的改了才写
    third = import_records([{**record, "rating": 9.5}])
    assert (third.updated, third.unchanged) == (1, 0)
    session.refresh(item)
    assert item.rating == 9.5
    # 时间戳是秒精度，同一秒内可能相等；用「再导入一次就该是未变化」证明改动确实落库了
    fourth = import_records([{**record, "rating": 9.5}])
    assert (fourth.updated, fourth.unchanged) == (0, 1)
    session.refresh(item)
    assert item.updated_at >= updated_at
