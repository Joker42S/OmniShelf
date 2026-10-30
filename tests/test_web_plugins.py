"""插件页面：列表、插件自己的页面、表单、重载、错误显示。"""

from __future__ import annotations

import time
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


# --------------------------------------------------------------- 网格 / 快速更新

QUICK_PLUGIN = '''
from omnishelf.plugins.api import Plugin, QuickParam, QuickUpdateResult, quick_params


class Quick(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    def render_page(self, request=None):
        return "<p>quick</p>"

    @quick_params(
        QuickParam("token", "访问令牌", required=True),
        QuickParam("password", "测试口令", secret=True),
    )
    def quick_update(self, options=None):
        options = dict(options or {})
        summary = "口令收到" if options.get("password") == "synthetic-pass" else "没带口令"
        summary += f" · 参数 {','.join(sorted(options))}"
        return QuickUpdateResult(ok=True, summary=summary, details="更新明细")
'''

ALT_QUICK_PLUGIN = '''
from omnishelf.plugins.api import Plugin, QuickParam, QuickUpdateResult, quick_params


class Alt(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    @quick_params(QuickParam("keyword", "别名关键字"))
    def quick_update(self, options=None):
        keyword = (options or {}).get("keyword") or "—"
        return QuickUpdateResult(ok=True, summary=f"关键字 {keyword}", details="")
'''


def wait_for_quick(client: TestClient, needle: str = "已结束", timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    html = ""
    while time.monotonic() < deadline:
        html = client.get("/plugins/quick-update/status").text
        if "已结束" in html and needle in html:
            return html
        time.sleep(0.02)
    raise AssertionError(html)


def test_plugins_grid_is_default_with_view_switch(settings: Settings, client: TestClient) -> None:
    write_plugin(settings.plugins_dir, "quick", code=QUICK_PLUGIN, metadata='[plugin]\ntitle = "快插件"\n')
    write_plugin(settings.plugins_dir, "plain", code=GOOD_PLUGIN, metadata='[plugin]\ntitle = "普通插件"\n')

    grid = client.get("/plugins").text
    assert 'class="plugin-grid"' in grid
    assert 'href="/plugins?view=detail"' in grid
    assert "一键快速更新" in grid
    assert 'name="quick_plugin" value="quick"' in grid
    assert 'form="quick-update-form"' in grid
    assert 'name="quick_plugin" value="plain"' not in grid  # 没实现的插件不给开关注
    assert 'href="/plugins/quick"' in grid and 'href="/plugins/plain"' in grid  # 打开按钮

    # 参数按插件分开，渲染在各自的卡片里
    assert 'name="quick_param:quick:token"' in grid
    assert 'name="quick_param:quick:password"' in grid
    assert 'type="password"' in grid and "访问令牌" in grid and "测试口令" in grid
    assert 'class="plugin-quick-required"' in grid
    assert "quick_param:plain:" not in grid
    assert 'name="password"' not in grid  # 旧的共享口令框已经去掉

    detail = client.get("/plugins?view=detail").text
    assert 'class="plugin-card"' in detail
    assert "一键快速更新" not in detail
    assert 'href="/plugins?view=grid"' in detail


def test_quick_update_runs_selected_plugins_with_own_params(
    settings: Settings, client: TestClient
) -> None:
    write_plugin(settings.plugins_dir, "quick", code=QUICK_PLUGIN, metadata='[plugin]\ntitle = "快插件"\n')
    write_plugin(settings.plugins_dir, "alt", code=ALT_QUICK_PLUGIN, metadata='[plugin]\ntitle = "另一个快插件"\n')
    write_plugin(settings.plugins_dir, "plain", code=GOOD_PLUGIN, metadata='[plugin]\ntitle = "普通插件"\n')
    client.get("/plugins")

    response = client.post(
        "/plugins/quick-update",
        headers={"HX-Request": "true"},
        data={
            "quick_plugin": ["quick", "alt"],
            "quick_param:quick:token": "t-alpha",
            "quick_param:quick:password": "synthetic-pass",
            "quick_param:quick:keyword": "不该收到",  # quick 没声明，忽略
            "quick_param:alt:keyword": "漫画",
            "quick_param:plain:token": "不该收到",    # 没选中的插件，忽略
        },
    )
    assert response.status_code == 200
    html = wait_for_quick(client, "关键字 漫画")
    assert "快插件" in html and "口令收到" in html
    assert "参数 password,token" in html  # 只收到自己声明过的参数
    assert "关键字 漫画" in html and "另一个快插件" in html
    assert "普通插件" not in html  # 只跑勾选的那个
    assert "synthetic-pass" not in html  # 口令不回显
    assert "不该收到" not in html


def test_quick_update_requires_required_params(settings: Settings, client: TestClient) -> None:
    write_plugin(settings.plugins_dir, "quick", code=QUICK_PLUGIN, metadata='[plugin]\ntitle = "快插件"\n')
    client.get("/plugins")

    client.post(
        "/plugins/quick-update",
        headers={"HX-Request": "true"},
        data={"quick_plugin": "quick", "quick_param:quick:password": "synthetic-pass"},
    )
    html = wait_for_quick(client, "缺少必填参数")
    assert "失败" in html and "token" in html
    assert "synthetic-pass" not in html


def test_quick_update_isolates_failures(settings: Settings, client: TestClient) -> None:
    boom = QUICK_PLUGIN.replace(
        'return QuickUpdateResult(ok=True, summary=summary, details="更新明细")',
        'raise RuntimeError("更新炸了")',
    )
    write_plugin(settings.plugins_dir, "quick", code=QUICK_PLUGIN, metadata='[plugin]\ntitle = "快插件"\n')
    write_plugin(settings.plugins_dir, "boom", code=boom, metadata='[plugin]\ntitle = "坏插件"\n')
    client.get("/plugins")

    client.post(
        "/plugins/quick-update",
        headers={"HX-Request": "true"},
        data={
            "quick_plugin": ["quick", "boom"],
            "quick_param:quick:token": "t-alpha",
            "quick_param:boom:token": "t-alpha",
        },
    )
    html = wait_for_quick(client, "更新炸了")
    assert "快插件" in html and "完成" in html
    assert "坏插件" in html and "失败" in html


def test_quick_update_without_selection_says_so(settings: Settings, client: TestClient) -> None:
    write_plugin(settings.plugins_dir, "quick", code=QUICK_PLUGIN)
    client.get("/plugins")
    response = client.post(
        "/plugins/quick-update", headers={"HX-Request": "true"}, data={"quick_param:quick:token": "x"}
    )
    assert "没有选中任何插件" in response.text
    assert "没有选中任何插件" in client.get("/plugins/quick-update/status").text
