"""豆瓣抓取插件的 HTTP 层测试：页面能打开、表单能存配置、能抓取并落盘。

抓取路径通过替换 ``urllib.request.urlopen`` 走固定 HTML（`tests/plugin_fixtures/`），
所以**测试不出网**。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import replace
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnishelf.config import Settings, repo_root
from omnishelf.web.app import create_app

PLUGIN_ID = "douban_fetch"
FIXTURES = Path(__file__).parent / "plugin_fixtures"


class FakeHeaders:
    def __init__(self, url: str) -> None:
        self.url = url

    def geturl(self) -> str:
        return self.url


class FakeResponse:
    """够用的 urlopen 替身：按 URL 里的 start 返回对应 fixture。"""

    def __init__(self, url: str, body: str) -> None:
        self.status = 200
        self._body = body.encode("utf-8")
        self.headers = FakeHeaders(url)

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


@pytest.fixture()
def settings_with_repo_plugins(settings: Settings) -> Settings:
    """仓库里的 plugins/ 才是被测代码；settings 默认把 plugins_dir 隔离到 tmp_path。"""
    return replace(settings, plugins_dir=repo_root() / "plugins")


@pytest.fixture()
def client(settings_with_repo_plugins: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings_with_repo_plugins)) as test_client:
        yield test_client


def install_fake_network(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[int, str],
    headers_out: list[dict[str, str]] | None = None,
) -> list[str]:
    """把 urllib.request.urlopen 换成按 start 返回 fixture 的假实现。

    ``pages`` 的值既可以是 ``plugin_fixtures/`` 里的文件名，也可以是现成的 HTML 文本
    （登录态那一类「改过的页」不必再复制一份 fixture）。
    ``headers_out`` 传列表就把每次请求的请求头按顺序录下来——Cookie 有没有真的带上，
    只有在这一层才看得见。
    """
    import urllib.request

    called: list[str] = []

    def fake_urlopen(request: Any, timeout: int | None = None) -> FakeResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if headers_out is not None:
            headers_out.append({str(k): str(v) for k, v in request.headers.items()})
        called.append(url)
        start = 0
        for part in url.split("?", 1)[-1].split("&"):
            if part.startswith("start="):
                start = int(part.split("=", 1)[1])
        page = start // 15 + 1
        body = pages.get(page)
        if body is None:
            raise RuntimeError(f"测试里没有准备第 {page} 页")
        if body.endswith(".html"):
            body = (FIXTURES / body).read_text(encoding="utf-8")
        return FakeResponse(url, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return called


def logged_in_page(name: str) -> str:
    """把游客 fixture 改成「登录态渲染出来的那一页」。

    真实页面上的差别就两处（三个站都验过）：标题从「<昵称>看过的影视」变成
    「我看过的影视」，导航里多一个退出链接。
    """
    text = (FIXTURES / name).read_text(encoding="utf-8")
    text = re.sub(r"<title>[^<]*?看过的影视", "<title>我看过的影视", text, count=1)
    return text.replace("</body>", '<a href="https://www.douban.com/accounts/logout">退出</a></body>')


def plugin_log(settings: Settings, plugin_id: str = PLUGIN_ID) -> list[str]:
    """宿主统一收集的插件日志（``data/logs/plugins/<id>.log``）里的行。"""
    path = settings.plugin_logs_dir / f"{plugin_id}.log"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def wait_for_fetch(client: TestClient, *, timeout: float = 60.0) -> str:
    """等后台抓取结束，返回「看到结果」的那一页 HTML。

    抓取搬到后台线程之后，POST 的响应里只有刚起步的进度条；结果得像浏览器
    轮询那样反复 GET 插件页（进度条走的就是这条 hx-get）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        html = client.get(f"/plugins/{PLUGIN_ID}").text
        if "抓取完成" in html or "抓取失败" in html or "没能执行" in html:
            return html
        threading.Event().wait(0.05)
    raise AssertionError(f"后台抓取没在 {timeout} 秒内结束")


