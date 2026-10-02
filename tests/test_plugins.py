"""插件运行时：元数据解析、加载/停止、热重载、错误隔离、宿主查询接口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.config import Settings
from omnishelf.db import build_engine
from omnishelf.plugins import logstore
from omnishelf.plugins.host import QueryHostAPI
from omnishelf.plugins.logstore import PluginLogStore
from omnishelf.plugins.manager import (
    STATE_ERROR,
    STATE_LOADED,
    PluginError,
    PluginManager,
)
from omnishelf.plugins.metadata import PluginMetadataError, load_metadata, parse_metadata_text

# ------------------------------------------------------------------ 造插件


def module_calls(loaded: Any) -> list[str]:
    """插件模块里那个用来记录 start/stop 的列表（测试用）。"""
    return list(getattr(loaded.module, "calls", []))


def write_plugin(
    root: Path,
    plugin_id: str,
    *,
    code: str,
    metadata: str | None = None,
    templates: dict[str, str] | None = None,
) -> Path:
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    if metadata is not None:
        (directory / "metadata.toml").write_text(metadata, encoding="utf-8")
    (directory / "plugin.py").write_text(code, encoding="utf-8")
    for name, body in (templates or {}).items():
        path = directory / "templates" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return directory


SIMPLE_PLUGIN = '''
from omnishelf.plugins.api import Plugin

calls = []


class Simple(Plugin):
    def start(self):
        self.log(f"started {self.plugin_id}")
        calls.append("start")

    def stop(self):
        self.log("stopped")
        calls.append("stop")

    def render_page(self, request=None):
        return f"<p>hello {self.plugin_id}</p>"
'''


NO_START_PLUGIN = '''
from omnishelf.plugins.api import Plugin


class HalfDone(Plugin):
    def stop(self):
        pass
'''


OLD_STYLE_PLUGIN = '''
# 旧的「两个孤立函数」写法：现在必须报错，提示要继承 Plugin
def start(ctx):
    pass


def stop(ctx):
    pass
'''

#: 复杂插件可以带自己的目录结构（入口文件只是入口），见 docs/plugin-development.md
PACKAGE_PLUGIN = '''
import sys
from pathlib import Path

from omnishelf.plugins.api import Plugin

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __PKG__ import describe


class WithPackage(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    def render_page(self, request=None):
        return f"<p>{describe()}</p>"
'''


@pytest.fixture()
def plugins_root(tmp_path: Path) -> Path:
    # settings fixture 也会建这个目录（插件路径统一落在 tmp_path 下）
    root = tmp_path / "plugins"
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture()
def manager(settings: Settings, plugins_root: Path, tmp_path: Path, engine: Any) -> PluginManager:
    """插件管理器（挂在已经建好表的 engine 上，插件可以真的导入数据）。"""
    return PluginManager(
        plugins_dir=plugins_root,
        plugins_data_dir=tmp_path / "plugins_data",
        engine=engine,
    )


# ------------------------------------------------------------------ 元数据


def test_parse_metadata_defaults(tmp_path: Path) -> None:
    meta = parse_metadata_text('[plugin]\ntitle = "统计图"\n', directory=tmp_path / "kind_chart")
    assert meta.id == "kind_chart"  # 默认取目录名
    assert meta.title == "统计图"
    assert meta.entry == "plugin.py" and meta.class_name is None
    assert meta.version == "0.0.0"


def test_parse_metadata_full(tmp_path: Path) -> None:
    meta = parse_metadata_text(
        """
