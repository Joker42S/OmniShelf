"""豆瓣插件的「抓取并导入」：页面按钮 + 命令行命令。

抓取仍然不出网（替换 urllib.request.urlopen 走固定 HTML），导入走宿主的 import 接口
（真写库），所以这里能验证整条链路：抓 → 落快照 → 调宿主 importer → 库里字段对不对。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.config import Settings, repo_root
from omnishelf.db import build_engine
from omnishelf.plugins.manager import PluginManager
from omnishelf.web.app import create_app

from tests.test_web_plugin_douban_fetch import (  # 复用「假网络」和「等抓取跑完」
    COOKIE,
    SECRET,
    FakeResponse,
    config_text,
    install_fake_network,
    logged_in_page,
    wait_for_fetch,
)

PLUGIN_ID = "douban_fetch"
USER = "12345678"
FIXTURES = Path(__file__).parent / "plugin_fixtures"


@pytest.fixture()
def settings_with_repo_plugins(settings: Settings) -> Settings:
    return replace(settings, plugins_dir=repo_root() / "plugins")


@pytest.fixture()
def client(settings_with_repo_plugins: Settings) -> Any:
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


def fetch_form(**overrides: str) -> dict[str, str]:
    form = {
        "action": "fetch_import",
        "media": "movie",
        "status": "collect",
        "user": USER,
        "pages": "1",
        "delay": "1",
    }
    form.update(overrides)
    return form


# ------------------------------------------------------------------ 页面按钮


def test_page_has_fetch_and_fetch_import_buttons(client: TestClient) -> None:
    html = client.get(f"/plugins/{PLUGIN_ID}").text
    assert 'name="action" value="fetch"' in html
    assert 'name="action" value="fetch_import"' in html
    assert "抓取并导入" in html


def test_fetch_import_button_imports_into_the_database(
    client: TestClient, settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch, session: Session,
) -> None:
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    assert response.status_code == 200

    # 页面上给出导入摘要（后台抓完之后由进度条那一栏换成报告）
    html = wait_for_fetch(client)
    assert "导入到 OmniShelf" in html
    assert "新增 15" in html
    assert "douban_movies" in html

    # 库里真的有了，而且状态是「看过」→ done（内置 importer 逐条读 status）
    session.expire_all()
    items = repo.list_items(session, kind="movie")
    assert len(items) == 15
    assert {item.status for item in items} == {"done"}
    assert all(item.extra.get("marked_date") for item in items)

    # 原始文件按来源存档（和上传页一致）
    archive = settings_with_repo_plugins.imports_dir / "douban_movie" / "douban_movie_collect.json"
    assert archive.is_file()


def test_fetch_import_is_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    wait_for_fetch(client)
    client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    assert "未变化 15" in wait_for_fetch(client)
    session.expire_all()
    assert len(repo.list_items(session, kind="movie")) == 15


def test_plain_fetch_does_not_touch_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """「开始抓取」只落快照，不入库——两个按钮的差别要清楚。"""
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form(action="fetch"))
    assert response.status_code == 200
    assert "导入到 OmniShelf" not in wait_for_fetch(client)
    session.expire_all()
    assert repo.list_items(session, kind="movie") == []


def test_import_failure_is_shown_without_losing_snapshot(
    client: TestClient, settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """导入万一失败：页面提示 + 快照仍在（抓取没白跑）。"""
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    from omnishelf.plugins import host as host_module

    def boom(self: Any, importer: str, path: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("假装 importer 坏了")

    monkeypatch.setattr(host_module.RepositoryHostAPI, "import_file", boom)
    response = client.post(f"/plugins/{PLUGIN_ID}", data=fetch_form())
    assert response.status_code == 200
    html = wait_for_fetch(client)
    assert "导入失败" in html
    assert "假装 importer 坏了" in html
    snapshot = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID / "douban_movie_collect.json"
    assert snapshot.is_file()


def test_fetch_form_uses_per_media_user_override(
    client: TestClient, settings_with_repo_plugins: Settings
) -> None:
    """overrides 里的用户 ID 要出现在抓取表单里（否则它永远不生效）。"""
    config_path = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        'user = "99999999"\nmedia = "movie"\n[overrides]\nmovie = "11111111"\n', encoding="utf-8"
    )
    html = client.get(f"/plugins/{PLUGIN_ID}").text
    assert 'value="11111111"' in html  # 抓取表单用的是当前媒体的 override


# ------------------------------------------------------------------ 命令行命令


def test_cli_command_is_registered(manager: PluginManager) -> None:
    manager.load_all()
    commands = {item.name: item for item in manager.commands(PLUGIN_ID)}
    assert "fetch_import" in commands
    for param in ("media", "pages", "status", "user", "delay",
                  "incremental", "dry_run", "cookie"):
        assert param in commands["fetch_import"].params, param
    # 起始页由页码范围（47-）决定，不再单开一个 --start-page
    assert "start_page" not in commands["fetch_import"].params


def test_cli_command_uses_the_cookie_and_never_prints_it(
    manager: PluginManager, settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """命令行带 Cookie 抓：请求真带上、输出里只报状态不报值，值只落在本地配置。"""
    manager.load_all()
    headers: list[dict[str, str]] = []
    install_fake_network(
        monkeypatch, {1: logged_in_page("douban_movies_page1.html")}, headers)
    output = manager.run_command(
        PLUGIN_ID, "fetch_import",
        ["--user", USER, "--pages", "1", "--delay", "1", "--dry-run", "--cookie", COOKIE],
    )
    assert SECRET in headers[0].get("Cookie", "")
    assert "认证      带 Cookie" in output and "登录态" in output
    assert SECRET not in output and "synthetic42" not in output
    assert SECRET in config_text(settings_with_repo_plugins)


def test_cli_command_rejects_unparsable_cookie(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnishelf.plugins.manager import PluginError

    manager.load_all()
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    with pytest.raises(PluginError) as excinfo:
        manager.run_command(
            PLUGIN_ID, "fetch_import",
            ["--user", USER, "--pages", "1", "--dry-run", "--cookie", "<html><body>整页</body></html>"],
        )
    assert "没解析出任何「名字=值」" in str(excinfo.value)


def test_cli_command_fetches_and_imports(
    manager: PluginManager, settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch, session: Session,
) -> None:
    manager.load_all()
    module = manager.get(PLUGIN_ID).module  # type: ignore[union-attr]
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    output = manager.run_command(
        PLUGIN_ID, "fetch_import", ["--user", USER, "--pages", "1", "--delay", "1"]
    )
    assert isinstance(output, str)
    assert "抓取完成" in output and "导入" in output
    assert "新增 15" in output
    session.expire_all()
    assert {item.status for item in repo.list_items(session, kind="movie")} == {"done"}
    assert module is not None


def test_cli_command_dry_run_only_fetches(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    manager.load_all()
    install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    output = manager.run_command(
        PLUGIN_ID, "fetch_import", ["--user", USER, "--pages", "1", "--delay", "1", "--dry-run"]
    )
    assert "已跳过（--dry-run）" in output
    session.expire_all()
    assert repo.list_items(session, kind="movie") == []


def test_incremental_early_stop_keeps_snapshot_coverage(
    manager: PluginManager,
    settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """增量早停只翻了第 1 页，但快照仍覆盖旧页；下一轮不能因此变回全量。"""
    data_dir = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID
    data_dir.mkdir(parents=True, exist_ok=True)
    snapshot = data_dir / "douban_movie_collect.json"
    snapshot.write_text(
        json.dumps({
            "media": "movie", "status": "done", "user": USER,
            "item_count": 15, "highest_page": 3, "total_pages": 3,
            "items": [{"subject_id": f"900001{i:02d}"} for i in range(1, 16)],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    manager.load_all()
    called = install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    args = ["--user", USER, "--pages", "0", "--delay", "1", "--dry-run"]

    first = manager.run_command(PLUGIN_ID, "fetch_import", args)
    assert "第 1 页整页都是已存在数据，增量停止" in first
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["highest_page"] == 3 and payload["total_pages"] == 3

    second = manager.run_command(PLUGIN_ID, "fetch_import", args)
    assert "第 1 页整页都是已存在数据，增量停止" in second
    assert len(called) == 2          # 两趟都只请求第 1 页，没有退化成全量


def test_cli_command_requires_user(manager: PluginManager) -> None:
    from omnishelf.plugins.manager import PluginError

    manager.load_all()
    with pytest.raises(PluginError) as excinfo:
        manager.run_command(PLUGIN_ID, "fetch_import", ["--pages", "1"])
    assert "豆瓣用户 ID" in str(excinfo.value)


# ------------------------------------------------------------------ 快速更新


def test_quick_update_runs_all_nine_lists_and_keeps_going(
    manager: PluginManager,
    settings_with_repo_plugins: Settings,
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """一键更新按脚本口径跑 3 媒体 × 3 列表；某一种抓不到只算「未抓全」，不中断后面的。"""
    config_path = settings_with_repo_plugins.plugins_data_dir / PLUGIN_ID / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f'user = "{USER}"\npages = "1"\ndelay = 1\n', encoding="utf-8")
    manager.load_all()

    def fake_urlopen(request: Any, timeout: int | None = None) -> FakeResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "movie.douban.com" in url:
            body = (FIXTURES / "douban_movies_page1.html").read_text(encoding="utf-8")
        elif "book.douban.com" in url:
            body = (FIXTURES / "douban_books_page1.html").read_text(encoding="utf-8")
        else:                                   # 游戏站这次抓不到
            raise RuntimeError("没有游戏页测试数据")
        return FakeResponse(url, body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    result = manager.quick_update(PLUGIN_ID)

    assert not result.ok
    assert "跑了 9 张列表" in result.summary
    assert "完成 6" in result.summary and "未抓全 3" in result.summary
    assert result.details.count("【") == 9
    assert "【电影/剧集 / 看过】" in result.details and "新增 15" in result.details
    assert "【游戏 / 玩过】" in result.details and "请求失败" in result.details


def test_quick_update_without_a_user_says_what_is_missing(
    manager: PluginManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_fake_network(monkeypatch, {1: "douban_movies_page1.html"})
    manager.load_all()

    result = manager.quick_update(PLUGIN_ID)

    assert not result.ok and "豆瓣用户 ID" in result.summary
    assert calls == []
