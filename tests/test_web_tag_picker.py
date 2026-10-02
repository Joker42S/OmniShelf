"""标签筛选选择器：模板结构 + JS 纯函数（有 node 时真跑一遍）。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.web.app import create_app

TAG_PICKER_JS = (
    Path(__file__).resolve().parents[1] / "src" / "omnishelf" / "web" / "static" / "tag-picker.js"
)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能跑 JS 单测")


@pytest.fixture()
def client(settings: Any) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture()
def tagged(session: Session, make_item: Any) -> dict[str, str]:
    alpha = make_item(kind="movie", title="Alpha", creators=["张三"])
    beta = make_item(kind="movie", title="Beta")
    gamma = make_item(kind="movie", title="Gamma")  # 没有任何标签
    repo.set_tags(session, alpha.id, ["科幻", "经典"])
    repo.set_tags(session, beta.id, ["动作"])
    session.commit()
    return {"alpha": alpha.id, "beta": beta.id, "gamma": gamma.id}


# ------------------------------------------------------------------- 结构


def test_tag_picker_structure(client: TestClient, tagged: dict[str, str]) -> None:
    html = client.get("/").text
    # 左：多选标签列表，按名称排序
    pool = re.search(r'<select class="tag-pool"[^>]*>(.*?)</select>', html, re.S)
    assert pool is not None
    options = re.findall(r'<option value="([^"]+)"', pool.group(1))
    assert options == sorted(options)  # 按名称排序
    assert set(options) == {"科幻", "经典", "动作"}
    # 标签池本身不是筛选字段（没有 name，不会被提交）
    assert 'name="tags_include"' in html  # 右框才是字段
    assert re.search(r'<select class="tag-pool"(?![^>]*name=)', html)
    # 中：两个按钮
    assert re.findall(r'data-tag-move="(\w+)"', html) == ["include", "exclude"]
    assert html.count('type="button"') >= 2
    # 右：两个文本框
    assert html.count('[data-tag-target=') + html.count("data-tag-target=") >= 2
    assert re.search(r"data-tag-target=\"include\"", html)
    assert re.search(r"data-tag-target=\"exclude\"", html)
    # 脚本
    assert "/static/tag-picker.js" in html
    assert client.get("/static/tag-picker.js").status_code == 200


def test_tag_picker_shows_active_tags(client: TestClient, tagged: dict[str, str]) -> None:
    html = client.get("/", params=[("tags_include", "科幻"), ("tags_exclude", "动作")]).text
    include_box = re.search(
        r'name="tags_include"[^>]*value="([^"]*)"', html, re.S
    )
    exclude_box = re.search(r'name="tags_exclude"[^>]*value="([^"]*)"', html, re.S)
    assert include_box is not None and include_box.group(1) == "科幻"
    assert exclude_box is not None and exclude_box.group(1) == "动作"
    html2 = client.get("/", params=[("tags_include", "科幻"), ("tags_include", "经典")]).text
    box2 = re.search(r'name="tags_include"[^>]*value="([^"]*)"', html2, re.S)
    assert box2 is not None and box2.group(1) == "科幻, 经典"


def test_comma_separated_tags_from_the_text_boxes(
    client: TestClient, tagged: dict[str, str]
) -> None:
    """文本框提交的是逗号分隔的标签，服务端要按 AND 解析（中英文逗号都认）。"""
    for value in ("科幻, 经典", "科幻,经典", "科幻 ， 经典"):
        html = client.get("/", params={"tags_include": value}).text
        titles = re.findall(r'<td>\s*<a href="/items/[0-9A-Z]{26}">([^<]+)</a>', html)
        assert titles == ["Alpha"], value
    # 排除是 AND：Beta 带着「动作」，所以也被排掉，只剩没标签的 Gamma
    html = client.get("/", params={"tags_exclude": "科幻, 动作"}).text
    assert re.findall(r'<td>\s*<a href="/items/[0-9A-Z]{26}">([^<]+)</a>', html) == ["Gamma"]
    html = client.get("/", params={"tags_exclude": "科幻"}).text
    assert set(re.findall(r'<td>\s*<a href="/items/[0-9A-Z]{26}">([^<]+)</a>', html)) == {
        "Beta",
        "Gamma",
    }


def test_seed_chips_do_full_navigation(client: TestClient, tagged: dict[str, str]) -> None:
    """去掉某个条件要整页跳转（否则上面的输入框会留着被去掉的值）。"""
    html = client.get("/", params=[("tags_include", "科幻")]).text
    chip = re.search(r'<a class="chip" href="([^"]+)"([^>]*)>', html)
    assert chip is not None
    assert "hx-get" not in chip.group(2)
    # chips 在 items-panel 里（会随结果一起刷新）
    assert html.index('class="chips"') > html.index('id="items-panel"')


def test_hx_trigger_does_not_fire_on_the_tag_pool(client: TestClient) -> None:
    """标签池是多选框，选中它不该触发表单提交。"""
    html = client.get("/").text
    trigger = re.search(r'hx-trigger="([^"]+)"', html)
    assert trigger is not None
    assert "change from:select[name='kind']" in trigger.group(1)
    assert "change from:select," not in trigger.group(1)
    assert "from:select[nam" in trigger.group(1)


# ------------------------------------------------------------------------- JS


@needs_node
def test_js_is_syntactically_valid() -> None:
    result = subprocess.run(
        ["node", "--check", str(TAG_PICKER_JS)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


@needs_node
def test_js_pure_functions() -> None:
    script = f"""
    const p = require({json.dumps(str(TAG_PICKER_JS))});
    const out = {{
      split: p.splitTags(" 科幻, 经典 ，动作,,科幻 "),
      empty: p.splitTags(""),
      nul: p.splitTags(null),
      merge: p.mergeTagList("科幻", ["经典", "科幻", " 动作 "]),
      mergeIntoEmpty: p.mergeTagList("", ["科幻"]),
      mergeKeepsOrder: p.mergeTagList("b, a", ["c"]),
    }};
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["split"] == ["科幻", "经典", "动作"]  # 中英文逗号、去空、去重
    assert out["empty"] == [] and out["nul"] == []
    assert out["merge"] == "科幻, 经典, 动作"  # 已有顺序保留 + 去重 + trim
    assert out["mergeIntoEmpty"] == "科幻"
    assert out["mergeKeepsOrder"] == "b, a, c"


