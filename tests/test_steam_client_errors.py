"""错误模型：状态码映射、重试策略、key 脱敏、User-Agent 与 format=json。

这一组测的是真 transport（假 ``urlopen``），不经过 FakeAPI——
验收用例 #8（缺 key 的报错不含明文 key）与「429 重试 / 4xx 不重试」都在这里。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from tests.steam_synth import FakeAPI, KEY, SID, make_client
from steam_client import SteamGameClient
from steam_client.errors import (
    SteamAuthError,
    SteamError,
    SteamNetworkError,
    SteamNotFoundError,
    SteamRateLimitError,
)
from steam_client.transport import API_BASE, SteamWebAPI


def http_error(url: str, code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, code, f"HTTP {code}", headers, io.BytesIO(b""))


class UrlopenRecorder:
    """假 urlopen：按脚本依次抛/返，记录每次请求。"""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests: list[str] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request.full_url)
        outcome = self.script.pop(0) if self.script else self.script_error()
        if isinstance(outcome, Exception):
            raise outcome
        body = json.dumps(outcome).encode("utf-8")
        return io.BytesIO(body)

    @staticmethod
    def script_error():
        raise AssertionError("假 urlopen 脚本用完了")


@pytest.fixture()
def api() -> SteamWebAPI:
    return SteamWebAPI(KEY, max_retries=3, backoff=0.0)


@pytest.mark.parametrize("code,expected", [(401, SteamAuthError), (403, SteamAuthError), (404, SteamNotFoundError)])
def test_client_errors_are_not_retried(api, monkeypatch, code, expected):
    fake = UrlopenRecorder(http_error("https://api.invalid/x", code))
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(expected):
        api.request("/x/")
    assert len(fake.requests) == 1                     # 4xx（除 429）不重试


def test_rate_limit_retries_then_raises_with_retry_after(api, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(api, "sleep", sleeps.append)
    fake = UrlopenRecorder(*([http_error("https://api.invalid/x", 429, retry_after="7")] * 4))
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(SteamRateLimitError) as excinfo:
        api.request("/x/")
    assert excinfo.value.retry_after == 7
    assert len(fake.requests) == 4                     # 首发 + 3 次重试
    assert sleeps[:1] == [7.0]                         # Retry-After 优先于退避


def test_rate_limit_can_recover(api, monkeypatch):
    monkeypatch.setattr(api, "sleep", lambda _: None)
    fake = UrlopenRecorder(http_error("https://api.invalid/x", 429), {"response": {}})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert api.request("/x/") == {"response": {}}


def test_server_errors_exhaust_retries_as_network_error(api, monkeypatch):
    monkeypatch.setattr(api, "sleep", lambda _: None)
    fake = UrlopenRecorder(*([http_error("https://api.invalid/x", 503)] * 4))
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(SteamNetworkError):
        api.request("/x/")
    assert len(fake.requests) == 4


def test_timeouts_are_network_errors(api, monkeypatch):
    monkeypatch.setattr(api, "sleep", lambda _: None)
    fake = UrlopenRecorder(*([urllib.error.URLError("timed out")] * 4))
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(SteamNetworkError):
        api.request("/x/")


def test_malformed_json_is_network_error(api, monkeypatch):
    class BadBody:
        def read(self): return b"<html>not json</html>"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: BadBody())
    with pytest.raises(SteamNetworkError):
        api.request("/x/")


def test_api_key_never_appears_in_exceptions(api, monkeypatch):
    """红线：api_key 绝不出现在异常信息里，一律 ***。"""
    def raise_url_error(request, timeout=None):
        # urllib 的报错常把完整 URL 带出来——那里面就有 key
        raise urllib.error.URLError(f"failed at {request.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", raise_url_error)
    with pytest.raises(SteamNetworkError) as excinfo:
        api.request("/IPlayerService/GetOwnedGames/v1/", {"steamid": SID})
    message = str(excinfo.value)
    assert KEY not in message
    assert "***" in message


def test_owned_games_without_key_message_has_no_plaintext_key(tmp_path):
    """验收 #8。"""
    with pytest.raises(SteamAuthError) as excinfo:
        SteamGameClient(None, cache_path=tmp_path / "cache").get_owned_games(SID)
    assert KEY not in str(excinfo.value)


def test_request_query_shape(api, monkeypatch):
    fake = UrlopenRecorder({"ok": True})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    api.request("/x/", {"include_free": True, "skip_unvetted_apps": "false", "steamid": SID})
    url = fake.requests[0]
    assert url.startswith(API_BASE)
    assert "format=json" in url
    assert f"key={KEY}" in url
    assert "include_free=1" in url                # bool 一律转 1/0
    assert "skip_unvetted_apps=false" in url


def test_request_uses_fixed_user_agent(api, monkeypatch):
    seen: dict[str, str] = {}

    class Ok:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def capture(request, timeout=None):
        seen["ua"] = request.get_header("User-agent")
        return Ok()

    monkeypatch.setattr(urllib.request, "urlopen", capture)
    api.request("/x/")
    assert seen["ua"] == "OmniShelf-steam-client/0.1 (public data queries)"


def test_facade_assembles_both_modules(tmp_path):
    """门面只组装：.user / .game 到位，两个子模块各持一个请求层。

    按类名而不是 isinstance 断言：插件管理器会重载 ``steam_client``，
    同一个类在 ``sys.modules`` 里可能有两份副本，isinstance 会误报。
    """
    client = make_client(FakeAPI(), tmp_path / "cache")
    assert type(client.user).__name__ == "SteamUserClient"
    assert type(client.game).__name__ == "SteamGameClient"
    assert isinstance(client.user.api, FakeAPI) and isinstance(client.game.api, FakeAPI)
