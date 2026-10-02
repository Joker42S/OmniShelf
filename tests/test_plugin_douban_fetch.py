"""豆瓣抓取插件（plugins/douban_fetch）的行为测试。

测试**不出网**：所有 HTTP 都通过替换模块里的 ``http_get`` 换成固定 HTML
（`tests/plugin_fixtures/douban_*.html`，形状照真实页面写的）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import Session

from omnishelf.config import Settings, repo_root
from omnishelf.db import build_engine
from omnishelf.plugins.manager import STATE_LOADED, PluginManager

PLUGIN_ID = "douban_fetch"
FIXTURES = Path(__file__).parent / "plugin_fixtures"


@pytest.fixture()
def manager(settings: Settings, session: Session) -> PluginManager:
    engine = build_engine(replace(settings))
    return PluginManager(
        plugins_dir=repo_root() / "plugins",
        plugins_data_dir=settings.plugins_data_dir,
        engine=engine,
    )


@pytest.fixture()
def loaded(manager: PluginManager) -> tuple[PluginManager, Any]:
    manager.load_all()
    status = manager.status(PLUGIN_ID)
    assert status.state == STATE_LOADED, status.error
    loaded_plugin = manager.get(PLUGIN_ID)
    assert loaded_plugin is not None
    return manager, loaded_plugin.module


def page_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fake_http(module: Any, pages: dict[int, str], calls: list[int] | None = None):
    """把模块的 http_get 换成「按 start 返回固定 HTML」的假实现。"""

    def _fake(url: str, media: str, **kwargs: Any) -> Any:
        start = 0
        for part in url.split("?", 1)[-1].split("&"):
            if part.startswith("start="):
                start = int(part.split("=", 1)[1])
        page_no = start // 15 + 1
        if calls is not None:
            calls.append(page_no)
        text = pages.get(page_no)
        if text is None:
            return module.FetchResult(404, "", "HTTP 404")
        return module.FetchResult(200, text)

    return _fake


# --------------------------------------------------------------------- 解析

def test_parses_movie_page(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    items, total_pages, reported = module.parse_movie_page(page_html("douban_movies_page1.html"))

    assert len(items) == 15
    assert total_pages == 3
    assert reported == 45
    first = items[0].to_dict()
    assert first["subject_id"] == "90000101"
    assert first["title"] == "示例影片101 / Sample Movie 101"
    assert first["rating"] == 8 and first["stars"] == 4
    assert first["marked_date"] == "2026-10-01"
    assert first["cover"].startswith("https://img9.example.invalid/")
    assert first["extra"]["playable"] is True
    assert first["extra"]["language"] == "汉语普通话"


def test_parses_book_page_with_year_and_publisher(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    items, total_pages, reported = module.parse_book_page(page_html("douban_books_page1.html"))

    assert len(items) == 15
    assert total_pages == 2          # 读书站没有 data-total-page，要从分页链接推
    assert reported == 25
    first = items[0].to_dict()
    assert first["subject_id"] == "91000101"
    assert first["title"] == "示例图书101"
    assert first["year"] == "2001"   # 从 pub 的「2001-5」里取
    assert first["extra"]["publisher"] == "示例出版社"
    assert first["rating"] == 6      # rating3-t -> 3 星 -> 6 分


def test_detects_challenge_and_empty_pages(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    challenge = page_html("douban_movies_challenge.html")
    empty = page_html("douban_movies_empty.html")

    assert module.is_challenge(challenge) is True
    assert module.is_challenge(empty) is False      # 空列表不是拦截页
    assert module.looks_empty(empty, 0) is True
    assert module.looks_empty(challenge, None) is False


def test_builds_urls_per_media(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    assert module.build_url("movie", "1", "collect", 30) == (
        "https://movie.douban.com/people/1/collect?start=30&sort=time&rating=all&filter=all&mode=grid"
    )
    assert module.build_url("book", "1", "wish", 0).startswith("https://book.douban.com/people/1/wish?")
    assert module.build_url("game", "1", "do", 15) == (
        "https://www.douban.com/people/1/games?action=do&start=15"
    )


def test_pages_parser(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    assert module.parse_pages("1") == ((1, 1), None)
    assert module.parse_pages("1-5") == ((1, 5), None)
    assert module.parse_pages("0") == ((0, 0), None)
    assert module.parse_pages("47-") == ((47, 0), None)
    pages, error = module.parse_pages("abc")
    assert pages == (1, 1) and error


def test_pages_default_is_full_crawl(loaded: tuple[PluginManager, Any]) -> None:
    """页码框默认「0 全抓」：少翻几页可能永远少一批数据，多翻几页只是慢一点。"""
    _, module = loaded
    assert module.DEFAULTS["pages"] == "0"
    assert module.effective_config(SimpleNamespace(config={}))["pages"] == "0"
    # 配置里存了空串也不能把默认值弄丢
    assert module.effective_config(SimpleNamespace(config={"pages": "  "}))["pages"] == "0"
    assert module.effective_config(SimpleNamespace(config={"pages": "1-5"}))["pages"] == "1-5"


def test_total_pages_only_grows(loaded: tuple[PluginManager, Any]) -> None:
    """总页数取「已知 / 这一页推出来的 / 站点报的条数换算的」里的最大者。

    「页面看不到分页链接」在 ``_total_pages`` 里会退回「只有 1 页」，那只是猜的：
    豆瓣标题报了 45 条就不可能只有一页，否则 ``页码=0`` 抓全量会只翻一页就停，
    报告还写着「已到最后一页（1）」，看着像抓完了。
    """
    _, module = loaded
    # 猜出来的 1 页 vs 站点报的 45 条 => 3 页
    assert module._reconcile_total_pages(None, 1, 45) == 3
    # 已经确认的总页数不能被后面某页改小
    assert module._reconcile_total_pages(5, 3, None) == 5
    # 页码条直接认出末页时以它为准
    assert module._reconcile_total_pages(2, 3, 25) == 3
    assert module._reconcile_total_pages(None, None, None) is None


# --------------------------------------------------------------------- 抓取

def test_fetch_one_page_saves_importable_snapshot(
    loaded: tuple[PluginManager, Any], tmp_path: Path
) -> None:
    _, module = loaded
    module.http_get = fake_http(module, {1: page_html("douban_movies_page1.html")})

    summary = module.fetch_list("movie", "12345678", "collect", pages=(1, 1), delay=0)
    assert summary.item_count == 15 and summary.fetched_count == 15
    assert summary.stop_reason == "到达指定末页 1"
    assert summary.deleted_or_hidden is None      # 没抓完就不算差额

    import_path, detail_path = module.save_snapshot(tmp_path, summary)
    payload = module.json.loads(import_path.read_text(encoding="utf-8"))
    # 这份 JSON 必须能被内置 importer 认出来
    assert payload["media"] == "movie"
    assert payload["status"] == "done"
    ids = sorted(row["subject_id"] for row in payload["items"])
    assert ids[0] == "90000101" and len(ids) == 15
    assert detail_path.is_file()
    # 详情里的 fetch 块是「上次抓到什么」的状态卡，逐页流水归宿主的运行日志
    detail = json.loads(detail_path.read_text(encoding="utf-8"))
    # 详情里的 fetch 块是「上次抓到什么」的状态卡，逐页流水归宿主的运行日志
    assert "page_notes" not in detail["fetch"]


def test_fetch_accumulates_into_one_snapshot(
    loaded: tuple[PluginManager, Any], tmp_path: Path
) -> None:
    _, module = loaded
    module.http_get = fake_http(module, {1: page_html("douban_movies_page1.html")})

    first = module.fetch_list("movie", "12345678", "collect", pages=(1, 1), delay=0)
    module.save_snapshot(tmp_path, first)

    module.http_get = fake_http(module, {2: page_html("douban_movies_page2.html")})
    previous = module.load_snapshot(tmp_path, "movie", "collect")
    assert len(previous) == 15

    second = module.fetch_list(
        "movie", "12345678", "collect", pages=(2, 2), delay=0,
        known_ids=set(previous), merge_with=previous,
    )
    assert second.fetched_count == 15
    assert second.item_count == 30                 # 累计，不是覆盖
    assert second.new_count == 15
    payload = module.json.loads(module.save_snapshot(tmp_path, second)[0].read_text(encoding="utf-8"))
    assert payload["item_count"] == 30
    ids = {row["subject_id"] for row in payload["items"]}
    assert "90000101" in ids and "90000201" in ids


def test_fetch_stops_at_last_page_and_reports_removed_items(
    loaded: tuple[PluginManager, Any]
) -> None:
    """抓到最后一页时差额才有意义：豆瓣报 45，实际只渲染 30 => 15 条被删/隐藏。"""
    _, module = loaded
    module.http_get = fake_http(module, {
        1: page_html("douban_movies_page1.html"),     # thispage=1, data-total-page=3, 报 45
        2: page_html("douban_movies_page2.html"),     # 报 45
        3: page_html("douban_movies_page1.html"),     # 拿第 1 页内容当第 3 页，凑满
    })
    summary = module.fetch_list("movie", "12345678", "collect", pages=(1, 0), delay=0)
    assert summary.total_pages == 3
    assert summary.stop_reason.startswith("已到最后一页")
    assert summary.reported_count == 45
    assert summary.item_count == 30                  # 第 1、3 页内容相同 => 去重
    assert summary.deleted_or_hidden == 15


def test_missing_paginator_still_uses_the_reported_count(
    loaded: tuple[PluginManager, Any]
) -> None:
    """没渲染分页链接的那一页不能当成「就一页」。

    复现的 bug：页码填 1 抓了一页，再改成 0 抓全量，结果还是只翻一页就停 ——
    ``_total_pages`` 看不到分页就猜「1 页」，抓全量立刻命中「已到最后一页（1）」。
    豆瓣标题里的 45 条是站点自己报的，它就是这一次的下限。
    """
    _, module = loaded
    module.http_get = fake_http(module, {
        1: page_html("douban_movies_page1_no_pager.html"),   # 没有分页区块，只报 45 条
        2: page_html("douban_movies_page2.html"),
        3: page_html("douban_movies_page1.html"),            # 拿第 1 页内容凑满
    })
    summary = module.fetch_list("movie", "12345678", "collect", pages=(1, 0), delay=0)

    assert summary.total_pages == 3
    assert summary.highest_page == 3
    assert summary.stop_reason == "已到最后一页（3）"
    assert summary.reached_end is True
    assert summary.item_count == 30 and summary.reported_count == 45
    assert summary.deleted_or_hidden == 15      # 真的翻到头了，差额才算得出


def test_partial_crawl_does_not_claim_a_removed_count(
    loaded: tuple[PluginManager, Any]
) -> None:
    """只抓了一页：差额不给（免得看着像抓漏），但总页数要留住（免得误判成已抓全）。"""
    _, module = loaded
    module.http_get = fake_http(module, {1: page_html("douban_movies_page1_no_pager.html")})
    summary = module.fetch_list("movie", "12345678", "collect", pages=(1, 1), delay=0)

    assert summary.stop_reason == "到达指定末页 1"
    assert summary.reached_end is False
    assert summary.total_pages == 3
    assert summary.deleted_or_hidden is None
    assert module.snapshot_is_complete(
        {"highest_page": summary.highest_page, "total_pages": summary.total_pages}) is False


def test_page_range_alone_decides_where_to_start(
    loaded: tuple[PluginManager, Any]
) -> None:
    """「从第几页开始」已经并进页码范围：`2` 就只请求第 2 页，`47-` 就从 47 翻到底。

    起页必须跟着范围走 —— 以前只写 `max(1, start_page)` 时出过 pages=(2,2)
    却从第 1 页请求、第 1 页不落在 2..2 里被当成「已到末页」、结果一页没抓的 bug。
    """
    _, module = loaded
    calls: list[int] = []
    module.http_get = fake_http(module, {2: page_html("douban_movies_page2.html")}, calls)
    summary = module.fetch_list("movie", "12345678", "collect", pages=(2, 2), delay=0)
    assert calls == [2]
    assert summary.item_count == 15
    assert summary.stop_reason == "到达指定末页 2"


def test_long_list_is_not_cut_off_after_a_page_budget(
    loaded: tuple[PluginManager, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """填 `0` 就得翻到底：以前第 25 页会来一句「本次已达单次上限」直接中断，
    列表一长就静默少一批数据，用户以为抓完了。现在只有强制休息，没有中断。
    """
    _, module = loaded
    sleeps: list[float] = []
    real_time = module.time
    # 只把 sleep 换成记账（time / strftime 还是真的），不然这条测试要真等十几秒
    monkeypatch.setattr(module, "time", SimpleNamespace(
        sleep=sleeps.append, time=real_time.time, strftime=real_time.strftime))
    # 把第 1 页的夹具改成「共 27 页、报 405 条」，每页内容都一样（去重后条数不变）
    many = (page_html("douban_movies_page1.html")
            .replace('data-total-page="3"', 'data-total-page="27"')
            .replace("(45)", "(405)"))
    module.http_get = fake_http(module, {n: many for n in range(1, 28)})

    summary = module.fetch_list(
        "movie", "12345678", "collect", pages=(1, 0), delay=0,
        rest_every_pages=20, rest_seconds=42.0,
    )
    assert summary.pages_fetched == 27
    assert summary.stop_reason == "已到最后一页（27）"
    assert "上限" not in summary.stop_reason
    assert 42.0 in sleeps                       # 连翻 20 页之后真的歇了
    assert any("强制休息" in message for message in summary.page_notes)
    assert summary.reached_end is True


def test_full_crawl_marks_snapshot_complete(loaded: tuple[PluginManager, Any]) -> None:
    """抓完整份之后 highest_page 必须等于 total_pages（否则增量永远判不出「已抓全」，
    每次都会从头重抓 —— 之前这里差一页，就是这么错的）。"""
    _, module = loaded
    module.http_get = fake_http(module, {
        1: page_html("douban_movies_page1.html"),
        2: page_html("douban_movies_page2.html"),
        3: page_html("douban_movies_page1.html"),
    })
    summary = module.fetch_list("movie", "12345678", "collect", pages=(1, 0), delay=0)
    assert summary.total_pages == 3
    assert summary.highest_page == 3
    assert module.snapshot_is_complete(
        {"highest_page": summary.highest_page, "total_pages": summary.total_pages}) is True


def test_fetch_stops_on_challenge_page(loaded: tuple[PluginManager, Any]) -> None:
    """拦截页绝不能当成「抓完了」——这是踩过的坑。"""
    _, module = loaded
    module.http_get = fake_http(module, {1: page_html("douban_movies_challenge.html")})
    summary = module.fetch_list("movie", "12345678", "collect", pages=(1, 0), delay=0)
    assert summary.item_count == 0
    assert "反爬拦截" in summary.stop_reason


def test_incremental_early_stop_only_when_snapshot_complete(
    loaded: tuple[PluginManager, Any]
) -> None:
    """early_stop 的语义（这条最容易写错）：

    * 快照不完整时 early_stop=False —— 必须把指定页码走完，否则第一页全是旧数据就停住，
      后面的页永远抓不到（这正是续抓 1-46 / 93 页时的坑）；
    * 快照完整时才 early_stop=True —— 列表按时间倒序，遇到整页已知就说明没有更新的了。
    """
    _, module = loaded
    pages = {
        1: page_html("douban_movies_page1.html"),
        2: page_html("douban_movies_page2.html"),
    }
    known = {f"900001{i:02d}" for i in range(1, 16)} | {f"900002{i:02d}" for i in range(1, 16)}

    calls: list[int] = []
    module.http_get = fake_http(module, pages, calls)
    walked = module.fetch_list(
        "movie", "12345678", "collect", pages=(1, 2),
        incremental=True, known_ids=known, early_stop=False, delay=0,
    )
    assert calls == [1, 2]                       # 不完整 => 走完页码
    assert walked.new_count == 0
    assert walked.stop_reason == "到达指定末页 2"
    assert walked.highest_page == 2

    calls.clear()
    module.http_get = fake_http(module, pages, calls)
    stopped = module.fetch_list(
        "movie", "12345678", "collect", pages=(1, 0),
        incremental=True, known_ids=known, early_stop=True, delay=0,
    )
    assert calls == [1]                          # 完整 => 遇到整页已知就停
    assert stopped.stop_reason.startswith("第 1 页整页都是已存在数据")


def test_snapshot_is_complete_helper(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    assert module.snapshot_is_complete({"highest_page": 93, "total_pages": 93}) is True
    assert module.snapshot_is_complete({"highest_page": 46, "total_pages": 93}) is False
    assert module.snapshot_is_complete({}) is False
    assert module.snapshot_is_complete({"highest_page": 3, "total_pages": None}) is False


def test_fetch_empty_status_is_not_an_error(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    module.http_get = fake_http(module, {1: page_html("douban_movies_empty.html")})
    summary = module.fetch_list("movie", "12345678", "do", pages=(1, 1), delay=0)
    assert summary.item_count == 0
    assert "列表为空" in summary.stop_reason


def test_fetch_rejects_bad_user(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    with pytest.raises(ValueError):
        module.fetch_list("movie", "not-a-number", "collect", pages=(1, 1), delay=0)
    with pytest.raises(ValueError):
        module.fetch_list("music", "1", "collect", pages=(1, 1), delay=0)


# --------------------------------------------------------------------- 登录 Cookie
# 合成值，长得像真的（dbcl2 就是「用户 ID:一串 token」），但不是任何人的凭证。
COOKIE = 'dbcl2="12345678:synthetic-cookie-value"; ck=synthetic42'
SECRET = "12345678:synthetic-cookie-value"


def logged_in(text: str) -> str:
    """把游客 fixture 扮成登录态：导航里有「退出」，标题换成「我看过的…」。"""
    return text.replace("</body>", '<a href="/accounts/logout">退出</a></body>')


def recording_http(module: Any, pages: dict[int, str], seen: list[Any]):
    """和 ``fake_http`` 一样，但把每次收到的 ``cookie`` 参数录下来。"""

    def _fake(url: str, media: str, **kwargs: Any) -> Any:
        seen.append(kwargs.get("cookie", ""))
        start = 0
        for part in url.split("?", 1)[-1].split("&"):
            if part.startswith("start="):
                start = int(part.split("=", 1)[1])
        text = pages.get(start // 15 + 1)
        if text is None:
            return module.FetchResult(404, "", "HTTP 404")
        return module.FetchResult(200, text)

    return _fake


def test_normalize_cookie_accepts_the_three_pastes(loaded: tuple[PluginManager, Any]) -> None:
    """Header 整串、两列（名字<TAB>值）、只贴 dbcl2 的裸值——三种抄法都得认。"""
    _, module = loaded
    assert module.normalize_cookie(COOKIE) == f"dbcl2={SECRET}; ck=synthetic42"
    assert module.normalize_cookie(f"dbcl2\t{SECRET}\nck\tsynthetic42") == (
        f"dbcl2={SECRET}; ck=synthetic42")
    assert module.normalize_cookie(f"dbcl2：{SECRET}") == f"dbcl2={SECRET}"
    assert module.normalize_cookie(SECRET) == f"dbcl2={SECRET}"          # 裸值补上名字
    assert module.normalize_cookie('  "1234567:tokenabc" ') == "dbcl2=1234567:tokenabc"


def test_normalize_cookie_refuses_things_that_are_not_cookies(
    loaded: tuple[PluginManager, Any]
) -> None:
    _, module = loaded
    assert module.normalize_cookie("") == ""
    assert module.normalize_cookie(None) == ""
    # 整页 HTML 粘进来了：不能当「一个 dbcl2 值」发出去
    assert module.normalize_cookie("<html><body>整页粘进来了</body></html>") == ""
    assert module.normalize_cookie("随便打一行字") == ""
    assert module.normalize_cookie("k=" + "y" * 5000) == ""            # 超长=不是 Cookie


def test_cookie_names_and_login_marker(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    assert module.cookie_names(module.normalize_cookie(COOKIE)) == ["dbcl2", "ck"]
    # 只有名字，值一个都不露
    assert SECRET not in str(module.cookie_names(module.normalize_cookie(COOKIE)))
    assert module.has_login_cookie(f"dbcl2={SECRET}") is True
    assert module.has_login_cookie("ck=synthetic42") is False


def test_looks_logged_in_reads_the_page(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    guest = page_html("douban_movies_page1.html")
    assert module.looks_logged_in(guest) is False                 # 游客页：没有退出、标题带昵称
    assert module.looks_logged_in(logged_in(guest)) is True
    assert module.looks_logged_in("<title>我看过的影视(45)</title>") is True
    assert module.looks_logged_in("") is False


def test_redact_swallows_every_shape_of_the_cookie(
    loaded: tuple[PluginManager, Any]
) -> None:
    """报错文本里出现整串、单个值、还是 dbcl2 的 token 段，都得抹掉。"""
    _, module = loaded
    for leaked in (COOKIE, f"dbcl2={SECRET}", SECRET, SECRET.split(":")[1], "synthetic42"):
        assert SECRET not in module.redact(f"HTTPError 403 {leaked}", COOKIE)
        assert SECRET.split(":")[1] not in module.redact(f"HTTPError 403 {leaked}", COOKIE)
        assert "synthetic42" not in module.redact(f"HTTPError 403 {leaked}", COOKIE)


def test_auth_label_reports_state_not_values(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    cookie = module.normalize_cookie(COOKIE)
    assert module.auth_label("") == "游客（未认证）"
    label = module.auth_label(cookie)
    assert "dbcl2" in label and SECRET not in label
    assert "登录态" in module.auth_label(cookie, True)
    assert "可能已失效" in module.auth_label(cookie, False)


def test_cookie_from_form_blank_reuses_the_saved_one(loaded: tuple[PluginManager, Any]) -> None:
    _, module = loaded
    config = {"cookie": module.normalize_cookie(COOKIE)}
    assert module.cookie_from_form({"cookie": ""}, config) == config["cookie"]
    assert module.cookie_from_form({}, config) == config["cookie"]
    assert module.cookie_from_form({"clear_cookie": "on", "cookie": ""}, config) == ""
    assert module.cookie_from_form({"cookie": "dbcl2=9999:abcdefgh"}, {}) == "dbcl2=9999:abcdefgh"


def test_http_get_sends_the_cookie_header(
    loaded: tuple[PluginManager, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """请求层真的带上 Cookie：不带就还是游客视角，数据静默少一批。"""
    import urllib.request

    _, module = loaded
    captured: list[dict[str, str]] = []

    class Resp:
        status = 200

        def __init__(self, url: str) -> None:
            self._body = b"<html>ok</html>"

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> "Resp":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    def fake_urlopen(request: Any, timeout: Any = None) -> Resp:
        captured.append({str(k): str(v) for k, v in request.headers.items()})
        return Resp("https://example.invalid")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    module.http_get("https://example.invalid/x", "movie", cookie=module.normalize_cookie(COOKIE))
    assert SECRET in captured[0]["Cookie"]

    module.http_get("https://example.invalid/x", "movie")
    assert "Cookie" not in captured[1]


def test_fetch_list_carries_cookie_and_records_auth_state(
    loaded: tuple[PluginManager, Any]
) -> None:
    _, module = loaded
    seen: list[Any] = []
    cookie = module.normalize_cookie(COOKIE)
    module.http_get = recording_http(module, {1: logged_in(page_html("douban_movies_page1.html"))}, seen)
    summary = module.fetch_list("movie", "12345678", "collect", pages=(1, 1), delay=0, cookie=cookie)
    assert seen == [cookie]
    assert summary.auth == "cookie" and summary.logged_in is True
    assert SECRET not in json.dumps(summary.summary_dict())


def test_fetch_list_flags_guest_view_and_expired_cookie(
    loaded: tuple[PluginManager, Any]
) -> None:
    """没带 Cookie / 带了却失效：两种都得在第一页就说清楚「这是游客视角，条目会少」。"""
    _, module = loaded
    guest = page_html("douban_movies_page1.html")

    seen: list[Any] = []
    module.http_get = recording_http(module, {1: guest}, seen)
    summary = module.fetch_list("movie", "12345678", "collect", pages=(1, 1), delay=0)
    assert summary.auth == "guest" and summary.logged_in is False
    assert any("游客身份（没配 Cookie）" in note for note in summary.page_notes)
    assert any("仅自己可见" in note for note in summary.page_notes)

    module.http_get = recording_http(module, {1: guest}, seen)
    stale = module.fetch_list(
        "movie", "12345678", "collect", pages=(1, 1), delay=0,
        cookie=module.normalize_cookie(COOKIE))
    assert stale.auth == "cookie" and stale.logged_in is False
    assert any("Cookie 没能换来登录态" in note for note in stale.page_notes)
    assert SECRET not in "\n".join(stale.page_notes)

