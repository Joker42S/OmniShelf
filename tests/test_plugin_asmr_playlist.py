"""asmr_playlist 插件：抓取（增量/全量/范围）、配置持久化、导入链路。

抓取不出网：把 ``urllib.request.urlopen`` 与 ``urllib.request.build_opener`` 换成本地假实现，
按 URL 里的 ``page=`` 返回 ``tests/plugin_fixtures/asmr_playlist_*.json``（**合成数据**）。
导入走宿主的 import 接口（真写库），所以能验证整条链路：
抓 → 落快照 → 调 importer asmr_one → 库里字段对不对。
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.config import Settings, repo_root
from omnishelf.db import build_engine
from omnishelf.plugins.manager import PluginManager
from omnishelf.web.app import create_app

PLUGIN_ID = "asmr_playlist"
PLAYLIST = "1a2b3c4d-5e6f-4a1b-8c2d-9e0f1a2b3c4d"
DEFAULT_BASE_URL = "https://api.asmr.one/api/"
PLAYLIST_ENDPOINT = DEFAULT_BASE_URL + "playlist/get-playlist-works"
FIXTURES = Path(__file__).parent / "plugin_fixtures"
REPO_PLUGINS = repo_root() / "plugins"


# --------------------------------------------------------------------- 假网络


class FakeHeaders:
    def __init__(self, url: str) -> None:
        self.url = url

    def geturl(self) -> str:
        return self.url


class FakeResponse:
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


def page_of(url: str) -> int:
    for part in url.split("?", 1)[-1].split("&"):
        if part.startswith("page="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return 1
    return 1


def install_fake_network(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[int, str],
    *,
    reject_page_size: bool = False,
    dead_proxy: bool = False,
    login: dict[str, Any] | None = None,
    require_bearer: str = "",
    log: list[dict[str, str]] | None = None,
) -> tuple[list[str], list[Any]]:
    """替换两个 HTTP 出口，返回 ``(请求过的 URL, build_opener 收到的代理配置)``。

    ``dead_proxy=True`` 时走代理的路径全部连不上（用来验证自动回退直连）。
    ``login`` 是登录接口（``POST …/auth/me``）回的 JSON，没准备就回 404；
    ``require_bearer`` 非空时，列表接口只认 ``Authorization: Bearer <require_bearer>``，
    其余一律回 401（用来验「令牌过期就重新登录」）。
    ``log`` 给了就把每次请求（地址/方法/令牌/请求体）记进去，断言请求头用得上。
    ``pages`` 的值既可以是夹具文件名，也可以直接是要回的 JSON 文本（以 ``{`` / ``[`` 开头）。
    """
    calls: list[str] = []
    proxies: list[Any] = []

    def respond(url: str, *, method: str, authorization: str) -> FakeResponse:
        if url.split("?", 1)[0].endswith("/auth/me"):
            if login is None:
                raise urllib.error.HTTPError(
                    url, 404, "Not Found", {},                                  # type: ignore[arg-type]
                    io.BytesIO(b'{"error":"login not implemented"}'),
                )
            return FakeResponse(url, json.dumps(login))
        if method == "GET" and require_bearer and authorization != f"Bearer {require_bearer}":
            raise urllib.error.HTTPError(
                url, 401, "Unauthorized", {},                                   # type: ignore[arg-type]
                io.BytesIO(b'{"error":"jwt expired"}'),
            )
        if reject_page_size and "pageSize=" in url:
            raise urllib.error.HTTPError(
                url, 400, "Bad Request", {},                       # type: ignore[arg-type]
                io.BytesIO(b'{"errors":[{"msg":"Invalid value"}],"error":"pageSize: Invalid value"}'),
            )
        name = pages.get(page_of(url))
        if name is None:
            raise RuntimeError(f"测试里没有准备第 {page_of(url)} 页")
        if name.lstrip().startswith(("{", "[")):        # 以 { / [ 开头 = 直接回这段内容
            return FakeResponse(url, name)
        return FakeResponse(url, (FIXTURES / name).read_text(encoding="utf-8"))

    def url_of(request: Any) -> str:
        return request.full_url if hasattr(request, "full_url") else str(request)

    def serve(request: Any) -> FakeResponse:
        url = url_of(request)
        calls.append(url)
        method = request.get_method() if hasattr(request, "get_method") else "GET"
        authorization = (request.get_header("Authorization") or "") \
            if hasattr(request, "get_header") else ""
        if log is not None:
            log.append({
                "url": url,
                "method": method,
                "authorization": authorization,
                "body": (getattr(request, "data", None) or b"").decode("utf-8", "replace"),
            })
        return respond(url, method=method, authorization=authorization)

    def fake_urlopen(request: Any, timeout: int | None = None, **kwargs: Any) -> FakeResponse:
        return serve(request)

    class FakeOpener:
        def open(self, request: Any, timeout: int | None = None, **kwargs: Any) -> FakeResponse:
            if dead_proxy:
                raise urllib.error.URLError("connection refused")
            return serve(request)

    def fake_build_opener(*handlers: Any) -> FakeOpener:
        for handler in handlers:
            if isinstance(handler, urllib.request.ProxyHandler):
                proxies.append(dict(getattr(handler, "proxies", {})))
        return FakeOpener()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setattr(time, "sleep", lambda *_: None)      # 别让重试/限速真的睡
    return calls, proxies


# --------------------------------------------------------------------- 夹具


@pytest.fixture()
def settings_with_repo_plugins(settings: Settings) -> Settings:
    """仓库里的 plugins/ 才是被测代码；数据目录仍隔离在 tmp_path。"""
    return replace(settings, plugins_dir=REPO_PLUGINS)


@pytest.fixture()
def client(settings_with_repo_plugins: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings_with_repo_plugins)) as test_client:
        yield test_client


@pytest.fixture()
def manager(settings_with_repo_plugins: Settings) -> PluginManager:
    return PluginManager(
        plugins_dir=settings_with_repo_plugins.plugins_dir,
        plugins_data_dir=settings_with_repo_plugins.plugins_data_dir,
        engine=build_engine(settings_with_repo_plugins),
        imports_dir=settings_with_repo_plugins.imports_dir,
        logs_dir=settings_with_repo_plugins.plugin_logs_dir,
    )


def data_dir(settings: Settings) -> Path:
    return settings.plugins_data_dir / PLUGIN_ID


def snapshot_file(settings: Settings) -> Path:
    return data_dir(settings) / f"asmr_playlist_{PLAYLIST}.json"


def fetch_form(**overrides: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "action": "fetch_import",
        "form": "fetch",
        "base_url": DEFAULT_BASE_URL,
        "playlist_id": PLAYLIST,
        "proxy": "",
        "pages": "0",
        "page_size": "100",
        "delay": "0",
        "incremental": "on",
    }
    form.update(overrides)
    if overrides.get("incremental", "on") is None:      # 取消勾选 = 表单里没有这个键
        form.pop("incremental", None)
    return form


def load_snapshot(settings: Settings) -> dict[str, Any]:
    return json.loads(snapshot_file(settings).read_text(encoding="utf-8"))


def plugin_log(settings: Settings, plugin_id: str = PLUGIN_ID) -> list[str]:
    """宿主统一收集的插件日志（``data/logs/plugins/<id>.log``）里的行。"""
    path = settings.plugin_logs_dir / f"{plugin_id}.log"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def wait_for_fetch(client: TestClient, *, timeout: float = 20.0) -> str:
    """等后台抓取结束，返回「看到结果」的那一页 HTML。

    抓取改到后台线程之后，POST 的响应里只有刚起步的进度条；结果要像浏览器
    轮询那样反复 GET 插件页（走的就是进度条自己那条 hx-get 路径）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        html = client.get(f"/plugins/{PLUGIN_ID}").text
        if "抓取完成" in html or "抓取失败" in html or "没能执行" in html:
            return html
        threading.Event().wait(0.02)        # 不用 time.sleep：本文件的假网络把它空转了
    raise AssertionError(f"后台抓取没在 {timeout} 秒内结束")


# --------------------------------------------------------------------- 纯函数


def test_plugin_only_uses_the_sdk() -> None:
    """规范：插件只能用标准库 + `omnishelf.plugins.api`，不能碰宿主内部模块。"""
    source = (REPO_PLUGINS / PLUGIN_ID / "plugin.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.M)
    assert imports, "总得 import 点什么"
    allowed_std = {"__future__", "json", "re", "time", "threading", "urllib", "uuid",
                   "dataclasses", "pathlib", "typing", "collections"}
    for name in imports:
        if name == "omnishelf.plugins.api":
            continue
        assert not name.startswith("omnishelf"), f"插件不该 import 宿主内部模块: {name}"
        assert name.split(".")[0] in allowed_std, name
    assert "from omnishelf.plugins.api import Plugin" in source
    assert "requests" not in source      # 参考脚本用的 requests 不是标准库


def test_metadata_and_files_contract() -> None:
    text = (REPO_PLUGINS / PLUGIN_ID / "metadata.toml").read_text(encoding="utf-8")
    for key in ("id", "title", "version", "description", "entry", "requires"):
        assert re.search(rf"^{key}\s*=", text, re.M), key
    assert 'id = "asmr_playlist"' in text
    assert '"import"' in text and '"query"' in text
    assert (REPO_PLUGINS / PLUGIN_ID / "templates" / "page.html").is_file()
    assert (REPO_PLUGINS / PLUGIN_ID / "README.md").is_file()


def test_parse_pages_accepts_the_documented_forms() -> None:
    module = _load_plugin_module()
    assert module.parse_pages("0") == ((0, 0), None)
    assert module.parse_pages("3") == ((3, 3), None)
    assert module.parse_pages("1-5") == ((1, 5), None)
    assert module.parse_pages("12-") == ((12, 0), None)
    assert module.parse_pages(" 2 - 4 ")[0] == (2, 4)
    assert module.parse_pages("")[1]
    assert module.parse_pages("abc")[1]
    assert module.parse_pages("5-2")[1]


def test_build_page_url_keeps_query_and_adds_id() -> None:
    module = _load_plugin_module()
    # base URL 是接口根地址：尾巴按抓取目标自动补全
    url = module.build_page_url(DEFAULT_BASE_URL, PLAYLIST, 3, 100)
    assert url.startswith(PLAYLIST_ENDPOINT + "?")
    assert "page=3" in url and "pageSize=100" in url and f"id={PLAYLIST}" in url
    # base_url 里已经带了 id：不要再插一个
    with_id = f"{PLAYLIST_ENDPOINT}?id={PLAYLIST}&foo=bar"
    assert module.build_page_url(with_id, "other", 2, 0).count("id=") == 1
    assert "foo=bar" in module.build_page_url(with_id, "", 2, 0)
    assert "page=2" in module.build_page_url(with_id, "", 2, 0)
    # {page} 占位符写法（用户完全接管这条地址）
    assert module.build_page_url("https://x/api?p={page}&id=", "abc", 4, 0) \
        == "https://x/api?p=4&id=abc"
    # 空/非法地址退回默认接口
    assert module.build_page_url("", "", 1, 0).startswith(PLAYLIST_ENDPOINT)
    assert module.playlist_id_of("", PLAYLIST) == PLAYLIST
    assert module.playlist_id_of(with_id, "") == PLAYLIST


def test_normalizers_and_config_clamping() -> None:
    module = _load_plugin_module()
    assert module.normalize_proxy("127.0.0.1:10809") == "http://127.0.0.1:10809"
    assert module.normalize_proxy("") == ""
    assert module.normalize_proxy("socks5://127.0.0.1:1080") == ""
    assert module.normalize_base_url("ftp://x") == ""
    assert module.normalize_base_url(" https://a/b ") == "https://a/b"

    class FakeCtx:
        config = {"page_size": "999", "delay": "99", "proxy": "127.0.0.1:10809",
                  "start_page": "3", "max_pages": "-3"}      # 存量配置里残留的旧键

    config = module.effective_config(FakeCtx())
    assert config["page_size"] == 100          # 夹到接口上限
    assert config["delay"] == 10.0
    assert config["proxy"] == "http://127.0.0.1:10809"
    assert config["base_url"] == DEFAULT_BASE_URL
    # 起始页和单次页数上限都已删除：旧配置里留着也不会被读出来
    assert "start_page" not in config
    assert "max_pages" not in config


def test_work_key_and_trim_fields() -> None:
    module = _load_plugin_module()
    assert module.work_key({"id": 1001}) == "1001"
    assert module.work_key({"title": "没有 id"}).startswith("_noid_")
    work = {"id": 1, "title": "x", "rank": [1], "translation_info": {"a": 1},
            "rate_count_detail": [1], "work_attributes": {"b": 2}, "tags": ["keep"]}
    trimmed = module.normalize_work(work, keep_raw=False)
    assert "rank" not in trimmed and "translation_info" not in trimmed
    assert trimmed["tags"] == ["keep"]
    assert set(module.normalize_work(work, keep_raw=True)) == set(work)


def _load_plugin_module() -> Any:
    """把插件入口当模块加载（宿主的加载方式就是 exec 源码，这里等价）。"""
    import importlib.util
    import sys

    path = REPO_PLUGINS / PLUGIN_ID / "plugin.py"
    spec = importlib.util.spec_from_file_location("asmr_playlist_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module        # dataclass 要靠 sys.modules 找所属模块
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- 页面 / 配置


def test_plugin_page_renders(client: TestClient) -> None:
    response = client.get(f"/plugins/{PLUGIN_ID}")
    assert response.status_code == 200
    body = response.text
    assert "ASMR.ONE 收藏抓取" in body
    assert PLUGIN_ID in body
    assert f'action="/plugins/{PLUGIN_ID}"' in body
    assert "播放列表 ID" in body
    assert "本地代理" in body
    assert 'name="action" value="fetch_import"' in body
    # 私人收藏：开关 + 三个认证输入框（密码框不能把值回显出来）
    assert "抓取私人收藏" in body
    assert 'name="favorites"' in body
    assert 'name="username"' in body
    assert 'name="authorization"' in body
    assert 'type="password" name="password"' in body


def test_plugin_appears_in_list(client: TestClient) -> None:
    body = client.get("/plugins").text
    assert "ASMR.ONE 收藏抓取" in body
    assert "已加载" in body


def test_save_config_persists_connection_settings(
    client: TestClient, settings_with_repo_plugins: Settings
) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save",
        "form": "save",
        "base_url": DEFAULT_BASE_URL,
        "playlist_id": PLAYLIST,
        "proxy": "127.0.0.1:10809",
        "pages": "1-3",
        "page_size": "50",
        "delay": "1.5",
        "timeout": "20",
        "incremental": "on",
        "keep_raw": "on",
    })
    assert response.status_code == 200
    assert "默认配置已保存" in response.text

    config_path = data_dir(settings_with_repo_plugins) / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    assert f'base_url = "{DEFAULT_BASE_URL}"' in text
    assert f'playlist_id = "{PLAYLIST}"' in text
    assert 'proxy = "http://127.0.0.1:10809"' in text   # 没写协议也会被补上
    assert 'pages = "1-3"' in text
    assert "page_size = 50" in text
    assert "incremental = true" in text
    assert "keep_raw = true" in text
    assert "start_page" not in text              # 续抓改用页码范围（12-）
    assert "max_pages" not in text               # 没有单次页数上限


def test_saved_defaults_survive_reload(
    client: TestClient, settings_with_repo_plugins: Settings
) -> None:
    from omnishelf.plugins.manager import STATE_LOADED

    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "base_url": DEFAULT_BASE_URL,
        "playlist_id": PLAYLIST, "proxy": "http://127.0.0.1:10809",
        "pages": "1-2", "page_size": "25", "delay": "2",
        "timeout": "40", "incremental": "on",
    })
    manager = client.app.state.plugin_manager       # type: ignore[attr-defined]
    manager.reload(PLUGIN_ID)
    status = manager.status(PLUGIN_ID)
    assert status.state == STATE_LOADED, status.error
    loaded = manager.get(PLUGIN_ID)
    assert loaded is not None
    config = loaded.context.config
    assert config["playlist_id"] == PLAYLIST
    assert config["pages"] == "1-2"
    assert config["page_size"] == 25
    assert config["delay"] == 2.0
    # 再存一次也要稳定（不能一次比一次漂）
    client.post(f"/plugins/{PLUGIN_ID}", data={"action": "save", "form": "save"})
    manager.reload(PLUGIN_ID)
    again = manager.get(PLUGIN_ID)
    assert again is not None
    assert again.context.config["pages"] == "1-2"
    assert again.context.config["page_size"] == 25


