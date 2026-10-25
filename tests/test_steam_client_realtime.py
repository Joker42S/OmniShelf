"""游戏库 / 愿望单：实时请求、key 前置校验、空响应语义、缓存隔离。

覆盖验收用例 #2、#3、#7，以及「用户数据路径不经过缓存」的结构断言。
"""

from __future__ import annotations

import pytest

from tests.steam_synth import SID, FakeAPI, make_client, owned_entry
from steam_client import SteamDataUnavailableError, SteamGameClient
from steam_client.errors import SteamAuthError


def test_owned_games_is_realtime_and_never_touches_cache(tmp_path):
    """验收 #2：同参数两次 → 两次请求；cache_path 下不产生用户维度文件。"""
    fake = FakeAPI(owned=[owned_entry(730), owned_entry(1097110, "EA SPORTS FC 26")])
    client = make_client(fake, tmp_path / "cache")
    games = client.game.get_owned_games(SID)
    client.game.get_owned_games(SID)
    assert fake.call_count == 2
    assert [g.appid for g in games] == [730, 1097110]
    assert games[0].name == "Counter-Strike 2"
    assert games[0].playtime_forever == 640
    assert games[0].playtime_2weeks is None
    assert not (tmp_path / "cache" / "users").exists()
    assert list((tmp_path / "cache").rglob("apps/**/*.json")) == []


def test_owned_games_params_are_explicit(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / "cache")
    client.game.get_owned_games(SID, include_free=False, include_appinfo=False)
    params = fake.calls[0]["params"]
    assert params["steamid"] == SID
    assert params["include_played_free_games"] is False
    assert params["include_appinfo"] is False
    # 显式 false，不依赖接口默认值
    assert params["skip_unvetted_apps"] == "false"


def test_owned_games_requires_key_before_request(tmp_path):
    """无 key：入口直接抛 SteamAuthError，一个请求都不发。"""
    fake = FakeAPI()
    client = SteamGameClient(None, cache_path=tmp_path / "cache")
    client.api = fake
    with pytest.raises(SteamAuthError):
        client.get_owned_games(SID)
    assert fake.call_count == 0


def test_wishlist_is_realtime(tmp_path):
    """验收 #3：同参数两次 → 两次请求；返回 appid 列表。"""
    fake = FakeAPI(wishlist=[{"appid": 730}, {"appid": 230}])
    client = make_client(fake, tmp_path / "cache")
    wish = client.game.get_wishlist(SID)
    client.game.get_wishlist(SID)
    assert fake.call_count == 2
    assert wish == [730, 230]
    assert not (tmp_path / "cache" / "users").exists()


@pytest.mark.parametrize("endpoint", ["owned", "wishlist"])
def test_empty_response_raises_unavailable(tmp_path, endpoint):
    """验收 #7：空数据抛 SteamDataUnavailableError，文案固定，且不落任何文件。"""
    if endpoint == "owned":
        fake = FakeAPI(owned=[])
    else:
        fake = FakeAPI(wishlist=[])
    client = make_client(fake, tmp_path / "cache")
    call = (lambda: client.game.get_owned_games(SID)) if endpoint == "owned" \
        else (lambda: client.game.get_wishlist(SID))
    with pytest.raises(SteamDataUnavailableError) as excinfo:
        call()
    assert str(excinfo.value) == "游戏信息不可用或未公开"
    assert not (tmp_path / "cache" / "users").exists()
    assert list((tmp_path / "cache").rglob("apps/**/*.json")) == []


def test_realtime_endpoints_survive_exploding_cache(tmp_path):
    """文档 §9 的断言方式：把客户端的缓存换成「碰一下就抛」的替身后，
    get_owned_games / get_wishlist 仍然正常——证明实现没绕道缓存。"""
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"实时路径不该碰缓存（{name}）")

    fake = FakeAPI()
    client = SteamGameClient("k", cache_path=tmp_path / "cache")
    client.cache = Exploding()
    client.api = fake
    assert client.get_owned_games(SID)
    assert client.get_wishlist(SID)


def test_steamid_format_error_is_value_error(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / "cache")
    with pytest.raises(ValueError):
        client.game.get_owned_games("12345")
    with pytest.raises(ValueError):
        client.game.get_wishlist("12345")
    assert fake.call_count == 0
