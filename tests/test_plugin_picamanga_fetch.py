"""picamanga_fetch 插件：同步（增量/全量）、认证、落盘、导入链路、随插件分发的工具包。

抓取本身在 ``plugins/picamanga_fetch/picamanga/``（由外部哔咔备份工具**重构**而来的包），
所以这里测的是「插件那一层怎么用它」：表单 → 配置 → 同步 → 状态库/导入文件 → importer
``picamanga`` 入库，外加这个包自己的几个关键约定（签名、翻页、增量边界判定）。

测试**不出网**：把 ``urllib.request.urlopen`` 和 ``build_opener`` 都换成假的（配了代理时客户端
走的是 ``build_opener``），按 ``page``/``page_size`` 切片返回
``tests/plugin_fixtures/picamanga_favourite*.json``（**合成数据**：假 id、假域名，没有任何真实收藏）。
忘了换的时候 ``tests/conftest.py`` 里那个兜底替身会直接报错，不会把请求发到真的哔咔接口上。
凭据也全是合成的，只用来断言「令牌会留下、口令不会」。
"""

from __future__ import annotations

import importlib
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

PLUGIN_ID = "picamanga_fetch"
FIXTURES = Path(__file__).parent / "plugin_fixtures"
REPO_PLUGINS = repo_root() / "plugins"
PLUGIN_DIR = REPO_PLUGINS / PLUGIN_ID
PACKAGE_DIR = PLUGIN_DIR / "picamanga"
DEFAULT_HOST = "https://picaapi.picacomic.com"

#: 合成凭据：只用来断言请求头和「谁落盘谁不落盘」，别换成真实 token / 邮箱口令
TOKEN = "synthetic-token-abcdef0123456789"
NEW_TOKEN = "synthetic-token-rotated-9876543210"
STALE_TOKEN = "synthetic-token-expired-1111111111"
EMAIL = "synthetic-user@example.invalid"
PASSWORD = "synthetic-password"

#: 合成代理地址：只用来断言「请求真的经过了它」，不是任何人在用的端口
PROXY = "http://proxy.invalid:10809"

#: 假站上的收藏夹：漫画 6 条（编号 3001~3006，新收藏排在头部）
FAVOURITE = "picamanga_favourite.json"
FAVOURITE_NEW = "picamanga_favourite_new.json"           # 头部多了一条 3007
FAVOURITE_SHORT = "picamanga_favourite_short.json"       # 最旧的 3001 被移出收藏夹
FAVOURITE_REFAV = "picamanga_favourite_refavourite.json"  # 重新收藏 + 三条新的跟在后面
FAVOURITE_BURIED = "picamanga_favourite_buried.json"     # 新收藏掉在尾部（排序前提坏了）
COMIC_ID_1 = "64a000000000000000003001"
COMIC_ID_6 = "64a000000000000000003006"
COMIC_ID_10 = "64a000000000000000003010"      # 只在 BURIED 那份里出现
#: 接口单页固定 20 条（``limit`` 参数会被服务端忽略），假站默认照这个切
API_PAGE_SIZE = 20


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