def test_save_config_rejects_bad_connection(client: TestClient) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "base_url": "ftp://nope", "playlist_id": PLAYLIST,
    })
    assert response.status_code == 200
    assert "必须以 http:// 或 https:// 开头" in response.text
    assert "默认配置已保存" not in response.text

    response = client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "playlist_id": "", "base_url": DEFAULT_BASE_URL,
    })
    assert "请填播放列表 ID" in response.text

    response = client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "playlist_id": PLAYLIST, "proxy": "socks5://x:1",
        "base_url": DEFAULT_BASE_URL,
    })
    assert "代理地址要写成" in response.text


# --------------------------------------------------------------------- 抓取


def test_fetch_writes_snapshot_and_reports(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _proxies = install_fake_network(monkeypatch, {
        1: "asmr_playlist_page1.json", 2: "asmr_playlist_page2.json",
    })
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch"))
    assert response.status_code == 200
    assert "正在抓取" in response.text             # POST 只给进度条，结果要等后台跑完
    body = wait_for_fetch(client)
    assert "抓取完成" in body
    assert "快照累计" in body
    assert len(calls) == 2                       # 两页都抓了

    payload = load_snapshot(settings_with_repo_plugins)
    assert [row["id"] for row in payload["works"]] == [1001, 1002, 1003, 1004]
    assert payload["playlist"]["id"] == PLAYLIST
    assert payload["fetch_info"]["item_count"] == 4
    assert payload["fetch_info"]["reported_count"] == 4
    assert payload["fetch_info"]["total_pages"] == 2
    assert payload["fetch_info"]["next_page"] is None
    assert payload["fetch_info"]["coverage_complete"] is True
    # 默认丢掉 importer 不用的大字段
    assert "translation_info" not in payload["works"][0]
    assert payload["works"][0]["tags"]

    meta = json.loads((data_dir(settings_with_repo_plugins)
                       / f"asmr_playlist_{PLAYLIST}.meta.json").read_text(encoding="utf-8"))
    assert meta["fetch_info"]["item_count"] == 4
    # 逐页明细不落进 meta：那份历史归宿主的运行日志，meta 只当「上次抓到什么」的状态卡
    assert "notes" not in meta["fetch_info"]

    lines = plugin_log(settings_with_repo_plugins)
    assert any("开始抓取" in line for line in lines)
    assert any("p1 抓到" in line for line in lines)      # 每一页那一行也上报了
    assert any("抓取完成" in line for line in lines)


def test_connection_settings_are_remembered_after_a_fetch(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口地址/播放列表 ID/代理 是「填一次就一直用」的，抓一次就该落盘。"""
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json",
                                       2: "asmr_playlist_page2.json"})
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch"))
    wait_for_fetch(client)
    text = (data_dir(settings_with_repo_plugins) / "config.toml").read_text(encoding="utf-8")
    assert f'playlist_id = "{PLAYLIST}"' in text
    assert f'base_url = "{DEFAULT_BASE_URL}"' in text


def test_keep_raw_keeps_every_field(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = data_dir(settings_with_repo_plugins) / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f'base_url = "{DEFAULT_BASE_URL}"\nplaylist_id = "{PLAYLIST}"\nkeep_raw = true\n',
        encoding="utf-8",
    )
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json"})
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", pages="1"))
    wait_for_fetch(client)
    payload = load_snapshot(settings_with_repo_plugins)
    assert "translation_info" in payload["works"][0]


def test_range_fetch_only_requests_that_page(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = install_fake_network(monkeypatch, {2: "asmr_playlist_page2.json"})
    response = client.post(
        f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", pages="2", incremental=None))
    assert response.status_code == 200
    assert "抓取完成" in wait_for_fetch(client)
    assert len(calls) == 1 and "page=2" in calls[0]
    payload = load_snapshot(settings_with_repo_plugins)
    assert [row["id"] for row in payload["works"]] == [1003, 1004]


def test_incremental_stops_on_a_fully_known_page(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = {1: "asmr_playlist_page1.json", 2: "asmr_playlist_page2.json"}
    install_fake_network(monkeypatch, pages)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", incremental=None))
    wait_for_fetch(client)

    calls, _ = install_fake_network(monkeypatch, pages)          # 重新计数
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch"))
    assert response.status_code == 200
    html = wait_for_fetch(client)
    assert len(calls) == 1, "第一页整页已知就该停，不该继续翻第二页"
    assert "整页都是已有数据" in html
    payload = load_snapshot(settings_with_repo_plugins)
    assert len(payload["works"]) == 4                            # 快照没被清空

    # 「已抓全」必须扛过增量抓取：再来一次仍然只翻一页，不能退化成全量重抓
    calls, _ = install_fake_network(monkeypatch, pages)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch"))
    wait_for_fetch(client)
    assert len(calls) == 1, "增量抓取不能把「已抓全」抹掉，否则下次就变全量了"
    assert load_snapshot(settings_with_repo_plugins)["fetch_info"]["coverage_complete"] is True


def test_partial_range_fetch_is_not_marked_complete(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只抓了前两页（一共 56 页）就不能算抓全，否则增量会立刻停在第一页。"""
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json"})
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", pages="1",
                                                         incremental=None))
    wait_for_fetch(client)
    info = load_snapshot(settings_with_repo_plugins)["fetch_info"]
    assert info["reached_end"] is False
    assert info["coverage_complete"] is False


