"""netease 模块：ncm-cli 交互封装（登录 / 收藏歌单 / 分页与全量歌曲）。

全部离线：每条命令都由 ``FakeRunner`` 回答，不执行真实 ncm-cli、不出网。
"""

from __future__ import annotations

import subprocess

import pytest

from tests.netease_synth import (
    CREATED_ID,
    FAVORITE_ID,
    FAVORITE_ORIGINAL_ID,
    LOGIN_URL,
    FakeClock,
    FakeRunner,
    command_args,
    created_payload,
    is_check,
    is_created,
    is_login,
    is_tracks,
    json_result,
    login_prompt_payload,
    login_status,
    option,
    text_result,
    track,
    tracks_payload,
)
from netease import (  # type: ignore[import-not-found]
    NcmCliError,
    NcmCliNotFound,
    NcmCliOutputError,
    NeteaseApiError,
    NeteaseClient,
    NeteaseLoginError,
    NeteaseLoginTimeout,
)


# ---------------------------------------------------------------- 登录

def test_login_returns_immediately_when_already_logged_in():
    def handler(command, timeout):
        assert is_check(command), command
        return json_result(command, login_status(True))

    runner = FakeRunner(handler)
    client = NeteaseClient(runner=runner)
    result = client.login()
    assert result.logged_in is True
    assert result.prompt is None
    assert [command_args(call) for call in runner.calls] == [
        ["login", "--check", "--output", "json"]
    ]


def test_login_generates_prompt_then_waits_until_confirmed():
    state = {"checks": 0}
    prompts: list[str] = []
    statuses: list[tuple[bool, int]] = []

    def handler(command, timeout):
        if is_check(command):
            state["checks"] += 1
            if state["checks"] == 1:
                return json_result(command, login_status(False))
            return json_result(command, login_status(True))
        if is_login(command):
            return json_result(command, login_prompt_payload())
        raise AssertionError(command)

    clock = FakeClock()
    runner = FakeRunner(handler)
    client = NeteaseClient(
        runner=runner, clock=clock.monotonic, sleep=clock.sleep
    )
    result = client.login(
        on_prompt=lambda prompt: prompts.append(prompt.url),
        on_status=lambda status, poll: statuses.append((status.logged_in, poll)),
    )

    assert result.logged_in is True
    assert result.prompt is not None and result.prompt.url == LOGIN_URL
    assert result.elapsed == 2.0
    assert prompts == [LOGIN_URL]
    assert statuses == [(True, 1)]
    assert [command_args(call) for call in runner.calls] == [
        ["login", "--check", "--output", "json"],
        ["login", "--output", "json"],
        ["login", "--check", "--output", "json"],
    ]


def test_login_without_wait_returns_prompt_without_polling():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(False))
        if is_login(command):
            return json_result(command, login_prompt_payload())
        raise AssertionError(command)

    runner = FakeRunner(handler)
    client = NeteaseClient(runner=runner)
    result = client.login(wait=False)
    assert result.logged_in is False
    assert result.prompt is not None and result.prompt.url == LOGIN_URL
    assert len(runner.calls) == 2


def test_login_timeout_does_not_put_the_link_into_the_error_text():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(False))
        if is_login(command):
            return json_result(command, login_prompt_payload())
        raise AssertionError(command)

    clock = FakeClock()
    client = NeteaseClient(
        runner=FakeRunner(handler),
        clock=clock.monotonic,
        sleep=clock.sleep,
        poll_interval=2.0,
        login_timeout=999.0,
    )
    with pytest.raises(NeteaseLoginTimeout) as excinfo:
        client.login(timeout=5.0)

    assert LOGIN_URL not in str(excinfo.value)
    assert excinfo.value.prompt is not None
    assert excinfo.value.prompt.url == LOGIN_URL
    assert clock.now == 6.0                       # 2 / 4 / 6 三次检查后超过 5 秒


