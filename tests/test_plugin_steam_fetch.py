"""steam_fetch 插件：配置保存、CLI sync（干跑/入库）、快照与脱敏。

抓取本身在随插件分发的 ``steam_client/`` 里（已由 test_steam_client_* 覆盖验收），
这里测「插件那一层怎么用它」：配置 -> sync -> 快照落盘 -> importer 入库，
以及两条红线——**key 不进快照/页面/日志**、**空数据不写成 0 条快照**。

测试不出网：把 ``SteamWebAPI.request`` 整个类方法换成 tests.steam_synth 的假请求层
（conftest 的 no_outbound_requests 仍是兜底红线）。凭据是合成的。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from omnishelf.config import Settings, repo_root
from omnishelf.db import build_engine
from omnishelf.importers.registry import load_builtin_importers
from omnishelf.plugins.manager import PluginManager
from omnishelf.web.app import create_app
from tests.steam_synth import KEY, SID, FakeAPI, app_data, owned_entry

PLUGIN_ID = "steam_fetch"
REPO_PLUGINS = repo_root() / "plugins"


@pytest.fixture()
def settings_with_repo_plugins(settings: Settings) -> Settings:
    """仓库里的 plugins/ 才是被测代码；数据目录仍隔离在 tmp_path。"""
    return replace(settings, plugins_dir=REPO_PLUGINS)


@pytest.fixture()
def engine_for_plugins(settings_with_repo_plugins: Settings, engine: Any):
    """先跑一遍 settings.engine（init_db 落在 settings.data_dir），再给插件用。"""
    return build_engine(settings_with_repo_plugins)


@pytest.fixture()
def plugin_data_dir(settings_with_repo_plugins: Settings) -> Path:
    path = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def configured(plugin_data_dir: Path) -> Path:
    """预写 config.toml（模拟用户在页面上保存过 key 和目标）。"""
    path = plugin_data_dir / "config.toml"
    path.write_text(
        f'api_key = "{KEY}"\nsteamid64 = "{SID}"\nvanity = ""\ncc = "US"\ndetails = false\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def manager(settings_with_repo_plugins: Settings, engine_for_plugins, configured: Path) -> PluginManager:
    mgr = PluginManager(
        plugins_dir=settings_with_repo_plugins.plugins_dir,
        plugins_data_dir=settings_with_repo_plugins.plugins_data_dir,
        engine=engine_for_plugins,
        imports_dir=settings_with_repo_plugins.imports_dir,
    )
    mgr.load_all()
    return mgr


@pytest.fixture()
def fake_transport(monkeypatch: pytest.MonkeyPatch):
    """把 steam_client 的 transport 类方法换成假请求层，返回装好的 FakeAPI。"""
    from steam_client.transport import API_BASE, SteamWebAPI

    holder: dict[str, FakeAPI] = {}

    def fake_request(self, path, params=None, *, key=None, base=None):
        fake = holder["api"]
        return fake.request(path, params, key=key, base=base if base is not None else API_BASE)

    def install(fake: FakeAPI) -> FakeAPI:
        holder["api"] = fake
        monkeypatch.setattr(SteamWebAPI, "request", fake_request)
        return fake

    return install


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- CLI sync

def test_sync_dry_run_writes_snapshots(manager: PluginManager, fake_transport, plugin_data_dir: Path):
    fake = fake_transport(FakeAPI(
        owned=[owned_entry(730), owned_entry(1097110, "EA SPORTS FC 26")],
        wishlist=[{"appid": 730}, {"appid": 1097110}],
        app_data={730: app_data(730), 1097110: app_data(1097110, "EA SPORTS FC 26")},
    ))
    out = manager.run_command(PLUGIN_ID, "sync", ["--dry-run"])
    assert "抓取完成" in out
    assert "只抓取不导入" not in out or "已跳过" in out

    # 商店信息逐款问（appdetails 不认逗号批量），愿望单那两份全靠缓存命中，不再多发一次
    assert sorted(call["params"]["appids"] for call in fake.calls_to("appdetails")) == [730, 1097110]

    owned = _read_json(plugin_data_dir / "steam_owned.json")
    assert owned["list"] == "owned" and owned["status"] == "done"
    assert [row["appid"] for row in owned["items"]] == [730, 1097110]
    assert owned["items"][0]["playtime_forever"] == 640

    wish = _read_json(plugin_data_dir / "steam_wishlist.json")
    assert wish["list"] == "wishlist" and wish["status"] == "wishlist"
    assert wish["items"][1]["name"] == "EA SPORTS FC 26"
    assert "9.99" in wish["items"][1]["price"]
    # 统一 schema：两份列表的 item 键完全一致（愿望单只是时长为 None）
    assert sorted(owned["items"][0]) == sorted(wish["items"][0])
    assert wish["items"][0]["developers"] == ["Synthetic Valve"]
    assert wish["items"][0]["genres"] == ["Action", "免费游玩"]
    assert wish["items"][0]["short_description"] == "Buy wisely.\nNo refunds."

    # 红线：key 不进快照
    for path in (plugin_data_dir / "steam_owned.json", plugin_data_dir / "steam_wishlist.json"):
        assert KEY not in path.read_text(encoding="utf-8")
    # 用户数据不落缓存：缓存里只有 app 详情，且按商店区域（默认 US）分目录
    cached = sorted(p.name for p in (plugin_data_dir / "steam_cache" / "apps" / "US").glob("*.json"))
    assert cached == ["1097110.json", "730.json"]


def test_sync_imports_into_game_shelf(manager: PluginManager, fake_transport,
                                      settings_with_repo_plugins: Settings, engine_for_plugins):
    from sqlmodel import Session

    from omnishelf import repository as repo

    fake_transport(FakeAPI(owned=[owned_entry(730)], wishlist=[{"appid": 230}],
                           app_data={730: app_data(730), 230: app_data(230, "Terraria")}))
    out = manager.run_command(PLUGIN_ID, "sync", [])
    assert out.count("新增 1") == 2                # 游戏库、愿望单各新增 1 条

    with Session(engine_for_plugins) as session:
        items = repo.list_items(session, kind="game", limit=50)
        titles = sorted(item.title for item in items)
        assert titles == ["Counter-Strike 2", "Terraria"]
        by_title = {item.title: item for item in items}
        assert by_title["Counter-Strike 2"].status == "done"
        assert by_title["Terraria"].status == "wishlist"
        assert by_title["Counter-Strike 2"].extra.get("playtime_hours") == 10.7
        # 商店信息扩充映射：developers->creators、genres->tags、short_description->notes、
        # header_image->extra.cover_url
        cs = by_title["Counter-Strike 2"]
        assert cs.creators == ["Synthetic Valve"]
        assert set(repo.list_tags(session, cs.id)) >= {"Action", "免费游玩"}
        assert cs.notes == "Buy wisely.\nNo refunds."
        assert cs.extra.get("cover_url") == "https://cdn.invalid/730_header.jpg"
        assert repo.get_item_by_external_id(session, "steam", "730") is not None


def test_reimport_updates_not_duplicates(manager: PluginManager, fake_transport, engine_for_plugins):
    from sqlmodel import Session

    from omnishelf import repository as repo

    fake = FakeAPI(owned=[owned_entry(730)], wishlist=[{"appid": 730}],
                   app_data={730: app_data(730)})
    fake_transport(fake)
    manager.run_command(PLUGIN_ID, "sync", [])
    second = manager.run_command(PLUGIN_ID, "sync", [])
    # 第二次：愿望单+游戏库都撞到同一条 (steam, 730)，不新增
    assert "新增 0" in second
    with Session(engine_for_plugins) as session:
        assert repo.count_items(session, kind="game") == 1


def test_placeholder_title_is_fixed_by_a_later_full_fetch(
    manager: PluginManager, fake_transport, engine_for_plugins
):
    """愿望单只有 appid，商店信息没取到时条目名就是那串数字（2026-10-18 那轮的现场）。

    盯的是补救路径：**重导旧快照没用**（快照里名字还是数字，值一模一样算「未变化」），
    得重抓一次让 ``appdetails`` 真把名字带回来；届时按 ``(steam, appid)`` 命中同一条并
    **覆盖写**，长回真名字，不会长出第二条。

    这里用 ``app_missing``（``success=false``）表示「这条没取到」，不注入异常类：
    插件是 exec 加载的，它那份 ``steam_client`` 可能与本测试进程里的不是同一个模块对象，
    异常类身份不同就接不住，会变成测试里的假异常把真代码打穿。
    """
    from sqlmodel import Session

    from omnishelf import repository as repo

    fake_transport(FakeAPI(owned=[owned_entry(230, "230")], wishlist=[{"appid": 230}],
                           app_missing={230}))
    first = manager.run_command(PLUGIN_ID, "sync", ["--lists", "wishlist"])
    assert "新增 1" in first
    with Session(engine_for_plugins) as session:
        assert repo.get_item_by_external_id(session, "steam", "230").title == "230"

    fake_transport(FakeAPI(owned=[owned_entry(230, "230")], wishlist=[{"appid": 230}],
                           app_data={230: app_data(230, "Terraria")}))
    again = manager.run_command(PLUGIN_ID, "sync", [])       # 抓全部才有愿望单快照
    assert "更新 1" in again
    with Session(engine_for_plugins) as session:
        assert repo.count_items(session, kind="game") == 1
        item = repo.get_item_by_external_id(session, "steam", "230")
        assert item.title == "Terraria"                       # 覆盖写把占位名换掉
        assert item.extra.get("cover_url") == "https://cdn.invalid/230_header.jpg"


def test_empty_lists_reported_as_unavailable_not_zero(
    manager: PluginManager, fake_transport, plugin_data_dir: Path
):
    """空数据 ≠ 无数据：报「不可用或未公开」，**不写快照**，免得界面显示 0 款游戏。"""
    fake_transport(FakeAPI(owned=[], wishlist=[]))
    out = manager.run_command(PLUGIN_ID, "sync", ["--dry-run"])
    assert "游戏信息不可用或未公开" in out
    assert not (plugin_data_dir / "steam_owned.json").exists()
    assert not (plugin_data_dir / "steam_wishlist.json").exists()


def test_sync_requires_target(manager: PluginManager, fake_transport, plugin_data_dir: Path):
    (plugin_data_dir / "config.toml").write_text(f'api_key = "{KEY}"\n', encoding="utf-8")
    manager.reload(PLUGIN_ID)
    fake_transport(FakeAPI())
    with pytest.raises(Exception) as excinfo:
        manager.run_command(PLUGIN_ID, "sync", ["--dry-run"])
    assert "SteamID64" in str(excinfo.value)


# ------------------------------------------------------------------------ 代理

def test_sync_cli_proxy_reports_the_route(manager: PluginManager, fake_transport):
    """``--proxy`` 只影响这一条命令：结果里要写明走的哪条路，且裸 IP:端口 会补协议。"""
    fake_transport(FakeAPI(owned=[owned_entry(730)], wishlist=[{"appid": 730}],
                           app_data={730: app_data(730)}))
    direct = manager.run_command(PLUGIN_ID, "sync", ["--dry-run"])
    assert "网络      直连" in direct

    out = manager.run_command(PLUGIN_ID, "sync", ["--dry-run", "--proxy", "127.0.0.1:10809"])
    assert "网络      走代理 http://127.0.0.1:10809" in out


def test_sync_cli_proxy_credentials_stay_out_of_the_output(manager: PluginManager, fake_transport):
    """带 ``user:pass@`` 的代理：结果/日志里只留 ``协议://主机:端口``。"""
    fake_transport(FakeAPI(owned=[owned_entry(730)], wishlist=[{"appid": 730}],
                           app_data={730: app_data(730)}))
    out = manager.run_command(
        PLUGIN_ID, "sync", ["--dry-run", "--proxy", "http://synthuser:synthpw@127.0.0.1:10809"]
    )
    assert "synthuser" not in out and "synthpw" not in out
    assert "走代理 http://127.0.0.1:10809" in out