[plugin]
id = "kind_chart"
title = "分类统计"
version = "1.2.3"
description = "按 kind 画柱状图"
author = "max"
homepage = "https://example.invalid"
entry = "main.py:MyThing"
requires = ["query"]
extra_key = "undeclared"
""",
        directory=tmp_path,
    )
    assert (meta.id, meta.version, meta.entry) == ("kind_chart", "1.2.3", "main.py")
    assert meta.class_name == "MyThing"
    assert meta.entry_spec == "main.py:MyThing"
    assert meta.requires == ("query",)
    assert meta.extra == {"extra_key": "undeclared"}  # 不认识的键原样留着


@pytest.mark.parametrize(
    "text",
    ["[plugin]\nrequires = 3\n", "not toml at all ===", "[plugin]\nid = []\n"],
)
def test_parse_metadata_rejects_garbage(tmp_path: Path, text: str) -> None:
    with pytest.raises(PluginMetadataError):
        parse_metadata_text(text, directory=tmp_path)


def test_load_metadata_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PluginMetadataError):
        load_metadata(tmp_path)


def test_metadata_paths(tmp_path: Path) -> None:
    meta = parse_metadata_text('[plugin]\n', directory=tmp_path)
    assert meta.entry_path == tmp_path / "plugin.py"
    assert meta.templates_dir == tmp_path / "templates"
    assert meta.as_dict()["id"] == tmp_path.name


# ------------------------------------------------------------------ 加载/停止


def test_discover_ignores_dirs_without_metadata(plugins_root: Path, manager: PluginManager) -> None:
    write_plugin(plugins_root, "good", code=SIMPLE_PLUGIN, metadata='[plugin]\ntitle = "好的"\n')
    (plugins_root / "_templates").mkdir()
    (plugins_root / "no_metadata").mkdir()
    found = manager.discover()
    assert list(found) == ["good"]


def test_load_calls_start_and_stop(plugins_root: Path, manager: PluginManager) -> None:
    write_plugin(plugins_root, "good", code=SIMPLE_PLUGIN, metadata='[plugin]\ntitle = "好的"\n')
    manager.load_all()
    status = manager.status("good")
    assert status.state == STATE_LOADED and status.has_page and status.title == "好的"

    loaded = manager.get("good")
    assert loaded is not None
    assert module_calls(loaded) == ["start"]
    assert loaded.context.logs == ["started good"]

    manager.stop_all()
    assert module_calls(loaded) == ["start", "stop"]
    assert manager.get("good") is None


def test_plugin_must_implement_start_and_stop(plugins_root: Path, manager: PluginManager) -> None:
    """start/stop 是抽象方法：没实现的话实例化就失败，并给出能看懂的原因。"""
    write_plugin(plugins_root, "halfdone", code=NO_START_PLUGIN, metadata="[plugin]\n")
    with pytest.raises(PluginError) as excinfo:
        manager.load("halfdone")
    assert "实例化 HalfDone 失败" in str(excinfo.value)
    assert "start" in str(excinfo.value)

    manager._safe_load("halfdone")  # noqa: SLF001 - 模拟 load_all
    assert manager.status("halfdone").state == STATE_ERROR


def test_old_function_style_plugin_is_rejected(plugins_root: Path, manager: PluginManager) -> None:
    """旧的函数式写法不再支持：报错并说明要继承 Plugin。"""
    write_plugin(plugins_root, "oldstyle", code=OLD_STYLE_PLUGIN, metadata="[plugin]\n")
    with pytest.raises(PluginError) as excinfo:
        manager.load("oldstyle")
    assert "没有 Plugin 子类" in str(excinfo.value)
    assert "omnishelf.plugins.api" in str(excinfo.value)


def test_plugin_class_must_be_explicit_when_several(plugins_root: Path, manager: PluginManager) -> None:
    code = SIMPLE_PLUGIN + '''

class Another(Simple):
    pass
'''
    write_plugin(plugins_root, "two", code=code, metadata="[plugin]\n")
    with pytest.raises(PluginError) as excinfo:
        manager.load("two")
    assert "多个 Plugin 子类" in str(excinfo.value) and "entry" in str(excinfo.value)


def test_class_name_can_be_given_in_metadata(plugins_root: Path, manager: PluginManager) -> None:
    code = SIMPLE_PLUGIN + '''

class Another(Simple):
    pass
'''
    write_plugin(plugins_root, "two", code=code, metadata='[plugin]\nentry = "plugin.py:Another"\n')
    manager.load_all()
    loaded = manager.get("two")
    assert loaded is not None and type(loaded.instance).__name__ == "Another"


def test_class_name_typo_is_reported(plugins_root: Path, manager: PluginManager) -> None:
    write_plugin(plugins_root, "typo", code=SIMPLE_PLUGIN, metadata='[plugin]\nentry = "plugin.py:Nope"\n')
    with pytest.raises(PluginError) as excinfo:
        manager.load("typo")
    assert "没有 Nope" in str(excinfo.value)


def test_instance_is_exposed_and_module_helpers_are_kept(
    plugins_root: Path, manager: PluginManager
) -> None:
    """装载结果同时给出插件实例（新契约）和模块（插件自己的辅助函数/常量）。"""
    from omnishelf.plugins.api import Plugin as SDKPlugin

    write_plugin(plugins_root, "good", code=SIMPLE_PLUGIN, metadata="[plugin]\n")
    manager.load_all()
    loaded = manager.get("good")
    assert loaded is not None
    assert isinstance(loaded.instance, SDKPlugin)
    assert loaded.instance.plugin_id == "good"
    assert loaded.module.calls == ["start"]  # 模块级的东西还在（插件内部实现）


def test_broken_plugin_does_not_break_others(plugins_root: Path, manager: PluginManager) -> None:
    write_plugin(plugins_root, "good", code=SIMPLE_PLUGIN, metadata="[plugin]\n")
    write_plugin(plugins_root, "raises", code="raise RuntimeError('炸了')\n", metadata="[plugin]\n")
    write_plugin(
        plugins_root, "bad_start",
        code=SIMPLE_PLUGIN.replace("calls.append(\"start\")", "raise ValueError('start 失败')"),
        metadata="[plugin]\n",
    )
    write_plugin(plugins_root, "bad_meta", code=SIMPLE_PLUGIN, metadata="这不是 toml ===")

    statuses = {s.id: s for s in manager.load_all()}
    assert statuses["good"].state == STATE_LOADED
    assert statuses["raises"].state == STATE_ERROR and "炸了" in (statuses["raises"].error or "")
    assert statuses["bad_start"].state == STATE_ERROR and "start 失败" in (
        statuses["bad_start"].error or ""
    )
    assert statuses["bad_meta"].state == STATE_ERROR and "TOML" in (statuses["bad_meta"].error or "")


def test_stop_error_is_recorded_but_not_fatal(plugins_root: Path, manager: PluginManager) -> None:
    code = SIMPLE_PLUGIN.replace('calls.append("stop")', "raise RuntimeError('stop 炸了')")
    write_plugin(plugins_root, "badstop", code=code, metadata="[plugin]\n")
    manager.load_all()
    manager.unload("badstop")
    assert "stop 炸了" in (manager.status("badstop").error or "")


# ------------------------------------------------------------------ 热重载


def test_reload_changed_picks_up_new_code(plugins_root: Path, manager: PluginManager) -> None:
    directory = write_plugin(
        plugins_root, "hot", code=SIMPLE_PLUGIN, metadata='[plugin]\ntitle = "第一版"\n'
    )
    manager.load_all()
    assert manager.status("hot").title == "第一版"
    assert manager.reload_changed() == []  # 没改就不用重载

    (directory / "plugin.py").write_text(
        SIMPLE_PLUGIN.replace("hello", "HELLO"), encoding="utf-8"
    )
    assert manager.reload_changed() == ["hot"]
    loaded = manager.get("hot")
    assert loaded is not None
    assert "HELLO" in manager.page("hot", None)
    # 旧的实例被 stop 掉了，新实例 start 过
    assert module_calls(loaded) == ["start"]


def test_reload_changed_detects_metadata_and_template_changes(
    plugins_root: Path, manager: PluginManager
) -> None:
    directory = write_plugin(
        plugins_root, "hot2", code=SIMPLE_PLUGIN, metadata="[plugin]\n",
        templates={"page.html": "v1"},
    )
    manager.load_all()
    (directory / "templates" / "page.html").write_text("v2", encoding="utf-8")
    assert manager.reload_changed() == ["hot2"]
    (directory / "metadata.toml").write_text('[plugin]\ntitle = "改了标题"\n', encoding="utf-8")
    assert manager.reload_changed() == ["hot2"]
    assert manager.status("hot2").title == "改了标题"


def test_reload_reimports_the_plugins_own_package(
    plugins_root: Path, manager: PluginManager
) -> None:
    """插件自带的子包也要跟着重载。

    入口文件是宿主直接编译执行的（每次都是新代码），它 import 的子包却走正常 import
    机制、留在 ``sys.modules`` 里；不清掉就会出现「入口是新的、子包是旧的」，
    表现为一句看不懂的 ``PicamangaConfig.__init__() got an unexpected keyword argument``。
    """
    pkg = "hotreload_pkg"
    directory = write_plugin(
        plugins_root,
        "withpkg",
        code=PACKAGE_PLUGIN.replace("__PKG__", pkg),
        metadata="[plugin]\n",
    )
    package = directory / pkg
    package.mkdir()
    (package / "__init__.py").write_text('def describe():\n    return "v1"\n', encoding="utf-8")
    manager.load_all()
    assert "v1" in manager.page("withpkg", None)

    # 改动要让文件大小也变：.pyc 按「源码 mtime（整秒）+ 大小」失效，
    # 同一秒内改成一样长的内容，import 机制自己就会拿旧代码，测不出这里要测的东西。
    (package / "__init__.py").write_text(
        'def describe():\n    return "v2-a-longer-body"\n', encoding="utf-8"
    )
    assert manager.reload_changed() == ["withpkg"]
    assert "v2-a-longer-body" in manager.page("withpkg", None)


def test_reload_single(plugins_root: Path, manager: PluginManager) -> None:
    for pid in ("a", "b"):
        write_plugin(plugins_root, pid, code=SIMPLE_PLUGIN, metadata="[plugin]\n")
    manager.load_all()
    assert manager.reload("a") == ["a"]
    assert manager.reload() == ["a", "b"]


# ------------------------------------------------------------------ 页面/表单


def test_page_and_post(plugins_root: Path, manager: PluginManager) -> None:
    code = '''
from omnishelf.plugins.api import Plugin


class Pagey(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    def render_page(self, request=None):
        return "<p>page</p>"

    def handle_post(self, request=None, form=None):
        self.save_config({"answer": (form or {}).get("answer", "")})
        return f"<p>saved {self.config['answer']}</p>"
'''
    write_plugin(plugins_root, "pagey", code=code, metadata="[plugin]\n")
    manager.load_all()
    assert "page" in manager.page("pagey", None)
    assert manager.post("pagey", None, {"answer": "42"}) == "<p>saved 42</p>"
    assert (manager.plugins_data_dir / "pagey" / "config.toml").is_file()
    assert manager.get("pagey").context.config == {"answer": "42"}  # type: ignore[union-attr]


def test_post_without_handler_returns_none(plugins_root: Path, manager: PluginManager) -> None:
    write_plugin(plugins_root, "quiet", code=SIMPLE_PLUGIN, metadata="[plugin]\n")
    manager.load_all()
    assert manager.post("quiet", None, {}) is None


def test_page_errors_are_wrapped(plugins_root: Path, manager: PluginManager) -> None:
    code = '''
from omnishelf.plugins.api import Plugin


class Boom(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    def render_page(self, request=None):
        raise KeyError("坏了")
'''
    write_plugin(plugins_root, "boom", code=code, metadata="[plugin]\n")
    manager.load_all()
    with pytest.raises(PluginError) as excinfo:
        manager.page("boom", None)
    assert "render_page() 出错" in str(excinfo.value)


def test_page_for_unknown_plugin(manager: PluginManager) -> None:
    with pytest.raises(PluginError):
        manager.page("nope", None)


def test_context_helpers(plugins_root: Path, manager: PluginManager) -> None:
    code = '''
from omnishelf.plugins.api import Plugin


class Ctx(Plugin):
    def start(self):
        self.log("hi")
        self.write_state({"n": 1})
        self.save_config({"color": "#123456", "n": 3, "flag": True, "tags": ["a", "b"]})

    def stop(self):
        pass

    def render_page(self, request=None):
        return self.render("t.html", value=self.read_state("state.json")["n"])
'''
    write_plugin(
        plugins_root, "ctx", code=code, metadata="[plugin]\n",
        templates={"t.html": "n={{ value }} color={{ ctx.config['color'] }}"},
    )
    manager.load_all()
    loaded = manager.get("ctx")
    assert loaded is not None
    assert loaded.context.logs == ["hi"]
    assert loaded.context.read_state()["n"] == 1
    html = manager.page("ctx", None)
    assert "n=1" in html and "#123456" in html
    config_text = (manager.plugins_data_dir / "ctx" / "config.toml").read_text(encoding="utf-8")
    assert 'color = "#123456"' in config_text and "flag = true" in config_text
    assert 'tags = ["a", "b"]' in config_text
    # 重新读配置（模拟进程重启）
    loaded.context.load_config()
    assert loaded.context.config["n"] == 3


def test_requires_reports_unknown_interfaces(plugins_root: Path, manager: PluginManager) -> None:
    write_plugin(
        plugins_root, "needs", code=SIMPLE_PLUGIN, metadata='[plugin]\nrequires = ["query", "net"]\n'
    )
    manager.load_all()
    meta = manager.metadata("needs")
    assert meta is not None
    assert manager.check_requires(meta) == ["net"]  # 宿主只提供 query / import / audio
    assert list(manager.interfaces()) == ["query", "import", "audio"]


# ------------------------------------------------------------- 宿主查询接口


def test_host_query_api(session: Session, settings: Settings) -> None:
    alpha = repo.create_item(session, kind="movie", title="Alpha", year=1999, rating=9.0, creators=["张三"])
    beta = repo.create_item(session, kind="game", title="Beta", year=2010)
    repo.set_tags(session, alpha.id, ["科幻"])
    session.commit()

    api = QueryHostAPI(build_engine(settings))
    stats = api.stats()
    assert stats["total"] == 2 and stats["by_kind"] == {"movie": 1, "game": 1}

    kinds = {item["name"]: item for item in api.kinds()}
    assert "movie" in kinds and kinds["movie"]["label"]
    assert {status["name"] for status in api.statuses()} >= {"wishlist", "doing", "done"}
    assert api.all_tags() == ["科幻"]

    assert api.count_items(kind="movie") == 1
    rows = api.list_items(sort="title", descending=False)
    assert [row["title"] for row in rows] == ["Alpha", "Beta"]
    assert rows[0]["creators"] == ["张三"] and rows[0]["tags"] == ["科幻"]
    assert isinstance(rows[0], dict)  # 插件只拿到普通数据，不是 ORM 对象

    one = api.get_item(alpha.id)
    assert one is not None and one["title"] == "Alpha"
    assert api.get_item("01ZZZZZZZZZZZZZZZZZZZZZZZZ") is None


def test_host_query_api_ignores_unknown_filters_and_caps_limit(session: Session, settings: Settings) -> None:
    for n in range(5):
        repo.create_item(session, kind="movie", title=f"M{n}")
    session.commit()
    api = QueryHostAPI(build_engine(settings), max_limit=3)
    assert api.count_items(kind="movie", totally_unknown="x") == 5  # 不认识的键被忽略
    assert len(api.list_items(limit=100)) == 3  # 被 max_limit 夹住
    assert len(api.list_items(limit=0)) == 1  # 至少 1 条
    assert json.loads(json.dumps(api.list_items()))  # 结果可 JSON 序列化


# --------------------------------------------------------------- import 接口


def test_host_import_file_runs_the_real_import_pipeline(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """宿主给插件的 import 接口就是内置导入链路：判重、审计、存档一个不少。"""
    from omnishelf.plugins.host import RepositoryHostAPI

    payload = tmp_path / "movies.json"
    payload.write_text(
        json.dumps({"items": [{"title": "M1", "year": 2020, "tmdb_id": 1}]}), encoding="utf-8"
    )
    api = RepositoryHostAPI(build_engine(settings), imports_dir=settings.imports_dir)

    result = api.import_file("json_movies", payload)
    assert result["inserted"] == 1
    assert result["error_count"] == 0 and result["warning_count"] == 0
    assert result["log_id"] and "共 1 条" in result["summary"]
    assert result["archive_path"] == "imports/json_movies/movies.json"
    assert (settings.imports_dir / "json_movies" / "movies.json").is_file()

    # 再导一次：幂等，未变化
    assert api.import_file("json_movies", payload)["unchanged"] == 1

    # archive=False 就不存档（插件只想试导时用）
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"items": [{"title": "M2", "tmdb_id": 2}]}), encoding="utf-8")
    assert api.import_file("json_movies", other, archive=False)["archive_path"] is None


def test_host_import_file_errors_are_clear(settings: Settings, tmp_path: Path) -> None:
    from omnishelf.plugins.host import RepositoryHostAPI

    api = RepositoryHostAPI(build_engine(settings))
    payload = tmp_path / "x.json"
    payload.write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        api.import_file("json_movies", tmp_path / "nope.json")
    with pytest.raises(ValueError) as excinfo:
        api.import_file("没有这个", payload)
    assert "没有这个 importer" in str(excinfo.value)
    assert "json_movies" in str(excinfo.value)  # 顺带告诉插件有哪些可用


def test_host_importer_names(settings: Settings) -> None:
    from omnishelf.plugins.host import RepositoryHostAPI

    names = RepositoryHostAPI(build_engine(settings)).importer_names()
    assert {"json_movies", "douban_movies", "picamanga", "music_files", "itunes_xml"} <= set(names)


def test_host_scan_audio_writes_a_manifest(settings: Settings, engine: Any, tmp_path: Path) -> None:
    """``audio`` 接口：插件只给「扫哪儿」和「清单写哪儿」，扫描口径与清单形状都在宿主这边。"""
    from omnishelf.plugins.host import RepositoryHostAPI
    from tests.audio_synth import CASES

    library = tmp_path / "library"
    library.mkdir()
    (library / "01 - Tagged.mp3").write_bytes(CASES["a_id3v23.mp3"]())
    (library / "$RECYCLE.BIN").mkdir()
    (library / "$RECYCLE.BIN" / "copy.mp3").write_bytes(CASES["a_id3v23.mp3"]())

    api = RepositoryHostAPI(engine, imports_dir=settings.imports_dir)
    seen: list[int] = []
    summary = api.scan_audio(
        [library], out_path=tmp_path / "out.json", progress=lambda count, _: seen.append(count)
    )
    assert (summary["scanned"], summary["item_count"], summary["failed"]) == (1, 1, 0)
    assert summary["manifest_path"].endswith("out.json")
    assert summary["summary"] == "扫了 1 个音频文件，读到 1 条"
    assert seen == [1]  # 回收站里那份副本没进场
    assert api.audio_suffixes() == sorted(api.audio_suffixes())

    imported = api.import_file("music_files", summary["manifest_path"])
    assert imported["inserted"] == 1
    assert api.count_items(kind="music") == 1


def test_plugin_base_import_helpers(plugins_root: Path, manager: PluginManager, tmp_path: Path) -> None:
    """基类把 import 接口包成了 self.import_file()/self.importer_names()。"""
    code = '''
import json
from omnishelf.plugins.api import Plugin


class Worker(Plugin):
    result = None

    def start(self):
        payload = self.data_dir / "made.json"
        payload.write_text(json.dumps({"items": [{"title": "From Plugin", "tmdb_id": 7}]}),
                           encoding="utf-8")
        self.result = self.import_file("json_movies", payload)
        self.log(f"imported {self.result['inserted']}")

    def stop(self):
        pass
'''
    write_plugin(plugins_root, "worker", code=code, metadata='[plugin]\nrequires = ["import"]\n')
    manager.load_all()
    loaded = manager.get("worker")
    assert loaded is not None
    assert loaded.instance.result["inserted"] == 1  # type: ignore[attr-defined]
    assert "json_movies" in loaded.instance.importer_names()
    assert manager.status("worker").requires == ("import",)


def test_plugin_base_audio_helpers(
    plugins_root: Path, manager: PluginManager, settings: Settings
) -> None:
    """基类把 audio 接口也包成了 ``self.scan_audio()`` / ``self.audio_suffixes()``。"""
    from tests.audio_synth import CASES

    # 测试提前把「用户的音乐目录」摆在插件自己的数据目录里，插件 start 时去扫它
    library = settings.plugins_data_dir / "listener" / "library"
    library.mkdir(parents=True)
    (library / "01 - Tagged.mp3").write_bytes(CASES["a_id3v23.mp3"]())

    code = '''
from omnishelf.plugins.api import Plugin


class Listener(Plugin):
    summary = None
    imported = None

    def start(self):
        self.summary = self.scan_audio(
            [self.data_dir / "library"],
            out_path=self.data_dir / "manifest.json",
            progress=lambda count, path: None,
        )
        self.imported = self.import_file("music_files", self.summary["manifest_path"])

    def stop(self):
        pass
'''
    write_plugin(
        plugins_root,
        "listener",
        code=code,
        metadata='[plugin]\nrequires = ["audio", "import"]\n',
    )
    manager.load_all()
    loaded = manager.get("listener")
    assert loaded is not None
    assert loaded.instance.summary["item_count"] == 1  # type: ignore[attr-defined]
    assert loaded.instance.summary["manifest_path"].endswith("manifest.json")  # type: ignore[attr-defined]
    assert loaded.instance.imported["inserted"] == 1  # type: ignore[attr-defined]
    assert ".flac" in loaded.instance.audio_suffixes()
    assert manager.status("listener").requires == ("audio", "import")


def test_available_interfaces_lists_audio() -> None:
    from omnishelf.plugins.host import AVAILABLE_INTERFACES

    assert AVAILABLE_INTERFACES == ("query", "import", "audio")


# ----------------------------------------------------- @cmd 与命令行命令执行


CMD_PLUGIN = '''
from omnishelf.plugins.api import Plugin, cmd


class Cli(Plugin):
    def start(self):
        pass

    def stop(self):
        pass

    @cmd("hello", help="打个招呼")
    def hello(self, name: str, times: int = 1, *, loud: bool = False, prefix: str = "hi") -> str:
        """打个招呼。

        name: 名字（必填，位置参数）
        times: 重复几次
        loud: 是否大写
        prefix: 前缀
        """
        text = f"{prefix} {name}"
        if loud:
            text = text.upper()
        return " / ".join([text] * times)

    @cmd("boom", help="故意出错")
    def boom(self) -> str:
        """一定会抛异常。"""
        raise RuntimeError("炸了")
'''


def test_commands_are_discovered(plugins_root: Path, manager: PluginManager) -> None:
    write_plugin(plugins_root, "cli", code=CMD_PLUGIN, metadata="[plugin]\n")
    manager.load_all()
    commands = {c.name: c for c in manager.commands("cli")}
    assert set(commands) == {"hello", "boom"}
    assert commands["hello"].help == "打个招呼"
    assert commands["hello"].method == "hello"
    assert commands["hello"].params == ("name", "times", "loud", "prefix")
    assert "name: 名字（必填，位置参数）" in commands["hello"].doc


def test_run_command_parses_arguments(plugins_root: Path, manager: PluginManager) -> None:
    write_plugin(plugins_root, "cli", code=CMD_PLUGIN, metadata="[plugin]\n")
    manager.load_all()
    assert manager.run_command("cli", "hello", ["Ada"]) == "hi Ada"
    assert manager.run_command("cli", "hello", ["Ada", "--times", "2"]) == "hi Ada / hi Ada"
    assert manager.run_command("cli", "hello", ["Ada", "--loud"]) == "HI ADA"
    # 下划线与连字符两种写法都认
    assert manager.run_command("cli", "hello", ["Ada", "--prefix", "yo"]) == "yo Ada"
    assert manager.run_command("cli", "hello", ["Ada", "--times=3"]).count("/") == 2


def test_run_command_errors(plugins_root: Path, manager: PluginManager) -> None:
    write_plugin(plugins_root, "cli", code=CMD_PLUGIN, metadata="[plugin]\n")
    manager.load_all()
    with pytest.raises(PluginError) as excinfo:
        manager.run_command("cli", "nope", [])
    assert "没有命令" in str(excinfo.value) and "hello" in str(excinfo.value)

    with pytest.raises(PluginError) as excinfo:
        manager.run_command("cli", "boom", [])
    assert "执行出错" in str(excinfo.value) and "炸了" in str(excinfo.value)

    # 缺必填参数：argparse 自己会退出（exit code 2）
    with pytest.raises(SystemExit):
        manager.run_command("cli", "hello", [])


def test_command_parser_help_comes_from_docstring(plugins_root: Path, manager: PluginManager) -> None:
    from omnishelf.plugins.manager import build_command_parser

    write_plugin(plugins_root, "cli", code=CMD_PLUGIN, metadata="[plugin]\n")
    manager.load_all()
    loaded = manager.get("cli")
    assert loaded is not None
    command = {c.name: c for c in manager.commands("cli")}["hello"]
    parser = build_command_parser("cli", command, getattr(loaded.instance, "hello"))
    assert parser.prog == "omnishelf plugin cli hello"
    assert "打个招呼" in (parser.description or "")
    helps = {action.dest: action.help for action in parser._actions}
    assert helps["name"] == "名字（必填，位置参数）"
    assert helps["loud"] == "是否大写"
    # bool 默认 True 的参数给 --x/--no-x
    parser2 = build_command_parser(
        "cli", command, lambda self, flag=True: None
    )
    assert parser2.parse_args([]).flag is True
    assert parser2.parse_args(["--no-flag"]).flag is False


def test_commands_for_unknown_or_unloaded_plugin(manager: PluginManager) -> None:
    assert manager.commands("没有这个") == []
    with pytest.raises(PluginError):
        manager.run_command("没有这个", "x", [])


# ------------------------------------------------------------------ 宿主统一日志

CHATTY_METADATA = '[plugin]\ntitle = "话多的插件"\n'   # 没有 metadata.toml 的目录不会被发现

CHATTY_PLUGIN = '''
from omnishelf.plugins.api import Plugin


class Chatty(Plugin):
    def start(self):
        self.log("开工")
        self.log("这一步没做成", level="warning")

    def stop(self):
        self.log("收工")
'''


@pytest.fixture()
def logged_manager(settings: Settings, plugins_root: Path, engine: Any) -> PluginManager:
    """带日志落盘的管理器（``serve`` 那种会轮转的形态）。"""
    return PluginManager(
        plugins_dir=plugins_root,
        plugins_data_dir=settings.plugins_data_dir,
        engine=engine,
        logs_dir=settings.plugin_logs_dir,
    )


def test_ctx_log_lands_in_the_host_file(plugins_root: Path, logged_manager: PluginManager, settings: Settings) -> None:
    """插件说的一句话必须出现在 data/logs/plugins/<id>.log 里，重启也还在。"""
    write_plugin(plugins_root, "chatty", code=CHATTY_PLUGIN, metadata=CHATTY_METADATA)
    logged_manager.load_all()
    text = (settings.plugin_logs_dir / "chatty.log").read_text(encoding="utf-8")
    assert "开工" in text and "这一步没做成" in text
    assert "| WARNING |" in text
    logged_manager.stop_all()
    assert "收工" in (settings.plugin_logs_dir / "chatty.log").read_text(encoding="utf-8")


def test_host_lines_share_the_plugin_file(plugins_root: Path, logged_manager: PluginManager, settings: Settings) -> None:
    """加载/卸载是宿主说的，也写进同一个文件：看日志不用在两个文件之间跳。"""
    write_plugin(plugins_root, "chatty", code=CHATTY_PLUGIN, metadata=CHATTY_METADATA)
    logged_manager.load_all()
    logged_manager.stop_all()
    text = (settings.plugin_logs_dir / "chatty.log").read_text(encoding="utf-8")
    assert "加载完成" in text and "已卸载" in text
    assert "| 宿主 |" in text


def test_hot_reload_appends_instead_of_rewriting(plugins_root: Path, logged_manager: PluginManager, settings: Settings) -> None:
    """热重载会 close 再 open 句柄，但**不该**把本次运行已经写的清掉。"""
    write_plugin(plugins_root, "chatty", code=CHATTY_PLUGIN, metadata=CHATTY_METADATA)
    logged_manager.load_all()
    logged_manager.load("chatty")
    logged_manager.load("chatty")
    text = (settings.plugin_logs_dir / "chatty.log").read_text(encoding="utf-8")
    assert text.count("开工") == 3                    # 三趟都在，没有互相覆盖
    assert text.count("加载完成") == 3


def test_broken_plugin_error_goes_to_its_own_file(plugins_root: Path, logged_manager: PluginManager, settings: Settings) -> None:
    write_plugin(
        plugins_root,
        "broken",
        code="raise RuntimeError('插件代码起不来')\n",
        metadata='[plugin]\ntitle = "坏插件"\n',
    )
    logged_manager.load_all()
    text = (settings.plugin_logs_dir / "broken.log").read_text(encoding="utf-8")
    assert "| ERROR | 宿主 |" in text and "加载失败" in text


def test_status_reports_the_log_file(plugins_root: Path, logged_manager: PluginManager, settings: Settings) -> None:
    write_plugin(plugins_root, "chatty", code=CHATTY_PLUGIN, metadata=CHATTY_METADATA)
    logged_manager.load_all()
    status = logged_manager.status("chatty")
    assert status.log_file == str(settings.plugin_logs_dir / "chatty.log")
    assert status.as_dict()["log_file"] == status.log_file


def test_manager_without_logs_dir_stays_in_memory(plugins_root: Path, manager: PluginManager, tmp_path: Path) -> None:
    """没给 ``logs_dir``（嵌入式/测试）就只在内存里留，不往磁盘上乱写。"""
    write_plugin(plugins_root, "chatty", code=CHATTY_PLUGIN, metadata=CHATTY_METADATA)
    manager.load_all()
    assert manager.status("chatty").log_file is None
    assert manager.status("chatty").logs == ["开工", "这一步没做成"]
    assert not (tmp_path / "data" / "logs").exists()


# ------------------------------------------------------------------ 日志文件本身


def test_two_stores_do_not_share_their_state(tmp_path: Path) -> None:
    """每个 store 自己持有句柄，不靠进程级全局 —— 两个 store 各写各的文件。"""
    first = PluginLogStore(tmp_path / "a")
    second = PluginLogStore(tmp_path / "b")
    first.write("demo", "只有第一份")
    second.write("demo", "只有第二份")
    assert "第一份" in (tmp_path / "a" / "demo.log").read_text(encoding="utf-8")
    assert "第二份" in (tmp_path / "b" / "demo.log").read_text(encoding="utf-8")
    assert "第二份" not in (tmp_path / "a" / "demo.log").read_text(encoding="utf-8")
    first.close()
    second.close()


def test_a_new_run_starts_the_file_from_scratch(tmp_path: Path) -> None:
    """宿主启动 = 本次运行开始：上一次的日志清掉，别攒成没人看的砖头。"""
    directory = tmp_path / "logs"
    first = PluginLogStore(directory)
    first.write("demo", "上一次运行的一句话")
    first.close()
    second = PluginLogStore(directory)
    second.write("demo", "这一次的一句话")
    text = (directory / "demo.log").read_text(encoding="utf-8")
    assert text.count("\n") == 1 and "上一次" not in text
    second.close()


def test_tail_returns_the_last_lines(tmp_path: Path) -> None:
    store = PluginLogStore(tmp_path)
    for index in range(10):
        store.write("demo", f"第 {index} 行")
    assert [line.split("| ")[-1] for line in store.tail("demo", limit=3)] == ["第 7 行", "第 8 行", "第 9 行"]
    assert store.tail("没有这个插件") == []
    store.close()


def test_continued_lines_stay_under_the_same_entry(tmp_path: Path) -> None:
    store = PluginLogStore(tmp_path)
    store.write("demo", "一句\n两句\n三句")
    lines = (tmp_path / "demo.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("| 一句")
    assert all(line.startswith(" " * len(lines[0].split("| ")[0]) ) for line in lines[1:])
    store.close()


def test_server_store_rotates_and_keeps_three_backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logstore, "MAX_BYTES", 400)
    store = PluginLogStore(tmp_path)
    for index in range(60):
        store.write("demo", f"第 {index} 行" + " " * 20)
    names = sorted(path.name for path in tmp_path.glob("demo.log*"))
    assert names == ["demo.log", "demo.log.1", "demo.log.2", "demo.log.3"]
    assert (tmp_path / "demo.log").stat().st_size <= 400
    store.close()


def test_cli_store_never_rotates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命令行是一次性进程：轮转会 rename 掉常驻宿主正在写的文件，日志就分叉了。"""
    monkeypatch.setattr(logstore, "MAX_BYTES", 400)
    store = PluginLogStore(tmp_path, rotate=False)
    for index in range(60):
        store.write("demo", f"第 {index} 行" + " " * 20)
    assert [path.name for path in tmp_path.glob("demo.log*")] == ["demo.log"]
    assert len(store.tail("demo", limit=1000)) == 60
    store.close()


def test_logging_survives_a_closed_handle(tmp_path: Path) -> None:
    store = PluginLogStore(tmp_path)
    store.write("demo", "关门之前")
    store.close("demo")
    store.write("demo", "关门之后")
    text = (tmp_path / "demo.log").read_text(encoding="utf-8")
    assert "关门之前" in text and "关门之后" in text
    store.close()
