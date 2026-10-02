"""copymanga_fetch 插件：同步（增量/全量）、认证、落盘、导入链路、随插件分发的工具包。

抓取本身在 ``plugins/copymanga_fetch/copymanga/``（原样复制的工具包）里，所以这里测的是
「插件那一层怎么用它」：表单 → 配置 → 同步 → 状态库/导入文件 → importer ``copymanga`` 入库。

测试**不出网**：把 ``urllib.request.urlopen`` 换成假站点，按 ``offset``/``limit`` 切片返回
``tests/plugin_fixtures/copymanga_collect_*.json``（**合成数据**：假 id、假域名，没有任何真实收藏）。
凭据也全是合成的，只用来断言「令牌会留下、密码不会」。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
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
from omnishelf.plugins.manager import STATE_LOADED, PluginManager
from omnishelf.web.app import create_app

PLUGIN_ID = "copymanga_fetch"
FIXTURES = Path(__file__).parent / "plugin_fixtures"
REPO_PLUGINS = repo_root() / "plugins"
PLUGIN_DIR = REPO_PLUGINS / PLUGIN_ID
VENDOR_DIR = PLUGIN_DIR / "copymanga"
DEFAULT_HOST = "https://copy4000.com"

#: 合成凭据：只用来断言请求头和「谁落盘谁不落盘」，别换成真实 token / 账号密码
TOKEN = "synthetic-token-abcdef0123456789"
NEW_TOKEN = "synthetic-token-rotated-9876543210"
STALE_TOKEN = "synthetic-token-expired-1111111111"
USER = "synthetic-user"
PASSWORD = "synthetic-password"

#: 假站上的书架：漫画 6 条（收藏 id 3001~3006，越新越大）
COMICS = "copymanga_collect_comics.json"
COMICS_NEW = "copymanga_collect_comics_new.json"        # 头部多了一条 3007
COMICS_SHORT = "copymanga_collect_comics_short.json"    # 少了最旧的 3001
COMIC_UUID = "11111111-1111-4111-8111-000000003006"


# --------------------------------------------------------------------- 假站点


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


def unauthorized(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, 401, "Unauthorized", {},                                   # type: ignore[arg-type]
        io.BytesIO(b'{"code":401,"message":"token invalid"}'),
    )


def rows_of(name: str) -> list[dict[str, Any]]:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return list(payload["results"]["list"])


def query_of(url: str) -> dict[str, str]:
    parts = urllib.parse.parse_qs(url.split("?", 1)[1]) if "?" in url else {}
    return {key: values[0] for key, values in parts.items()}


def install_fake_network(
    monkeypatch: pytest.MonkeyPatch,
    *,
    comics: str | None = COMICS,
    valid_token: str = TOKEN,
    login_token: str = "",
    boom: str = "",
    log: list[dict[str, str]] | None = None,
    before_request: Any = None,
) -> list[str]:
    """把假站点接上，返回「请求过的 URL」。

    ``comics`` 是书架数据（``None`` = 这个来源没有，接口直接回空）；動畫那个来源故意留空，
    这样万一插件去碰它就是空手而归，测试能直接看出来；
    ``valid_token`` 是唯一认的令牌，其余一律 401；``login_token`` 给了才认账号密码登录，
    登录要按工具的协议校验 ``base64(密码-salt)``，对不上就回「账号或密码错误」；
    ``boom`` 非空时收藏接口连不上（验证「接口挂了不炸页面」）；
    ``before_request(url)`` 在每个请求前调用一次（进度条测试用它卡住线程）。
    """
    calls: list[str] = []
    shelf = {"comics": rows_of(comics) if comics else [], "cartoons": []}

    def serve(request: Any) -> FakeResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        calls.append(url)
        method = request.get_method() if hasattr(request, "get_method") else "GET"
        authorization = request.get_header("Authorization") or ""
        body = (getattr(request, "data", None) or b"").decode("utf-8", "replace")
        if before_request is not None:
            before_request(url)
        if log is not None:
            log.append({"url": url, "method": method,
                        "authorization": authorization, "body": body})
        path = url.split("?", 1)[0]

        if path.endswith("/api/kb/web/login") or path.endswith("/api/v1/login"):
            if not login_token:
                return FakeResponse(url, json.dumps(
                    {"code": 404, "message": "登录接口没准备"}, ensure_ascii=False))
            sent = urllib.parse.parse_qs(body)
            plain = ""
            try:
                plain = base64.b64decode((sent.get("password") or [""])[0]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                plain = ""
            ok = ((sent.get("username") or [""])[0] == USER
                  and plain == f"{PASSWORD}-{(sent.get('salt') or [''])[0]}")
            if not ok:
                return FakeResponse(url, json.dumps(
                    {"code": 400, "message": "账号或密码错误"}, ensure_ascii=False))
            return FakeResponse(url, json.dumps(
                {"code": 200, "message": "登录成功", "results": {"token": login_token}},
                ensure_ascii=False))

        if authorization != f"Token {valid_token}":
            raise unauthorized(url)

        if path.endswith("/api/v2/web/user/info"):
            return FakeResponse(url, json.dumps({
                "code": 200, "message": "请求成功",
                "results": {"user_id": 900001, "username": USER, "nickname": "合成账号",
                            "avatar": "https://img.synthetic.invalid/avatar.png",
                            "datetime_created": "2024-01-01"},
            }, ensure_ascii=False))

        match = re.search(r"/api/v3/member/collect/(comics|cartoons)$", path)
        if match is None:
            raise urllib.error.HTTPError(
                url, 404, "Not Found", {},                              # type: ignore[arg-type]
                io.BytesIO(b'{"code":404,"message":"no such api"}'),
            )
        if boom:
            raise urllib.error.URLError(boom)
        params = query_of(url)
        limit = int(params.get("limit") or 100)
        offset = int(params.get("offset") or 0)
        rows = shelf[match.group(1)][offset:offset + limit]
        return FakeResponse(url, json.dumps({
            "code": 200, "message": "请求成功",
            "results": {"list": rows, "total": len(shelf[match.group(1)]),
                        "limit": limit, "offset": offset},
        }, ensure_ascii=False))

    def fake_urlopen(request: Any, timeout: float | None = None, **kwargs: Any) -> FakeResponse:
        return serve(request)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda *_: None)        # 限速/退避不能真的睡
    return calls


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
    )


def data_dir(settings: Settings) -> Path:
    return settings.plugins_data_dir / PLUGIN_ID


def store_file(settings: Settings) -> Path:
    return data_dir(settings) / "copymanga_favorites.json"


def import_file(settings: Settings) -> Path:
    return data_dir(settings) / "copymanga_import.json"


def config_file(settings: Settings) -> Path:
    return data_dir(settings) / "config.toml"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def plugin_log(settings: Settings, plugin_id: str = PLUGIN_ID) -> list[str]:
    """宿主统一收集的插件日志（``data/logs/plugins/<id>.log``）里的行。"""
    path = settings.plugin_logs_dir / f"{plugin_id}.log"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def fetch_form(**overrides: Any) -> dict[str, Any]:
    """页面上「同步」那张表的默认提交值（复选框默认不勾）。

    ``page_size`` 故意填 3：6 条漫画就会真的走一遍翻页。
    值传 ``None`` 表示「这个字段不提交」（复选框不勾就是这个效果）。
    """
    form: dict[str, Any] = {
        "action": "fetch_import",
        "form": "fetch",
        "host": DEFAULT_HOST,
        "token": TOKEN,
        "username": "",
        "password": "",
        "mode": "incremental",
        "page_size": "3",
        "delay": "0",
        "timeout": "10",
        "has_raw": "1",
    }
    for key, value in overrides.items():
        if value is None:
            form.pop(key, None)
        else:
            form[key] = value
    return form


def wait_for_sync(client: TestClient, *, timeout: float = 30.0) -> str:
    """等后台同步结束，返回「看到结果」的那一页 HTML。

    同步跑在后台线程里，POST 的响应只有刚起步的进度条；结果要像浏览器那样
    反复 GET 插件页（走的正是进度条自己那条 hx-get 轮询）。
    """
    deadline = time.monotonic() + timeout
    html = ""
    while time.monotonic() < deadline:
        html = client.get(f"/plugins/{PLUGIN_ID}").text
        if "同步完成" in html or "同步失败" in html or "没能执行" in html:
            return html
        threading.Event().wait(0.02)        # 不用 time.sleep：本文件的假站点把它空转了
    block = re.search(r'id="cm-fetch-status"(.*?)</article>', html, re.S)
    stuck = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block.group(1)))[:300] if block else ""
    raise AssertionError(f"后台同步没在 {timeout} 秒内结束，进度栏停在：{stuck or html[-200:]}")


def _load_plugin_module() -> Any:
    """把插件入口当模块加载（宿主的加载方式就是 exec 源码，这里等价）。"""
    path = PLUGIN_DIR / "plugin.py"
    spec = importlib.util.spec_from_file_location("copymanga_fetch_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module        # dataclass 要靠 sys.modules 找所属模块
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- 随插件分发的工具包


#: 上游 v2.0.0 的逐个文件哈希：**工具必须逐字节原样**（见 VENDOR.md）
VENDORED_SHA256 = {
    "copymanga/__init__.py": "5a3927b23de63b2d0088f18cd97b08595808e88798182692c000922c003790d9",
    "copymanga/__main__.py": "06cb7131c4d541259a13abe4e6b8e2bbb2d90dcf2ee7b5b8e8bc395280078531",
    "copymanga/cli.py": "82eb238ef178430aec93ac753c697da9879c57a06f4635be2ebdd7fa5fb6dd35",
    "copymanga/client.py": "c11362cf2256b6ef7f547fa881177f662e18b948a6e9adf37bbe16ff21d94f1e",
    "copymanga/config.py": "16edbb6ec9c1d0e54516d16790418393ae010be2b2f07e4740ff55c0bcc93609",
    "copymanga/errors.py": "dcf09c8f60668f8e4e467a105946b904881d719870d3c31643765a48263d11d3",
    "copymanga/exporter.py": "b12b7a8dd6fb92d695a97bf957203d80b619287cb42f26155fe38228aaad36ec",
    "copymanga/formats.py": "519d1fcd85ad81491f168553e83d6a71d67ea58a6cb2b49f4a40978e3b9d7842",
    "copymanga/models.py": "f33d5a7de85813c393eebad17ea14df47715e9b68221d408d66c0e98d59f24fb",
    "copymanga/store.py": "c81e43edb6fa2f5dfaf02e77ae0eea388f0d6be6d55f8cc9ff10833461ff9a32",
}


def test_vendored_tool_is_unmodified() -> None:
    """红线：抓取的活儿由那份工具包干，但**一行都不许改**。

    需要工具配合什么，就在插件里绕（进度靠套一层 client，令牌自己存）；
    真到了非改工具不可的地步，停下来向用户提需求，别偷偷动。
    """
    present = sorted(str(path.relative_to(PLUGIN_DIR)) for path in VENDOR_DIR.glob("*.py"))
    assert present == sorted(VENDORED_SHA256), "工具包的文件增删了：先确认上游是不是真改了"
    for relative, expected in VENDORED_SHA256.items():
        path = PLUGIN_DIR / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == expected, f"{relative} 被改动过（期望 {expected[:12]}…，实际 {digest[:12]}…）"


def test_vendored_tool_ships_no_credentials_or_real_data() -> None:
    """上游目录里的 config.json / data/ / README / docs 都带真实凭据和真实收藏：**不带进来**。

    （插件自己的 README.md 是写给用户看的，和上游那份无关，所以只查工具包目录。）
    """
    stray = sorted(str(path.relative_to(VENDOR_DIR)) for path in VENDOR_DIR.rglob("*")
                   if path.is_file() and "__pycache__" not in path.parts)
    assert [path for path in stray if not path.endswith(".py")] == []
    for name in ("config.json", "data", "README.md", "docs"):
        assert not (VENDOR_DIR / name).exists(), name
    for name in ("config.json", "data"):
        assert not (PLUGIN_DIR / name).exists(), name
    text = (PLUGIN_DIR / "VENDOR.md").read_text(encoding="utf-8")
    assert "不许改动" in text and "config.json" in text


def test_plugin_only_uses_the_sdk() -> None:
    """规范：插件只用标准库 + SDK + 自己目录里那份工具包，不碰宿主内部模块。"""
    source = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.M)
    assert imports, "总得 import 点什么"
    allowed_std = {"__future__", "json", "math", "os", "sys", "threading", "time",
                   "urllib", "uuid", "dataclasses", "pathlib", "typing"}
    for name in imports:
        if name == "omnishelf.plugins.api":
            continue
        if name == "copymanga" or name.startswith("copymanga."):
            continue                                    # 随插件分发的工具包（用户拍板的）
        assert not name.startswith("omnishelf"), f"插件不该 import 宿主内部模块: {name}"
        assert name.split(".")[0] in allowed_std, name
    assert "from omnishelf.plugins.api import Plugin" in source
    # 入口文件是被 exec 的，包之间的相对导入要先把插件目录挂上 sys.path
    assert source.index("sys.path.insert") < source.index("from copymanga import")


def test_metadata_and_files_contract() -> None:
    text = (PLUGIN_DIR / "metadata.toml").read_text(encoding="utf-8")
    for key in ("id", "title", "version", "description", "entry", "requires"):
        assert re.search(rf"^{key}\s*=", text, re.M), key
    assert 'id = "copymanga_fetch"' in text
    assert '"import"' in text and '"query"' in text
    for path in (PLUGIN_DIR / "templates" / "page.html", PLUGIN_DIR / "README.md",
                 PLUGIN_DIR / "VENDOR.md"):
        assert path.is_file(), path


# --------------------------------------------------------------------- 纯函数


def test_normalize_host_and_token() -> None:
    module = _load_plugin_module()
    assert module.normalize_host(DEFAULT_HOST) == DEFAULT_HOST
    # 贴整条书架页地址（用户最常这么干）只取域名
    assert module.normalize_host("https://copy4000.com/web/person/shujia?tab=1") == DEFAULT_HOST
    assert module.normalize_host("copy4000.com") == DEFAULT_HOST
    assert module.normalize_host("ftp://copy4000.com") == ""
    assert module.normalize_host("") == ""
    assert module.normalize_host("https://a b.com") == ""          # 带空格的一律不当地址

    assert module.normalize_token(f"Token {TOKEN}") == TOKEN
    assert module.normalize_token(f"token={TOKEN};") == TOKEN
    assert module.normalize_token(f'"{TOKEN}"') == TOKEN
    # 连着 Cookie 抄下来的样子：前缀、引号、结尾分号一层套一层，得剥干净
    assert module.normalize_token(f'token="{TOKEN}";') == TOKEN
    assert module.normalize_token(f'Token "{TOKEN}";') == TOKEN
    assert module.normalize_token(TOKEN) == TOKEN
    assert module.normalize_token("") == ""


def test_mode_and_clamping() -> None:
    module = _load_plugin_module()
    assert module.normalize_mode("full") == "full"
    assert module.normalize_mode("FULL") == "full"
    assert module.normalize_mode("") == "incremental"

    config = module.effective_config(module._FakeContext({
        "host": "ftp://nope", "token": f"Token {TOKEN}", "mode": "whatever",
        "page_size": "9999", "delay": "99", "max_pages": "-3", "timeout": "1",
    }))
    assert config["host"] == DEFAULT_HOST               # 坏地址退回默认值
    assert config["token"] == TOKEN
    assert config["mode"] == "incremental"
    assert config["page_size"] == 500                   # 夹到接口上限
    assert config["delay"] == 10.0
    assert config["timeout"] == 5
    # 陈旧配置里的多余键（以前的「動畫」/额外导出/单次页数上限）会被丢掉，不会再传下去
    assert "cartoons" not in config and "formats" not in config
    assert "max_pages" not in config                    # 没有单次页数上限这回事


def test_redact_masks_secrets() -> None:
    module = _load_plugin_module()
    message = f"请求 https://x/{TOKEN} 失败，密码 {PASSWORD}"
    masked = module.redact(message, TOKEN, PASSWORD)
    assert TOKEN not in masked and PASSWORD not in masked
    assert "***" in masked
    assert module.redact("短", "ab") == "短"            # 太短的值不抹，免得把普通文本打满星


def test_build_import_payload_skips_removed(tmp_path: Path) -> None:
    """导入文件只放还没取消收藏的，也不带接口原始返回。"""
    module = _load_plugin_module()
    path = tmp_path / "store.json"
    path.write_text(json.dumps({"items": [
        {"collect_id": "3002", "name": "老", "shelf_position": 1, "raw": {"接口原始返回": "占位"}},
        {"collect_id": "3003", "name": "新", "shelf_position": 0},
        {"collect_id": "3001", "name": "已取消", "shelf_position": 2, "removed": True},
    ]}, ensure_ascii=False), encoding="utf-8")
    store = module.JsonStore(str(path)).load()
    assert len(store) == 3

    payload = module.build_import_payload(store, host=DEFAULT_HOST, fetched_at="2026-10-13 10:00:00")
    assert [item["collect_id"] for item in payload["items"]] == ["3003", "3002"]
    assert payload["total"] == 2
    assert payload["source"] == f"{DEFAULT_HOST}/web/person/shujia"
    assert not any("raw" in item for item in payload["items"])


# --------------------------------------------------------------------- 页面 / 配置


def test_plugin_page_renders(client: TestClient) -> None:
    response = client.get(f"/plugins/{PLUGIN_ID}")
    assert response.status_code == 200
    body = response.text
    assert "拷贝漫画书架抓取" in body
    assert PLUGIN_ID in body
    assert f'action="/plugins/{PLUGIN_ID}"' in body
    assert "站点地址" in body and "每页条数" in body
    assert 'name="action" value="fetch_import"' in body
    assert 'name="token"' in body
    assert 'name="username"' in body
    assert 'type="password" name="password"' in body
    # 复选框配了同名标记（不勾时要能区分「没勾」和「这张表不管这个开关」）
    assert 'name="has_raw"' in body
    # 動畫只能说明一句，不能给选项；额外导出的那一整套都撤了
    assert "只抓漫画" in body
    assert 'name="cartoons"' not in body and 'name="has_cartoons"' not in body
    assert 'name="export_' not in body and 'name="has_exports"' not in body
    assert "额外导出" not in body and "openpyxl" not in body


def test_plugin_appears_in_list(client: TestClient) -> None:
    body = client.get("/plugins").text
    assert "拷贝漫画书架抓取" in body
    assert "已加载" in body


def test_save_config_persists_but_never_the_password(
    client: TestClient, settings_with_repo_plugins: Settings
) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": "https://copy4000.com/web/person/shujia",
        "token": f"Token {TOKEN}", "username": USER, "password": PASSWORD,
        "mode": "full", "page_size": "200", "max_pages": "20", "delay": "1.5",
        "timeout": "30",
        # 浏览器提交表单时，那几个隐藏标记和复选框是一起发出去的
        "has_raw": "1", "include_raw": "on",
    })
    assert response.status_code == 200
    assert "默认配置已保存" in response.text
    text = config_file(settings_with_repo_plugins).read_text(encoding="utf-8")
    assert f'host = "{DEFAULT_HOST}"' in text            # 书架页地址只留下域名
    assert f'token = "{TOKEN}"' in text                 # 令牌会存（和 Cookie 一个性质）
    assert f'username = "{USER}"' in text
    assert PASSWORD not in text                         # 密码不落盘
    assert 'mode = "full"' in text
    assert "include_raw = true" in text
    assert "cartoons" not in text and "formats" not in text
    assert "max_pages" not in text                      # 表单里塞的旧键不会落进配置


def test_save_config_rejects_bad_host(client: TestClient) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": "ftp://nope"})
    assert "站点地址要写成" in response.text
    assert "默认配置已保存" not in response.text


def test_unchecking_a_box_really_turns_it_off(
    client: TestClient, settings_with_repo_plugins: Settings
) -> None:
    """勾上存一次、再取消勾存一次：配置里得跟着变（同名标记的那套逻辑不能失灵）。"""
    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": DEFAULT_HOST,
        "has_raw": "1", "include_raw": "on"})
    assert "include_raw = true" in config_file(settings_with_repo_plugins).read_text(encoding="utf-8")

    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": DEFAULT_HOST, "has_raw": "1"})
    assert "include_raw = false" in config_file(settings_with_repo_plugins).read_text(encoding="utf-8")


def test_blank_token_box_keeps_the_stored_one(
    client: TestClient, settings_with_repo_plugins: Settings
) -> None:
    """令牌框空着提交不等于「把令牌忘掉」：配置里那个还在（密码登录换的就是它）。"""
    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": DEFAULT_HOST, "token": TOKEN})
    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": DEFAULT_HOST, "token": ""})
    assert f'token = "{TOKEN}"' in config_file(settings_with_repo_plugins).read_text(encoding="utf-8")


def test_broken_config_still_renders(client: TestClient, settings_with_repo_plugins: Settings) -> None:
    """配置被手改坏了（非法地址/类型）不能把页面弄炸：夹回默认值并照常显示。"""
    config_file(settings_with_repo_plugins).parent.mkdir(parents=True, exist_ok=True)
    config_file(settings_with_repo_plugins).write_text(
        'host = "ftp://nope"\npage_size = "abc"\nmode = 42\nmax_pages = -1\n',
        encoding="utf-8",
    )
    manager = client.app.state.plugin_manager            # type: ignore[attr-defined]
    assert "拷贝漫画书架抓取" in client.get(f"/plugins/{PLUGIN_ID}").text
    manager.reload(PLUGIN_ID)                        # reload 会重读那份坏配置
    assert manager.status(PLUGIN_ID).state == STATE_LOADED
    body = client.get(f"/plugins/{PLUGIN_ID}").text
    assert "拷贝漫画书架抓取" in body
    assert re.search(r'name="page_size"[^>]*value="100"', body)
    assert 'name="max_pages"' not in body              # 那栏已经从页面上拿掉了


def test_start_logs_without_touching_the_network(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """插件启动只是读本地配置和状态库，一帧网络都不碰（宿主运行时不出网这条红线）。"""
    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("start() 不该出网")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    manager.load_all()
    status = manager.status(PLUGIN_ID)
    assert status.state == STATE_LOADED, status.error
    loaded = manager.get(PLUGIN_ID)
    assert loaded is not None
    logs = "\n".join(loaded.context.logs)
    assert DEFAULT_HOST in logs and "本地状态库 0 条" in logs


# --------------------------------------------------------------------- 同步


def collect_calls(calls: list[str]) -> list[str]:
    """只看收藏接口的请求（登录/用户信息那些不算翻页）。"""
    return [url for url in calls if "/api/v3/member/collect/" in url]


def test_full_sync_writes_store_and_import_file(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_fake_network(monkeypatch, comics=COMICS)
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    assert response.status_code == 200
    assert "正在同步" in response.text                   # POST 只给进度条，结果等后台跑完
    html = wait_for_sync(client)

    assert "同步完成" in html
    assert "合成账号" in html
    assert "新增 6" in html
    assert "max-pages" not in html
    # 漫画两页，加上 free_type=2 那一组「和上一组重复」的探测
    assert len(collect_calls(calls)) == 3, calls

    assert DEFAULT_HOST in html

    store = read_json(store_file(settings_with_repo_plugins))
    assert store["total"] == 6 and store["active_total"] == 6
    assert store["schema_version"] == 3
    assert store["user"]["nickname"] == "合成账号"

    payload = read_json(import_file(settings_with_repo_plugins))
    assert payload["total"] == 6
    assert payload["host"] == DEFAULT_HOST
    assert payload["source"] == f"{DEFAULT_HOST}/web/person/shujia"
    assert {item["name"] for item in payload["items"]} == {
        "合成漫画一", "合成漫画二", "合成漫画三", "合成漫画四", "合成漫画五", "合成漫画六",
    }
    assert not any("raw" in item for item in payload["items"])   # 接口原始返回不进导入文件

    meta = read_json(data_dir(settings_with_repo_plugins) / "copymanga_fetch.meta.json")
    assert meta["added"] == 6 and meta["mode"] == "full"
    # meta 只管「上次什么样」的状态；逐页流水在宿主日志里，不在这里再存一份每次覆盖的副本
    assert "notes" not in meta
    assert "本地状态库" in "\n".join(plugin_log(settings_with_repo_plugins))
    assert "本地已有的数据" in html and "copymanga_import.json" in html


def test_never_fetches_cartoons(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """動畫在库里没有对应类型，插件就不该去问这个接口（表单里也没这个开关）。"""
    calls = install_fake_network(monkeypatch, comics=COMICS)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full", cartoons="on"))
    assert "新增 6" in wait_for_sync(client)
    assert not any("/cartoons" in url for url in calls), "不该抓動畫"
    payload = read_json(import_file(settings_with_repo_plugins))
    assert len(payload["items"]) == 6


def test_connection_settings_are_remembered_after_a_sync(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """站点地址/令牌/方式 是「填一次就一直用」的，同步一次就该落盘。"""
    install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)
    text = config_file(settings_with_repo_plugins).read_text(encoding="utf-8")
    assert f'host = "{DEFAULT_HOST}"' in text
    assert f'token = "{TOKEN}"' in text
    assert 'mode = "full"' in text and PASSWORD not in text


def test_incremental_stops_at_the_watermark(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先全量把书架落下来，之后增量只翻到「整页都不比水位线新」就收手。"""
    install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)

    calls = install_fake_network(monkeypatch)            # 重新计数
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="incremental", page_size="6"))
    html = wait_for_sync(client)
    assert len(collect_calls(calls)) == 1, "第一页整页都不高于水位线就该停"
    assert "水位线" in html
    assert "watermark-reached" in html
    assert read_json(import_file(settings_with_repo_plugins))["total"] == 6

    # 增量不会把已抓全的状态库清空
    assert read_json(store_file(settings_with_repo_plugins))["total"] == 6