def test_sync_cli_rejects_broken_proxy(manager: PluginManager, fake_transport):
    """命令行亲手填了一个认不出来的代理，就明确报错，别悄悄退回直连。"""
    fake_transport(FakeAPI())
    with pytest.raises(Exception) as excinfo:
        manager.run_command(PLUGIN_ID, "sync", ["--dry-run", "--proxy", "socks9://127.0.0.1:1080"])
    assert "代理地址要写成" in str(excinfo.value)


# --------------------------------------------------------------------- 页面

def test_page_save_config_roundtrip_does_not_echo_key(
    settings_with_repo_plugins: Settings, engine_for_plugins, plugin_data_dir: Path
):
    with TestClient(create_app(settings_with_repo_plugins)) as client:
        response = client.post(
            f"/plugins/{PLUGIN_ID}",
            data={"form": "save", "action": "save", "api_key": KEY,
                  "steamid64": SID, "vanity": "", "cc": "us", "details": "on", "lists": "all"},
        )
        assert response.status_code == 200
        assert KEY not in response.text
        assert "已保存" in response.text                      # auth_label 的说法

        saved = (plugin_data_dir / "config.toml").read_text(encoding="utf-8")
        assert KEY in saved                                   # 本地配置里有（不进 git）
        page = client.get(f"/plugins/{PLUGIN_ID}").text
        assert KEY not in page

        logs = Path(settings_with_repo_plugins.data_dir)
        leaked = [p for p in logs.rglob("*") if p.is_file() and KEY in p.read_text("utf-8", "ignore")]
        assert leaked == []                                   # 日志里也不许留明文