def _load_plugin_module() -> Any:
    """把插件入口当模块加载（宿主的加载方式就是 exec 源码，这里等价）。"""
    path = PLUGIN_DIR / "plugin.py"
    spec = importlib.util.spec_from_file_location("picamanga_fetch_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module        # dataclass 要靠 sys.modules 找所属模块
    spec.loader.exec_module(module)
    return module


#: exec 一次就够（它会把插件目录挂进 sys.path，之后 import 那个包就找得到）
_PACKAGE: dict[str, Any] = {}


def _package(name: str) -> Any:
    if not _PACKAGE:
        _load_plugin_module()
    key = f"picamanga.{name}"
    if key not in _PACKAGE:
        _PACKAGE[key] = importlib.import_module(key)
    return _PACKAGE[key]


def docs_of(name: str) -> list[dict[str, Any]]:
    """夹具里的一条条漫画（假站按页切片用）。"""
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return list(payload["data"]["comics"]["docs"])


def query_of(url: str) -> dict[str, str]:
    parts = urllib.parse.parse_qs(url.split("?", 1)[1]) if "?" in url else {}
    return {key: values[0] for key, values in parts.items()}


def expected_signature(method: str, url: str, timestamp: str) -> str:
    """按工具的协议重算签名：**去掉前导斜杠**的路径（带查询串）参与计算。

    带着 ``/`` 去签，哔咔服务端不会报错，而是回一个假的 ``{"code":200,"message":"success"}``
    空响应 —— 那种 bug 极难查，所以假站点把每个请求的签名都重算一遍。
    """
    parts = urllib.parse.urlsplit(url)
    path = parts.path.lstrip("/")
    if parts.query:
        path = f"{path}?{parts.query}"
    return _package("client").PicamangaClient.sign(method, path, timestamp)


def install_fake_network(
    monkeypatch: pytest.MonkeyPatch,
    *,
    shelf: str | None = FAVOURITE,
    valid_token: str = TOKEN,
    login_token: str = "",
    page_size: int = API_PAGE_SIZE,
    fail_pages: tuple[int, ...] = (),
    boom: str = "",
    log: list[dict[str, Any]] | None = None,
    proxy_trace: list[dict[str, str]] | None = None,
    before_request: Any = None,
) -> list[str]:
    """把假站点接上，返回「请求过的 URL」。

    ``shelf`` 是收藏夹数据（``None`` = 空收藏夹）；
    ``valid_token`` 是唯一认的令牌，其余一律 401；``login_token`` 给了才认邮箱口令登录，
    登录要按协议校验请求体里的 ``email``/``password``，对不上就不给令牌；
    ``page_size`` 是假站每页给几条（真接口固定 20 条；测试里调小才能真的走一遍翻页）；
    ``fail_pages`` 里的页码回业务错误码（验证「失败的一页绝不当成列表结束」）；
    ``boom`` 非空时收藏接口连不上（验证「接口挂了不炸页面」）；
    ``proxy_trace`` 给了就记录每次 ``build_opener`` 收到的代理表（只有配了代理才会建）；
    ``before_request(url)`` 在每个请求前调用一次（进度条测试用它卡住线程）。
    """
    calls: list[str] = []
    docs = docs_of(shelf) if shelf else []

    def serve(request: Any) -> FakeResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        calls.append(url)
        method = request.get_method() if hasattr(request, "get_method") else "GET"
        # urllib 的 Request 会把头名首字母大写（``authorization`` → ``Authorization``），
        # 而 get_header 是普通字典查找，所以这里按两种写法各试一次
        headers = {name: (request.get_header(name) or request.get_header(name.capitalize()))
                   for name in ("authorization", "api-key", "nonce", "signature", "time")}
        body = (getattr(request, "data", None) or b"").decode("utf-8", "replace")
        if before_request is not None:
            before_request(url)
        if log is not None:
            log.append({"url": url, "method": method, "headers": headers, "body": body})
        path = url.split("?", 1)[0]

        if path.endswith("/auth/sign-in"):
            if login_token:
                sent = json.loads(body or "{}")
                if sent.get("email") == EMAIL and sent.get("password") == PASSWORD:
                    return FakeResponse(url, json.dumps(
                        {"code": 200, "message": "success",
                         "data": {"token": login_token}}, ensure_ascii=False))
            # 不给令牌：登录响应里就是没有 token，客户端据此报「认证失败」
            return FakeResponse(url, json.dumps(
                {"code": 200, "message": "账号或密码错误", "data": {}}, ensure_ascii=False))

        # 每个请求都得带一套合法的签名头，否则这个测试站点根本没在验签名
        assert headers["api-key"] and headers["nonce"] and headers["time"], headers
        assert headers["signature"] == expected_signature(
            method, url, str(headers["time"])), f"签名不对：{url}"

        if headers["authorization"] != valid_token:
            raise unauthorized(url)

        if path.endswith("/users/favourite"):
            if boom:
                raise urllib.error.URLError(boom)
            params = query_of(url)
            page = int(params.get("page") or 1)
            assert params.get("s"), "翻页要带上排序值，签名也算它"
            if page in fail_pages:
                return FakeResponse(url, json.dumps(
                    {"code": 404, "message": f"第 {page} 页没准备好"}, ensure_ascii=False))
            rows = docs[(page - 1) * page_size:page * page_size]
            total_pages = max(1, -(-len(docs) // page_size))
            return FakeResponse(url, json.dumps({
                "code": 200, "message": "success",
                "data": {"comics": {"docs": rows, "total": len(docs), "page": page,
                                    "pages": total_pages, "limit": page_size}},
            }, ensure_ascii=False))

        raise urllib.error.HTTPError(
            url, 404, "Not Found", {},                                   # type: ignore[arg-type]
            io.BytesIO(b'{"code":404,"message":"no such api"}'),
        )

    def fake_urlopen(request: Any, timeout: float | None = None, **kwargs: Any) -> FakeResponse:
        return serve(request)

    class FakeOpener:
        """配了代理时客户端建出来的那台 opener：照样把请求交给同一个假站点。"""

        def open(self, request: Any, timeout: Any = None, **kwargs: Any) -> FakeResponse:
            return serve(request)

    def fake_build_opener(*handlers: Any) -> FakeOpener:
        # 只认显式传进来的 ProxyHandler：客户端配了代理才建 opener，留空走 urlopen
        for handler in handlers:
            proxies = getattr(handler, "proxies", None)
            if isinstance(proxies, dict) and proxy_trace is not None:
                proxy_trace.append(dict(proxies))
        return FakeOpener()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
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
    return data_dir(settings) / "picamanga_favorites.json"


def import_file(settings: Settings) -> Path:
    return data_dir(settings) / "picamanga_import.json"


def config_file(settings: Settings) -> Path:
    return data_dir(settings) / "config.toml"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def plugin_log(settings: Settings, plugin_id: str = PLUGIN_ID) -> list[str]:
    """宿主统一收集的插件日志（``data/logs/plugins/<id>.log``）里的行。"""
    path = settings.plugin_logs_dir / f"{plugin_id}.log"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def comic_ids(payload: dict[str, Any]) -> list[str]:
    return [item["comicId"] for item in payload["items"]]


def fetch_form(**overrides: Any) -> dict[str, Any]:
    """页面上「同步」那张表的默认提交值（复选框默认不勾）。

    值传 ``None`` 表示「这个字段不提交」（复选框不勾就是这个效果）。
    """
    form: dict[str, Any] = {
        "action": "fetch_import",
        "form": "fetch",
        "host": DEFAULT_HOST,
        "token": TOKEN,
        "email": "",
        "password": "",
        "mode": "incremental",
        "sort": "dd",
        "delay": "0",
        "timeout": "10",
        "proxy": "",
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
    block = re.search(r'id="pm-fetch-status"(.*?)</section>', html, re.S)
    stuck = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block.group(1)))[:300] if block else ""
    raise AssertionError(f"后台同步没在 {timeout} 秒内结束，进度栏停在：{stuck or html[-200:]}")


def status_text(html: str) -> str:
    """进度/报告那一栏的纯文本。

    整页 HTML 里表单本来就要把令牌回填进输入框（它允许落盘），所以「没泄漏」只能对着
    状态栏断言，别拿整页说事。
    """
    block = re.search(r'id="pm-fetch-status"(.*?)</section>', html, re.S)
    assert block, "这一屏没有状态栏"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block.group(1)))


def favourite_calls(calls: list[str]) -> list[str]:
    """只看收藏接口的请求（登录那一次不算翻页）。"""
    return [url for url in calls if "/users/favourite" in url]


def auth_of(row: dict[str, Any]) -> str:
    return str(row["headers"].get("authorization") or "")


class StubClient:
    """给工具层用的假客户端：按预置的 ``{页码: (docs, total, pages)}`` 应答。"""

    def __init__(self, config: Any, responses: dict[int, tuple[list[dict[str, Any]], int, int]]
                 ) -> None:
        self.config = config
        self.token = TOKEN
        self.logged_in = False
        self.responses = responses
        self.calls: list[tuple[int, str]] = []

    def close(self) -> None:
        return None

    def ensure_authenticated(self) -> str:
        return self.token

    def fetch_favourite_page(self, page: int = 1, *, sort: Any = None
                             ) -> tuple[list[dict[str, Any]], int, int]:
        self.calls.append((page, str(sort)))
        return self.responses.get(page, ([], 0, 0))


# --------------------------------------------------------------------- 随插件分发的工具包


def test_package_mirrors_the_copymanga_layout() -> None:
    """两个来源的包**文件结构一一对应**（少一个 formats.py 是有意撤掉额外导出）。

    copymanga 那份是逐字节复制的上游工具（有哈希冻结测试），这份是我们自己重构的代码，
    由本文件和 ``tests/test_importers*.py`` 覆盖 —— 所以这里只约束「结构对齐」。
    """
    ours = sorted(path.name for path in PACKAGE_DIR.glob("*.py"))
    theirs = sorted(path.name for path in (REPO_PLUGINS / "copymanga_fetch" / "copymanga").glob("*.py"))
    assert ours == [name for name in theirs if name != "formats.py"]


