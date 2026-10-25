"""代理：地址归一、口令脱敏，以及「配了只走它 / 留空走默认」的路由。

和 :mod:`tests.test_steam_client_errors` 同一套路——测的是真 transport，
假 ``urlopen`` / ``build_opener``，不发真请求。

三条口径都要钉住：
* 只写 ``IP:端口`` 会被补成 ``http://``（否则 urllib 悄悄忽略，等于没配）；
* 配了代理就**只走它**（显式 ``ProxyHandler`` 顶掉环境变量那份），留空才用默认路径；
* 代理地址里的 ``user:pass@`` 口令绝不出现在日志、报错和 ``proxy_label`` 里。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from tests.steam_synth import KEY, SID  # noqa: F401  （先把插件目录挂进 sys.path）
from steam_client import SteamClient
from steam_client.transport import (
    SteamWebAPI,
    normalize_proxy,
    proxy_credentials,
    proxy_without_credentials,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        (None, ""),
        ("   ", ""),
        ("127.0.0.1:10809", "http://127.0.0.1:10809"),   # 裸 IP:端口 按 HTTP 代理对待
        ("http://127.0.0.1:10809", "http://127.0.0.1:10809"),
        ("  socks5://127.0.0.1:10808  ", "socks5://127.0.0.1:10808"),
        ("https://proxy.invalid:8443", "https://proxy.invalid:8443"),
    ],
)
def test_normalize_proxy_fills_the_scheme(raw, expected):
    assert normalize_proxy(raw) == expected


def test_proxy_label_never_shows_credentials():
    assert proxy_without_credentials("http://user:pw@127.0.0.1:10809") == "http://127.0.0.1:10809"
    assert proxy_without_credentials("127.0.0.1:10809") == "http://127.0.0.1:10809"
    assert proxy_without_credentials("") == ""
    assert proxy_credentials("http://user:pw@127.0.0.1:10809") == "user:pw"
    assert proxy_credentials("http://127.0.0.1:10809") == ""


def ok_body(payload: dict | None = None):
    return io.BytesIO(json.dumps(payload if payload is not None else {"response": {}}).encode())


class Openers:
    """记录 build_opener 收到的 ProxyHandler，以及最终开出去的几个 opener。"""

    def __init__(self) -> None:
        self.handlers: list[list[object]] = []
        self.opened: list[str] = []
        self.urlopen_urls: list[str] = []

    def build_opener(self, *handlers):
        self.handlers.append(list(handlers))

        class Opener:
            def __init__(self, owner):
                self.owner = owner

            def open(self, request, timeout=None):
                self.owner.opened.append(request.full_url)
                return ok_body()

        return Opener(self)

    def urlopen(self, request, timeout=None):
        self.urlopen_urls.append(request.full_url)
        return ok_body()


def test_proxy_routes_through_a_dedicated_opener(monkeypatch):
    seen = Openers()
    monkeypatch.setattr(urllib.request, "build_opener", seen.build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", seen.urlopen)

    api = SteamWebAPI(KEY, proxy="127.0.0.1:10809")
    assert api.request("/x/") == {"response": {}}

    assert seen.urlopen_urls == []                    # 配了代理就不走默认路径
    assert len(seen.opened) == 1
    handlers = seen.handlers[0]
    proxy_handler = next(h for h in handlers if type(h).__name__ == "ProxyHandler")
    assert proxy_handler.proxies == {
        "http": "http://127.0.0.1:10809",
        "https": "http://127.0.0.1:10809",            # 裸地址已补协议，两个域名同一出口
    }


def test_without_proxy_urlopen_default_path_is_used(monkeypatch):
    """留空时不建 opener：``HTTPS_PROXY`` 之类的环境变量照旧由 urllib 自己认。"""
    seen = Openers()
    monkeypatch.setattr(urllib.request, "build_opener", seen.build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", seen.urlopen)

    api = SteamWebAPI(KEY)
    assert api.proxy == ""
    assert api.request("/x/") == {"response": {}}
    assert seen.handlers == []
    assert len(seen.urlopen_urls) == 1


class FailOpener:
    """假 opener：open() 就抛 URLError（代理通道不通的典型形态）。"""

    def __init__(self, message: str) -> None:
        self.message = message

    def open(self, request, timeout=None):
        raise urllib.error.URLError(self.message)


def test_network_error_says_which_route_failed(monkeypatch):
    """连不上时分不清走的哪条路，报错里要带上代理地址（口令已去）。"""
    monkeypatch.setattr(
        urllib.request, "build_opener",
        lambda *handlers: FailOpener(f"failed at {handlers[0].proxies['https']}/connect"),
    )
    api = SteamWebAPI(KEY, max_retries=0, proxy="http://user:secret@127.0.0.1:10809")
    with pytest.raises(Exception) as excinfo:
        api.request("/IPlayerService/GetOwnedGames/v1/", {"steamid": SID})
    message = str(excinfo.value)
    assert "user:secret" not in message
    assert KEY not in message
    assert "***" in message                           # 原文里的口令被 redact 抹掉
    assert "经代理 http://127.0.0.1:10809" in message  # 主机端口留着，才看得出走的哪条路


def test_direct_failure_does_not_mention_a_proxy(monkeypatch):
    def raise_url_error(request, timeout=None):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", raise_url_error)
    api = SteamWebAPI(KEY, max_retries=0)
    with pytest.raises(Exception) as excinfo:
        api.request("/x/")
    assert "经代理" not in str(excinfo.value)


@pytest.mark.parametrize("base", ["user", "game"])
def test_proxy_reaches_both_modules(tmp_path, base):
    """愿望单走 api 域名、商店信息走 store 域名，直连不通时两个都得走代理。"""
    client = SteamClient(api_key=KEY, cache_path=tmp_path / "cache", proxy="127.0.0.1:10809")
    api = getattr(client, base).api
    assert api.proxy == "http://127.0.0.1:10809"
    assert api.proxy_label == "http://127.0.0.1:10809"


def test_proxy_defaults_to_direct(tmp_path):
    client = SteamClient(api_key=KEY, cache_path=tmp_path / "cache")
    assert client.user.api.proxy == "" and client.game.api.proxy == ""
