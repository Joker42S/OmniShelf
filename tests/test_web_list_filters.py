"""列表页：搜索 / 高级筛选 / 排序 / 新列（用户提的 6 点）。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.web.app import create_app


@pytest.fixture()
def client(settings: Any) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture()
def page_data(session: Session, make_item: Any) -> dict[str, str]:
    """几条特征不同的数据（含中文标题、作者、标签、年份、评分）。"""
    alpha = make_item(kind="movie", title="星屑のカルテ", year=1999, rating=9.5, creators=["张三"])
    beta = make_item(kind="movie", title="銀河食堂の夜", year=2010, rating=4.0, creators=["李四"])
    gamma = make_item(kind="movie", title="雨の日の図書館", year=None, rating=None, creators=["张三", "王五"])
    delta = make_item(kind="book", title="Delta Notes", year=2020, rating=7.5, creators=[])
    repo.set_tags(session, alpha.id, ["科幻", "经典"])
    repo.set_tags(session, beta.id, ["动作"])
    repo.set_tags(session, gamma.id, ["科幻"])
    session.commit()
    return {"alpha": alpha.id, "beta": beta.id, "gamma": gamma.id, "delta": delta.id}


def titles_in(html: str) -> list[str]:
    """从列表 HTML 里按出现顺序取出标题（表格里的标题链接）。"""
    return re.findall(r'<td>\s*<a href="/items/[0-9A-Z]{26}">([^<]+)</a>', html)


# ------------------------------------------------------------------ 1) 搜索框


def test_search_box_exists(client: TestClient) -> None:
    html = client.get("/").text
    assert 'name="search"' in html
    assert "搜索标题" in html


def test_search_filters_titles_only(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/", params={"search": "食堂"}).text
    assert titles_in(html) == ["銀河食堂の夜"]
    # 搜创作者名不该命中（只搜标题）
    assert titles_in(client.get("/", params={"search": "张三"}).text) == []


# ------------------------------------------------------------- 2) 高级筛选


def test_advanced_panel_has_all_inputs(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/").text
    assert 'class="advanced"' in html
    for name in (
        "rating_min",
        "rating_max",
        "created_from",
        "created_to",
        "creator",
        "tags_include",
        "tags_exclude",
    ):
        assert f'name="{name}"' in html, name
    # 标签：左列表（多选、按名称排序）+ 中间两个按钮 + 右边两个文本框
    pool = re.search(r'<select class="tag-pool"[^>]*>(.*?)</select>', html, re.S)
    assert pool is not None
    options = re.findall(r'<option value="([^"]+)"', pool.group(1))
    assert options == sorted(options) and {"科幻", "动作"} <= set(options)
    assert re.findall(r'data-tag-move="(\w+)"', html) == ["include", "exclude"]
    for field in ("tags_include", "tags_exclude"):
        assert re.search(rf'<input type="text" name="{field}"', html)


def test_advanced_panel_opens_when_active(client: TestClient, page_data: dict[str, str]) -> None:
    assert "advanced\" open" not in client.get("/").text
    assert "advanced\" open" in client.get("/", params={"creator": "张三"}).text


def test_rating_range_filter(client: TestClient, page_data: dict[str, str]) -> None:
    assert set(titles_in(client.get("/", params={"rating_min": "7"}).text)) == {
        "星屑のカルテ",
        "Delta Notes",
    }
    assert titles_in(client.get("/", params={"rating_min": "9", "rating_max": "10"}).text) == [
        "星屑のカルテ"
    ]
    assert client.get("/", params={"rating_min": "9", "rating_max": "1"}).status_code == 400


def test_created_range_filter(client: TestClient, page_data: dict[str, str]) -> None:
    from datetime import datetime

    today = datetime.now().astimezone().date().isoformat()
    assert len(titles_in(client.get("/", params={"created_from": today}).text)) == 4
    assert titles_in(client.get("/", params={"created_to": "2000-01-01"}).text) == []


def test_creator_filter(client: TestClient, page_data: dict[str, str]) -> None:
    assert set(titles_in(client.get("/", params={"creator": "张三"}).text)) == {
        "星屑のカルテ",
        "雨の日の図書館",
    }
    assert titles_in(client.get("/", params={"creator": "王五"}).text) == ["雨の日の図書館"]


def test_tags_include_is_and_logic(client: TestClient, page_data: dict[str, str]) -> None:
    """多个 include 标签是 AND：都要有。"""
    one = client.get("/", params=[("tags_include", "科幻")]).text
    assert set(titles_in(one)) == {"星屑のカルテ", "雨の日の図書館"}
    two = client.get("/", params=[("tags_include", "科幻"), ("tags_include", "经典")]).text
    assert titles_in(two) == ["星屑のカルテ"]
    none = client.get("/", params=[("tags_include", "科幻"), ("tags_include", "动作")]).text
    assert titles_in(none) == []


def test_tags_exclude_is_and_logic(client: TestClient, page_data: dict[str, str]) -> None:
    """排除：这些标签一个都不能有。"""
    one = client.get("/", params=[("tags_exclude", "科幻")]).text
    assert set(titles_in(one)) == {"銀河食堂の夜", "Delta Notes"}
    two = client.get(
        "/", params=[("tags_exclude", "科幻"), ("tags_exclude", "动作")]
    ).text
    assert titles_in(two) == ["Delta Notes"]


def test_include_and_exclude_together(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get(
        "/", params=[("tags_include", "科幻"), ("tags_exclude", "经典")]
    ).text
    assert titles_in(html) == ["雨の日の図書館"]


# --------------------------------------------- 3) 三来源共同决定 + 4) 新列


def test_three_filter_sources_combine(client: TestClient, page_data: dict[str, str]) -> None:
    params = [("kind", "movie"), ("search", "の"), ("rating_min", "9"), ("creator", "张三")]
    assert titles_in(client.get("/", params=params).text) == ["星屑のカルテ"]
    # 基础筛选（kind=book）把它排除掉
    params_book = [("kind", "book"), ("search", "の")]
    assert titles_in(client.get("/", params=params_book).text) == []


def test_new_columns_year_and_added_time(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/").text
    assert "<th>年份" in html or ">年份 " in html
    assert "添加时间" in html
    assert ">1999<" in html  # 年份列渲染出来了
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", html)  # 添加时间（本地时间）


# ------------------------------------------------------- 5) 创作者可点击


def test_creator_is_a_link(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/").text
    links = re.findall(r'href="/\?creator=([^"]+)"', html)
    assert links, "创作者列应当是链接"
    from urllib.parse import unquote

    assert "张三" in {unquote(link) for link in links}


def test_clicking_creator_filters_by_it(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/").text
    link = re.search(r'href="(/\?creator=[^"]+)"', html)
    assert link is not None
    followed = client.get(link.group(1)).text
    assert set(titles_in(followed)) == {"星屑のカルテ", "雨の日の図書館"}
    assert 'name="creator"' in followed and 'value="张三"' in followed  # 高级筛选里回填了


def test_tag_chip_links_to_advanced_include(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/").text
    assert 'href="/?tags_include=%E7%A7%91%E5%B9%BB"' in html
    followed = client.get("/?tags_include=%E7%A7%91%E5%B9%BB").text
    assert set(titles_in(followed)) == {"星屑のカルテ", "雨の日の図書館"}


# ---------------------------------------------------------- 6) 点击表头排序


def test_sort_headers_exist_for_required_columns(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/").text
    assert html.count('title="点击切换排序"') == 4
    for label in ("标题", "评分", "年份", "添加时间"):
        assert label in html


def test_default_sort_is_added_time_desc(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/").text
    # 默认状态：添加时间那列是当前排序列，方向 ↓
    header = re.search(r'<a href="[^"]*"[^>]*>添加时间 <span class="sort-ind">([^<]+)</span>', html)
    assert header is not None and header.group(1) == "↓"
    assert 'href="/?sort=title&amp;dir=asc"' in html  # 标题列给出升序链接


def test_sort_by_title_ascending(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/", params={"sort": "title", "dir": "asc"}).text
    # 三个 CJK 标题 + 一个拉丁标题，按 lower(title) 升序
    assert titles_in(html)[0] == "Delta Notes"
    header = re.search(r'<a href="[^"]*"[^>]*>标题 <span class="sort-ind">([^<]+)</span>', html)
    assert header is not None and header.group(1) == "↑"
    # 再点一次变成降序
    assert 'dir=desc' in html


def test_sort_by_rating_puts_unrated_last(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/", params={"sort": "rating", "dir": "desc"}).text
    assert titles_in(html) == ["星屑のカルテ", "Delta Notes", "銀河食堂の夜", "雨の日の図書館"]


def test_sort_by_year(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/", params={"sort": "year", "dir": "desc"}).text
    assert titles_in(html) == ["Delta Notes", "銀河食堂の夜", "星屑のカルテ", "雨の日の図書館"]


# ------------------------------------------------------------- 其它行为


def test_invalid_filter_returns_400_with_message(client: TestClient) -> None:
    response = client.get("/", params={"rating_min": "abc"})
    assert response.status_code == 400
    assert "筛选条件有问题" in response.text
    assert "评分下限" in response.text


def test_pager_and_sort_links_keep_all_filters(client: TestClient, session: Session, make_item: Any) -> None:
    for n in range(25):
        make_item(kind="movie", title=f"Movie {n:02d}", creators=["批量"])
    session.commit()
    html = client.get(
        "/", params=[("creator", "批量"), ("search", "Movie"), ("per_page", "10"), ("sort", "title")]
    ).text
    # 分页链接保留 creator + search + sort
    page2 = re.search(r'href="(/\?[^"]*page=2[^"]*)"', html)
    assert page2 is not None
    url = page2.group(1)
    assert "creator=" in url and "search=Movie" in url and "sort=title" in url
    follow = client.get(url.replace("&amp;", "&")).text  # HTML 里的链接是转义过的
    assert titles_in(follow) == [f"Movie {n:02d}" for n in range(10, 20)]


def test_filter_chips_can_remove_one_condition(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/", params=[("creator", "张三"), ("search", "書館")]).text
    assert "当前筛选" in html
    chips = re.findall(r'class="chip" href="([^"]+)"', html)
    assert len(chips) == 2
    # 去掉搜索那条，保留创作者
    remove_search = next(c for c in chips if "creator=" in c)
    assert "search=" not in remove_search
    assert set(titles_in(client.get(remove_search.replace("&amp;", "&")).text)) == {
        "星屑のカルテ",
        "雨の日の図書館",
    }


def test_empty_result_shows_hint(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get("/", params={"search": "绝对不存在的标题"}).text
    assert "没有符合当前筛选的收藏" in html


# ---------------------------------------------------- 输入框回填（曾经渲染出 None）


def test_no_none_in_input_values(client: TestClient, page_data: dict[str, str]) -> None:
    """回归：筛选输入框默认必须是空字符串，不能渲染出 value="None"。"""
    for url in ("/", "/?search=x", "/?kind=movie&status=doing&per_page=10"):
        html = client.get(url).text
        assert 'value="None"' not in html, url
        assert 'value="none"' not in html, url
    html = client.get("/").text
    for field in ("search", "title_prefix", "creator", "tags_include", "tags_exclude"):
        match = re.search(rf'name="{field}"[^>]*value="([^"]*)"', html, re.S)
        assert match is not None and match.group(1) == "", field


def test_filter_inputs_echo_submitted_values(client: TestClient, page_data: dict[str, str]) -> None:
    html = client.get(
        "/",
        params={
            "search": "食堂",
            "title_prefix": "銀",
            "creator": "李四",
            "rating_min": "4",
            "rating_max": "5",
            "created_from": "2026-01-01",
            "created_to": "2026-12-31",
        },
    ).text
    expected = {
        "search": "食堂",
        "title_prefix": "銀",
        "creator": "李四",
        "rating_min": "4",
        "rating_max": "5",
        "created_from": "2026-01-01",
        "created_to": "2026-12-31",
    }
    for field, value in expected.items():
        match = re.search(rf'name="{field}"[^>]*value="([^"]*)"', html, re.S)
        assert match is not None and match.group(1) == value, field
