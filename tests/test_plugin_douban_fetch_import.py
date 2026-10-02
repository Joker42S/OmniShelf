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
    config_text,
    install_fake_network,
    logged_in_page,
    wait_for_fetch,
)

PLUGIN_ID = "douban_fetch"
USER = "12345678"


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


def test_cli_command_requires_user(manager: PluginManager) -> None:
    from omnishelf.plugins.manager import PluginError

    manager.load_all()
    with pytest.raises(PluginError) as excinfo:
        manager.run_command(PLUGIN_ID, "fetch_import", ["--pages", "1"])
    assert "豆瓣用户 ID" in str(excinfo.value)
