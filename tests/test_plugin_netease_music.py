"""netease_music 插件：登录、抓取、快照、导入与页面。

测试不出网、也不真的执行 ncm-cli：把 ``netease.client.run_ncm_cli`` 换成
``tests.netease_synth.FakeRunner``（或给插件换一个带假 runner 的 client）。
数据全是合成的（``Synthetic Track N`` / ``.invalid`` 登录链接）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.config import Settings, repo_root
from omnishelf.db import build_engine
from omnishelf.importers.registry import load_builtin_importers
from omnishelf.plugins.manager import PluginError, PluginManager
from tests.netease_synth import (
    FAVORITE_ID,
    FAVORITE_ORIGINAL_ID,
    LOGIN_URL,
    FakeClock,
    FakeRunner,
    created_payload,
    is_check,
    is_created,
    is_login,
    is_tracks,
    json_result,
    login_prompt_payload,
    login_status,
    option,
    track,
    tracks_payload,
)

PLUGIN_ID = "netease_music"
REPO_PLUGINS = repo_root() / "plugins"
SNAPSHOT = "netease_favorites.json"


# --------------------------------------------------------------------- 夹具


@pytest.fixture()
def settings_with_repo_plugins(settings: Settings) -> Settings:
    return replace(settings, plugins_dir=REPO_PLUGINS)


@pytest.fixture()
def engine_for_plugins(settings_with_repo_plugins: Settings, engine: Any):
    return build_engine(settings_with_repo_plugins)


@pytest.fixture()
def plugin_data_dir(settings_with_repo_plugins: Settings) -> Path:
    path = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def manager(settings_with_repo_plugins: Settings, engine_for_plugins) -> PluginManager:
    mgr = PluginManager(
        plugins_dir=settings_with_repo_plugins.plugins_dir,
        plugins_data_dir=settings_with_repo_plugins.plugins_data_dir,
        engine=engine_for_plugins,
        imports_dir=settings_with_repo_plugins.imports_dir,
    )
    mgr.load_all()
    return mgr


class Scenario:
    """一段假 ncm-cli 会话：登录状态 + 歌单 + 歌曲页。"""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        logged_in: bool = True,
        checks_before_login: int = 0,
        track_count: int | None = None,
        playlist_name: str = "Synthetic Favorites",
    ) -> None:
        self.rows = list(rows or [])
        self.logged_in = logged_in
        self.checks_before_login = checks_before_login
        self.track_count = len(self.rows) if track_count is None else track_count
        self.playlist_name = playlist_name
        self.pending = False
        self.calls: list[list[str]] = []

    def handle(self, command: Sequence[str], timeout: float):
        self.calls.append(list(command))
        if is_check(command):
            if self.pending:
                self.checks_before_login -= 1
                if self.checks_before_login <= 0:
                    self.pending = False
                    self.logged_in = True
            message = "已登录实名账号" if self.logged_in else "未登录，请执行 ncm-cli login"
            return json_result(command, login_status(self.logged_in, message))
        if is_login(command):
            self.pending = True
            return json_result(command, login_prompt_payload())
        if is_created(command):
            payload = created_payload(track_count=self.track_count)
            payload["data"]["records"][-1]["name"] = self.playlist_name
            return json_result(command, payload)
        if is_tracks(command):
            offset = int(option(command, "--offset") or 0)
            limit = int(option(command, "--limit") or 100)
            return json_result(command, tracks_payload(self.rows[offset:offset + limit]))
        raise AssertionError(f"假 ncm-cli 不认识的命令：{list(command)}")

    def of(self, predicate) -> list[list[str]]:
        return [call for call in self.calls if predicate(call)]


def install_client(module: Any, monkeypatch: pytest.MonkeyPatch, scenario: Scenario) -> FakeRunner:
    """把插件的 make_client 换成「假 runner + 假时钟」的 client。

    客户端类从**插件模块里取**（``module.NeteaseClient``），而不是测试进程顶部
    import 的那份：插件是 exec 加载的，宿主卸载时会清掉插件目录的子包，
    两份 ``netease`` 的异常类身份可能不同，``except NeteaseLoginTimeout`` 就接不住。
    """
    runner = FakeRunner(scenario.handle)
    clock = FakeClock()
    client_cls = module.NeteaseClient

    def factory() -> Any:
        return client_cls(
            runner=runner,
            clock=clock.monotonic,
            sleep=clock.sleep,
            poll_interval=1.0,
        )

    monkeypatch.setattr(module, "make_client", factory)
    return runner


def module_of(manager: PluginManager) -> Any:
    loaded = manager.get(PLUGIN_ID)
    assert loaded is not None
    return loaded.module


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _all_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


# --------------------------------------------------------------------- 登录状态


def test_status_when_logged_in(manager: PluginManager, monkeypatch: pytest.MonkeyPatch):
    scenario = Scenario(logged_in=True)
    install_client(module_of(manager), monkeypatch, scenario)
    out = manager.run_command(PLUGIN_ID, "status")
    assert "已登录" in out and "已登录实名账号" in out


def test_status_when_logged_out(manager: PluginManager, monkeypatch: pytest.MonkeyPatch):
    scenario = Scenario(logged_in=False)
    install_client(module_of(manager), monkeypatch, scenario)
    out = manager.run_command(PLUGIN_ID, "status")
    assert "未登录" in out


def test_login_cli_prints_link_and_waits_for_confirmation(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    scenario = Scenario(logged_in=False, checks_before_login=2)
    install_client(module_of(manager), monkeypatch, scenario)

    out = manager.run_command(PLUGIN_ID, "login")
    printed = capsys.readouterr().out

    assert LOGIN_URL in printed                 # 链接必须当场打印，用户才扫得到
    assert "登录成功" in out
    assert scenario.of(is_login) and scenario.of(is_check)


def test_login_timeout_does_not_stuff_link_into_error(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    scenario = Scenario(logged_in=False, checks_before_login=10_000)
    install_client(module_of(manager), monkeypatch, scenario)

    out = manager.run_command(PLUGIN_ID, "login", ["--timeout", "5"])
    printed = capsys.readouterr().out

    assert LOGIN_URL in printed                 # 链接照常打印
    assert "超时" in out
    assert LOGIN_URL not in out                 # 返回值/异常文本里不再带链接


# --------------------------------------------------------------------- 抓取


def test_sync_requires_login_and_never_calls_playlist(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch
):
    scenario = Scenario(logged_in=False)
    install_client(module_of(manager), monkeypatch, scenario)

    with pytest.raises(PluginError) as excinfo:
        manager.run_command(PLUGIN_ID, "sync", ["--dry-run"])

    assert "未登录" in str(excinfo.value)
    assert scenario.of(is_created) == []
    assert scenario.of(is_tracks) == []


def test_sync_dry_run_writes_snapshot_only(
    manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    plugin_data_dir: Path,
    engine_for_plugins,
):
    rows = [track(1), track(3), track(2), track(4)]
    scenario = Scenario(rows, track_count=4)
    install_client(module_of(manager), monkeypatch, scenario)

    out = manager.run_command(PLUGIN_ID, "sync", ["--dry-run", "--page-size", "2"])

    assert "抓取完成：4 首（去重后 4 首）" in out
    assert "已跳过（--dry-run）" in out
    # 每页 2 首：offset 0 / 2 / 4，最后一页为空才算到底
    offsets = [option(call, "--offset") for call in scenario.of(is_tracks)]
    assert offsets == ["0", "2", "4"]
    assert all(option(call, "--playlistId") == FAVORITE_ID for call in scenario.of(is_tracks))

    snapshot = _read_json(plugin_data_dir / SNAPSHOT)
    assert snapshot["count"] == 4 and snapshot["unique_count"] == 4
    assert snapshot["playlist"] == {
        "id": FAVORITE_ID,
        "original_id": FAVORITE_ORIGINAL_ID,
        "name": "Synthetic Favorites",
        "track_count": 4,
    }
    first = snapshot["items"][0]
    assert first == {
        "song_id": 900001,
        "name": "Synthetic Track 1",
        "artists": ["Synthetic Artist 1"],
        "album": "Synthetic Album",
        "duration_seconds": 180,
    }
    with Session(engine_for_plugins) as session:
        assert repo.count_items(session, kind="music") == 0


def test_sync_imports_into_music_shelf_and_dedupes(
    manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    engine_for_plugins,
):
    load_builtin_importers()
    scenario = Scenario([track(1), track(2), track(2)])
    install_client(module_of(manager), monkeypatch, scenario)

    out = manager.run_command(PLUGIN_ID, "sync", ["--page-size", "10"])
    assert "抓取完成：3 首（去重后 2 首）" in out
    assert "新增 2" in out

    with Session(engine_for_plugins) as session:
        assert repo.count_items(session, kind="music") == 2
        item = repo.get_item_by_external_id(session, "netease_music", "900001")
        assert item is not None
        assert item.title == "Synthetic Track 1"
        assert item.creators == ["Synthetic Artist 1"]
        assert item.extra.get("album") == "Synthetic Album"
        assert item.extra.get("artists") == ["Synthetic Artist 1"]
        assert item.extra.get("duration_seconds") == 180

    second = manager.run_command(PLUGIN_ID, "sync", ["--page-size", "10"])
    assert "没有新歌" in second
    with Session(engine_for_plugins) as session:
        assert repo.count_items(session, kind="music") == 2


def test_second_sync_is_incremental_and_prepends_new_tracks(
    manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    plugin_data_dir: Path,
):
    scenario = Scenario([track(3), track(4)], track_count=2)
    install_client(module_of(manager), monkeypatch, scenario)

    first = manager.run_command(PLUGIN_ID, "sync", ["--dry-run", "--page-size", "2"])
    assert "抓取完成：2 首" in first

    # 之后又收藏了 1、2（最新）和 5（更旧）
    scenario.rows = [track(1), track(2), track(3), track(4), track(5)]
    scenario.track_count = 5
    scenario.calls.clear()

    second = manager.run_command(PLUGIN_ID, "sync", ["--dry-run", "--page-size", "2"])

    assert "增量完成" in second
    assert "检查 4 首" in second and "新增 2 首" in second
    # 第 2 页整页已知就停，第 5 首不再请求
    assert [option(call, "--offset") for call in scenario.of(is_tracks)] == ["0", "2"]
    snapshot = _read_json(plugin_data_dir / SNAPSHOT)
    assert snapshot["count"] == 4
    assert [item["song_id"] for item in snapshot["items"]] == [
        900001, 900002, 900003, 900004,
    ]


def test_full_sync_drops_tracks_removed_from_the_playlist(
    manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    plugin_data_dir: Path,
):
    scenario = Scenario([track(1), track(2), track(3)], track_count=3)
    install_client(module_of(manager), monkeypatch, scenario)
    manager.run_command(PLUGIN_ID, "sync", ["--dry-run", "--page-size", "2"])

    scenario.rows = [track(1), track(2)]
    scenario.track_count = 2
    scenario.calls.clear()

    out = manager.run_command(
        PLUGIN_ID, "sync", ["--dry-run", "--page-size", "2", "--full"]
    )

    assert "抓取完成：2 首" in out
    snapshot = _read_json(plugin_data_dir / SNAPSHOT)
    assert [item["song_id"] for item in snapshot["items"]] == [900001, 900002]


def test_incremental_falls_back_when_snapshot_is_for_another_playlist(
    manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    plugin_data_dir: Path,
):
    (plugin_data_dir / SNAPSHOT).write_text(json.dumps({
        "playlist": {"id": "OTHER", "name": "别的歌单"},
        "items": [{"song_id": 999999, "name": "Old", "artists": [], "album": None}],
    }), encoding="utf-8")
    scenario = Scenario([track(1), track(2)], track_count=2)
    install_client(module_of(manager), monkeypatch, scenario)

    out = manager.run_command(PLUGIN_ID, "sync", ["--dry-run", "--page-size", "2"])

    assert "抓取完成：2 首" in out
    snapshot = _read_json(plugin_data_dir / SNAPSHOT)
    assert [item["song_id"] for item in snapshot["items"]] == [900001, 900002]


def test_empty_playlist_is_not_written_as_zero(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch, plugin_data_dir: Path
):
    scenario = Scenario([], track_count=0)
    install_client(module_of(manager), monkeypatch, scenario)

    out = manager.run_command(PLUGIN_ID, "sync", ["--dry-run"])

    assert "没有抓到歌曲" in out
    assert not (plugin_data_dir / SNAPSHOT).exists()


def test_page_size_is_saved_and_used(
    manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    plugin_data_dir: Path,
):
    scenario = Scenario([track(1), track(2), track(3)])
    install_client(module_of(manager), monkeypatch, scenario)
    loaded = manager.get(PLUGIN_ID)
    assert loaded is not None
    plugin: Any = loaded.instance

    html = plugin.handle_post(None, {"action": "save", "page_size": "2"}) or ""
    assert "默认配置已保存" in html
    assert 'value="2"' in html

    plugin.sync(dry_run=True)
    assert [option(call, "--limit") for call in scenario.of(is_tracks)] == ["2", "2"]


# --------------------------------------------------------------------- 页面 / 后台任务


def test_page_shows_login_state_and_cli_hints(manager: PluginManager, monkeypatch: pytest.MonkeyPatch):
    install_client(module_of(manager), monkeypatch, Scenario(logged_in=False))
    plugin = manager.get(PLUGIN_ID).instance  # type: ignore[union-attr]
    html = plugin.render_page()
    assert "未登录" in html
    assert "omnishelf plugin netease_music login" in html
    assert "netease_music" in html


def test_login_worker_shows_link_on_page_but_not_in_logs(
    manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    settings_with_repo_plugins: Settings,
):
    module = module_of(manager)
    scenario = Scenario(logged_in=False, checks_before_login=1)
    install_client(module, monkeypatch, scenario)
    plugin = manager.get(PLUGIN_ID).instance  # type: ignore[union-attr]

    job = module._start_job(kind="login", label="扫码登录网易云")
    assert job is not None
    module._login_worker(plugin.ctx, job["id"])

    view = module._job_view()
    assert view is not None
    assert view["state"] == "done" and view["logged_in"] is True
    assert view["prompt"]["url"] == LOGIN_URL

    html = plugin.render_page()
    assert LOGIN_URL in html                    # 链接要展示给用户
    assert f'data-url="{LOGIN_URL}"' in html    # 二维码内容 = ncm-cli 给的一次性链接
    assert 'id="nm-login-qr"' in html
    assert "createSvgTag" in html               # 二维码库已内联进页面
    assert "已登录" in html

    leaked = [
        path for path in _all_files(Path(settings_with_repo_plugins.data_dir))
        if LOGIN_URL in path.read_text("utf-8", "ignore")
    ]
    assert leaked == []                         # 日志/状态文件里不许有登录链接
    assert not (Path(settings_with_repo_plugins.plugins_data_dir) / PLUGIN_ID / SNAPSHOT).exists()


def test_sync_worker_reports_progress_and_snapshot(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch
):
    module = module_of(manager)
    scenario = Scenario([track(1), track(2)])
    install_client(module, monkeypatch, scenario)
    plugin = manager.get(PLUGIN_ID).instance  # type: ignore[union-attr]

    job = module._start_job(kind="sync", label="我喜欢的音乐", do_import=False)
    assert job is not None
    module._sync_worker(
        plugin.ctx, job["id"], page_size=10, do_import=False, full=False
    )

    view = module._job_view()
    assert view is not None and view["state"] == "done"
    assert view["report"]["count"] == 2
    assert any("第 1 次请求" in note for note in view["notes"])
