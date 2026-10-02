"""仓库里那个真插件（plugins/kind_chart）：既是示例也是模板。"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.config import Settings, repo_root
from omnishelf.db import build_engine
from omnishelf.plugins.manager import STATE_LOADED, PluginManager

REPO_PLUGINS = repo_root() / "plugins"


@pytest.fixture()
def chart_manager(settings: Settings, session: Session) -> PluginManager:
    engine = build_engine(replace(settings))
    return PluginManager(
        plugins_dir=REPO_PLUGINS,
        plugins_data_dir=settings.plugins_data_dir,
        engine=engine,
    )


@pytest.fixture()
def seeded(session: Session) -> None:
    repo.create_item(session, kind="movie", title="M1", status="done", rating=8.0)
    repo.create_item(session, kind="movie", title="M2", status="done")
    repo.create_item(session, kind="book", title="B1", status="wishlist")
    repo.create_item(session, kind="game", title="G1")
    session.commit()


def test_kind_chart_loads(chart_manager: PluginManager) -> None:
    statuses = {s.id: s for s in chart_manager.load_all()}
    assert "kind_chart" in statuses
    status = statuses["kind_chart"]
    assert status.state == STATE_LOADED
    assert status.title == "分类统计图"
    assert status.version and status.has_page
    assert status.requires == ("query",)
    assert status.logs and "已启动" in status.logs[0]


def test_kind_chart_draws_bars_from_host_data(
    chart_manager: PluginManager, seeded: None
) -> None:
    chart_manager.load_all()
    html = chart_manager.page("kind_chart", None)

    assert html.count("<svg") == 2  # kind 图 + 状态图
    rows = re.findall(r"<tr><td>([^<]+)</td><td>(\d+)</td><td>[\d.]+%</td></tr>", html)
    assert rows == [("影视", "2"), ("图书", "1"), ("游戏", "1")]  # 按数量降序，用 kind 的中文名
    assert "共 <strong>4</strong> 条" in html
    assert re.search(r'width="100(\.0)?"', html)  # 最大的那根占满


def test_kind_chart_config_changes_output(
    chart_manager: PluginManager, seeded: None
) -> None:
    chart_manager.load_all()
    assert chart_manager.post(
        "kind_chart", None, {"bar_color": "#ff0000", "max_bars": "1", "show_status": "on"}
    )
    loaded = chart_manager.get("kind_chart")
    assert loaded is not None
    assert loaded.context.config == {"bar_color": "#ff0000", "show_status": True, "max_bars": 1}
    config_file = loaded.context.config_path
    assert config_file.is_file() and 'bar_color = "#ff0000"' in config_file.read_text(encoding="utf-8")

    html = chart_manager.page("kind_chart", None)
    assert html.count("<rect") == 2  # kind 图 1 根 + 状态图 1 根（max_bars=1）
    assert "#ff0000" in html

    # 关掉状态图
    chart_manager.post("kind_chart", None, {"bar_color": "#00ff00", "max_bars": "5"})
    assert html.count("<h3>按状态分布</h3>") == 1
    html2 = chart_manager.page("kind_chart", None)
    assert "<h3>按状态分布</h3>" not in html2 and html2.count("<rect") == 3


def test_kind_chart_survives_bad_config(chart_manager: PluginManager, seeded: None) -> None:
    """配置写坏了也不能炸：非法值退回默认。"""
    chart_manager.load_all()
    loaded = chart_manager.get("kind_chart")
    assert loaded is not None
    loaded.context.save_config({"bar_color": "red", "max_bars": "很多", "show_status": False})
    html = chart_manager.page("kind_chart", None)
    assert 'fill="#3b82f6"' in html  # 颜色退回默认
    assert html.count("<rect") == 3  # max_bars 退回默认 12 -> 3 类


def test_kind_chart_writes_state(chart_manager: PluginManager, seeded: None) -> None:
    chart_manager.load_all()
    chart_manager.page("kind_chart", None)
    loaded = chart_manager.get("kind_chart")
    assert loaded is not None
    state = loaded.context.read_state(default={})
    assert state["total"] == 4 and state["last_render"].endswith("+00:00")


def test_plugin_only_uses_the_sdk() -> None:
    """规范：插件只能用标准库 + `omnishelf.plugins.api` 这个 SDK，不能碰宿主内部模块。"""
    source = (REPO_PLUGINS / "kind_chart" / "plugin.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.M)
    assert imports, "总得 import 点什么"
    allowed_std = {"__future__", "datetime", "typing", "json", "re", "math"}
    for name in imports:
        if name == "omnishelf.plugins.api":
            continue  # 这是插件 SDK，唯一允许 import 的宿主模块
        assert not name.startswith("omnishelf"), f"插件不该 import 宿主内部模块: {name}"
        assert name.split(".")[0] in allowed_std, name
    assert "from omnishelf.plugins.api import Plugin" in source


def test_kind_chart_is_a_plugin_subclass(chart_manager: PluginManager) -> None:
    """宿主按「Plugin 子类」的契约实例化插件。"""
    from omnishelf.plugins.api import Plugin as SDKPlugin

    chart_manager.load_all()
    loaded = chart_manager.get("kind_chart")
    assert loaded is not None
    assert isinstance(loaded.instance, SDKPlugin)
    assert type(loaded.instance).__name__ == "KindChart"
    assert loaded.instance.plugin_id == "kind_chart"
    assert loaded.instance.metadata.version == "1.0.0"


def test_metadata_matches_template_contract() -> None:
    """模板/规范要求：metadata.toml 有 id/title/version/entry/requires 等。"""
    text = (REPO_PLUGINS / "kind_chart" / "metadata.toml").read_text(encoding="utf-8")
    for key in ("id", "title", "version", "description", "entry", "requires"):
        assert re.search(rf"^{key}\s*=", text, re.M), key
    assert (REPO_PLUGINS / "kind_chart" / "templates" / "page.html").is_file()
    assert (REPO_PLUGINS / "kind_chart" / "README.md").is_file()


def test_kind_chart_cli_command(chart_manager: PluginManager, seeded: None) -> None:
    """模板插件同时演示了 @cmd：命令行能调用它。"""
    chart_manager.load_all()
    commands = {item.name: item for item in chart_manager.commands("kind_chart")}
    assert "top" in commands
    assert commands["top"].params == ("limit",)

    output = chart_manager.run_command("kind_chart", "top", ["--limit", "2"])
    assert "共 4 条" in output
    lines = [line for line in str(output).splitlines() if line.startswith("  ")]
    assert len(lines) == 2  # --limit 2
    assert "影视" in lines[0] and "2" in lines[0]