def test_plugin_page_renders(client: TestClient) -> None:
    response = client.get(f"/plugins/{PLUGIN_ID}")
    assert response.status_code == 200
    body = response.text
    assert "豆瓣收藏抓取" in body          # 页面标题（= 插件展示名）
    assert "douban_fetch" in body          # 同时露出插件 id，方便对照目录
    assert 'href="/upload"' in body        # 给出去哪导入
    assert "已删除的条目" in body              # 差额的说明要写在页面上
    assert 'action="/plugins/douban_fetch"' in body
    assert "豆瓣用户 ID" in body
    # 页码框默认全抓；「从第几页开始」已经并进页码范围（47-），不该再有一个输入框
    assert 'name="pages" value="0"' in body
    assert "start_page" not in body
    # 登录 Cookie：抓「仅自己可见」的条目要靠它；输入框不回显已存的值
    assert 'name="cookie"' in body
    assert 'type="password" name="cookie"' in body
    assert "仅自己可见" in body


def test_plugin_appears_in_list(client: TestClient) -> None:
    body = client.get("/plugins").text
    assert "豆瓣收藏抓取" in body
    assert "已加载" in body


def test_save_config_roundtrip(client: TestClient, settings_with_repo_plugins: Settings) -> None:
    response = client.post(
        f"/plugins/{PLUGIN_ID}",
        data={
            "action": "save",
            "user": "12345678",
            "media": "book",
            "status": "wish",
            "pages": "0",
            "delay": "5",
            "incremental": "on",
            "user_game": "12345",
        },
    )
    assert response.status_code == 200
    config_path = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID / "config.toml"
    assert config_path.is_file()
    text = config_path.read_text(encoding="utf-8")
    assert 'user = "12345678"' in text
    assert 'media = "book"' in text
    assert "overrides" in text and "12345" in text
    assert "默认配置已保存" in response.text


def test_saved_defaults_survive_reload(client: TestClient) -> None:
    """保存默认值之后，重新加载插件不能把配置打回原形（config 要能稳定复现）。"""
    from omnishelf.plugins.manager import STATE_LOADED

    client.post(
        f"/plugins/{PLUGIN_ID}",
        data={
            "action": "save", "user": "12345678", "media": "game", "status": "wish",
            "pages": "5", "delay": "6", "incremental": "on",
        },
    )
    manager = client.app.state.plugin_manager  # type: ignore[attr-defined]
    manager.reload(PLUGIN_ID)
    status = manager.status(PLUGIN_ID)
    assert status.state == STATE_LOADED, status.error
    loaded = manager.get(PLUGIN_ID)
    assert loaded is not None
    config = loaded.context.config
    assert config["media"] == "game"
    assert config["status"] == "wish"
    assert config["pages"] == "5"
    assert config["delay"] == 6.0
    assert config["incremental"] is True

    # 再存一次也必须稳定（不能一次比一次漂）
    client.post(f"/plugins/{PLUGIN_ID}", data={"action": "save", "user": "12345678"})
    manager.reload(PLUGIN_ID)
    reloaded = manager.get(PLUGIN_ID)
    assert reloaded is not None
    again = reloaded.context.config
    assert again["media"] == "game" and again["pages"] == "5"


def test_save_config_rejects_bad_user(client: TestClient) -> None:
    response = client.post(
        f"/plugins/{PLUGIN_ID}", data={"action": "save", "user": "不是数字"}
    )
    assert response.status_code == 200
    assert "必须是一串数字" in response.text


