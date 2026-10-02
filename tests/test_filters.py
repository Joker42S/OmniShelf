"""筛选解析服务 + repository 的筛选/排序（列表页三来源共同决定结果的基础）。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import text
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.services.filters import (
    DEFAULT_DESCENDING,
    DEFAULT_SORT,
    FilterError,
    ListFilters,
    default_descending,
    parse_filters,
    utc_bounds_for_local_date,
)

# --------------------------------------------------------------------- 解析/校验


def test_parse_trims_and_dedups() -> None:
    filters = parse_filters(
        kind=" manga ",
        search="  星屑  ",
        creator=" 张三 ",
        tags_include=["科幻", " 科幻 ", "", "经典"],
        tags_exclude="福利",
        rating_min="8",
        rating_max=9.5,
    )
    assert (filters.kind, filters.search, filters.creator) == ("manga", "星屑", "张三")
    assert filters.tags_include == ("科幻", "经典")  # 去重、保持顺序
    assert filters.tags_exclude == ("福利",)
    assert (filters.rating_min, filters.rating_max) == (8.0, 9.5)


def test_parse_blank_is_none() -> None:
    filters = parse_filters(kind="", search="   ", tags_include=[], rating_min="")
    assert filters.kind is None and filters.search is None
    assert filters.rating_min is None and filters.tags_include == ()
    assert not filters.is_filtered


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rating_min": "abc"},
        {"created_from": "2026-13-01"},
        {"created_to": "not-a-date"},
        {"sort": "nope"},
        {"dir": "sideways"},
        {"rating_min": "9", "rating_max": "1"},
        {"created_from": "2026-10-05", "created_to": "2026-10-01"},
    ],
)
def test_parse_rejects_bad_input(kwargs: dict[str, Any]) -> None:
    with pytest.raises(FilterError):
        parse_filters(**kwargs)


def test_local_date_becomes_utc_bounds() -> None:
    """用户输入的是本地日期，库里存 UTC：边界要按本地时区算。"""
    start = utc_bounds_for_local_date("2026-10-01")
    end = utc_bounds_for_local_date("2026-10-01", end=True)
    local_start = datetime.fromisoformat(start).astimezone()
    local_end = datetime.fromisoformat(end).astimezone()
    assert (local_start.hour, local_start.minute, local_start.second) == (0, 0, 0)
    assert (local_end.hour, local_end.minute, local_end.second) == (23, 59, 59)
    assert local_start.date() == local_end.date() == datetime(2026, 10, 1).date()
    assert start < end


def test_parse_keeps_date_text_for_the_form() -> None:
    filters = parse_filters(created_from="2026-10-01", created_to="2026-10-03")
    assert (filters.created_from, filters.created_to) == ("2026-10-01", "2026-10-03")
    kwargs = filters.repo_kwargs()
    assert kwargs["created_from"] == utc_bounds_for_local_date("2026-10-01")
    assert kwargs["created_to"] == utc_bounds_for_local_date("2026-10-03", end=True)


def test_sort_defaults_and_direction() -> None:
    assert parse_filters().sort == DEFAULT_SORT
    assert parse_filters().descending is DEFAULT_DESCENDING
    assert parse_filters(sort="title").descending is False  # 标题默认 A→Z
    assert parse_filters(sort="title", dir="desc").descending is True
    assert parse_filters(sort="rating", dir="asc").descending is False
    assert parse_filters(sort="rating", descending=True).descending is True
    assert default_descending("year") is True and default_descending("title") is False


def test_flags_basic_search_advanced() -> None:
    assert parse_filters(kind="movie").has_basic
    assert not parse_filters(kind="movie").has_advanced
    assert parse_filters(search="x").is_filtered and not parse_filters(search="x").has_advanced
    assert parse_filters(rating_min=1).has_advanced
    assert parse_filters(tags_exclude=["x"]).has_advanced
    assert not parse_filters().is_filtered


def test_to_query_round_trips() -> None:
    """页面上的链接（分页/排序/去掉某个条件）必须能完整还原筛选条件。"""
    original = parse_filters(
        kind="manga",
        status="doing",
        tag="科幻",
        title_prefix="星",
        search="屑",
        creator="张三",
        rating_min=7.5,
        rating_max=9,
        created_from="2026-10-01",
        created_to="2026-10-03",
        tags_include=["经典", "短篇"],
        tags_exclude=["福利"],
        sort="rating",
        dir="asc",
    )
    query = original.to_query(page=3, per_page=25)
    assert query.startswith("/?")
    params = parse_qs(urlparse(query).query)
    again = parse_filters(
        kind=params.get("kind", [None])[0],
        status=params.get("status", [None])[0],
        tag=params.get("tag", [None])[0],
        title_prefix=params.get("title_prefix", [None])[0],
        search=params.get("search", [None])[0],
        creator=params.get("creator", [None])[0],
        rating_min=params.get("rating_min", [None])[0],
        rating_max=params.get("rating_max", [None])[0],
        created_from=params.get("created_from", [None])[0],
        created_to=params.get("created_to", [None])[0],
        tags_include=params.get("tags_include", []),
        tags_exclude=params.get("tags_exclude", []),
        sort=params.get("sort", [None])[0],
        dir=params.get("dir", [None])[0],
    )
    assert again == original
    assert params["page"] == ["3"] and params["per_page"] == ["25"]


def test_to_query_default_is_root() -> None:
    assert parse_filters().to_query() == "/"


def test_sort_links_toggle_current_field() -> None:
    links = {link["name"]: link for link in parse_filters(sort="title", dir="asc").sort_links()}
    assert links["title"]["active"] and links["title"]["indicator"] == "↑"
    assert "dir=desc" in links["title"]["url"]  # 再点一次反向
    assert links["rating"]["active"] is False
    assert "dir=desc" in links["rating"]["url"]  # 其它列用各自的默认方向
    assert "sort=year" in links["year"]["url"] and "dir=desc" in links["year"]["url"]
    # 默认状态（created_at desc）的链接就是干净的 /
    assert links["created_at"]["url"] == "/"


def test_active_filters_removal_keeps_the_rest() -> None:
    filters = parse_filters(kind="manga", search="屑", tags_include=["经典", "短篇"])
    chips = {chip["label"]: chip["url"] for chip in filters.active_filters()}
    assert set(chips) == {"类型 manga", "搜索 “屑”", "含 tag 经典", "含 tag 短篇"}
    removed = parse_qs(urlparse(chips["含 tag 经典"]).query)
    assert removed["tags_include"] == ["短篇"]
    assert removed["kind"] == ["manga"] and removed["search"] == ["屑"]
    assert parse_qs(urlparse(chips["类型 manga"]).query).get("kind") is None


# ------------------------------------------------------------------ repository


@pytest.fixture()
def library(session: Session, make_item: Any) -> dict[str, str]:
    """一小批已知数据：评分/年份/创作者/标签/入库时间都不相同。"""
    alpha = make_item(kind="movie", title="Alpha 100%", year=1999, rating=9.5, creators=["张三"])
    beta = make_item(kind="movie", title="beta_X", year=2010, rating=4.0, creators=["李四"])
    gamma = make_item(kind="movie", title="Gamma", year=None, rating=None, creators=["张三", "王五"])
    delta = make_item(kind="book", title="Delta", year=2020, rating=7.5)
    repo.set_tags(session, alpha.id, ["科幻", "经典"])
    repo.set_tags(session, beta.id, ["动作"])
    repo.set_tags(session, gamma.id, ["科幻"])
    # created_at 由 repository 写死为「现在」，这里直接改库来测排序/区间
    stamps = {
        alpha.id: "2026-10-01T00:00:00+00:00",
        beta.id: "2026-10-02T00:00:00+00:00",
        gamma.id: "2026-10-03T00:00:00+00:00",
        delta.id: "2026-10-04T00:00:00+00:00",
    }
    for item_id, stamp in stamps.items():
        session.execute(
            text("update items set created_at = :ts where id = :id").bindparams(
                ts=stamp, id=item_id
            )
        )
    session.commit()
    return {"alpha": alpha.id, "beta": beta.id, "gamma": gamma.id, "delta": delta.id}


def titles(session: Session, **kwargs: Any) -> list[str]:
    return [item.title for item in repo.list_items(session, **kwargs)]


def test_search_matches_substring_case_insensitively(session: Session, library: dict[str, str]) -> None:
    assert titles(session, search="lph") == ["Alpha 100%"]
    assert titles(session, search="a") != []


def test_search_escapes_like_wildcards(session: Session, library: dict[str, str]) -> None:
    """搜索里的 % 和 _ 是字面量，不是通配符。"""
    assert titles(session, search="100%") == ["Alpha 100%"]
    assert titles(session, search="beta_") == ["beta_X"]
    assert titles(session, search="100x") == []
    assert titles(session, search="_") == ["beta_X"]  # 只匹配真的带下划线的那条


def test_creator_contains(session: Session, library: dict[str, str]) -> None:
    assert titles(session, creator="张三") == ["Alpha 100%", "Gamma"] or set(
        titles(session, creator="张三")
    ) == {"Alpha 100%", "Gamma"}
    assert titles(session, creator="王五") == ["Gamma"]  # 多作者里的一部分也能命中
    assert titles(session, creator="不存在") == []
    assert titles(session, creator="张三", kind="book") == []


def test_rating_range_excludes_unrated(session: Session, library: dict[str, str]) -> None:
    assert set(titles(session, rating_min=7)) == {"Alpha 100%", "Delta"}
    assert set(titles(session, rating_max=5)) == {"beta_X"}
    assert set(titles(session, rating_min=4, rating_max=8)) == {"beta_X", "Delta"}
    assert titles(session, rating_min=0) != [] and "Gamma" not in titles(session, rating_min=0)


def test_created_range_is_inclusive(session: Session, library: dict[str, str]) -> None:
    assert titles(session, created_from="2026-10-02T00:00:00+00:00") == [
        "beta_X",
        "Gamma",
        "Delta",
    ] or set(titles(session, created_from="2026-10-02T00:00:00+00:00")) == {
        "beta_X",
        "Gamma",
        "Delta",
    }
    assert titles(
        session, created_from="2026-10-01T00:00:00+00:00", created_to="2026-10-01T00:00:00+00:00"
    ) == ["Alpha 100%"]
    assert titles(
        session, created_from="2026-10-02T00:00:00+00:00", created_to="2026-10-03T00:00:00+00:00"
    ) == ["beta_X", "Gamma"] or set(
        titles(session, created_from="2026-10-02T00:00:00+00:00", created_to="2026-10-03T00:00:00+00:00")
    ) == {"beta_X", "Gamma"}


def test_tags_include_is_and(session: Session, library: dict[str, str]) -> None:
    assert titles(session, tags_include=["科幻"]) != []
    assert set(titles(session, tags_include=["科幻"])) == {"Alpha 100%", "Gamma"}
    assert titles(session, tags_include=["科幻", "经典"]) == ["Alpha 100%"]  # 两个都要有
    assert titles(session, tags_include=["科幻", "动作"]) == []  # 没人同时有


def test_tags_exclude_is_and(session: Session, library: dict[str, str]) -> None:
    assert set(titles(session, tags_exclude=["科幻"])) == {"beta_X", "Delta"}
    assert titles(session, tags_exclude=["科幻", "动作"]) == ["Delta"]  # 两个都不能有


def test_basic_and_search_and_advanced_combine(session: Session, library: dict[str, str]) -> None:
    """三种筛选来源共同决定结果。"""
    assert titles(
        session, kind="movie", search="a", rating_min=9, tags_include=["科幻"]
    ) == ["Alpha 100%"]
    assert titles(session, kind="book", tags_include=["科幻"]) == []
    assert titles(session, kind="movie", creator="李四", rating_max=5) == ["beta_X"]


@pytest.mark.parametrize(
    ("sort", "descending", "expected"),
    [
        ("created_at", True, ["Delta", "Gamma", "beta_X", "Alpha 100%"]),
        ("created_at", False, ["Alpha 100%", "beta_X", "Gamma", "Delta"]),
        ("title", False, ["Alpha 100%", "beta_X", "Delta", "Gamma"]),
        ("title", True, ["Gamma", "Delta", "beta_X", "Alpha 100%"]),
        ("rating", True, ["Alpha 100%", "Delta", "beta_X", "Gamma"]),  # NULL 最后
        ("rating", False, ["beta_X", "Delta", "Alpha 100%", "Gamma"]),  # NULL 最后
        ("year", False, ["Alpha 100%", "beta_X", "Delta", "Gamma"]),
    ],
)
def test_sorting(
    session: Session, library: dict[str, str], sort: str, descending: bool, expected: list[str]
) -> None:
    assert titles(session, sort=sort, descending=descending) == expected


def test_count_matches_list_for_every_filter(session: Session, library: dict[str, str]) -> None:
    cases: list[dict[str, Any]] = [
        {},
        {"search": "a"},
        {"creator": "张三"},
        {"rating_min": 5},
        {"tags_include": ["科幻"]},
        {"tags_include": ["科幻", "经典"]},
        {"tags_exclude": ["科幻"]},
        {"kind": "movie", "search": "a", "rating_min": 1},
        {"created_from": "2026-10-02T00:00:00+00:00"},
    ]
    for case in cases:
        assert repo.count_items(session, **case) == len(repo.list_items(session, **case)), case


def test_sort_and_pagination_are_stable(session: Session, library: dict[str, str]) -> None:
    first = repo.list_items(session, sort="title", descending=False, limit=2, offset=0)
    second = repo.list_items(session, sort="title", descending=False, limit=2, offset=2)
    assert [i.title for i in first] + [i.title for i in second] == [
        "Alpha 100%",
        "beta_X",
        "Delta",
        "Gamma",
    ]


def test_unknown_sort_falls_back_to_default(session: Session, library: dict[str, str]) -> None:
    """repository 层不校验 sort（校验在服务层），未知值退回默认排序而不是报错。"""
    assert len(repo.list_items(session, sort="nope")) == 4


def test_filters_repo_kwargs_are_accepted_by_repository(
    session: Session, library: dict[str, str]
) -> None:
    filters: ListFilters = parse_filters(search="beta", sort="title")
    assert [i.title for i in repo.list_items(session, **filters.repo_kwargs())] == ["beta_X"]
    assert repo.count_items(session, **filters.repo_kwargs()) == 1


def test_local_today_boundaries_match_freshly_created_items(session: Session, make_item: Any) -> None:
    """刚入库的条目一定落在「本地今天」这个区间里（时区换算正确性）。"""
    make_item(kind="movie", title="Just Added")
    session.commit()
    today = datetime.now().astimezone().date().isoformat()
    filters = parse_filters(created_from=today, created_to=today)
    assert repo.count_items(session, **filters.repo_kwargs()) == 1
    yesterday = datetime.now().astimezone().replace(hour=0).timestamp() - 86400
    filters = parse_filters(
        created_to=datetime.fromtimestamp(yesterday).astimezone().date().isoformat()
    )
    assert repo.count_items(session, **filters.repo_kwargs()) == 0


def test_creator_filter_matches_any_of_the_creators(session: Session) -> None:
    """creators 是多值：过滤「任意一个创作者命中」就算命中。"""
    repo.create_item(
        session, kind="asmr", title="多人作品", creators=["社团甲", "声优乙", "声优丙"]
    )
    repo.create_item(session, kind="asmr", title="单人作品", creators=["声优丁"])
    session.commit()

    for needle, expected in (("社团甲", ["多人作品"]), ("声优乙", ["多人作品"]),
                             ("声优丙", ["多人作品"]), ("声优丁", ["单人作品"])):
        rows = repo.list_items(session, creator=needle)
        assert [row.title for row in rows] == expected, needle
    assert repo.count_items(session, creator="社团") == 1
    assert repo.list_items(session, creator="不存在的人") == []