def test_login_failure_from_cli_is_reported():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(False))
        if is_login(command):
            return json_result(command, {"success": False, "message": "生成二维码失败"})
        raise AssertionError(command)

    with pytest.raises(NeteaseLoginError, match="生成二维码失败"):
        NeteaseClient(runner=FakeRunner(handler)).login()


def test_login_accepts_non_json_output_when_a_link_is_present():
    state = {"logged_in": False}

    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(state["logged_in"]))
        if is_login(command):
            state["logged_in"] = True
            return text_result(command, f"请打开链接登录：{LOGIN_URL}\n")
        raise AssertionError(command)

    clock = FakeClock()
    client = NeteaseClient(
        runner=FakeRunner(handler), clock=clock.monotonic, sleep=clock.sleep
    )
    result = client.login()
    assert result.logged_in is True
    assert result.prompt is not None and result.prompt.url == LOGIN_URL


def test_check_login_handles_non_json_token_expiry():
    def handler(command, timeout):
        return text_result(command, "❌ TokenExpiredError: 登录已过期，请重新登录\n")

    status = NeteaseClient(runner=FakeRunner(handler)).check_login()
    assert status.logged_in is False
    assert "登录已过期" in status.message


# ---------------------------------------------------------------- 收藏歌单

def test_favorite_playlist_uses_last_record_and_encrypted_id():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(True))
        if is_created(command):
            return json_result(command, created_payload())
        raise AssertionError(command)

    runner = FakeRunner(handler)
    playlist = NeteaseClient(runner=runner).favorite_playlist()

    assert playlist.id == FAVORITE_ID               # 加密 id
    assert playlist.id != CREATED_ID
    assert playlist.original_id == FAVORITE_ORIGINAL_ID
    assert playlist.name == "Synthetic Favorites"
    assert playlist.track_count == 3

    created = runner.calls[1]
    assert option(created, "--limit") == "1"
    assert option(created, "--offset") == "0"
    assert option(created, "--output") == "json"


def test_favorite_playlist_requires_login_first():
    runner = FakeRunner(lambda command, timeout: json_result(command, login_status(False)))
    with pytest.raises(NeteaseLoginError, match="未登录"):
        NeteaseClient(runner=runner).favorite_playlist()
    assert all(is_check(call) for call in runner.calls)


def test_playlist_command_token_expiry_becomes_login_error():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(True))
        if is_created(command):
            return text_result(command, "❌ TokenExpiredError: 登录已过期，请重新登录\n")
        raise AssertionError(command)

    with pytest.raises(NeteaseLoginError, match="登录"):
        NeteaseClient(runner=FakeRunner(handler)).favorite_playlist()


def test_playlist_empty_records_is_api_error():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(True))
        return json_result(command, created_payload(records=[]))

    with pytest.raises(NeteaseApiError, match="没有歌单"):
        NeteaseClient(runner=FakeRunner(handler)).favorite_playlist()


def test_playlist_missing_encrypted_id_is_api_error():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(True))
        return json_result(command, created_payload(records=[
            {"originalId": 1, "name": "No encrypted id", "trackCount": 1},
        ]))

    with pytest.raises(NeteaseApiError, match="加密 id"):
        NeteaseClient(runner=FakeRunner(handler)).favorite_playlist()


def test_playlist_api_code_error_is_reported():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(True))
        return json_result(command, {"code": 400, "message": "参数错误"})

    with pytest.raises(NeteaseApiError, match="code=400"):
        NeteaseClient(runner=FakeRunner(handler)).favorite_playlist()


# ---------------------------------------------------------------- 歌曲

def _favorite_handler(pages, *, track_count=3):
    """按 offset 分页回答 tracks；同时处理 check / created。"""
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(True))
        if is_created(command):
            return json_result(command, created_payload(track_count=track_count))
        if is_tracks(command):
            offset = option(command, "--offset")
            return json_result(command, tracks_payload(pages.get(offset, [])))
        raise AssertionError(command)
    return handler


