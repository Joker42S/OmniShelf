"""玩家资料模块：实时请求、vanity 解析、格式校验、批量分片。

验收用例 #1（同参数连续两次 ``get_player_summary`` → 请求层被调用 2 次）。
"""

from __future__ import annotations

import pytest

from tests.steam_synth import OTHER_KEY, SID, FakeAPI, make_client, player
from steam_client import SteamDataUnavailableError, SteamNotFoundError, SteamUserClient
from steam_client.errors import SteamAuthError


def test_construction_requires_api_key(tmp_path):
    with pytest.raises(SteamAuthError):
        SteamUserClient("")
    with pytest.raises(SteamAuthError):
        SteamUserClient(None)  # type: ignore[arg-type]


def test_player_summary_is_realtime(tmp_path):
    """硬约束 1：用户模块不读写缓存，两次调用就是两次远端请求。"""
    fake = FakeAPI()
    client = make_client(fake, tmp_path / "cache")
    summary = client.user.get_player_summary(SID)
    summary2 = client.user.get_player_summary(SID)
    assert fake.calls_to("GetPlayerSummaries") and fake.call_count == 2
    assert summary.steamid == SID
    assert summary.persona_name == "SyntheticGuy"
    assert summary.communityvisibilitystate == 3
    assert summary.raw["primarystate"] == "Synthetic Land"
    assert summary2.steamid == SID
    assert not (tmp_path / "cache" / "users").exists()


def test_per_call_api_key_override(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / "cache")
    client.user.get_player_summary(SID, api_key=OTHER_KEY)
    assert fake.calls[0]["key"] == OTHER_KEY


def test_steamid_format_error_is_value_error(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / "cache")
    for bad in ("7656119796028799", "not-a-steamid", "", None):
        with pytest.raises(ValueError):
            client.user.get_player_summary(bad)
    assert fake.call_count == 0                       # 格式错误不发请求


def test_private_profile_is_unavailable_not_empty(tmp_path):
    """空 players 不等于「没有这个人」：按不可用/未公开抛错，不返回假数据。"""
    fake = FakeAPI(players={})
    client = make_client(fake, tmp_path / "cache")
    with pytest.raises(SteamDataUnavailableError):
        client.user.get_player_summary(SID)


def test_resolve_vanity_url(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / "cache")
    assert client.user.resolve_vanity_url("gabelogannewell") == SID
    assert client.user.resolve_vanity_url("  gabelogannewell  ") == SID


def test_resolve_vanity_failure_raises_not_none(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / "cache")
    with pytest.raises(SteamNotFoundError):
        client.user.resolve_vanity_url("definitely-not-a-vanity")
    with pytest.raises(ValueError):
        client.user.resolve_vanity_url("   ")


def test_summaries_chunk_at_100(tmp_path):
    players = {}
    for index in range(250):
        steamid = f"76561198{index:09d}"
        players[steamid] = player(steamid=steamid)
    fake = FakeAPI(players=players)
    client = make_client(fake, tmp_path / "cache")
    ids = list(players)
    summaries = client.user.get_player_summaries(ids)
    chunks = fake.calls_to("GetPlayerSummaries")
    assert len(chunks) == 3                            # 250 个 ID 分 3 片
    assert [len(c["params"]["steamids"].split(",")) for c in chunks] == [100, 100, 50]
    assert len(summaries) == 250


def test_user_module_never_imports_cache():
    """结构约束：user.py 不 import cache（文档 §9）。"""
    import inspect
    import steam_client.user as user_module

    assert "from .cache" not in inspect.getsource(user_module)
    assert "FileCache" not in vars(user_module)