def test_package_is_stdlib_only() -> None:
    """这个包只用标准库（宿主不给第三方依赖，也不给 net 接口）。"""
    allowed = {"__future__", "argparse", "dataclasses", "datetime", "hashlib", "hmac",
               "json", "os", "ssl", "sys", "threading", "time", "typing", "urllib"}
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        for name in re.findall(r"^\s*(?:from|import)\s+([.\w]+)", path.read_text(encoding="utf-8"), re.M):
            if name.startswith("."):
                continue          # 包内的相对导入
            root = name.split(".")[0]
            if root == "picamanga":
                continue        # 文档字符串里那句用法示例
            assert root in allowed, f"{path.name}: {name}"
            assert not name.startswith("omnishelf"), f"{path.name}: {name}"


def test_package_ships_no_credentials_and_reads_no_credential_file() -> None:
    """上游那份 ``config.json`` 里是真实邮箱和口令：**不带进来，也不再支持那种文件**。

    凭据只从插件的 ``config.toml`` / 参数 / 环境变量进来，落盘由插件那一侧负责（口令永不落盘）。
    """
    stray = sorted(str(path.relative_to(PACKAGE_DIR)) for path in PACKAGE_DIR.rglob("*")
                   if path.is_file() and "__pycache__" not in path.parts)
    assert [name for name in stray if not name.endswith(".py")] == []
    for name in ("config.json", "data", "README.md", "docs"):
        assert not (PACKAGE_DIR / name).exists(), name
    for name in ("config.json", "data"):
        # 插件自己的 README.md 是写给用户看的，和上游那份无关
        assert not (PLUGIN_DIR / name).exists(), name

    source = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE_DIR.rglob("*.py")
                       if "__pycache__" not in path.parts)
    assert "config.json" not in source, "工具不该再读那份带真实凭据的配置文件"
    assert "persist_token" not in source, "令牌由插件存，工具不管落盘"
    assert "eyJ" not in source, "源码里不许出现真实 JWT"
    assert EMAIL not in source and PASSWORD not in source

    text = (PLUGIN_DIR / "VENDOR.md").read_text(encoding="utf-8")
    assert "重构" in text and "口令" in text and "签名" in text


def test_fixtures_look_like_the_real_payload() -> None:
    """夹具得像接口真的返回过：24 位 hex 的 ObjectId + 完整信封字段。"""
    for name in (FAVOURITE, FAVOURITE_NEW, FAVOURITE_SHORT, FAVOURITE_REFAV, FAVOURITE_BURIED):
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        comics = payload["data"]["comics"]
        assert set(comics) == {"docs", "total", "page", "pages", "limit"}, name
        assert comics["limit"] == API_PAGE_SIZE, name
        for doc in comics["docs"]:
            assert re.fullmatch(r"[0-9a-f]{24}", doc["_id"]), (name, doc["_id"])
            assert doc["title"].startswith("合成") and "synthetic.invalid" in doc["thumb"]["fileServer"]


# --------------------------------------------------------------------- 纯函数


def test_normalize_host_token_email_sort() -> None:
    module = _load_plugin_module()
    assert module.normalize_host(DEFAULT_HOST) == DEFAULT_HOST
    # 贴整条收藏接口地址（用户最常这么干）只取域名
    assert module.normalize_host(f"{DEFAULT_HOST}/users/favourite?s=dd&page=1") == DEFAULT_HOST
    assert module.normalize_host("picaapi.picacomic.com") == DEFAULT_HOST
    assert module.normalize_host("ftp://picaapi.picacomic.com") == ""
    assert module.normalize_host("") == ""
    assert module.normalize_host("https://a b.com") == ""          # 带空格的一律不当地址

    assert module.normalize_token(f"Bearer {TOKEN}") == TOKEN
    assert module.normalize_token(f"authorization: {TOKEN}") == TOKEN
    assert module.normalize_token(f'"{TOKEN}";') == TOKEN
    # 从抓包面板整行连着抄：前缀、Bearer、引号、结尾分号一层套一层
    assert module.normalize_token(f'authorization: Bearer "{TOKEN}";') == TOKEN
    assert module.normalize_token(TOKEN) == TOKEN
    assert module.normalize_token("") == ""

    assert module.normalize_email(f"  {EMAIL.upper()} ") == EMAIL

    assert module.normalize_mode("full") == "full"
    assert module.normalize_mode("FULL") == "full"
    assert module.normalize_mode("") == "incremental"
    assert module.normalize_sort("vd") == "vd"
    assert module.normalize_sort("nonsense") == "dd"     # 白名单外一律退回新到旧


def test_normalize_proxy() -> None:
    module = _load_plugin_module()
    assert module.normalize_proxy(PROXY) == PROXY
    # 本地代理软件通常只给「IP:端口」，不补协议 urllib 会直接忽略它（= 悄悄没走代理）
    assert module.normalize_proxy("127.0.0.1:10809") == "http://127.0.0.1:10809"
    # SOCKS 得自己写全协议头，补成 http:// 就全错了
    assert module.normalize_proxy("socks5://127.0.0.1:10808") == "socks5://127.0.0.1:10808"
    assert module.normalize_proxy("  " + PROXY + " ") == PROXY
    assert module.normalize_proxy("") == ""
    assert module.normalize_proxy(None) == ""
    assert module.normalize_proxy("ftp://127.0.0.1:21") == ""      # 不支持的协议
    assert module.normalize_proxy("http://127.0.0.1 :10809") == ""  # 带空格的不当地址
    assert module.normalize_proxy("http://") == ""                  # 只有协议没有主机


def test_mode_and_clamping() -> None:
    module = _load_plugin_module()
    config = module.effective_config(module._FakeContext({
        "host": "ftp://nope", "token": f"Bearer {TOKEN}", "email": EMAIL.upper(),
        "mode": "whatever", "sort": "ua", "delay": "99", "timeout": "1",
    }))
    assert config["host"] == DEFAULT_HOST               # 坏地址退回默认值
    assert config["token"] == TOKEN
    assert config["email"] == EMAIL
    assert config["mode"] == "incremental"
    assert config["delay"] == 10.0
    assert config["timeout"] == 5
    # 增量 + 别的排序 = 工具会直接抛错的组合：这里就把它折回 dd
    assert config["sort"] == "dd"
    # 全量允许按热度/旧到新浏览
    assert module.effective_config(module._FakeContext({"mode": "full", "sort": "ld"}))["sort"] == "ld"
    # 代理：配置里补协议、坏值当没填（默认就是不指定代理）
    assert module.effective_config(
        module._FakeContext({"proxy": "127.0.0.1:10809"}))["proxy"] == "http://127.0.0.1:10809"
    assert module.effective_config(module._FakeContext({"proxy": "ftp://nope"}))["proxy"] == ""
    assert module.effective_config(module._FakeContext({}))["proxy"] == ""
    # 陈旧配置里的多余键（调不动的每页条数、以前的账号密码登录、删掉的单次页数上限）会被丢掉
    stale = module.effective_config(module._FakeContext(
        {"page_size": "100", "username": "old", "max_pages": "30"}))
    assert "page_size" not in stale and "username" not in stale
    assert "max_pages" not in stale