def test_page_rejects_bad_steamid(settings_with_repo_plugins: Settings, engine_for_plugins):
    with TestClient(create_app(settings_with_repo_plugins)) as client:
        response = client.post(
            f"/plugins/{PLUGIN_ID}",
            data={"form": "save", "action": "save", "api_key": KEY, "steamid64": "12345"},
        )
        assert "17 位数字" in response.text


def _wait_job(client: TestClient, timeout: float = 30.0) -> str:
    """抓取在后台线程跑，POST 的响应里只有刚起步的进度条——结果要反复 GET。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        html = client.get(f"/plugins/{PLUGIN_ID}").text
        if "抓取结束" in html or "抓取失败" in html:
            return html
        threading.Event().wait(0.05)
    raise AssertionError(f"后台抓取没在 {timeout} 秒内结束")


def test_fetch_remembers_target_and_cc(configured: Path, plugin_data_dir: Path,
                                       settings_with_repo_plugins: Settings,
                                       engine_for_plugins, fake_transport):
    """页面上填 cn 抓一次，下次打开还得是 cn。

    早先只有「api_key 变了」才写回配置，于是区域永远被打回 US——
    抓 CN 价格要每次重填。现在点抓取就把目标/区域记住（坏值在校验那步已经拦掉）。
    """
    fake_transport(FakeAPI(owned=[owned_entry(730)], wishlist=[{"appid": 730}],
                           app_data={730: app_data(730)}))
    with TestClient(create_app(settings_with_repo_plugins)) as client:
        response = client.post(
            f"/plugins/{PLUGIN_ID}",
            data={"form": "save", "action": "fetch", "api_key": "", "steamid64": SID,
                  "vanity": "", "cc": "cn", "lists": "wishlist"},
        )
        assert response.status_code == 200
        assert "抓取中" in response.text

        saved = (plugin_data_dir / "config.toml").read_text(encoding="utf-8")
        assert 'cc = "CN"' in saved
        assert f'steamid64 = "{SID}"' in saved
        assert "details" not in saved                       # 旧开关不再被写回

        assert "抓取结束" in _wait_job(client)
        page = client.get(f"/plugins/{PLUGIN_ID}").text
        assert 'value="CN"' in page
        assert KEY not in page


def test_page_proxy_is_saved_and_shown(configured: Path, plugin_data_dir: Path,
                                       settings_with_repo_plugins: Settings,
                                       engine_for_plugins, fake_transport):
    """代理和 cc 一个待遇：点抓取就记住，进度栏写明走的哪条路。"""
    fake_transport(FakeAPI(owned=[owned_entry(730)], wishlist=[{"appid": 730}],
                           app_data={730: app_data(730)}))
    with TestClient(create_app(settings_with_repo_plugins)) as client:
        response = client.post(
            f"/plugins/{PLUGIN_ID}",
            data={"form": "save", "action": "fetch", "api_key": "", "steamid64": SID,
                  "vanity": "", "cc": "us", "lists": "all", "proxy": "127.0.0.1:10809"},
        )
        assert response.status_code == 200
        saved = (plugin_data_dir / "config.toml").read_text(encoding="utf-8")
        assert 'proxy = "http://127.0.0.1:10809"' in saved   # 裸地址已补协议再存

        html = _wait_job(client)
        assert "网络：走代理 http://127.0.0.1:10809" in html
        assert 'value="http://127.0.0.1:10809"' in html       # 下次打开还在，不用重填


def test_page_rejects_bad_proxy_without_touching_config(
    configured: Path, plugin_data_dir: Path,
    settings_with_repo_plugins: Settings, engine_for_plugins,
):
    """两个按钮同一个校验：坏代理当场拦，配置一个字节都不动。"""
    before = (plugin_data_dir / "config.toml").read_text(encoding="utf-8")
    with TestClient(create_app(settings_with_repo_plugins)) as client:
        for action in ("save", "fetch"):
            response = client.post(
                f"/plugins/{PLUGIN_ID}",
                data={"form": "save", "action": action, "api_key": KEY, "steamid64": SID,
                      "vanity": "", "cc": "us", "lists": "all", "proxy": "代理://nope"},
            )
            assert "代理地址要写成" in response.text
            assert "抓取中" not in response.text
        assert (plugin_data_dir / "config.toml").read_text(encoding="utf-8") == before


def test_importers_registered():
    load_builtin_importers()
    from omnishelf.importers.registry import IMPORTERS, get

    assert get("steam").source == "steam"
    # 旧的按列表拆分的两个 importer 已收敛成单一 ``steam``
    assert "steam_library" not in IMPORTERS and "steam_wishlist" not in IMPORTERS
