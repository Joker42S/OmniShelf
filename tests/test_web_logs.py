"""运行日志页 ``/logs``：宿主统一收集的插件日志 + 实时刷新。

盯三件事：

* 插件说的行和宿主说的行**在同一个文件、同一个页面**里按时间排；
* 轮询片段只换日志那一块，关掉自动刷新就不该有 ``hx-trigger``；
* 级别/插件过滤是**读文件时**做的，不动文件内容。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnishelf.config import Settings
from omnishelf.web.app import create_app

DEMO_PLUGIN = '''
from omnishelf.plugins.api import Plugin


class Demo(Plugin):
    def start(self):
        self.log("开始做事")
        self.log("口令 已经打码", level="warning")
        self.log("炸了\\n第二行跟着上面那条", level="error")

    def stop(self):
        pass
'''

OTHER_PLUGIN = '''
from omnishelf.plugins.api import Plugin


class Other(Plugin):
    def start(self):
        self.log("别的插件的一行")

    def stop(self):
        pass
'''


def write_plugin(root: Path, plugin_id: str, code: str) -> Path:
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metadata.toml").write_text("[plugin]\n", encoding="utf-8")
    (directory / "plugin.py").write_text(code, encoding="utf-8")
    return directory


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    write_plugin(settings.plugins_dir, "demo", DEMO_PLUGIN)
    write_plugin(settings.plugins_dir, "other", OTHER_PLUGIN)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ------------------------------------------------------------------ 整页


def test_logs_page_lists_both_the_plugin_and_the_host(client: TestClient, settings: Settings) -> None:
    response = client.get("/logs")
    assert response.status_code == 200
    html = response.text
    assert "运行日志" in html
    # 插件自己说的
    assert "开始做事" in html and "炸了" in html
    # 宿主记的（加载）
    assert "加载完成" in html
    # 一个插件一个文件，路径摆在页面上
    assert str(settings.plugin_logs_dir) in html
    assert 'class="log-warning"' in html and "第二行跟着上面那条" in html


def test_logs_page_polls_by_default_and_stops_when_asked(client: TestClient) -> None:
    assert 'hx-trigger="every 2s"' in client.get("/logs").text
    assert "hx-trigger" not in client.get("/logs?follow=0").text


# ------------------------------------------------------------------ 过滤


def test_plugin_filter_only_reads_that_plugin_file(client: TestClient) -> None:
    html = client.get("/logs?plugin=demo&follow=0").text
    assert "别的插件的一行" not in html
    assert "开始做事" in html


def test_level_filter_keeps_the_multiline_entry(client: TestClient) -> None:
    html = client.get("/logs?level=error&follow=0").text
    assert "炸了" in html and "第二行跟着上面那条" in html   # 续行跟着它上面那条走
    assert "开始做事" not in html                           # info 被滤掉


# ------------------------------------------------------------------ 轮询片段


def test_tail_endpoint_returns_a_bare_fragment(client: TestClient) -> None:
    response = client.get("/logs/tail?plugin=demo&level=all&limit=200&follow=0")
    assert response.status_code == 200
    assert "<html" not in response.text.lower()             # 不套站点布局，不然每两秒闪一整屏
    assert "开始做事" in response.text


def test_unknown_plugin_gives_an_empty_state_instead_of_404(client: TestClient) -> None:
    html = client.get("/logs?plugin=nope&follow=0").text
    assert "还没有日志" in html


# ------------------------------------------------------------------ 落盘的是文件，不是内存


def test_the_page_reads_the_same_file_the_cli_writes(client: TestClient, settings: Settings) -> None:
    """宿主日志的唯一真相就是那个文件：页面读它，``tail -f`` 也读它。"""
    path = settings.plugin_logs_dir / "demo.log"
    assert "开始做事" in client.get("/logs?plugin=demo&follow=0").text
    before = path.read_text(encoding="utf-8")
    assert "开始做事" in before and "宿主" in before
    assert path.read_text(encoding="utf-8") == before        # 看页面不该动文件