def test_redact_masks_secrets() -> None:
    module = _load_plugin_module()
    message = f"请求 https://x/{TOKEN} 失败，口令 {PASSWORD}"
    masked = module.redact(message, TOKEN, PASSWORD)
    assert TOKEN not in masked and PASSWORD not in masked
    assert "***" in masked
    assert module.redact("短", "ab") == "短"            # 太短的值不抹，免得把普通文本打满星


def test_build_import_payload_matches_the_importer_fields(tmp_path: Path) -> None:
    """导入文件只放还没取消收藏的，字段集合正好是 importer ``picamanga`` 认的那 19 个。"""
    module = _load_plugin_module()
    export_fields = set(_package("models").EXPORT_FIELDS)
    path = tmp_path / "store.json"
    path.write_text(json.dumps({"items": [
        {"comic_id": COMIC_ID_6, "title": "合成漫画六", "author": "合成作者戊",
         "categories": ["長篇"], "thumb": {"file_server": "https://fs.synthetic.invalid",
                                          "path": "/media/a.jpg", "original_name": "a.jpg"},
         "eps_count": 8, "finished": True},
        {"comic_id": COMIC_ID_1, "title": "合成漫画一", "author": "合成作者甲",
         "removed": True, "shelf_position": 2},
    ]}, ensure_ascii=False), encoding="utf-8")
    store = module.JsonStore(str(path)).load()
    assert len(store) == 2

    payload = module.build_import_payload(store, host=DEFAULT_HOST,
                                         fetched_at="2026-10-13 10:00:00")
    assert payload["schemaVersion"] == module.SCHEMA_VERSION
    assert payload["total"] == 1 and comic_ids(payload) == [COMIC_ID_6]
    assert payload["host"] == DEFAULT_HOST
    assert payload["source"] == f"{DEFAULT_HOST}/users/favourite"
    assert payload["groups"] == [{"id": "default", "name": "默认", "displayOrder": 0}]
    assert set(payload["items"][0]) == export_fields
    assert not any("raw" in item for item in payload["items"])


# --------------------------------------------------------------------- 工具层：增量边界判定


def test_sort_suspect_only_fires_on_a_growing_shelf_with_no_new_items() -> None:
    """「远端总数涨了但头部零新收藏」是唯一能在没有收藏时间的前提下成立的破绽信号。"""
    exporter = _package("exporter").FavoritesExporter
    scan = {"fetched": 6, "added": 0, "total_remote": 7, "errors": []}
    assert exporter._sort_suspect(6, scan)
    assert exporter._sort_suspect(6, {**scan, "added": 1}) is None      # 找到了新收藏，前提没问题
    assert exporter._sort_suspect(7, scan) is None                      # 总数没涨
    assert exporter._sort_suspect(None, scan) is None                   # 冷启动没有可比的水位
    assert exporter._sort_suspect(6, {**scan, "errors": ["第 2 页失败"]}) is None


def test_enforce_sort_off_only_flags_and_never_marks_uncollected(tmp_path: Path) -> None:
    """``enforce_sort=False`` 只报告异常、不自动改跑全量；半路停手也不许标取消收藏。

    这正是默认值 ``enforce_sort=True`` 存在的理由：关掉它，藏在头部之后的新收藏就静默漏掉了。
    """
    models = _package("models")
    exporter_class = _package("exporter").FavoritesExporter
    config_class = _package("config").PicamangaConfig
    known = docs_of(FAVOURITE)                        # 3001~3006，全在头部
    buried = docs_of(FAVOURITE_BURIED)                # 同样 6 条在前 + 尾部多一条新的

    def running(responses: dict[int, tuple[list[dict[str, Any]], int, int]]) -> Any:
        cfg = config_class(token=TOKEN, mode="incremental", sort="dd", data_dir=str(tmp_path),
                           consecutive_known_stop=2, enforce_sort=False, quiet=True).validate()
        return exporter_class(client=StubClient(cfg, responses), config=cfg)

    first = running({1: (known, len(known), 1)})
    report = first.sync()
    assert report.added == 6 and report.verification == models.VERIFY_BASELINE

    again = running({1: (buried[:6], len(buried), 1), 2: (buried[6:], len(buried), 1)})
    report = again.sync()
    assert report.sort_verified is False and report.fell_back_to_full is False
    assert report.added == 0 and report.removed == 0
    assert report.stop_reason == models.STOP_KNOWN_BOUNDARY
    assert report.verification == models.VERIFY_CHANGED_SCAN
    assert again.client.calls == [(1, "dd")]                      # 没有自动改跑全量
    store = _package("store").JsonStore(str(first.config.store_path)).load()
    assert COMIC_ID_10 not in store.ids(), "关掉校验就是接受这种漏数据"


def test_request_headers_carry_the_app_signature() -> None:
    """签名必须齐、且签的是「去掉前导斜杠 + 带查询串」的那截路径。

    带着 ``/`` 去签不会报错，只会拿到一个假的空成功响应 —— 所以这里直接钉死算法。
    """
    client_class = _package("client").PicamangaClient
    config_class = _package("config").PicamangaConfig
    client = client_class(config_class(token=TOKEN))
    headers = client.build_headers("GET", "/users/favourite?s=dd&page=2", TOKEN)
    assert headers["signature"] == client_class.sign(
        "GET", "users/favourite?s=dd&page=2", str(headers["time"]))
    assert headers["signature"] != client_class.sign(
        "GET", "/users/favourite?s=dd&page=2", str(headers["time"]))
    assert headers["authorization"] == TOKEN
    assert {"api-key", "nonce", "time", "app-version", "app-platform", "app-channel"} <= set(headers)
    # 登录本身是匿名请求
    assert "authorization" not in client.build_headers("POST", "/auth/sign-in", None)


def test_proxy_reaches_urllib_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """配了代理就要真的交给 ``ProxyHandler``（http 和 https 两个口都得填）。

    这是「代理功能生效」唯一的凭据：客户端里只是存了个字符串没用——
    ``build_opener`` 收不到显式 handler 时 urllib 会用环境变量里那份，收不到才叫没配。
    """
    trace: list[dict[str, str]] = []
    install_fake_network(monkeypatch, proxy_trace=trace)
    config_class = _package("config").PicamangaConfig
    client_class = _package("client").PicamangaClient

    docs, total, _pages = client_class(
        config_class(token=TOKEN, proxy="127.0.0.1:10809", data_dir=str(tmp_path))
    ).fetch_favourite_page(1)
    assert docs and total == 6
    assert trace == [{"http": "http://127.0.0.1:10809", "https": "http://127.0.0.1:10809"}]

    # 留空 = 不在这层指定代理：压根不建 opener，走 urlopen 的默认路径
    trace.clear()
    client_class(config_class(token=TOKEN, data_dir=str(tmp_path))).fetch_favourite_page(1)
    assert trace == []

    # 代理地址没写协议时补成 http://，否则 urllib 会当它不存在
    assert config_class(proxy="127.0.0.1:10809").proxy_target == "http://127.0.0.1:10809"
    assert config_class(proxy="socks5://127.0.0.1:10808").proxy_target == "socks5://127.0.0.1:10808"
    assert config_class(proxy="  ").proxy_target == ""


