"""`omnishelf plugin ...`：插件命令的发现与执行（CLI 侧）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from omnishelf.cli import app

PLUGIN_CODE = '''
from omnishelf.plugins.api import Plugin, cmd


class Demo(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    @cmd("greet", help="打招呼")
    def greet(self, name: str, times: int = 1, *, upper: bool = False) -> str:
        """按名字打招呼。

        name: 名字（必填）
        times: 重复次数
        upper: 是否大写
        """
        text = f"hi {name}".upper() if upper else f"hi {name}"
        return " / ".join([text] * times)

    @cmd("stats", help="看看宿主统计")
    def stats(self) -> dict:
        """返回宿主统计（dict 会被打印成 JSON）。"""
        return self.host.stats()
'''


@pytest.fixture()
def plugin_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """装一个测试插件，并把 CLI 指向这个插件目录 + 临时数据目录。"""
    plugins = tmp_path / "plugins"
    (plugins / "demo").mkdir(parents=True)
    (plugins / "demo" / "metadata.toml").write_text(
        '[plugin]\ntitle = "演示插件"\nrequires = ["query"]\n', encoding="utf-8"
    )
    (plugins / "demo" / "plugin.py").write_text(PLUGIN_CODE, encoding="utf-8")
    monkeypatch.setenv("OMNISHELF_PLUGINS_DIR", str(plugins))
    monkeypatch.setenv("OMNISHELF_PLUGINS_DATA_DIR", str(tmp_path / "plugins_data"))
    runner = CliRunner()
    return lambda *args: runner.invoke(app, ["--data-dir", str(tmp_path / "data"), *args])


def test_plugin_overview_lists_plugins_and_commands(plugin_cli: Any) -> None:
    result = plugin_cli("plugin")
    assert result.exit_code == 0
    assert "OmniShelf 插件命令" in result.output
    assert "demo" in result.output and "演示插件" in result.output
    assert "greet" in result.output and "打招呼" in result.output
    assert "stats" in result.output


def test_plugin_help_without_subcommand(plugin_cli: Any) -> None:
    for argument in ("plugin", "--help", "-h"):
        result = plugin_cli("plugin", argument) if argument != "plugin" else plugin_cli("plugin")
        assert result.exit_code == 0, argument


def test_plugin_command_listing_shows_param_docs(plugin_cli: Any) -> None:
    result = plugin_cli("plugin", "demo")
    assert result.exit_code == 0
    assert "omnishelf plugin demo greet [--参数]" in result.output
    assert "name: 名字（必填）" in result.output
    assert "times: 重复次数" in result.output
    assert "upper: 是否大写" in result.output


def test_run_plugin_command(plugin_cli: Any) -> None:
    assert plugin_cli("plugin", "demo", "greet", "Ada").output.strip() == "hi Ada"
    out = plugin_cli("plugin", "demo", "greet", "Ada", "--times", "2", "--upper").output
    assert "HI ADA / HI ADA" in out


def test_plugin_command_help_goes_to_the_plugin(plugin_cli: Any) -> None:
    """`-h`/`--help` 要透传给插件自己的参数解析器（而不是打印宿主的帮助）。"""
    for flag in ("-h", "--help"):
        result = plugin_cli("plugin", "demo", "greet", flag)
        assert result.exit_code == 0, flag
        assert "按名字打招呼" in result.output
        assert "name: 名字（必填）" in result.output
        assert "--upper" in result.output


def test_plugin_command_returning_dict_is_json(plugin_cli: Any) -> None:
    result = plugin_cli("plugin", "demo", "stats")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "total" in payload and "by_kind" in payload


def test_unknown_plugin_and_command_exit_nonzero(plugin_cli: Any) -> None:
    missing_plugin = plugin_cli("plugin", "nope", "greet", "x")
    assert missing_plugin.exit_code == 1
    assert "没有插件" in missing_plugin.output

    missing_command = plugin_cli("plugin", "demo", "nope")
    assert missing_command.exit_code == 1
    assert "没有命令" in missing_command.output and "greet" in missing_command.output


def test_missing_required_argument_is_reported(plugin_cli: Any) -> None:
    result = plugin_cli("plugin", "demo", "greet")
    assert result.exit_code == 2  # argparse 的用法错误
    assert "usage:" in result.output


def test_real_plugins_are_listed(plugin_cli: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """仓库里那两个真插件也走同一条路径（这里只验「能列出来 + 有参数说明」）。"""
    monkeypatch.delenv("OMNISHELF_PLUGINS_DIR", raising=False)
    monkeypatch.delenv("OMNISHELF_PLUGINS_DATA_DIR", raising=False)
    result = plugin_cli("plugin", "douban_fetch")
    assert result.exit_code == 0
    assert "fetch_import" in result.output
    assert "media: 媒体类型" in result.output