# ------------------------------------------------ JS 交互（极简 DOM 桩，真跑）

_JS_HARNESS = """
const api = require({js});

function option(value, selected) {
  return {{ value: value, selected: !!selected, tagName: "OPTION" }};
}
function input(name) {
  const classes = new Set();
  return {{
    value: "", focused: 0, flashes: 0,
    classList: {{
      add: (c) => classes.add(c),
      remove: (c) => {{ classes.delete(c); if (c === "flash") this.flashes++; }},
      has: (c) => classes.has(c),
    }},
  }};
}
function button(target) {{
  const handlers = {{}};
  return {{
    target,
    getAttribute: (name) => (name === "data-tag-move" ? target : null),
    addEventListener: (type, fn) => (handlers[type] = fn),
    click: () => handlers.click(),
  }};
}}

function makePicker() {{
  const pool = {{
    options: [option("科幻", true), option("经典", false), option("动作", true)],
    focused: 0,
    get selectedOptions() {{ return this.options.filter((o) => o.selected); }},
    focus() {{ this.focused++; }},
    handlers: {{}},
    addEventListener(type, fn) {{ this.handlers[type] = fn; }},
  }};
  const include = input("include");
  const exclude = input("exclude");
  const buttons = [button("include"), button("exclude")];
  const picker = {{
    querySelector: (sel) => {{
      if (sel === "[data-tag-pool]") return pool;
      if (sel.includes("include")) return include;
      if (sel.includes("exclude")) return exclude;
      return null;
    }},
    querySelectorAll: (sel) => (sel === "[data-tag-move]" ? buttons : []),
  }};
  return {{ picker, pool, include, exclude, buttons }};
}}

const out = {{}};

// 1) 选中两个 -> 点「包含」
let p = makePicker();
api.initPicker(p.picker);
p.buttons[0].click();
out.includeAfterClick = p.include.value;
out.selectionCleared = p.pool.selectedOptions.length;

// 2) 点「排除」
p.pool.options[1].selected = true; // 经典
p.buttons[1].click();
out.excludeAfterClick = p.exclude.value;
out.poolUntouched = p.include.value;

// 3) 重复添加不产生重复（科幻 已经在包含里）
p.pool.options[0].selected = true;
p.pool.options[2].selected = true;
p.buttons[0].click();
out.noDuplicates = p.include.value;

// 4) 没选中任何标签时点按钮：不该改内容，只是把焦点给列表
let q = makePicker();
api.initPicker(q.picker);
q.pool.options.forEach((o) => (o.selected = false));
q.buttons[0].click();
out.emptyClickValue = q.include.value;
out.emptyClickFocused = q.pool.focused;

// 5) 双击左边标签 -> 加入「包含」
let r = makePicker();
api.initPicker(r.picker);
r.pool.handlers.dblclick({{ target: r.pool.options[1] }});
out.dblclickInclude = r.include.value;

console.log(JSON.stringify(out));
"""