# --------------------------------------------------------------------- 页面 / 配置


def test_plugin_page_renders(client: TestClient) -> None:
    response = client.get(f"/plugins/{PLUGIN_ID}")
    assert response.status_code == 200
    body = response.text
    assert "哔咔收藏夹抓取" in body
    assert PLUGIN_ID in body
    assert f'action="/plugins/{PLUGIN_ID}"' in body
    assert "接口地址" in body and "同步方式" in body and "列表排序" in body
    assert 'name="action" value="fetch_import"' in body
    assert 'name="token"' in body
    assert 'name="email"' in body
    assert 'type="password" name="password"' in body
    # 这一家的接口在很多网络环境里直连不通，页面上得有一栏代理
    assert 'name="proxy"' in body
    assert "http://127.0.0.1:10809" in body            # placeholder 给个能照抄的形状
    # 复选框配了同名标记（不勾时要能区分「没勾」和「这张表不管这个开关」）
    assert 'name="has_raw"' in body
    assert "邮箱和口令" in body
    # 单页条数是服务端定死的：不该有一个调不动的旋钮
    assert 'name="page_size"' not in body and "每页条数" not in body
    # 也不该有从别的来源抄过来的多余开关
    assert 'name="username"' not in body and 'name="cartoons"' not in body
    assert 'name="export_' not in body and "openpyxl" not in body


def test_plugin_appears_in_list(client: TestClient) -> None:
    body = client.get("/plugins").text
    assert "哔咔收藏夹抓取" in body
    assert "已加载" in body


def test_save_config_persists_but_never_the_password(
    client: TestClient, settings_with_repo_plugins: Settings
) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": f"{DEFAULT_HOST}/users/favourite?s=dd",
        "token": f"Bearer {TOKEN}", "email": EMAIL.upper(), "password": PASSWORD,
        "mode": "full", "sort": "ld", "delay": "1.5", "timeout": "30",
        # 浏览器提交表单时，那几个隐藏标记和复选框是一起发出去的
        "has_raw": "1", "include_raw": "on",
    })
    assert response.status_code == 200
    assert "默认配置已保存" in response.text
    text = config_file(settings_with_repo_plugins).read_text(encoding="utf-8")
    assert f'host = "{DEFAULT_HOST}"' in text            # 整条接口地址只留下域名
    assert f'token = "{TOKEN}"' in text                 # 令牌会存（和 Cookie 一个性质）
    assert f'email = "{EMAIL}"' in text                 # 邮箱会小写归一
    assert PASSWORD not in text and "password" not in text   # 口令不落盘
    assert 'mode = "full"' in text and 'sort = "ld"' in text
    assert "include_raw = true" in text
    assert "max_pages" not in text                      # 没有单次页数上限这回事


def test_save_config_rejects_bad_host(client: TestClient) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": "ftp://nope"})
    assert "接口地址要写成" in response.text
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
    """令牌框空着提交不等于「把令牌忘掉」：配置里那个还在（口令登录换的就是它）。"""
    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": DEFAULT_HOST, "token": TOKEN})
    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": DEFAULT_HOST, "token": ""})
    assert f'token = "{TOKEN}"' in config_file(settings_with_repo_plugins).read_text(encoding="utf-8")


def test_broken_config_still_renders(client: TestClient, settings_with_repo_plugins: Settings) -> None:
    """配置被手改坏了（非法地址/类型）不能把页面弄炸：夹回默认值并照常显示。"""
    config_file(settings_with_repo_plugins).parent.mkdir(parents=True, exist_ok=True)
    config_file(settings_with_repo_plugins).write_text(
        'host = "ftp://nope"\ndelay = "abc"\nmode = 42\nsort = 7\nmax_pages = 3000\n',
        encoding="utf-8",
    )
    manager = client.app.state.plugin_manager            # type: ignore[attr-defined]
    assert "哔咔收藏夹抓取" in client.get(f"/plugins/{PLUGIN_ID}").text
    manager.reload(PLUGIN_ID)                        # reload 会重读那份坏配置
    assert manager.status(PLUGIN_ID).state == STATE_LOADED
    body = client.get(f"/plugins/{PLUGIN_ID}").text
    assert "哔咔收藏夹抓取" in body
    assert 'name="max_pages"' not in body           # 那栏已经从页面上拿掉了


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


