"""商店信息补全：单条请求 + ≤8 路并发 + 按连续失败熔断 + 失败后补跑。

盯的是两次现场教训：

* 2026-10-18：``appdetails`` 的逗号多 appid 写法会被 Steam 直接回 HTTP 400
  （实测 ``appids=730,570`` → 400，``appids=730`` → 200），只能逐条问；
  逐条问的代价在链路断掉时最要命——每条都要付 4 次超时，300 款能挂半小时，
  所以链路真断了要收手（但按**连续**失败判定，一次抖动不能连坐后面几百条）；
* 2026-10-30：一次网络失败把愿望单尾部 100 多款全留成数字标题。失败/被跳过的 id
  必须隔一会儿补跑，而不是直接当作「不可用」。
"""

from __future__ import annotations

import threading

import pytest

from tests.steam_synth import FakeAPI, app_data, make_client
from steam_client import SteamNetworkError
from steam_client.errors import CacheError, SteamAuthError, SteamError, SteamRateLimitError
from steam_client.game import _MAX_ENRICH_WORKERS
from steam_client.transport import API_BASE

CACHE = "cache"


def _ids(count: int, start: int = 1) -> list[int]:
    return list(range(start, start + count))


class _BusyAPI(FakeAPI):
    """在真替身之外再记一笔「同时在飞几个请求」，用来验证并发上限。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def request(self, path, params=None, *, key=None, base=API_BASE):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        threading.Event().wait(0.02)
        try:
            return super().request(path, params, key=key, base=base)
        finally:
            with self._lock:
                self.active -= 1


def test_multi_appid_list_is_refused(tmp_path):
    """假请求层照抄真机的 400：谁把代码改回逗号批量，这里立刻红。"""
    fake = FakeAPI(app_data={730: app_data(730), 570: app_data(570)})
    with pytest.raises(SteamError):
        fake.request("/api/appdetails", {"appids": "730,570", "cc": "US"})


def test_each_uncached_id_is_requested_alone(tmp_path):
    ids = _ids(16, start=100)
    fake = FakeAPI(app_data={appid: app_data(appid) for appid in ids})
    client = make_client(fake, tmp_path / CACHE)
    details = client.game.enrich_apps(ids)

    requested = [call["params"]["appids"] for call in fake.calls_to("appdetails")]
    assert sorted(requested) == sorted(ids)                 # 一款一次，一条不多一条不少
    assert all("," not in str(value) for value in requested)
    assert [detail.appid if detail else None for detail in details] == ids
    assert len(list(client.game.cache.apps_dir.glob("*/*.json"))) == 16


def test_concurrency_stays_under_the_cap(tmp_path):
    fake = _BusyAPI(app_data={appid: app_data(appid) for appid in _ids(16, start=100)})
    client = make_client(fake, tmp_path / CACHE)
    details = client.game.enrich_apps(_ids(16, start=100))

    assert all(detail is not None for detail in details)
    assert fake.peak > 1                                    # 确实是并发，不是排着队等
    assert fake.peak <= _MAX_ENRICH_WORKERS


def test_cached_ids_stay_out_of_the_requests(tmp_path):
    fake = FakeAPI(app_data={appid: app_data(appid) for appid in (730, 570, 440)})
    client = make_client(fake, tmp_path / CACHE)
    client.game.get_app_details(730)                        # 先让一条进缓存
    client.game.enrich_apps([730, 570, 440])

    requested = [call["params"]["appids"] for call in fake.calls_to("appdetails")][1:]
    assert sorted(requested) == [440, 570]


def test_second_round_needs_no_request_at_all(tmp_path):
    fake = FakeAPI(app_data={appid: app_data(appid) for appid in (730, 570)})
    client = make_client(fake, tmp_path / CACHE)
    client.game.enrich_apps([730, 570])
    client.game.enrich_apps([730, 570])
    assert fake.call_count == 2


def test_network_failure_fuses_the_rest_of_the_queue(tmp_path):
    """链路真断了就收手：排队的 id 留空，不再一个个去撞超时（补跑也只会撞有限几次）。"""
    ids = _ids(200)
    fake = FakeAPI(app_data={appid: app_data(appid) for appid in ids},
                   app_error=SteamNetworkError("timed out"))
    client = make_client(fake, tmp_path / CACHE)
    client.game.recovery_pause = 0
    details = client.game.enrich_apps(ids)

    assert details == [None] * len(ids)
    calls = len(fake.calls_to("appdetails"))
    assert 1 <= calls <= 4 * _MAX_ENRICH_WORKERS            # 撞墙的只有有限几拨，绝不是 200 次
    assert list(client.game.cache.apps_dir.glob("*/*.json")) == []


def test_consecutive_failures_trip_the_breaker_but_a_later_round_recovers_the_tail(tmp_path):
    """2026-10-30 现场：一段网络抖动把愿望单尾部 50 多条全跳过，结果全是数字标题。

    熔断可以开，但跳过/失败的 id 要在补跑那一轮里再问一次；链路只是抖了一下时，
    整份愿望单仍然每条都有真名字。
    """
    ids = _ids(60, start=900)
    fake = FakeAPI(
        app_data={appid: app_data(appid, f"Game {appid}") for appid in ids},
        # 前 8 次 appdetails 调用失败（足够撞开熔断），之后链路恢复——和 appid 无关
        app_fail_calls=8,
        app_fail_error=SteamNetworkError("timed out"),
    )
    client = make_client(fake, tmp_path / CACHE)
    client.game.recovery_pause = 0
    stats: dict[str, int] = {}

    details = client.game.enrich_apps(ids, stats=stats)

    assert [detail.name if detail else None for detail in details] == [f"Game {appid}" for appid in ids]
    assert stats == {"total": 60, "cached": 0, "resolved": 60, "unavailable": 0, "failed": 0}
    requested = [int(call["params"]["appids"]) for call in fake.calls_to("appdetails")]
    assert set(requested) == set(ids)                      # 被跳过的尾部也补问过了
    assert len(requested) < 2 * len(ids)                   # 补跑次数有限，不是无限重试


def test_stats_separate_unavailable_from_persistent_network_failure(tmp_path):
    """「商店说不给」和「网络没取回」要分开计数，插件才能给出不同的日志。"""
    ids = _ids(4, start=900)
    fake = FakeAPI(
        app_data={appid: app_data(appid) for appid in ids},
        app_missing={900},
        app_errors={901: SteamNetworkError("boom")},
    )
    client = make_client(fake, tmp_path / CACHE)
    client.game.recovery_pause = 0
    stats: dict[str, int] = {}

    details = client.game.enrich_apps(ids, stats=stats)

    assert [detail is not None for detail in details] == [False, False, True, True]
    assert stats == {"total": 4, "cached": 0, "resolved": 2, "unavailable": 1, "failed": 1}


def test_rate_limited_id_is_retried_not_marked_unavailable(tmp_path):
    fake = FakeAPI(
        app_data={900: app_data(900, "Retry Me")},
        app_error_once={900: SteamRateLimitError("HTTP 429", retry_after=1)},
    )
    client = make_client(fake, tmp_path / CACHE)
    client.game.recovery_pause = 0

    details = client.game.enrich_apps([900])

    assert details[0] is not None and details[0].name == "Retry Me"


def test_other_failures_do_not_trip_the_breaker(tmp_path):
    """401/400 这类单条问题不代表链路断了：后面的 id 照常问。"""
    ids = _ids(5, start=700)
    fake = FakeAPI(app_error=SteamAuthError("HTTP 401"))
    client = make_client(fake, tmp_path / CACHE)
    details = client.game.enrich_apps(ids)

    assert details == [None] * len(ids)
    assert len(fake.calls_to("appdetails")) == len(ids)


def test_duplicate_ids_ask_once_and_stay_aligned(tmp_path):
    fake = FakeAPI(app_data={730: app_data(730), 570: app_data(570, "PINY")})
    client = make_client(fake, tmp_path / CACHE)
    details = client.game.enrich_apps([730, 570, 730])

    assert [detail.appid if detail else None for detail in details] == [730, 570, 730]
    assert len(fake.calls_to("appdetails")) == 2


def test_force_refresh_refetches_every_id(tmp_path):
    fake = FakeAPI(app_data={730: app_data(730), 570: app_data(570, "PINY")})
    client = make_client(fake, tmp_path / CACHE)
    client.game.enrich_apps([730, 570])
    client.game.enrich_apps([730, 570], force_refresh=True)

    assert fake.call_count == 4
    assert sorted(call["params"]["appids"] for call in fake.calls_to("appdetails")) == [570, 570, 730, 730]


def test_cache_is_partitioned_by_region(tmp_path):
    fake = FakeAPI(app_data={730: app_data(730)})
    client = make_client(fake, tmp_path / CACHE)
    client.game.enrich_apps([730], cc="CN")

    assert fake.calls[0]["params"]["cc"] == "CN"
    assert client.game.cache.path(730, "CN").is_file()
    assert not client.game.cache.path(730, "US").exists()

    client.game.enrich_apps([730], cc="US")                 # US 不能冒领 CN 那份报价
    assert fake.call_count == 2
    assert client.game.cache.path(730, "US").is_file()


def test_empty_input_makes_no_request(tmp_path):
    fake = FakeAPI()
    client = make_client(fake, tmp_path / CACHE)
    assert client.game.enrich_apps([]) == []
    assert client.game.get_app_details_map([]) == {}
    assert fake.call_count == 0


def test_unwritable_cache_still_returns_the_fetched_detail(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """缓存写不进去（磁盘满之类）不能把手上这份商店信息一起丢掉。"""
    fake = FakeAPI(app_data={730: app_data(730)})
    client = make_client(fake, tmp_path / CACHE)

    def refuse(*args, **kwargs):
        raise CacheError("disk full")

    monkeypatch.setattr(client.game.cache, "set", refuse)
    details = client.game.enrich_apps([730])
    assert details[0] is not None and details[0].name == "Counter-Strike 2"