@needs_node
def test_js_button_interaction() -> None:
    # 桩是按 f-string 风格写的双花括号，这里还原成 JS 的单花括号
    script = _JS_HARNESS.replace("{{", "{").replace("}}", "}")
    script = script.replace("{js}", json.dumps(str(TAG_PICKER_JS)))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    assert out["includeAfterClick"] == "科幻, 动作"  # 按池里的顺序加入
    assert out["selectionCleared"] == 0  # 加完清空选中，防重复点击
    assert out["excludeAfterClick"] == "经典"
    assert out["poolUntouched"] == "科幻, 动作"  # 排除不影响包含
    assert out["noDuplicates"] == "科幻, 动作"  # 已在包含里的不再重复
    assert out["emptyClickValue"] == ""  # 没选中就什么都不做
    assert out["emptyClickFocused"] == 1  # 只把焦点给列表
    assert out["dblclickInclude"] == "经典"  # 双击 = 加入包含


# ------------------------------------------------- 静态资源版本号（防缓存）


def test_static_assets_are_versioned(client: TestClient) -> None:
    """页面引用静态资源时带内容哈希版本号：改了 CSS/JS 刷新就能生效，不会用到旧缓存。"""
    from omnishelf.web.app import asset_version

    html = client.get("/").text
    version = asset_version()
    assert len(version) == 10
    assert f"/static/app.css?v={version}" in html
    assert f"/static/vendor/pico.min.css?v={version}" in html
    assert f"/static/tag-picker.js?v={version}" in html
    # 带版本号也能正常取到文件
    assert client.get(f"/static/app.css?v={version}").status_code == 200


def test_asset_version_changes_with_content(tmp_path: Path) -> None:
    from omnishelf.web.app import asset_version

    (tmp_path / "sub").mkdir()
    css = tmp_path / "sub" / "a.css"
    css.write_text("body{color:red}", encoding="utf-8")
    before = asset_version(tmp_path)
    css.write_text("body{color:blue}", encoding="utf-8")
    after = asset_version(tmp_path)
    assert before != after
    # 非 css/js 文件不影响版本号
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert asset_version(tmp_path) == after


def test_tag_picker_layout_rules_are_present() -> None:
    """布局契约：三列用 grid + minmax(0,…)（不会被内容撑破换行），按钮竖排。"""
    import re

    css = TAG_PICKER_JS.with_name("app.css").read_text(encoding="utf-8")
    grid = re.search(r"\.tag-picker-grid\s*\{([^}]*)\}", css)
    assert grid is not None
    body = grid.group(1)
    assert "display: grid" in body
    assert "minmax(0, 3fr) auto minmax(0, 3fr)" in body  # 左右两栏可收缩，中间按钮自适应
    assert "flex-wrap: wrap" not in body  # 换行正是之前布局走样的原因
    actions = re.search(r"\.tag-actions\s*\{([^}]*)\}", css)
    assert actions is not None and "flex-direction: column" in actions.group(1)
    # 手机宽度才退化成上下排列
    assert "@media (max-width: 480px)" in css