def test_full_sync_writes_store_and_import_file(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_fake_network(monkeypatch, page_size=3)     # 6 条 / 每页 3 条：真的翻两页
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    assert response.status_code == 200
    assert "正在同步" in response.text                   # POST 只给进度条，结果等后台跑完
    html = wait_for_sync(client)

    assert "同步完成" in html
    assert "新增 6" in html
    assert "每页 20 条" in html                             # 报告里写的是接口那个固定值
    assert len(favourite_calls(calls)) == 2, calls

    store = read_json(store_file(settings_with_repo_plugins))
    assert store["total"] == 6 and store["active_total"] == 6
    assert store["schema_version"] == 1
    assert store["host"] == DEFAULT_HOST and store["sort"] == "dd"
    assert store["page_size"] == API_PAGE_SIZE                # 假站那页切 3 条改不了这个常量
    assert store["server_total"] == 6
    # 本地一条都没有的第一次同步记成 baseline（没有可比的水位），之后才算 full-scan
    assert store["last_scan"]["verification"] == "baseline"
    assert store["last_scan"]["mode"] == "full" and store["last_scan"]["pages"] == 2
    assert [item["comic_id"] for item in store["items"]][0] == COMIC_ID_6

    payload = read_json(import_file(settings_with_repo_plugins))
    assert payload["total"] == 6
    assert payload["source"] == f"{DEFAULT_HOST}/users/favourite"
    assert {item["title"] for item in payload["items"]} == {f"合成漫画{c}" for c in "一二三四五六"}
    assert not any("raw" in item for item in payload["items"])   # 接口原始返回不进导入文件

    meta = read_json(data_dir(settings_with_repo_plugins) / "picamanga_fetch.meta.json")
    assert meta["added"] == 6 and meta["mode"] == "full"
    assert meta["requests"] == 2 and meta["stop_reason"] == "complete"
    # meta 只是「上次什么样」的状态卡：逐页流水归宿主日志，不在这里留一份每次覆盖的副本
    assert "notes" not in meta
    assert "本地状态库" in "\n".join(plugin_log(settings_with_repo_plugins))
    assert "本地已有的数据" in html and "picamanga_import.json" in html


def test_every_request_carries_a_valid_signature(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """哔咔每个请求都要带一套合法的签名头；有令牌时不该再多打一次登录。"""
    log: list[dict[str, Any]] = []
    install_fake_network(monkeypatch, page_size=3, log=log)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)

    rows = [row for row in log if "/users/favourite" in row["url"]]
    assert len(rows) == 2
    for row in rows:
        headers = row["headers"]
        assert headers["authorization"] == TOKEN
        assert headers["signature"] == expected_signature("GET", row["url"], str(headers["time"]))
        assert headers["api-key"] and headers["nonce"] and headers["time"]
        assert query_of(row["url"])["s"] == "dd"
    assert not any("/auth/sign-in" in row["url"] for row in log)


def test_incremental_stops_after_five_known_items(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先全量把收藏夹落下来，之后增量从头部翻、连续命中 5 条本地已有的就收手。"""
    install_fake_network(monkeypatch, page_size=3)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)

    calls = install_fake_network(monkeypatch)            # 重新计数（每页 20：一页就是整份收藏夹）
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="incremental"))
    html = wait_for_sync(client)
    assert len(favourite_calls(calls)) == 1, "第一页整页都是本地已有的就该停"
    assert "known-boundary" in html and "changed-scan" in html

    # 增量半路停手：既不会清空已抓全的状态库，也不许把没翻到的当成取消收藏
    store = read_json(store_file(settings_with_repo_plugins))
    assert store["total"] == 6 and store["removed_total"] == 0
    assert read_json(import_file(settings_with_repo_plugins))["total"] == 6


def test_incremental_picks_up_a_new_favourite(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_network(monkeypatch, page_size=3)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)

    calls = install_fake_network(monkeypatch, shelf=FAVOURITE_NEW, page_size=4)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="incremental"))
    html = wait_for_sync(client)
    assert "新增 1" in html
    assert len(favourite_calls(calls)) == 2, "第一页有新收藏就继续翻，第二页整页都旧才停"

    ids = comic_ids(read_json(import_file(settings_with_repo_plugins)))
    assert len(ids) == 7 and any(item.endswith("3007") for item in ids)


def test_refavourite_at_the_head_does_not_cut_the_scan_short(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重新收藏会把一条老收藏刷回列表头部——「命中一条就停」会漏掉它后面所有的新收藏。"""
    install_fake_network(monkeypatch, page_size=3)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)

    calls = install_fake_network(monkeypatch, shelf=FAVOURITE_REFAV, page_size=4)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="incremental"))
    html = wait_for_sync(client)
    assert "新增 3" in html, "头部第一条是老的，但后面跟着三条新的：不能就此收手"
    assert len(favourite_calls(calls)) == 3, calls
    assert "known-boundary" in html
    assert len(comic_ids(read_json(import_file(settings_with_repo_plugins)))) == 9


def test_full_sync_marks_uncollected_but_never_deletes(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量时发现「已经取消收藏」的条目：状态库里打标记，导入文件里不放它，库里那条也不删。"""
    install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)
    assert len(read_json(import_file(settings_with_repo_plugins))["items"]) == 6

    install_fake_network(monkeypatch, shelf=FAVOURITE_SHORT)      # 3001 被移出收藏夹了
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    html = wait_for_sync(client)
    assert "同步完成" in html and "已取消收藏 1" in html
    assert "不会" in html and "从 OmniShelf 里删掉" in html

    store = read_json(store_file(settings_with_repo_plugins))
    assert store["total"] == 6 and store["removed_total"] == 1
    removed = [item for item in store["items"] if item["comic_id"] == COMIC_ID_1]
    assert removed and removed[0]["removed"] is True

    payload = read_json(import_file(settings_with_repo_plugins))
    assert payload["total"] == 5 and COMIC_ID_1 not in comic_ids(payload)


def test_broken_sort_premise_falls_back_to_a_full_scan(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口不再「新收藏在头部」时：增量发现总数涨了却零新增，自动改跑一遍全量。"""
    install_fake_network(monkeypatch, page_size=3)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)

    calls = install_fake_network(monkeypatch, shelf=FAVOURITE_BURIED, page_size=6)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="incremental"))
    html = wait_for_sync(client)

    assert "这次没能确认「新收藏在头部」" in html and "已自动改跑全量" in html
    assert "新增 1" in html, "那条新收藏掉在头部之后，只翻一页就静默漏掉了"
    # 一次头部扫描（1 页）+ 全量重扫（2 页）
    assert len(favourite_calls(calls)) == 3, calls
    assert COMIC_ID_10 in comic_ids(read_json(import_file(settings_with_repo_plugins)))
    meta = read_json(data_dir(settings_with_repo_plugins) / "picamanga_fetch.meta.json")
    assert meta["fell_back_to_full"] is True and meta["sort_verified"] is False
    assert meta["mode"] == "full"


def test_full_sync_walks_every_page_without_a_budget(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量就得翻到末页：这里以前会被「单次页数上限」在半路掐断，静默少一批收藏。"""
    calls = install_fake_network(monkeypatch, page_size=1)      # 6 条 => 6 页
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    html = wait_for_sync(client)
    assert len(favourite_calls(calls)) == 6
    assert "页数上限" not in html and "max-pages" not in html
    assert read_json(import_file(settings_with_repo_plugins))["total"] == 6
    meta = read_json(data_dir(settings_with_repo_plugins) / "picamanga_fetch.meta.json")
    assert meta["requests"] == 6 and meta["fetched"] == 6, meta


def test_failed_page_is_reported_as_incomplete(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """某一页报错：已经抓到的部分要留下，页面要写清楚这次不完整。"""
    install_fake_network(monkeypatch, page_size=3, fail_pages=(2,))
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    html = wait_for_sync(client)
    assert "同步完成" in html and "第 2 页抓取失败" in html
    assert "有页面没抓到" in html
    assert read_json(store_file(settings_with_repo_plugins))["total"] == 3
    assert read_json(import_file(settings_with_repo_plugins))["total"] == 3


def test_failed_page_never_marks_uncollected(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「这次没抓到」不等于「用户取消了收藏」：有页面失败就不打取消收藏标记。"""
    install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    wait_for_sync(client)

    install_fake_network(monkeypatch, shelf=FAVOURITE_SHORT, page_size=3, fail_pages=(2,))
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(mode="full"))
    html = wait_for_sync(client)
    text = status_text(html)
    assert "第 2 页抓取失败" in text and "有页面没抓到，这次不会标记取消收藏" in text
    assert "<th>取消收藏</th>" not in html          # 没有「这次标记了 N 条取消收藏」那一行
    assert read_json(store_file(settings_with_repo_plugins))["removed_total"] == 0