def test_incremental_merges_new_work_and_keeps_the_rest(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json",
                                       2: "asmr_playlist_page2.json"})
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", incremental=None))
    wait_for_fetch(client)

    # 收藏了一个新作品：第 1 页出现 1005，第 2 页整页已知
    calls, _ = install_fake_network(monkeypatch, {
        1: "asmr_playlist_page1_new.json", 2: "asmr_playlist_page2_shifted.json",
    })
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch"))
    html = wait_for_fetch(client)
    assert len(calls) == 2
    payload = load_snapshot(settings_with_repo_plugins)
    ids = [row["id"] for row in payload["works"]]
    assert 1005 in ids and len(ids) == 5          # 新增 1 条，被移出列表的 1004 仍保留
    assert "新增 1" in html


def inline_page(page_no: int, *, total: int, page_size: int = 2) -> str:
    """现场造一页接口响应：``pageSize`` 条、接口报 ``total`` 条（=> 共 ceil(total/page_size) 页）。"""
    works = [{"id": page_no * 100 + i, "title": f"作品 {page_no}-{i}"}
             for i in range(1, page_size + 1)]
    return json.dumps({
        "works": works,
        "pagination": {"page": page_no, "pageSize": page_size, "totalCount": total},
    })


def test_full_crawl_is_not_cut_off_after_a_page_budget(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """填 `0` 就得翻到接口报的末页：以前这里有个「单次 100 页上限」会直接中断。"""
    page_count = 12
    calls, _ = install_fake_network(
        monkeypatch,
        {n: inline_page(n, total=page_count * 2) for n in range(1, page_count + 1)},
    )
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", incremental=None))
    html = wait_for_fetch(client)
    assert len(calls) == page_count                    # 一页不少
    assert f"已到最后一页（{page_count}）" in html
    assert "页数上限" not in html
    payload = load_snapshot(settings_with_repo_plugins)
    assert payload["fetch_info"]["next_page"] is None
    assert len(payload["works"]) == page_count * 2


def test_failed_page_suggests_resuming_with_a_page_range(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """中途失败才给续抓页码：报告说「页码范围填 3- 接着抓」，不再单开一个起始页。"""
    install_fake_network(monkeypatch, {
        1: inline_page(1, total=8),
        2: inline_page(2, total=8),
        3: "[1, 2]",                                   # 顶层不是对象 => 这一页算失败
    })
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", incremental=None))
    html = wait_for_fetch(client)
    assert "停在第 3 页" in html
    assert "3-" in html

    page = client.get(f"/plugins/{PLUGIN_ID}").text
    assert "上次停在第 3 页，填 <code>3-</code> 接着抓" in page
    assert 'name="start_page"' not in page
    assert 'name="max_pages"' not in page


def test_page_size_rejection_falls_back_to_server_default(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = install_fake_network(
        monkeypatch,
        {1: "asmr_playlist_page1.json", 2: "asmr_playlist_page2.json"},
        reject_page_size=True,
    )
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", pages="1-2"))
    assert response.status_code == 200
    html = wait_for_fetch(client)
    assert "pageSize" in html and "400" in html
    assert any("pageSize=100" in url for url in calls)      # 先带 pageSize 试
    assert any("pageSize" not in url for url in calls)      # 被拒后去掉再试
    assert len(load_snapshot(settings_with_repo_plugins)["works"]) == 4


def test_proxy_is_used_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, proxies = install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json"})
    response = client.post(
        f"/plugins/{PLUGIN_ID}",
        data=fetch_form(action="fetch", pages="1", proxy="127.0.0.1:10809"),
    )
    assert response.status_code == 200
    html = wait_for_fetch(client)
    assert proxies and proxies[0].get("https") == "http://127.0.0.1:10809"
    assert calls, "代理路径也要真的发出请求"
    assert "http://127.0.0.1:10809" in html


def test_dead_proxy_falls_back_to_direct(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """代理连不上不该让抓取失败：回退直连，并在页面上说清楚。"""
    calls, proxies = install_fake_network(
        monkeypatch, {1: "asmr_playlist_page1.json"}, dead_proxy=True)
    response = client.post(
        f"/plugins/{PLUGIN_ID}",
        data=fetch_form(action="fetch", pages="1", proxy="http://127.0.0.1:10809"),
    )
    assert response.status_code == 200
    html = wait_for_fetch(client)
    assert "已自动回退直连" in html
    assert proxies, "先试过代理"
    assert any("page=1" in url for url in calls), "回退直连后要真的抓到数据"
    assert len(load_snapshot(settings_with_repo_plugins)["works"]) == 2


def test_empty_playlist_is_handled(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_network(monkeypatch, {1: "asmr_playlist_empty.json"})
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch"))
    assert response.status_code == 200
    assert "播放列表是空的" in wait_for_fetch(client)
    assert load_snapshot(settings_with_repo_plugins)["works"] == []


def test_request_failure_is_shown_not_raised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口挂了/连不上：页面要出提示，不能 500。"""
    install_fake_network(monkeypatch, {})            # 任何页都没有 => 假网络直接抛错
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", pages="1"))
    assert response.status_code == 200
    assert "请求失败" in wait_for_fetch(client)


# --------------------------------------------------------------------- 进度条


def test_progress_bar_polls_and_blocks_a_second_fetch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抓取跑在后台：POST 立刻回一栏进度条（自己轮询自己），没跑完之前不能再点。"""
    go = threading.Event()                # 放行第一页，好让「正在抓取」那一栏稳定可见
    requested = threading.Event()

    def fake_urlopen(request: Any, timeout: int | None = None, **kwargs: Any) -> FakeResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        requested.set()
        go.wait(10)
        body = (FIXTURES / "asmr_playlist_page1.json").read_text(encoding="utf-8")
        return FakeResponse(url, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", pages="1"))
    assert response.status_code == 200
    assert requested.wait(10), "后台线程要真的发出请求"
    body = response.text
    assert "正在抓取" in body
    assert 'id="ap-fetch-status"' in body
    assert 'hx-get="/plugins/asmr_playlist"' in body
    assert 'hx-trigger="every' in body                      # 进度条自己定时轮询
    assert 'hx-select="#ap-fetch-status"' in body           # 只换这一栏，不套第二层页面

    busy = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch", pages="1"))
    assert "已经有一个抓取在跑了" in busy.text

    go.set()
    assert "抓取完成" in wait_for_fetch(client)
    # 抓完这一栏就换成正式报告，轮询也随之停掉
    assert 'hx-trigger="every' not in client.get(f"/plugins/{PLUGIN_ID}").text


# --------------------------------------------------------------------- 导入


def test_fetch_import_writes_into_the_database(
    client: TestClient, settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch, session: Session,
) -> None:
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json",
                                       2: "asmr_playlist_page2.json"})
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    assert response.status_code == 200
    html = wait_for_fetch(client)
    assert "导入到 OmniShelf" in html
    assert "新增 4" in html
    assert "asmr_one" in html

    session.expire_all()
    items = {item.title: item for item in repo.list_items(session, kind="asmr")}
    assert len(items) == 4

    first = items["合成作品一"]
    assert first.creators == ["合成社团", "合成声优A"]
    assert first.year == 2024
    assert repo.list_tags(session, first.id) == ["おっぱい", "淫語"]   # 日文原名（list_tags 会排序）
    assert first.rating is None                          # userRating = null
    assert first.extra["release_date"] == "2024-05-02"
    assert first.extra["duration_minutes"] == 60         # 3600 秒
    assert first.extra["languages"] == ["日本語"]
    assert first.extra["nsfw"] is True
    assert first.extra["price_jpy"] == 1320
    assert first.extra["playlist_added_at"] == "2024-05-02 10:00:00"
    assert first.status in (None, "", "unset")           # 播放列表 ≠ 听过，状态留空
    links = {link.source: link.external_id
             for link in repo.list_external_ids(session, first.id)}
    assert links["asmr_one"] == "1001"
    assert links["dlsite"] == "RJ00001001"

    second = items["合成作品二"]
    assert second.rating == 8.0                          # 4 星 ×2
    assert repo.list_tags(session, second.id) == ["低语", "治愈"]   # 没有日文就退回 name

    archive = settings_with_repo_plugins.imports_dir / "asmr_one" / snapshot_file(
        settings_with_repo_plugins).name
    assert archive.is_file()                             # 原始文件按来源存档


def test_fetch_import_is_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    pages = {1: "asmr_playlist_page1.json", 2: "asmr_playlist_page2.json"}
    install_fake_network(monkeypatch, pages)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    wait_for_fetch(client)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(incremental=None))
    assert "未变化 4" in wait_for_fetch(client)
    session.expire_all()
    assert len(repo.list_items(session, kind="asmr")) == 4


def test_plain_fetch_does_not_touch_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json",
                                       2: "asmr_playlist_page2.json"})
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch"))
    assert response.status_code == 200
    assert "导入到 OmniShelf" not in wait_for_fetch(client)
    session.expire_all()
    assert repo.list_items(session, kind="asmr") == []


def test_import_failure_is_shown_without_losing_snapshot(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json"})
    from omnishelf.plugins import host as host_module

    def boom(self: Any, importer: str, path: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("假装 importer 坏了")

    monkeypatch.setattr(host_module.RepositoryHostAPI, "import_file", boom)
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(pages="1"))
    assert response.status_code == 200
    html = wait_for_fetch(client)
    assert "导入失败" in html
    assert "假装 importer 坏了" in html
    assert snapshot_file(settings_with_repo_plugins).is_file()


# --------------------------------------------------------------------- 私人收藏（要认证）

#: 合成令牌/账号：只用来断言请求头，别换成真实凭据
TOKEN = "eyJhbGciOiJIUzI1NiIs.synthetic.signature"
STALE_TOKEN = "stale.synthetic.token"
USER = "synthetic-user"
PASSWORD = "synthetic-password"
FAVORITES_PAGES = {1: "asmr_favorites_page1.json", 2: "asmr_favorites_page2.json"}


def favorites_form(**overrides: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "action": "fetch",
        "form": "fetch",
        "base_url": DEFAULT_BASE_URL,
        "playlist_id": "",
        "proxy": "",
        "pages": "1",
        "page_size": "100",
        "delay": "0",
        "incremental": "on",
        "has_favorites": "1",
        "favorites": "on",
        "username": "",
        "password": "",
        "authorization": TOKEN,
    }
    form.update(overrides)
    if overrides.get("favorites", "on") is None:  # 取消勾选 = 复选框不提交（同名标记还在）
        form.pop("favorites", None)
    return form


def favorites_snapshot(settings: Settings) -> dict[str, Any]:
    path = data_dir(settings) / "asmr_playlist_my_favorites.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_favorites_endpoint_completion() -> None:
    """base URL 是接口根地址，尾巴按目标补；整条粘过来也别拼两遍。"""
    module = _load_plugin_module()
    root = module.endpoint_url(DEFAULT_BASE_URL, "favorites")
    assert root == "https://api.asmr.one/api/review?order=updated_at&sort=desc"
    # 老的写法（完整端点 + id）照样能用，抓私人收藏时不会把 id 带过去
    legacy = f"{PLAYLIST_ENDPOINT}?id={PLAYLIST}"
    assert module.endpoint_url(legacy, "playlist") == legacy
    assert module.endpoint_url(legacy, "favorites") == root
    assert module.endpoint_url("https://api.asmr.one/api/review", "favorites") == root
    assert module.endpoint_url(DEFAULT_BASE_URL, "login") == "https://api.asmr.one/api/auth/me"
    page = module.build_page_url(DEFAULT_BASE_URL, "anything", 2, 100, "favorites")
    assert "id=" not in page and "page=2" in page and "pageSize=100" in page

    assert module.normalize_token("Bearer " + TOKEN) == TOKEN
    assert module.normalize_token(TOKEN) == TOKEN
    assert module.token_from_login({"token": "a.b"}) == "a.b"
    assert module.token_from_login({"data": {"token": "Bearer c.d"}}) == "c.d"
    assert module.token_from_login({"message": "no token here"}) == ""


def test_favorites_fetch_uses_bearer_and_writes_its_own_snapshot(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[dict[str, str]] = []
    calls, _ = install_fake_network(
        monkeypatch, FAVORITES_PAGES, require_bearer=TOKEN, log=log)
    response = client.post(f"/plugins/{PLUGIN_ID}", data=favorites_form())
    assert "正在抓取" in response.text
    html = wait_for_fetch(client)

    assert "抓取完成：私人收藏" in html
    assert "my_favorites" in html
    assert "/api/review" in calls[0] and "order=updated_at" in calls[0]
    assert "id=" not in calls[0]
    gets = [row for row in log if "/review" in row["url"]]
    assert [row["authorization"] for row in gets] == [f"Bearer {TOKEN}"]
    assert not any(row["url"].endswith("/auth/me") for row in log), "有令牌就不该再登录"

    payload = favorites_snapshot(settings_with_repo_plugins)
    assert [row["id"] for row in payload["works"]] == [1001, 1002]
    assert payload["fetch_info"]["target"] == "favorites"
    assert payload["playlist"]["id"] == "my_favorites"
    # 快照列表那一栏认得出这是私人收藏
    assert "私人收藏" in client.get(f"/plugins/{PLUGIN_ID}").text


def test_favorites_logs_in_when_there_is_no_token(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[dict[str, str]] = []
    install_fake_network(monkeypatch, FAVORITES_PAGES,
                         login={"token": TOKEN, "user": {"name": USER}}, log=log)
    client.post(f"/plugins/{PLUGIN_ID}", data=favorites_form(
        authorization="", username=USER, password=PASSWORD))
    assert "抓取完成" in wait_for_fetch(client)

    login_calls = [row for row in log if row["url"].endswith("/auth/me")]
    assert len(login_calls) == 1                    # 登录一次就够，不用每页都登
    assert login_calls[0]["method"] == "POST"
    assert json.loads(login_calls[0]["body"]) == {"name": USER, "password": PASSWORD}
    assert [row["authorization"] for row in log if "/review" in row["url"]] == [f"Bearer {TOKEN}"]

    text = (data_dir(settings_with_repo_plugins) / "config.toml").read_text(encoding="utf-8")
    assert f'authorization = "{TOKEN}"' in text     # 令牌留着下次用
    assert f'username = "{USER}"' in text
    assert PASSWORD not in text                     # 密码不落盘
    assert PASSWORD not in client.get(f"/plugins/{PLUGIN_ID}").text
    # 打码是插件自己的事：上报给宿主之前先过 redact()，日志文件里也不能有口令
    assert PASSWORD not in "\n".join(plugin_log(settings_with_repo_plugins))


def test_login_token_is_saved_even_when_the_crawl_fails(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登录一成功就把令牌存进配置：后面抓取失败，这一次登录也不算白做。"""
    install_fake_network(monkeypatch, {}, login={"token": TOKEN})
    client.post(f"/plugins/{PLUGIN_ID}", data=favorites_form(
        authorization="", username=USER, password=PASSWORD))
    assert "请求失败" in wait_for_fetch(client)

    text = (data_dir(settings_with_repo_plugins) / "config.toml").read_text(encoding="utf-8")
    assert f'authorization = "{TOKEN}"' in text
    assert PASSWORD not in text


def test_expired_token_relogs_in_and_retries_the_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先用 authorization；接口回 401 就换用户名密码登录，然后重试这一页。"""
    log: list[dict[str, str]] = []
    install_fake_network(monkeypatch, FAVORITES_PAGES,
                         login={"data": {"token": TOKEN}},
                         require_bearer=TOKEN, log=log)
    client.post(f"/plugins/{PLUGIN_ID}", data=favorites_form(
        authorization=f"Bearer {STALE_TOKEN}", username=USER, password=PASSWORD))
    html = wait_for_fetch(client)
    assert "抓取完成" in html
    assert "已用用户名密码重新登录" in html

    assert [row["authorization"] for row in log if "/review" in row["url"]] == [
        f"Bearer {STALE_TOKEN}", f"Bearer {TOKEN}",
    ]
    assert len([row for row in log if row["url"].endswith("/auth/me")]) == 1


def test_favorites_without_any_credential_is_refused(client: TestClient) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}", data=favorites_form(
        authorization="", username="", password=""))
    assert "要先认证" in response.text
    assert "正在抓取" not in response.text          # 别起一个注定失败的后台任务


def test_favorites_login_failure_is_reported(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_network(monkeypatch, FAVORITES_PAGES)      # 登录接口没准备 = 404
    response = client.post(f"/plugins/{PLUGIN_ID}", data=favorites_form(
        authorization="", username=USER, password=PASSWORD))
    # 这种「秒级失败」也照样先给进度条：结果由轮询那一次给出，一条只让人看一次
    assert 'hx-trigger="every' in response.text
    assert "抓取失败" not in response.text
    html = wait_for_fetch(client)
    assert "登录失败" in html
    assert PASSWORD not in html                    # 报错也不能把密码印到页面上
    # 上报给宿主的日志同样只见过打码之后的消息
    assert PASSWORD not in "\n".join(plugin_log(settings_with_repo_plugins))


def test_favorites_ratings_import_as_scores(
    client: TestClient, settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """私人收藏里的 userRating 就是本人打分，导入后应该变成条目的评分。"""
    install_fake_network(monkeypatch, FAVORITES_PAGES, require_bearer=TOKEN)
    client.post(f"/plugins/{PLUGIN_ID}", data=favorites_form(action="fetch_import", pages="1"))
    html = wait_for_fetch(client)
    assert "导入到 OmniShelf" in html
    assert "新增 2" in html
    assert "条带你打过的分" in html

    session.expire_all()
    items = {item.title: item for item in repo.list_items(session, kind="asmr")}
    assert items["合成作品一"].rating == 10.0        # 5 星 ×2
    assert items["合成作品二"].rating == 8.0         # 4 星 ×2
    assert items["合成作品一"].notes == "合成评价：很顶"
    assert favorites_snapshot(settings_with_repo_plugins)["fetch_info"]["new_count"] == 2


def test_favorites_target_is_remembered_and_switch_can_turn_off(
    client: TestClient, settings_with_repo_plugins: Settings
) -> None:
    config_path = data_dir(settings_with_repo_plugins) / "config.toml"
    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "base_url": DEFAULT_BASE_URL,
        "playlist_id": PLAYLIST, "has_favorites": "1", "favorites": "on",
        "authorization": TOKEN, "username": USER, "password": PASSWORD,
    })
    text = config_path.read_text(encoding="utf-8")
    assert 'target = "favorites"' in text
    assert PASSWORD not in text

    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "base_url": DEFAULT_BASE_URL,
        "playlist_id": PLAYLIST, "has_favorites": "1",
    })
    assert 'target = "playlist"' in config_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------- 命令行