def test_favorite_tracks_page_uses_offset_and_limit():
    runner = FakeRunner(_favorite_handler({"0": [track(1), track(2)],
                                           "2": [track(3)]}))
    rows = NeteaseClient(runner=runner).favorite_tracks(page=2, page_size=2)
    assert [row["name"] for row in rows] == ["Synthetic Track 3"]
    tracks_calls = [call for call in runner.calls if is_tracks(call)]
    assert len(tracks_calls) == 1
    assert option(tracks_calls[0], "--offset") == "2"
    assert option(tracks_calls[0], "--limit") == "2"
    assert option(tracks_calls[0], "--playlistId") == FAVORITE_ID


def test_favorite_tracks_without_page_fetches_everything():
    progress = []
    runner = FakeRunner(_favorite_handler({"0": [track(1), track(2)],
                                           "2": [track(3)]}))
    rows = NeteaseClient(runner=runner).favorite_tracks(
        page_size=2, on_page=lambda page, fetched, total: progress.append(
            (page, fetched, total))
    )
    assert [row["name"] for row in rows] == [
        "Synthetic Track 1", "Synthetic Track 2", "Synthetic Track 3",
    ]
    tracks_calls = [call for call in runner.calls if is_tracks(call)]
    assert [option(call, "--offset") for call in tracks_calls] == ["0", "2"]
    assert progress == [(1, 2, 3), (2, 3, 3)]


def test_favorite_tracks_stops_on_short_page_even_without_total():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(True))
        if is_created(command):
            row = created_payload()["data"]["records"][-1] | {"trackCount": None}
            return json_result(command, created_payload(records=[
                created_payload()["data"]["records"][0], row,
            ]))
        if is_tracks(command):
            offset = option(command, "--offset")
            rows = {"0": [track(1), track(2)], "2": [track(3)]}.get(offset, [])
            return json_result(command, tracks_payload(rows))
        raise AssertionError(command)

    runner = FakeRunner(handler)
    rows = NeteaseClient(runner=runner).favorite_tracks(page_size=2)
    assert len(rows) == 3
    tracks_calls = [call for call in runner.calls if is_tracks(call)]
    assert [option(call, "--offset") for call in tracks_calls] == ["0", "2"]


def test_all_fetch_advances_offset_by_returned_rows_when_server_clamps():
    def handler(command, timeout):
        if is_check(command):
            return json_result(command, login_status(True))
        if is_created(command):
            return json_result(command, created_payload(track_count=5))
        if is_tracks(command):
            offset = int(option(command, "--offset"))
            rows = {0: [track(1), track(2)], 2: [track(3), track(4)],
                    4: [track(5)]}.get(offset, [])
            return json_result(command, tracks_payload(rows))
        raise AssertionError(command)

    runner = FakeRunner(handler)
    rows = NeteaseClient(runner=runner).favorite_tracks(page_size=3)
    assert len(rows) == 5
    tracks_calls = [call for call in runner.calls if is_tracks(call)]
    assert [option(call, "--offset") for call in tracks_calls] == ["0", "2", "4"]


# ---------------------------------------------------------------- 增量

def test_incremental_stops_when_a_page_is_all_known():
    pages = {0: [track(1), track(2)], 2: [track(3), track(4)],
             4: [track(5)]}

    def handler(command, timeout):
        offset = int(option(command, "--offset"))
        return json_result(command, tracks_payload(pages.get(offset, [])))

    runner = FakeRunner(handler)
    known = {900003, 900004}
    result = NeteaseClient(runner=runner).fetch_playlist_tracks(
        FAVORITE_ID,
        page_size=2,
        known=lambda row: row["originalId"] in known,
    )

    # 第 2 页整页都是已知歌曲就停：第 5 首（更旧）不再请求
    assert [row["originalId"] for row in result.rows] == [
        900001, 900002, 900003, 900004,
    ]
    assert result.requests == 2
    assert result.complete is False and result.stopped_early is True
    assert [option(call, "--offset") for call in runner.calls] == ["0", "2"]


