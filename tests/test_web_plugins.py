"""插件页面：列表、插件自己的页面、表单、重载、错误显示。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnishelf.config import Settings
from omnishelf.web.app import create_app

GOOD_PLUGIN = '''
from omnishelf.plugins.api import Plugin


class Good(Plugin):
    def start(self):
        self.log("started")

    def stop(self):
        self.log("stopped")

    def render_page(self, request=None):
        return f"<p id='body'>来自 {self.plugin_id} 的页面（配置 {self.config.get('n', 0)}）</p>"
'''


def write_plugin(root: Path, plugin_id: str, *, code: str, metadata: str | None = None) -> Path:
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metadata.toml").write_text(metadata or "[plugin]\n", encoding="utf-8")
    (directory / "plugin.py").write_text(code, encoding="utf-8")
    return directory


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ------------------------------------------------------------------ 列表


def test_nav_has_plugins_entry(client: TestClient) -> None:
    assert 'href="/plugins">插件</a>' in client.get("/").text


def test_plugins_page_empty(client: TestClient) -> None:
    html = client.get("/plugins").text
    assert "还没有插件" in html


def test_plugins_page_lists_plugins(settings: Settings, client: TestClient) -> None:
    write_plugin(
        settings.plugins_dir, "alpha",
        code=GOOD_PLUGIN,
        metadata='[plugin]\ntitle = "插件甲"\nversion = "2.1.0"\ndescription = "干点事"\nauthor = "max"\nrequires = ["query"]\n',
    )
    write_plugin(
        settings.plugins_dir, "beta",
        code=GOOD_PLUGIN.replace('self.log("started")', "raise ValueError('坏了')"),
    )
    html = client.get("/plugins").text

    assert "插件甲" in html and "v2.1.0" in html and "干点事" in html
    assert 'href="/plugins/alpha"' in html  # 有页面的插件给出入口
    assert "已加载" in html and "出错" in html
    assert "坏了" in html  # 错误原因显示出来
    assert "beta" in html


def test_plugins_without_page(settings: Settings, client: TestClient) -> None:
    code = '''
from omnishelf.plugins.api import Plugin


class NoPage(Plugin):
    def start(self):
        pass

    def stop(self):
        pass
'''
    write_plugin(settings.plugins_dir, "nopage", code=code, metadata='[plugin]\ntitle = "无页面"\n')
    html = client.get("/plugins").text
    assert "没有自己的页面" in html
    assert '/plugins/nopage"' not in html  # 不给入口
    page = client.get("/plugins/nopage")
    assert page.status_code == 200
    assert "没有实现 <code>render_page()</code>" in page.text


# ------------------------------------------------------------------ 页面


def test_plugin_page_renders_plugin_html(settings: Settings, client: TestClient) -> None:
    metadata = '[plugin]\ntitle = "插件甲"\ndescription = "描述"\n'
    write_plugin(settings.plugins_dir, "alpha", code=GOOD_PLUGIN, metadata=metadata)
    response = client.get("/plugins/alpha")
    assert response.status_code == 200
    assert "来自 alpha 的页面" in response.text  # 插件返回的 HTML 被放进站点布局
    assert "插件甲" in response.text and "描述" in response.text
    assert "← 插件列表" in response.text


def test_unknown_plugin_is_404(client: TestClient) -> None:
    response = client.get("/plugins/nope")
    assert response.status_code == 404
    assert "没有这个插件" in response.text


def test_plugin_render_error_shows_500(settings: Settings, client: TestClient) -> None:
    code = '''
from omnishelf.plugins.api import Plugin


class Boom(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    def render_page(self, request=None):
        raise KeyError("渲染炸了")
'''
    write_plugin(settings.plugins_dir, "boom", code=code)
    response = client.get("/plugins/boom")
    assert response.status_code == 500
    assert "渲染炸了" in response.text


# ------------------------------------------------------------------ 表单


def test_plugin_post_saves_config(settings: Settings, client: TestClient) -> None:
    code = '''
from omnishelf.plugins.api import Plugin


class Cfg(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    def render_page(self, request=None):
        return f"<p>n={self.config.get('n', 0)}</p>"

    def handle_post(self, request=None, form=None):
        self.save_config({"n": int((form or {}).get("n") or 0)})
        return self.render_page(request)
'''
    write_plugin(settings.plugins_dir, "cfg", code=code)
    client.get("/plugins/cfg")  # 先让它加载
    response = client.post("/plugins/cfg", data={"n": "7"})
    assert response.status_code == 200
    assert "n=7" in response.text
    config_file = settings.plugins_data_dir / "cfg" / "config.toml"
    assert config_file.is_file() and "n = 7" in config_file.read_text(encoding="utf-8")


def test_plugin_post_without_handler_redirects(settings: Settings, client: TestClient) -> None:
    write_plugin(settings.plugins_dir, "quiet", code=GOOD_PLUGIN)
    response = client.post("/plugins/quiet", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/plugins/quiet"


def test_plugin_post_error_shows_400(settings: Settings, client: TestClient) -> None:
    code = '''
from omnishelf.plugins.api import Plugin


class Bad(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    def handle_post(self, request=None, form=None):
        raise RuntimeError("表单处理炸了")
'''
    write_plugin(settings.plugins_dir, "bad", code=code)
    response = client.post("/plugins/bad", data={})
    assert response.status_code == 400
    assert "表单处理炸了" in response.text


# ------------------------------------------------------------------ 重载


def test_reload_all_and_single(settings: Settings, client: TestClient) -> None:
    for pid in ("alpha", "beta"):
        write_plugin(settings.plugins_dir, pid, code=GOOD_PLUGIN)
    client.get("/plugins")
    response = client.post("/plugins/reload", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert "reloaded=alpha" in response.headers["location"]
    assert "reloaded=beta" in response.headers["location"]

    one = client.post("/plugins/reload", data={"plugin_id": "beta"}, follow_redirects=False)
    assert "reloaded=beta" in one.headers["location"] and "alpha" not in one.headers["location"]
    assert "已重载" in client.get(one.headers["location"]).text


def test_reload_broken_plugin_shows_400(settings: Settings, client: TestClient) -> None:
    write_plugin(
        settings.plugins_dir, "boom",
        code=GOOD_PLUGIN.replace('self.log("started")', "raise ValueError('起不来')"),
    )
    response = client.post("/plugins/reload", data={"plugin_id": "boom"})
    assert response.status_code == 400
    assert "起不来" in response.text


def test_hot_reload_picks_up_edits_without_restart(settings: Settings, client: TestClient) -> None:
    """改插件代码后刷新页面就生效（不需要重启进程）。"""
    directory = write_plugin(settings.plugins_dir, "hot", code=GOOD_PLUGIN)
    assert "来自 hot 的页面" in client.get("/plugins/hot").text

    (directory / "plugin.py").write_text(GOOD_PLUGIN.replace("来自", "改过了"), encoding="utf-8")
    response = client.get("/plugins/hot")
    assert response.status_code == 200  # 改坏了会变成 500（错误状态）
    assert "改过了 hot 的页面" in response.text

    # 改坏掉的情况：页面变成 500 并显示原因，而不是静默用旧代码
    (directory / "plugin.py").write_text(
        GOOD_PLUGIN.replace('self.log("started")', "raise ValueError('写坏了')"), encoding="utf-8"
    )
    broken = client.get("/plugins/hot")
    assert broken.status_code == 500 and "写坏了" in broken.text