def test_cli_command_is_registered(manager: PluginManager) -> None:
    manager.load_all()
    commands = {item.name: item for item in manager.commands(PLUGIN_ID)}
    assert "fetch_import" in commands
    for param in ("playlist_id", "pages", "proxy", "base_url", "page_size",
                  "delay", "timeout", "incremental", "dry_run",
                  "target", "authorization", "username", "password"):
        assert param in commands["fetch_import"].params, param
    # 起始页交给页码范围（12-），单次页数上限已删除
    assert "start_page" not in commands["fetch_import"].params
    assert "max_pages" not in commands["fetch_import"].params


def test_cli_command_fetches_and_imports(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    manager.load_all()
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json",
                                       2: "asmr_playlist_page2.json"})
    output = manager.run_command(
        PLUGIN_ID, "fetch_import",
        ["--playlist-id", PLAYLIST, "--pages", "0", "--delay", "0", "--no-incremental"],
    )
    assert isinstance(output, str)
    assert "抓取完成" in output
    assert "新增 4" in output
    session.expire_all()
    assert len(repo.list_items(session, kind="asmr")) == 4


def test_cli_command_dry_run_only_fetches(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    manager.load_all()
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json"})
    output = manager.run_command(
        PLUGIN_ID, "fetch_import",
        ["--playlist-id", PLAYLIST, "--pages", "1", "--dry-run"],
    )
    assert "已跳过（--dry-run）" in output
    session.expire_all()
    assert repo.list_items(session, kind="asmr") == []


def test_cli_run_writes_into_the_same_host_log(
    manager: PluginManager, settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """命令行模式跑插件：日志和 serve 落在同一个文件里，宿主那一行和插件那一行都在。"""
    manager.load_all()
    install_fake_network(monkeypatch, {1: "asmr_playlist_page1.json"})
    manager.run_command(
        PLUGIN_ID, "fetch_import",
        ["--playlist-id", PLAYLIST, "--pages", "1", "--dry-run"],
    )
    lines = plugin_log(settings_with_repo_plugins)
    assert any("| 宿主 | 加载完成" in line for line in lines)
    assert any("开始抓取" in line for line in lines)
    assert any("p1 抓到" in line for line in lines)


def test_cli_command_requires_a_playlist_id(manager: PluginManager) -> None:
    from omnishelf.plugins.manager import PluginError
    manager.load_all()
    with pytest.raises(PluginError):
        manager.run_command(PLUGIN_ID, "fetch_import", [])


def test_cli_command_fetches_favorites(manager: PluginManager, monkeypatch: pytest.MonkeyPatch) -> None:
    manager.load_all()
    install_fake_network(monkeypatch, FAVORITES_PAGES, require_bearer=TOKEN)
    output = manager.run_command(
        PLUGIN_ID, "fetch_import",
        ["--target", "favorites", "--authorization", TOKEN,
         "--pages", "1", "--delay", "0", "--dry-run"],
    )
    assert isinstance(output, str)
    assert "抓取完成：私人收藏 my_favorites" in output
    assert f"认证      用 authorization 里的令牌认证" in output
    assert "已跳过（--dry-run）" in output


def test_cli_command_favorites_needs_a_credential(manager: PluginManager) -> None:
    from omnishelf.plugins.manager import PluginError

    manager.load_all()
    with pytest.raises(PluginError):
        manager.run_command(PLUGIN_ID, "fetch_import", ["--target", "favorites"])