def test_include_raw_only_bloats_the_store(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_network(monkeypatch)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(include_raw="on"))
    wait_for_sync(client)
    store = read_json(store_file(settings_with_repo_plugins))
    assert "raw" in store["items"][0]
    assert not any("raw" in item for item in read_json(import_file(settings_with_repo_plugins))["items"])


def test_connection_failure_is_shown_not_raised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口连不上：页面要出提示，不能 500。"""
    install_fake_network(monkeypatch, boom="connection refused")
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    assert response.status_code == 200
    html = wait_for_sync(client)
    assert "请求失败" in html and "connection refused" in html


# --------------------------------------------------------------------- 代理


def test_form_proxy_is_used_shown_and_persisted(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """页面上填的代理要真的走它，并且进度栏/报告里看得见走了哪条路。

    「静默没走代理」是这种功能最糟的失效方式：哔咔直连通常根本不通，
    用户只会看到一句莫名的请求失败。所以除了 ProxyHandler 那一头，页面上也得有一行凭据。
    """
    trace: list[dict[str, str]] = []
    install_fake_network(monkeypatch, proxy_trace=trace)
    response = client.post(f"/plugins/{PLUGIN_ID}",
                           data=fetch_form(proxy="127.0.0.1:10809", mode="full"))
    assert response.status_code == 200
    html = wait_for_sync(client)

    assert trace and all(row["https"] == "http://127.0.0.1:10809" for row in trace)
    text = status_text(html)
    assert "开始全量同步（走代理 http://127.0.0.1:10809）" in text     # 进度栏第一行就说清了走哪条路
    assert "网络 走代理 http://127.0.0.1:10809" in text               # 报告里也有一行凭据
    # 代理地址不是凭据，和别的连接项一样存进配置（补上的协议也存下来了）
    assert 'proxy = "http://127.0.0.1:10809"' in config_file(settings_with_repo_plugins).read_text(encoding="utf-8")

def test_blank_proxy_box_means_direct(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """代理框空着就是「这次不指定代理」：压根不建 opener，报告里写直连。

    和令牌框相反（那里留空 = 用存着的那个）——代理不是凭据，重填不费事，
    而「临时直连一次」是真实需求。
    """
    trace: list[dict[str, str]] = []
    install_fake_network(monkeypatch, proxy_trace=trace)
    client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": DEFAULT_HOST, "proxy": PROXY})
    assert f'proxy = "{PROXY}"' in config_file(settings_with_repo_plugins).read_text(encoding="utf-8")

    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(proxy=""))
    html = wait_for_sync(client)
    assert trace == [], "代理框空着还建带 ProxyHandler 的 opener，就是没按表单来"
    assert "（直连）" in status_text(html) and "走代理" not in status_text(html)


def test_bad_proxy_in_the_form_is_refused_before_starting(
    client: TestClient, settings_with_repo_plugins: Settings
) -> None:
    """亲手填了一个认不出来的代理就该说清楚，而不是悄悄换成直连跑一次。"""
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(proxy="proxy 10809"))
    assert "代理地址要写成" in response.text
    assert "正在同步" not in response.text               # 别起一个注定失败的后台任务
    path = config_file(settings_with_repo_plugins)
    assert "proxy 10809" not in (path.read_text(encoding="utf-8") if path.exists() else "")


def test_bad_proxy_is_not_saved_as_a_default(client: TestClient) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}", data={
        "action": "save", "form": "save", "host": DEFAULT_HOST, "proxy": "ftp://127.0.0.1:21"})
    assert "代理地址要写成" in response.text
    assert "默认配置已保存" not in response.text


def test_proxy_failure_names_the_proxy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """代理不通时，错误里要带上是哪个代理——和「接口挂了」两种修法完全不同。"""
    install_fake_network(monkeypatch, boom="connection refused")
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(proxy=PROXY))
    assert response.status_code == 200
    text = status_text(wait_for_sync(client))
    assert "请求失败" in text and "connection refused" in text
    assert f"经代理 {PROXY}" in text


# --------------------------------------------------------------------- 认证


def test_missing_credential_is_refused_before_starting(client: TestClient) -> None:
    response = client.post(f"/plugins/{PLUGIN_ID}",
                           data=fetch_form(token="", email="", password=""))
    assert "要先认证" in response.text
    assert "正在同步" not in response.text               # 别起一个注定失败的后台任务


def test_login_without_token_keeps_the_token_not_the_password(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有令牌就用邮箱口令登录；登录那一刻换到的令牌当场存进配置，口令永远不落盘。"""
    log: list[dict[str, Any]] = []
    calls = install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token=NEW_TOKEN, log=log)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(token="", email=EMAIL, password=PASSWORD))
    html = wait_for_sync(client)

    assert "同步完成" in html
    assert "登录换的令牌" in html
    assert calls[0].endswith("/auth/sign-in"), "没令牌就该先登录，而不是拿空令牌去撞接口"
    row = log[0]
    assert row["method"] == "POST" and not auth_of(row)     # 登录本身是匿名请求
    assert json.loads(row["body"]) == {"email": EMAIL, "password": PASSWORD}
    assert {auth_of(item) for item in log if "/users/favourite" in item["url"]} == {NEW_TOKEN}

    text = config_file(settings_with_repo_plugins).read_text(encoding="utf-8")
    assert f'token = "{NEW_TOKEN}"' in text
    assert f'email = "{EMAIL}"' in text
    assert PASSWORD not in text
    page = client.get(f"/plugins/{PLUGIN_ID}").text
    assert PASSWORD not in page


def test_stored_token_survives_a_blank_token_box(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登录换到的令牌得真的「存下来」：下次令牌框留空也能直接用它，不用再登一次。

    用户看到的就是这一步——第一次口令登录之后再点同步，如果还被要求认证（或者令牌框
    一直空着），那就等于没保存。
    """
    install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token=NEW_TOKEN)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(token="", email=EMAIL, password=PASSWORD))
    wait_for_sync(client)
    text = config_file(settings_with_repo_plugins).read_text(encoding="utf-8")
    assert f'token = "{NEW_TOKEN}"' in text
    assert f'email = "{EMAIL}"' in text and PASSWORD not in text

    # 拿手里那屏（令牌框还是空的）再点一次
    calls = install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token="")
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(token="", email="", password=""))
    html = wait_for_sync(client)
    assert "同步完成" in html, "表单留空时该用配置里存着的令牌"
    assert not any("/auth/sign-in" in url for url in calls), "有存好的令牌就不该再登录一次"
    assert f'token = "{NEW_TOKEN}"' in config_file(settings_with_repo_plugins).read_text(encoding="utf-8")


def test_token_is_saved_even_when_the_pages_fail(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登录一成功就存令牌：后面翻页全失败，这一次登录也不白做。"""
    install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token=NEW_TOKEN, boom="reset")
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(token="", email=EMAIL, password=PASSWORD))
    assert "请求失败" in wait_for_sync(client)
    text = config_file(settings_with_repo_plugins).read_text(encoding="utf-8")
    assert f'token = "{NEW_TOKEN}"' in text and PASSWORD not in text
    # 上报给宿主的日志同样只说过打码的话：口令不在里面（令牌在，那是配置里本来就存的）
    assert PASSWORD not in "\n".join(plugin_log(settings_with_repo_plugins))