def test_incremental_picks_up_a_new_collect(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)

    calls = install_fake_network(monkeypatch, comics=COMICS_NEW)     # 书架上加了一条新的，排在头部
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="incremental"))
    html = wait_for_sync(client)
    assert "新增 1" in html and "未变化 5" in html
    assert len(collect_calls(calls)) == 2, "翻到第二页整页都旧就停"
    ids = [item["collect_id"] for item in read_json(import_file(settings_with_repo_plugins))["items"]]
    assert "3007" in ids and len(ids) == 7


def test_full_sync_marks_uncollected_but_never_deletes(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量时发现「已经取消收藏」的条目：状态库里打标记，导入文件里不放它，库里那条也不删。"""
    install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    wait_for_sync(client)
    assert len(read_json(import_file(settings_with_repo_plugins))["items"]) == 6

    install_fake_network(monkeypatch, comics=COMICS_SHORT)           # 3001 被移出书架了
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    html = wait_for_sync(client)
    assert "同步完成" in html and "已取消收藏 1" in html
    assert "不会" in html and "从 OmniShelf 里删掉" in html

    store = read_json(store_file(settings_with_repo_plugins))
    assert store["total"] == 6 and store["removed_total"] == 1
    removed = [item for item in store["items"] if item["collect_id"] == "3001"]
    assert removed and removed[0]["removed"] is True

    payload = read_json(import_file(settings_with_repo_plugins))
    assert payload["total"] == 5
    assert "3001" not in [item["collect_id"] for item in payload["items"]]


def test_full_sync_walks_every_page_without_a_budget(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量就得翻到末页：这里以前会被「单次页数上限」在半路掐断，静默少一批收藏。"""
    calls = install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full", page_size="1"))
    html = wait_for_sync(client)
    # 6 条一页一条 = 6 次，加上 free_type=2 那一组「和上一组重复」的探测
    assert len(collect_calls(calls)) == 7, calls
    assert "页数上限" not in html and "max-pages" not in html
    payload = read_json(import_file(settings_with_repo_plugins))
    assert payload["total"] == 6                                     # 一条都没少


def test_include_raw_only_bloats_the_store(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(include_raw="on"))
    wait_for_sync(client)
    store = read_json(store_file(settings_with_repo_plugins))
    assert "raw" in store["items"][0]
    assert not any("raw" in item for item in read_json(import_file(settings_with_repo_plugins))["items"])


def test_request_failure_is_shown_not_raised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口挂了/连不上：页面要出提示，不能 500。"""
    install_fake_network(monkeypatch, boom="connection refused")
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    assert response.status_code == 200
    assert "请求失败" in wait_for_sync(client)


# --------------------------------------------------------------------- 认证


def test_missing_credential_is_refused_before_starting(client: TestClient) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(token="", username="", password=""))
    assert "要先认证" in response.text
    assert "正在同步" not in response.text               # 别起一个注定失败的后台任务


def test_login_without_token_keeps_the_token_not_the_password(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没令牌就用账号密码登录；登录那一刻换到的令牌当场存进配置，密码永远不落盘。"""
    log: list[dict[str, str]] = []
    calls = install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token=NEW_TOKEN, log=log)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(token="", username=USER, password=PASSWORD))
    html = wait_for_sync(client)

    assert "同步完成" in html
    assert "登录换的 token" in html
    assert "/api/kb/web/login" in calls[0], "没令牌就该先登录，而不是拿空令牌去撞接口"
    gets = [row for row in log if "/collect/" in row["url"]]
    assert {row["authorization"] for row in gets} == {f"Token {NEW_TOKEN}"}

    text = config_file(settings_with_repo_plugins).read_text(encoding="utf-8")
    assert f'token = "{NEW_TOKEN}"' in text
    assert f'username = "{USER}"' in text
    assert PASSWORD not in text
    page = client.get(f"/plugins/{PLUGIN_ID}").text
    assert PASSWORD not in page


