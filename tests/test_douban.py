"""豆瓣三个来源（看过 / 读过 / 玩过）的 importer。离线；fixture 为合成数据。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.importers.base import DictRawRecord
from omnishelf.importers.douban import (
    DoubanBooksImporter,
    DoubanGamesImporter,
    DoubanMoviesImporter,
    author_from_pub,
    douban_status,
    platforms,
    release_date,
    runtime_minutes,
    segments,
    to_int,
)
from omnishelf.importers.registry import load_builtin_importers, names
from omnishelf.services.import_ import import_file

#: 真实导出的字段集合（2026-10 实测：423 游戏 / 666 影视 / 347 图书）
REAL_ITEM_KEYS = {
    "subject_id", "title", "year", "rating", "stars", "marked_date", "url", "cover",
    "extra", "media", "status", "page", "first_seen", "last_seen",
}
REAL_TOP_KEYS = {
    "media", "media_cn", "status", "status_cn", "user", "list_url", "fetched_at",
    "total_pages", "total_items_reported", "pages_fetched", "page_range", "incremental", "items",
}

FIXTURES = {
    "douban_games": ("game", DoubanGamesImporter, "douban_game", "game"),
    "douban_movies": ("movie", DoubanMoviesImporter, "douban_movie", "movie"),
    "douban_books": ("book", DoubanBooksImporter, "douban_book", "book"),
}


def _fixture(fixtures_dir: Path, name: str) -> Path:
    return fixtures_dir / f"{name}.json"


def _record(**payload: Any) -> DictRawRecord:
    base: dict[str, Any] = {
        "subject_id": "90000001",
        "title": "T",
        "year": "2020",
        "rating": 8,
        "stars": 4,
        "marked_date": "2026-01-01",
        "url": "https://example.invalid/movie/90000001/",
        "cover": "https://img.invalid/x.jpg",
        "extra": {},
        "media": "movie",
        "status": "collect",
    }
    base.update(payload)
    return DictRawRecord(source="douban_movie", payload=base)


def test_importers_are_registered() -> None:
    load_builtin_importers()
    assert {"douban_games", "douban_movies", "douban_books"} <= set(names())


@pytest.mark.parametrize("name", list(FIXTURES))
def test_fixture_matches_real_schema(fixtures_dir: Path, name: str) -> None:
    payload = json.loads(_fixture(fixtures_dir, name).read_text(encoding="utf-8"))
    _, _, _, media = FIXTURES[name]
    assert set(payload) == REAL_TOP_KEYS
    assert payload["media"] == media and payload["status"] == "collect"
    assert payload["total_items_reported"] == len(payload["items"])
    for row in payload["items"]:
        assert set(row) == REAL_ITEM_KEYS, "fixture 必须与真实导出的字段集合一致"
        assert isinstance(row["extra"], dict) and row["media"] == media
        assert row["rating"] in (None, 2, 4, 6, 8, 10)  # 豆瓣 1-5 星 ×2
        assert row["status"] == "collect"


@pytest.mark.parametrize("name", list(FIXTURES))
def test_import_fixture(fixtures_dir: Path, session: Session, name: str) -> None:
    _, _, source, kind = FIXTURES[name]
    result = import_file(session, importer_name=name, path=_fixture(fixtures_dir, name))
    assert (result.total, result.inserted, result.error_count, result.warning_count) == (5, 5, 0, 0)
    items = repo.list_items(session, kind=kind)
    assert len(items) == 5
    for item in items:
        links = repo.list_external_ids(session, item.id)
        assert [link.source for link in links] == [source]
        assert link_has_url(session, item.id, source)
        assert item.status == "done"  # collect -> done
        assert item.extra["cover_url"].startswith("https://")
        assert item.extra["marked_date"]


def link_has_url(session: Session, item_id: str, source: str) -> bool:
    return all(link.url for link in repo.list_external_ids(session, item_id) if link.source == source)


# ------------------------------------------------------------------ 游戏


def test_games_platform_filter_and_release_date(
    fixtures_dir: Path, session: Session
) -> None:
    import_file(session, importer_name="douban_games", path=_fixture(fixtures_dir, "douban_games"))
    items = {item.title: item for item in repo.list_items(session, kind="game")}

    one = items["示例游戏 一"]
    assert one.extra["platform"] == ["PC", "PS4"]  # 中文类型名（动作/角色扮演）被丢掉
    assert one.extra["info"].endswith("2013-10-28")  # 原串保留
    assert one.extra["release_date"] == "2013-10-28"
    assert one.rating == 10.0 and one.status == "done"
    assert one.creators == []  # 豆瓣这份导出没有开发商信息，不猜

    # 只有中文 token 时保留原样（宁可不干净也别丢数据）
    assert items["样例：平台全中文"].extra["platform"] == ["动作", "冒险"]
    # 没有年份的条目
    assert items["示例游戏 三"].year is None
    # 未评分
    assert items["示例游戏 二"].rating is None


# ------------------------------------------------------------------ 影视


def test_movies_runtime_intro_and_no_creator(fixtures_dir: Path, session: Session) -> None:
    import_file(session, importer_name="douban_movies", path=_fixture(fixtures_dir, "douban_movies"))
    items = {item.title: item for item in repo.list_items(session, kind="movie")}

    jia = items["示例影片 甲"]
    assert jia.extra["runtime"] == 98
    assert jia.extra["language"] == "汉语普通话"
    assert jia.extra["playable"] is True
    assert "导演甲" in jia.extra["intro"]  # 原串保留，没拆
    assert jia.creators == []  # 无标签的混合串 -> 不猜创作者
    assert jia.year is None  # 影视导出的 year 全是空的

    assert items["示例影片 乙"].extra["runtime"] == 45
    assert items["示例影片 乙"].extra["playable"] is False
    assert "runtime" not in items["示例影片 丙（无时长）"].extra  # 没有 N分钟 段
    assert items["示例影片 丙（无时长）"].rating is None


# ------------------------------------------------------------------ 图书


def test_books_creator_from_pub(fixtures_dir: Path, session: Session) -> None:
    import_file(session, importer_name="douban_books", path=_fixture(fixtures_dir, "douban_books"))
    items = {item.title: item for item in repo.list_items(session, kind="book")}

    assert items["示例图书 甲"].creators == ["作者甲"]
    assert items["示例图书 乙"].creators == ["作者乙、作者丙"]  # 一个来源字段里的顿号原样保留
    assert items["示例图书 丙（首段即出版社）"].creators == []  # 首段是出版社就不当作者
    assert items["示例图书 丁（无出版社）"].creators == ["作者丁"]
    assert items["示例图书 甲"].extra["publisher"] == "示例出版社"
    assert items["示例图书 甲"].extra["pub"].startswith("作者甲 /")
    assert items["示例图书 甲"].year == 2024


# ------------------------------------------------------- 工具函数 / 引擎


def test_helpers() -> None:
    assert segments("a / b // c ") == ["a", "b", "c"]
    assert segments(None) == []
    assert to_int("2013") == 2013 and to_int("") is None and to_int("x") is None
    assert douban_status("collect") == "done"
    assert douban_status("WISH") == "wishlist"
    assert douban_status("whatever") is None  # 未知不猜

    assert runtime_minutes("a / 98分钟 / 剧情") == 98
    assert runtime_minutes("a / 剧情") is None
    assert runtime_minutes("单集45分钟") is None  # 形态不符就不取
    assert runtime_minutes("a / 45分钟 / 60分钟") == 60  # 取最后一段（正常数据里只有一段）

    assert release_date("PC / 动作 / 2013-10-28") == "2013-10-28"
    assert release_date("PC / 动作") is None

    assert platforms(["PC", "动作"]) == ["PC"]
    assert platforms(["动作", "冒险"]) == ["动作", "冒险"]  # 兜底保留
    assert platforms("PC / 动作") == ["PC"]
    assert platforms(None) is None

    assert author_from_pub("作者 / 译者 / 社 / 2024-1", "社") == "作者"
    assert author_from_pub("社 / 2024-1", "社") is None
    assert author_from_pub("", "社") is None
    assert author_from_pub("作者", "") == "作者"


def test_rating_is_already_0_to_10() -> None:
    """豆瓣导出里的 rating 就是 0-10（1-5 星 ×2），别再乘一次。"""
    importer = DoubanMoviesImporter(Path("unused.json"))
    for stars, rating in ((5, 10), (4, 8), (3, 6), (2, 4), (1, 2)):
        draft = importer.normalize(_record(stars=stars, rating=rating))
        assert draft.rating == float(rating)
    draft = importer.normalize(_record(stars=None, rating=None))
    assert draft.rating is None  # 未评分


def test_year_and_marked_date() -> None:
    importer = DoubanBooksImporter(Path("unused.json"))
    draft = importer.normalize(_record(year="2024", marked_date="2025-08-07", extra={}))
    assert draft.year == 2024 and draft.extra["marked_date"] == "2025-08-07"
    assert importer.normalize(_record(year="", extra={})).year is None


def test_external_id_and_url() -> None:
    draft = DoubanMoviesImporter(Path("unused.json")).normalize(_record())
    assert [(link.source, link.external_id) for link in draft.external_ids] == [
        ("douban_movie", "90000001")
    ]
    assert draft.external_ids[0].url == "https://example.invalid/movie/90000001/"


def test_media_mismatch_is_rejected(fixtures_dir: Path, session: Session) -> None:
    """把电影列表喂给游戏 importer：抓取阶段报错，不写脏数据。"""
    result = import_file(
        session, importer_name="douban_games", path=_fixture(fixtures_dir, "douban_movies")
    )
    assert result.total == 0 and result.inserted == 0
    assert result.error_count == 1
    assert "这个文件是 movie 列表" in result.errors[0]["message"]
    assert repo.list_items(session, kind="game") == []


@pytest.mark.parametrize("name", list(FIXTURES))
def test_reimport_is_idempotent(fixtures_dir: Path, session: Session, name: str) -> None:
    path = _fixture(fixtures_dir, name)
    import_file(session, importer_name=name, path=path)
    again = import_file(session, importer_name=name, path=path)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 5)


def test_three_sources_use_separate_sources(session: Session, fixtures_dir: Path) -> None:
    """三种媒体的 subject_id 是各自编号空间：source 必须分开，避免撞号互相覆盖。"""
    for name, (_, cls, source, _) in FIXTURES.items():
        assert cls.source == source
    sources = {cls.source for _, cls, _, _ in FIXTURES.values()}
    assert len(sources) == 3