def test_expired_token_relogs_in(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先用手上的令牌；它被接口顶回来就用邮箱口令重登一次，然后重放那个请求。"""
    log: list[dict[str, Any]] = []
    install_fake_network(monkeypatch, valid_token=NEW_TOKEN, login_token=NEW_TOKEN, log=log)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(
        token=f"Bearer {STALE_TOKEN}", email=EMAIL, password=PASSWORD))
    html = wait_for_sync(client)

    assert "同步完成" in html
    rows = [auth_of(row) for row in log if "/users/favourite" in row["url"]]
    assert rows[0] == STALE_TOKEN and set(rows[1:]) == {NEW_TOKEN}
    assert len([row for row in log if row["url"].endswith("/auth/sign-in")]) == 1
    assert f'token = "{NEW_TOKEN}"' in config_file(settings_with_repo_plugins).read_text(encoding="utf-8")


def test_login_failure_is_reported_and_password_stays_hidden(
    client: TestClient, settings_with_repo_plugins: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登录接口不给令牌：失败要说清楚，但口令不能跟着报错印到页面上。"""
    install_fake_network(monkeypatch, login_token="")          # 这个假站点不给令牌
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(
        token="", email=EMAIL, password=PASSWORD))
    # 这种「秒级失败」也照样先给进度条：结果由轮询那一次给出，一份报告只让人看一次
    assert 'hx-trigger="every' in response.text
    assert "同步失败" not in response.text
    html = wait_for_sync(client)
    assert "认证失败" in html and "账号或密码错误" in html
    assert PASSWORD not in html
    assert f'password="{PASSWORD}"' not in html and f'name="password" value="{PASSWORD}"' not in html


def test_bad_token_without_credentials_says_auth_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只粘了一个过期令牌、没填邮箱口令：失败要写「认证失败」，不能当成「没有数据」。"""
    install_fake_network(monkeypatch, valid_token=NEW_TOKEN)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    html = wait_for_sync(client)
    assert "认证失败" in html and "同步完成" not in html
    # 令牌本身是允许留下的（它会存进配置、下次直接用），但它绝不该出现在报错文本里
    assert TOKEN not in status_text(html)
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
        if "/users/favourite" in url and not gated["done"]:
            gated["done"] = True
            requested.set()
            hold.wait(10)

    install_fake_network(monkeypatch, before_request=before_request)
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    assert response.status_code == 200
    assert requested.wait(10), "后台线程要真的发出请求"

    body = response.text
    assert "正在同步" in body
    assert 'id="pm-fetch-status"' in body
    assert f'hx-get="/plugins/{PLUGIN_ID}"' in body
    assert 'hx-trigger="every' in body                    # 进度条自己定时轮询
    assert 'hx-select="#pm-fetch-status"' in body         # 只换这一栏，不套第二层页面

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
    assert "picamanga" in html

    session.expire_all()
    items = {item.title: item for item in repo.list_items(session, kind="manga")}
    assert len(items) == 6

    first = items["合成漫画一"]
    assert first.creators == ["合成作者甲"]
    assert set(repo.list_tags(session, first.id)) == {"長篇", "熱血"}
    assert first.extra["cover_url"] == (
        f"https://fs.pica.synthetic.invalid/media/{COMIC_ID_1}/thumbnail/thumbnail-003001.jpg")
    assert first.extra["chapter_count"] == 5
    assert first.extra["finished"] is False
    # 这个来源没有年份、评分，也没有「最新话 / 源站更新时间 / 是否有新话」
    assert first.year is None and first.rating is None
    for missing in ("latest_chapter", "remote_updated_at", "has_new_chapter"):
        assert missing not in first.extra
    links = {link.source: link for link in repo.list_external_ids(session, first.id)}
    assert links["picamanga"].external_id == COMIC_ID_1

    assert items["合成漫画六"].extra["finished"] is True       # 已完結
    assert items["合成漫画三"].extra["finished"] is True       # 短篇
    assert items["合成漫画三"].creators == ["合成作者丙"]

    archive = settings_with_repo_plugins.imports_dir / "picamanga" / \
        import_file(settings_with_repo_plugins).name
    assert archive.is_file()                                   # 原始文件按来源存档


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
    for param in ("mode", "host", "token", "email", "password", "sort", "delay",
                  "timeout", "proxy", "include_raw", "dry_run"):
        assert param in commands["fetch_import"].params, param
    # 单页条数是服务端定死的，命令行不该有个调不动的 --page-size；账号那套也是别处的
    # 单次页数上限也已删除：全量就一路翻完
    for gone in ("page_size", "username", "cartoons", "formats", "max_pages"):
        assert gone not in commands["fetch_import"].params, gone


def test_cli_command_reports_and_validates_the_proxy(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令行和页面看到的是同一套代理逻辑：填了就报走代理，填坏了就报错别硬跑。"""
    from omnishelf.plugins.manager import PluginError

    manager.load_all()
    trace: list[dict[str, str]] = []
    install_fake_network(monkeypatch, proxy_trace=trace)
    output = manager.run_command(PLUGIN_ID, "fetch_import",
                                 ["--token", TOKEN, "--proxy", "127.0.0.1:10809"])
    assert "网络      走代理 http://127.0.0.1:10809" in output
    assert trace and all(row["https"] == "http://127.0.0.1:10809" for row in trace)

    trace.clear()
    output = manager.run_command(PLUGIN_ID, "fetch_import", ["--token", TOKEN, "--proxy", ""])
    # 没传 --proxy = 用配置里刚存的那个（和 host/token 一个口径），不是「这次直连」
    assert "走代理 http://127.0.0.1:10809" in output and trace

    with pytest.raises(PluginError):
        manager.run_command(PLUGIN_ID, "fetch_import", ["--token", TOKEN, "--proxy", "ftp://x"])


def test_cli_command_syncs_and_imports(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    manager.load_all()
    install_fake_network(monkeypatch)
    output = manager.run_command(PLUGIN_ID, "fetch_import", [
        "--token", TOKEN, "--mode", "full", "--delay", "0"])
    assert isinstance(output, str)
    assert "同步完成" in output
    assert "每页固定 20 条" in output and "排序" in output
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