def test_stored_token_survives_a_blank_token_box(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登录换到的令牌得真的「存下来」：下次令牌框留空也能直接用它，不用再登一次。

    用户看到的就是这一步——第一次密码登录之后再点同步，如果还被要求认证（或者令牌框
    一直空着），那就等于没保存。
    """
    install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token=NEW_TOKEN)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(token="", username=USER, password=PASSWORD))
    wait_for_sync(client)
    text = config_file(settings_with_repo_plugins).read_text(encoding="utf-8")
    assert f'token = "{NEW_TOKEN}"' in text
    assert f'username = "{USER}"' in text and PASSWORD not in text

    # 拿手里那屏（令牌框还是空的）再点一次
    calls = install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token="")
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(token="", username="", password=""))
    html = wait_for_sync(client)
    assert "同步完成" in html, "表单留空时该用配置里存着的令牌"
    assert not any("/login" in url for url in calls), "有存好的令牌就不该再登录一次"
    assert f'token = "{NEW_TOKEN}"' in config_file(settings_with_repo_plugins).read_text(encoding="utf-8")


def test_token_is_saved_even_when_the_sync_fails(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登录一成功就存令牌：后面同步失败，这一次登录也不白做。"""
    install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token=NEW_TOKEN, boom="reset")
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(token="", username=USER, password=PASSWORD))
    assert "接口出错" in wait_for_sync(client)
    text = config_file(settings_with_repo_plugins).read_text(encoding="utf-8")
    assert f'token = "{NEW_TOKEN}"' in text and PASSWORD not in text


