"""app 详情缓存：命中/强制刷新/失败不污染/损坏自愈/过期/隔离。

覆盖验收用例 #4、#5、#6、#9 与 enrich_apps 的顺序对齐。
"""

from __future__ import annotations

import json
import time

import pytest

from tests.steam_synth import SID, FakeAPI, app_data, make_client, owned_entry
from steam_client import SteamNetworkError
from steam_client.cache import FileCache
from steam_client.errors import CacheError

CACHE = "cache"


def _envelope_of(client, appid: int) -> dict:
    path = client.game.cache.path(appid)
    return json.loads(path.read_text(encoding="utf-8"))


def test_first_call_writes_cache_second_hits(tmp_path):
    """验收 #4：首次写缓存；第二次不发请求。"""
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    first = client.game.get_app_details(730)
    second = client.game.get_app_details(730)
    assert fake.call_count == 1
    assert first is not None and second is not None
    assert first.name == second.name == "Counter-Strike 2"
    assert first.price_cents == 999 and first.currency == "USD"
    assert first.header_image and first.release_date == "21 Aug, 2012"
    assert not first.is_free
    envelope = _envelope_of(client, 730)
    assert envelope["_source"] == "appdetails"
    assert envelope["_ttl"] == 604800
    assert envelope["_cc"] == "US"
    assert envelope["payload"]["name"] == "Counter-Strike 2"
    assert (tmp_path / CACHE / "apps" / "US" / "730.json").is_file()


def test_cache_is_partitioned_by_region(tmp_path):
    """价格随商店区域变化：US 与 CN 各存一份，互不冒领（真实 bug 回归）。"""
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    client.game.get_app_details(730, cc="US")
    client.game.get_app_details(730, cc="CN")           # 换了区域必须重新请求
    assert fake.call_count == 2
    assert [c["params"]["cc"] for c in fake.calls] == ["US", "CN"]
    assert (tmp_path / CACHE / "apps" / "US" / "730.json").is_file()
    assert (tmp_path / CACHE / "apps" / "CN" / "730.json").is_file()
    client.game.get_app_details(730, cc="US")           # 各自命中各自的
    assert fake.call_count == 2


def test_force_refresh_always_requests_and_overwrites(tmp_path):
    """验收 #5：force_refresh 必发请求并覆盖写。"""
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    client.game.get_app_details(730)
    client.game.get_app_details(730, force_refresh=True)
    assert fake.call_count == 2
    assert fake.calls[1]["params"]["appids"] == 730
    assert fake.calls[1]["params"]["cc"] == "US"


def test_force_refresh_failure_keeps_old_cache(tmp_path):
    """验收 #6：失败不污染缓存——force_refresh 失败后旧文件仍在，普通调用仍命中。"""
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    client.game.get_app_details(730)
    fake.app_error = SteamNetworkError("boom")
    with pytest.raises(SteamNetworkError):
        client.game.get_app_details(730, force_refresh=True)
    fake.app_error = None
    calls_before = fake.call_count
    detail = client.game.get_app_details(730)
    assert detail is not None and detail.name == "Counter-Strike 2"
    assert fake.call_count == calls_before            # 旧缓存还在，一次没发


def test_success_false_returns_none_and_does_not_delete(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    client.game.get_app_details(730)                  # 先写 good 缓存
    fake.app_missing = {730}
    assert client.game.get_app_details(730, force_refresh=True) is None
    assert client.game.cache.path(730).is_file()      # 不删
    fake.app_missing = set()
    assert client.game.get_app_details(730) is not None   # 普通调用仍命中旧缓存


def test_unknown_app_has_no_cache_file(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    assert client.game.get_app_details(999999) is None
    assert not client.game.cache.path(999999).exists()


def test_corrupted_cache_is_silent_miss(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    client.game.get_app_details(730)
    client.game.cache.path(730).write_text("{not json", encoding="utf-8")
    detail = client.game.get_app_details(730)
    assert fake.call_count == 2                        # 损坏 → 未命中，重新请求
    assert detail is not None
    assert _envelope_of(client, 730)["payload"]["name"] == "Counter-Strike 2"


def test_expired_cache_refetches(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE, app_ttl=60)
    client.game.get_app_details(730)
    envelope = _envelope_of(client, 730)
    envelope["_cached_at"] = time.time() - 3600        # 把时间戳拨老一小时
    client.game.cache.path(730).write_text(json.dumps(envelope), encoding="utf-8")
    client.game.get_app_details(730)
    assert fake.call_count == 2


def test_enrich_apps_aligns_and_survives_failures(tmp_path):
    fake = FakeAPI(
        app_data={
            730: app_data(730, "Counter-Strike 2"),
            570: app_data(570, "PINY"),
        },
        app_missing={230},
    )
    client = make_client(fake, tmp_path / CACHE)
    details = client.game.enrich_apps([730, 230, 570])
    assert [d.appid if d else None for d in details] == [730, None, 570]
    assert [d.name if d else None for d in details] == ["Counter-Strike 2", None, "PINY"]


def test_enrich_apps_network_failure_yields_none(tmp_path):
    fake = FakeAPI(app_error=SteamNetworkError("down"))
    client = make_client(fake, tmp_path / CACHE)
    assert client.game.enrich_apps([730, 440]) == [None, None]


def test_enrich_empty_input(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    assert client.game.enrich_apps([]) == []
    assert fake.call_count == 0


def test_clear_cache_only_touches_apps(tmp_path):
    """验收 #9：clear_cache 只删 apps/，缓存目录里其他文件不受影响。"""
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    client.game.get_app_details(730)
    client.game.get_app_details(570) if 570 in fake.app_data else None
    (tmp_path / CACHE / "other.txt").write_text("x", encoding="utf-8")
    removed = client.game.clear_cache()
    assert removed == 1
    assert (tmp_path / CACHE / "other.txt").exists()
    assert not client.game.cache.path(730).exists()


def test_cache_dir_unwritable_raises_at_construction(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    with pytest.raises(CacheError):
        FileCache(blocker / "steam_cache")


def test_realtime_paths_use_no_force_refresh_parameter():
    """硬约束 2 的签名面：get_owned_games / get_wishlist 不接受 force_refresh。"""
    import inspect

    from steam_client import SteamGameClient

    for name in ("get_owned_games", "get_wishlist"):
        signature = inspect.signature(getattr(SteamGameClient, name))
        assert "force_refresh" not in signature.parameters