def test_fetch_writes_snapshot_and_reports(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = install_fake_network(monkeypatch, {
        1: "douban_movies_page1.html",
        2: "douban_movies_page2.html",
    })
    response = client.post(
        f"/plugins/{PLUGIN_ID}",
        data={
            "action": "fetch",
            "media": "movie",
            "status": "collect",
            "user": "12345678",
            "pages": "1-2",
            "delay": "1",
        },
    )
    assert response.status_code == 200
    assert "正在抓取" in response.text             # POST 只给进度条，结果要等后台跑完
    body = wait_for_fetch(client)
    assert "抓取完成" in body
    assert "快照累计" in body
    assert len(called) == 2

    snapshot = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID / "douban_movie_collect.json"
    assert snapshot.is_file()
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["media"] == "movie"
    assert payload["status"] == "done"
    assert payload["item_count"] == 30
    assert payload["fetched_count"] == 30
    assert payload["highest_page"] == 2
    # 名字要在页面上出现，好让用户去 /upload 选它
    assert snapshot.name in body

    # 进度条那一行的同时，宿主日志里也留了一份：关掉页面、重启进程都还在
    lines = plugin_log(settings_with_repo_plugins)
    assert any("| 宿主 | 加载完成" in line for line in lines)
    assert any("p1 抓到" in line for line in lines)
    assert any("p2 抓到" in line for line in lines)
    assert any("抓取完成" in line for line in lines)


def test_pages_zero_after_a_one_page_fetch_crawls_the_rest(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先 ``页码=1`` 只抓一页，再 ``页码=0`` 抓全量——第二遍必须把剩下的页翻完。

    盯的是「0 = 全抓」这个约定不能被前一次的部分快照吃掉：第二遍只抓一页就停，
    用户看到的是「我明明填了 0」。
    """
    called = install_fake_network(monkeypatch, {
        1: "douban_movies_page1.html",
        2: "douban_movies_page2.html",
        3: "douban_movies_page2.html",
    })
    form = {
        "action": "fetch", "media": "movie", "status": "collect",
        "user": "12345678", "delay": "1",
    }
    client.post(f"/plugins/{PLUGIN_ID}", data={**form, "pages": "1"})
    wait_for_fetch(client)
    assert len(called) == 1

    client.post(f"/plugins/{PLUGIN_ID}", data={**form, "pages": "0"})
    body = wait_for_fetch(client)
    assert "抓取完成" in body
    # 第二遍从第 1 页重新翻到第 3 页（一共 3 页）
    assert len(called) == 4
    snapshot = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID / "douban_movie_collect.json"
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["highest_page"] == 3


def test_missing_paginator_does_not_cut_a_full_crawl_short(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """页码条没渲染出来 ≠ 只有一页：站点自己报了 45 条，就该按 3 页翻下去。

    ``_total_pages`` 在没有分页链接时会退回「1 页」，这个退回值不能盖过
    标题里的条数（``看过的影视(45)``）——否则「抓全量」只翻一页就停，
    报告还写着「已到最后一页」，看着像抓完了。
    """
    called = install_fake_network(monkeypatch, {
        1: "douban_movies_page1_no_pager.html",
        2: "douban_movies_page2.html",
        3: "douban_movies_page2.html",
    })
    client.post(
        f"/plugins/{PLUGIN_ID}",
        data={
            "action": "fetch", "media": "movie", "status": "collect",
            "user": "12345678", "pages": "0", "delay": "1",
        },
    )
    body = wait_for_fetch(client)
    assert "抓取完成" in body
    assert len(called) == 3
    assert "第 3 页" in body or "已到最后一页（3）" in body

    snapshot = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID / "douban_movie_collect.json"
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["total_pages"] == 3
    assert payload["highest_page"] == 3


def test_fetch_without_user_shows_error(client: TestClient) -> None:
    response = client.post(
        f"/plugins/{PLUGIN_ID}", data={"action": "fetch", "media": "movie", "pages": "1"}
    )
    assert response.status_code == 200
    assert "请先填豆瓣用户 ID" in response.text


def test_fetch_with_bad_pages_shows_error(client: TestClient) -> None:
    response = client.post(
        f"/plugins/{PLUGIN_ID}",
        data={"action": "fetch", "user": "1", "media": "movie", "pages": "abc"},
    )
    assert response.status_code == 200
    assert "页码范围看不懂" in response.text


def test_snapshot_imports_with_builtin_importer(
    client: TestClient, settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch, session: Any,
) -> None:
    """最关键的一条：插件产出的快照必须能被内置 importer 直接导入。"""
    from omnishelf.importers.registry import load_builtin_importers
    from omnishelf.services.import_ import import_file

    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    client.post(
        f"/plugins/{PLUGIN_ID}",
        data={
            "action": "fetch", "media": "movie", "status": "collect",
            "user": "12345678", "pages": "1", "delay": "1",
        },
    )
    wait_for_fetch(client)
    snapshot = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID / "douban_movie_collect.json"
    assert snapshot.is_file()

    load_builtin_importers()
    result = import_file(session, importer_name="douban_movies", path=snapshot)
    session.commit()

    assert result.total == 15
    assert result.inserted == 15
    assert not result.errors

    # 再导一次是幂等的：不算新增
    again = import_file(session, importer_name="douban_movies", path=snapshot)
    session.commit()
    assert again.inserted == 0
    assert again.total == 15


def test_progress_bar_polls_and_blocks_a_second_fetch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抓取跑在后台：POST 立刻回一栏进度条（自己轮询自己），没跑完之前不能再点。"""
    import urllib.request

    go = threading.Event()                # 放行第一页，好让「正在抓取」那一栏稳定可见
    requested = threading.Event()

    def fake_urlopen(request: Any, timeout: int | None = None) -> FakeResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        requested.set()
        go.wait(30)
        body = (FIXTURES / "douban_movies_page1.html").read_text(encoding="utf-8")
        return FakeResponse(url, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    form = {"action": "fetch", "media": "movie", "status": "collect", "user": "12345678",
            "pages": "1", "delay": "1"}
    response = client.post(f"/plugins/{PLUGIN_ID}", data=form)
    assert response.status_code == 200
    assert requested.wait(30), "后台线程要真的发出请求"

    body = response.text
    assert "正在抓取" in body
    assert 'id="df-fetch-status"' in body
    assert 'hx-get="/plugins/douban_fetch"' in body
    assert 'hx-trigger="every' in body                       # 进度条自己定时轮询
    assert 'hx-select="#df-fetch-status"' in body            # 只换这一栏，不套第二层页面

    busy = client.post(f"/plugins/{PLUGIN_ID}", data=form)
    assert "已经有一个抓取在跑了" in busy.text

    go.set()
    assert "抓取完成" in wait_for_fetch(client)
    # 抓完这一栏就换成正式报告，轮询也随之停掉
    assert 'hx-trigger="every' not in client.get(f"/plugins/{PLUGIN_ID}").text


# --------------------------------------------------------------------- 登录 Cookie
# 合成值，长得像真的（dbcl2 的格式就是「用户 ID:一串 token」），但不是任何人的凭证。
COOKIE = 'dbcl2="12345678:synthetic-cookie-value"; ck=synthetic42'
SECRET = "12345678:synthetic-cookie-value"


def cookie_form(**overrides: str) -> dict[str, str]:
    form = {
        "action": "fetch", "media": "movie", "status": "collect",
        "user": "12345678", "pages": "1", "delay": "1",
    }
    form.update(overrides)
    return form


def config_text(settings: Settings) -> str:
    return (settings.plugins_data_dir / PLUGIN_ID / "config.toml").read_text(encoding="utf-8")


def test_cookie_is_sent_and_page_marks_login_state(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """带 Cookie 抓：请求头里真的挂上它，报告按「登录态」说话。"""
    headers: list[dict[str, str]] = []
    install_fake_network(monkeypatch, {1: logged_in_page("douban_movies_page1.html")}, headers)
    client.post(f"/plugins/{PLUGIN_ID}", data=cookie_form(cookie=COOKIE))
    body = wait_for_fetch(client)

    sent = headers[0].get("Cookie", "")
    assert sent.startswith("dbcl2=12345678:synthetic-cookie-value")
    assert "ck=synthetic42" in sent
    assert "认证" in body and "登录态" in body
    assert "游客（未认证）" not in body
    # 只报字段名，值一个都不露
    assert "dbcl2" in body and SECRET not in body
    # Cookie 落在本地配置文件里（这是设计：和 token 一个性质，仓库不跟踪这个目录）
    assert SECRET in config_text(settings_with_repo_plugins)
    # 但绝不进快照/详情（那两个文件是要拿去上传、分享的）
    data_dir = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID
    for path in sorted(data_dir.glob("douban_movie_collect*.json")):
        assert SECRET not in path.read_text(encoding="utf-8")
    assert SECRET not in "\n".join(plugin_log(settings_with_repo_plugins))


def test_blank_cookie_box_reuses_the_saved_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cookie 框留空点抓取 = 沿用已存的（和兄弟插件的令牌框同一套语义）。"""
    headers: list[dict[str, str]] = []
    install_fake_network(monkeypatch, {1: logged_in_page("douban_movies_page1.html")}, headers)
    client.post(f"/plugins/{PLUGIN_ID}", data=cookie_form(cookie=COOKIE))
    wait_for_fetch(client)
    headers.clear()

    client.post(f"/plugins/{PLUGIN_ID}", data=cookie_form(cookie=""))
    body = wait_for_fetch(client)
    assert SECRET in headers[0].get("Cookie", "")
    assert "登录态" in body


def test_clear_cookie_checkbox_forgets_it(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers: list[dict[str, str]] = []
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"}, headers)
    client.post(f"/plugins/{PLUGIN_ID}", data=cookie_form(cookie=COOKIE))
    wait_for_fetch(client)
    assert SECRET in headers[0].get("Cookie", "")
    headers.clear()

    client.post(f"/plugins/{PLUGIN_ID}", data=cookie_form(clear_cookie="on", cookie=""))
    body = wait_for_fetch(client)
    assert "Cookie" not in headers[0]
    assert "游客（未认证）" in body
    assert SECRET not in config_text(settings_with_repo_plugins)


def test_guest_fetch_tells_you_items_are_missing(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没配 Cookie 就明说是游客视角：那些「仅自己可见」的条目这次抓不到。"""
    headers: list[dict[str, str]] = []
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"}, headers)
    client.post(f"/plugins/{PLUGIN_ID}", data=cookie_form())
    body = wait_for_fetch(client)

    assert "Cookie" not in headers[0]
    assert "游客身份（没配 Cookie）" in body
    assert "仅自己可见" in body
    assert any("游客身份（没配 Cookie）" in line for line in plugin_log(settings_with_repo_plugins))


def test_expired_cookie_is_reported(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """豆瓣按游客渲染 = Cookie 已经失效：这一趟照样是游客视角，必须报出来。"""
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    client.post(f"/plugins/{PLUGIN_ID}", data=cookie_form(cookie=COOKIE))
    body = wait_for_fetch(client)

    assert "Cookie 没能换来登录态" in body
    assert "可能已失效" in body
    # 值不进日志（日志会长期留在磁盘上、还会显示在 /logs 页）
    lines = "\n".join(plugin_log(settings_with_repo_plugins))
    assert SECRET not in lines and "synthetic42" not in lines


def test_unparsable_cookie_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = client.post(
        f"/plugins/{PLUGIN_ID}",
        data=cookie_form(cookie="<html><body>整页粘进来了</body></html>"),
    )
    assert "没解析出任何「名字=值」" in response.text


def test_cookie_without_dbcl2_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有 ck / ll 之类的话抓回来还是游客视角，那就别假装带上了认证。"""
    response = client.post(f"/plugins/{PLUGIN_ID}", data=cookie_form(cookie="ck=synthetic42"))
    assert "dbcl2" in response.text