def test_expired_token_relogs_in(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先用手上的 token；它换不到用户信息就用账号密码重登一次，然后继续同步。"""
    log: list[dict[str, str]] = []
    install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token=NEW_TOKEN, log=log)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(
        token=f"Token {STALE_TOKEN}", username=USER, password=PASSWORD))
    html = wait_for_sync(client)

    assert "同步完成" in html
    assert len([row for row in log if row["url"].endswith("/api/kb/web/login")]) == 1
    auths = [row["authorization"] for row in log if "/collect/" in row["url"]]
    assert auths and set(auths) == {f"Token {NEW_TOKEN}"}


def test_login_failure_is_reported_and_password_stays_hidden(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登录接口不给令牌：失败要说清楚，但密码不能跟着报错印到页面上。"""
    install_fake_network(monkeypatch)                            # login_token 没准备
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(
        token="", username=USER, password=PASSWORD))
    # 这种「秒级失败」也照样先给进度条：结果由轮询那一次给出，一份报告只让人看一次
    assert 'hx-trigger="every' in response.text
    assert "同步失败" not in response.text
    html = wait_for_sync(client)
    assert "认证失败" in html
    assert PASSWORD not in html


# --------------------------------------------------------------------- 进度条


def test_progress_bar_polls_and_blocks_a_second_sync(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步跑在后台：POST 立刻回一栏进度条（自己轮询自己），没跑完之前不能再点。"""
    hold = threading.Event()
    requested = threading.Event()
    gated = {"done": False}

    def before_request(url: str) -> None:
        if "/collect/" in url and not gated["done"]:
            gated["done"] = True
            requested.set()
            hold.wait(10)

    install_fake_network(monkeypatch, before_request=before_request)
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    assert response.status_code == 200
    assert requested.wait(10), "后台线程要真的发出请求"

    body = response.text
    assert "正在同步" in body
    assert 'id="cm-fetch-status"' in body
    assert f'hx-get="/plugins/{PLUGIN_ID}"' in body
    assert 'hx-trigger="every' in body                    # 进度条自己定时轮询
    assert 'hx-select="#cm-fetch-status"' in body         # 只换这一栏，不套第二层页面

    busy = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    assert "已经有一个同步在跑了" in busy.text

    hold.set()
    assert "同步完成" in wait_for_sync(client)
    # 抓完这一栏就换成正式报告，轮询也随之停掉
    assert 'hx-trigger="every' not in client.get(f"/plugins/{PLUGIN_ID}").text


def test_worker_crash_still_ends_the_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台线程半路炸了也要把这一栏结掉——不然进度条一直转到超时（30 分钟）为止。"""
    install_fake_network(monkeypatch)
    client.get(f"/plugins/{PLUGIN_ID}")                    # 首个请求会把插件加载好
    loaded = client.app.state.plugin_manager.get(PLUGIN_ID)   # type: ignore[attr-defined]
    assert loaded is not None

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("同步跑到一半炸了")

    monkeypatch.setattr(loaded.module, "run_sync", crash)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    html = wait_for_sync(client, timeout=10)
    assert "同步线程意外退出" in html
    assert "同步跑到一半炸了" in html


# --------------------------------------------------------------------- 导入


def test_fetch_import_writes_into_the_database(
    client: TestClient, settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    install_fake_network(monkeypatch)
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    assert response.status_code == 200
    html = wait_for_sync(client)
    assert "导入到 OmniShelf" in html
    assert "新增 6" in html
    assert "copymanga" in html

    session.expire_all()
    items = {item.title: item for item in repo.list_items(session, kind="manga")}
    assert len(items) == 6

    first = items["合成漫画一"]
    assert first.creators == ["合成作者甲", "合成作者乙"]        # authors 本来就是数组
    assert set(repo.list_tags(session, first.id)) == {"熱血", "冒险"}
    assert first.extra["cover_url"] == "https://img.synthetic.invalid/cover/3006.jpg"
    assert first.extra["status_label"] == "連載中"
    assert first.extra["finished"] is False
    assert first.extra["latest_chapter"] == "第12話"
    assert first.extra["reading_progress"] == "第10話"
    assert first.extra["remote_updated_at"] == "2026-10-01"
    assert first.year is None and first.rating is None          # 这个来源没有年份和评分
    links = {link.source: link for link in repo.list_external_ids(session, first.id)}
    assert links["copymanga"].external_id == COMIC_UUID
    assert links["copymanga"].url == f"{DEFAULT_HOST}/comic/synthetic-comic-one"

    assert items["合成漫画二"].extra["finished"] is True          # 已完結
    assert items["合成漫画三"].extra["finished"] is True          # 短篇
    assert not items["合成漫画三"].extra.get("reading_progress")   # 没读过就不写
    assert items["合成漫画三"].creators == ["合成作者丙"]

    archive = settings_with_repo_plugins.imports_dir / "copymanga" / \
        import_file(settings_with_repo_plugins).name
    assert archive.is_file()                                     # 原始文件按来源存档


def test_fetch_import_is_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    assert "未变化 6" in wait_for_sync(client)
    session.expire_all()
    assert len(repo.list_items(session, kind="manga")) == 6


def test_plain_sync_does_not_touch_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    install_fake_network(monkeypatch)
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full", action="fetch"))
    assert response.status_code == 200
    assert "导入到 OmniShelf" not in wait_for_sync(client)
    session.expire_all()
    assert repo.list_items(session, kind="manga") == []


def test_import_failure_keeps_the_files(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步已经成功了，导入坏了不该把结果一起弄丢：页面提示失败，文件还在。"""
    install_fake_network(monkeypatch)
    from omnishelf.plugins import host as host_module

    def boom(self: Any, importer: str, path: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("假装 importer 坏了")

    monkeypatch.setattr(host_module.RepositoryHostAPI, "import_file", boom)
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    assert response.status_code == 200
    html = wait_for_sync(client)
    assert "导入失败" in html and "假装 importer 坏了" in html
    assert import_file(settings_with_repo_plugins).is_file()


# --------------------------------------------------------------------- 命令行


def test_cli_command_is_registered(manager: PluginManager) -> None:
    manager.load_all()
    commands = {item.name: item for item in manager.commands(PLUGIN_ID)}
    assert "fetch_import" in commands
    for param in ("mode", "host", "token", "username", "password", "page_size", "delay",
                  "timeout", "include_raw", "dry_run"):
        assert param in commands["fetch_import"].params, param
    # 動畫和额外导出的开关撤了；单次页数上限也一起去掉——全量就该翻到末页
    for gone in ("cartoons", "formats", "max_pages"):
        assert gone not in commands["fetch_import"].params, gone


def test_cli_command_syncs_and_imports(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    manager.load_all()
    install_fake_network(monkeypatch)
    output = manager.run_command(PLUGIN_ID, "fetch_import", [
        "--token", TOKEN, "--mode", "full", "--page-size", "3", "--delay", "0"])
    assert isinstance(output, str)
    assert "同步完成" in output
    assert "漫画" in output and "動畫" not in output
    assert "新增 6" in output
    session.expire_all()
    assert len(repo.list_items(session, kind="manga")) == 6


def test_cli_command_dry_run_only_syncs(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    manager.load_all()
    install_fake_network(monkeypatch)
    output = manager.run_command(PLUGIN_ID, "fetch_import", ["--token", TOKEN, "--dry-run"])
    assert "已跳过（--dry-run）" in output
    session.expire_all()
    assert repo.list_items(session, kind="manga") == []


def test_cli_command_needs_a_credential(manager: PluginManager) -> None:
    from omnishelf.plugins.manager import PluginError

    manager.load_all()
    with pytest.raises(PluginError):
        manager.run_command(PLUGIN_ID, "fetch_import", [])