def test_incremental_stops_on_the_very_first_page_when_nothing_is_new():
    def handler(command, timeout):
        return json_result(command, tracks_payload([track(1), track(2)]))

    runner = FakeRunner(handler)
    result = NeteaseClient(runner=runner).fetch_playlist_tracks(
        FAVORITE_ID,
        page_size=2,
        known=lambda row: True,
    )

    assert result.rows == [track(1), track(2)]
    assert result.requests == 1
    assert result.complete is False


def test_fetch_without_known_reads_until_short_page():
    pages = {0: [track(1), track(2)], 2: [track(3)]}

    def handler(command, timeout):
        offset = int(option(command, "--offset"))
        return json_result(command, tracks_payload(pages.get(offset, [])))

    result = NeteaseClient(runner=FakeRunner(handler)).fetch_playlist_tracks(
        FAVORITE_ID, page_size=2)
    assert len(result.rows) == 3
    assert result.requests == 2
    assert result.complete is True and result.stopped_early is False


def test_playlist_tracks_accepts_data_mapping_records():
    def handler(command, timeout):
        assert is_tracks(command)
        return json_result(command, {"code": 200, "data": {"records": [track(7)]}})

    rows = NeteaseClient(runner=FakeRunner(handler)).playlist_tracks(
        FAVORITE_ID, page=1, page_size=10)
    assert [row["originalId"] for row in rows] == [900007]


def test_playlist_tracks_rejects_unknown_data_shape():
    def handler(command, timeout):
        return json_result(command, {"code": 200, "data": {"unexpected": []}})

    with pytest.raises(NeteaseApiError, match="不是数组"):
        NeteaseClient(runner=FakeRunner(handler)).playlist_tracks(
            FAVORITE_ID, page=1)


def test_tracks_api_code_error_is_reported():
    def handler(command, timeout):
        return json_result(command, {"code": 500, "message": "服务端异常"})

    with pytest.raises(NeteaseApiError, match="code=500"):
        NeteaseClient(runner=FakeRunner(handler)).playlist_tracks(
            FAVORITE_ID, page=1)


# ---------------------------------------------------------------- 参数与输出

def test_invalid_arguments_are_rejected_before_running_anything():
    runner = FakeRunner(lambda command, timeout: pytest.fail("不应执行命令"))
    client = NeteaseClient(runner=runner)

    with pytest.raises(ValueError, match="page"):
        client.favorite_tracks(page=0)
    with pytest.raises(ValueError, match="page_size"):
        client.favorite_tracks(page_size=0)
    with pytest.raises(ValueError, match="page_size"):
        client.favorite_tracks(page_size=501)
    with pytest.raises(ValueError, match="playlist_id"):
        client.playlist_tracks("   ")
    assert runner.calls == []


def test_json_with_a_log_line_in_front_is_still_parsed():
    def handler(command, timeout):
        return text_result(
            command,
            "log: refreshing token\n"
            + '{"success": true, "message": "已登录"}\n',
        )

    status = NeteaseClient(runner=FakeRunner(handler)).check_login()
    assert status.logged_in is True


def test_cli_failure_is_reported_with_exit_code():
    def handler(command, timeout):
        return text_result(command, "", returncode=2, stderr="boom")

    with pytest.raises(NcmCliError, match="退出码 2"):
        NeteaseClient(runner=FakeRunner(handler)).check_login()


def test_non_json_check_output_is_a_parse_error():
    def handler(command, timeout):
        return text_result(command, "not json at all")

    with pytest.raises(NcmCliOutputError, match="不是 JSON"):
        NeteaseClient(runner=FakeRunner(handler)).check_login()


def test_missing_ncm_cli_raises_binary_not_found(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NcmCliNotFound, match="npm install"):
        NeteaseClient().check_login()
