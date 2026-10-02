"""本地音乐导入插件（plugins/music_import）：系统选择框、后台扫描任务、扫完入库。

测试**不弹真选择框**（把 ``osascript`` 那一段换掉），也不碰真实音乐目录——
音频文件全部现场合成（``tests.audio_synth``）。
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from omnishelf.config import Settings, repo_root
from omnishelf.db import build_engine
from omnishelf.plugins.manager import STATE_LOADED, LoadedPlugin, PluginManager
from tests.audio_synth import CASES, flac

PLUGIN_ID = "music_import"
REPO_PLUGINS = repo_root() / "plugins"


@pytest.fixture()
def manager(settings: Settings, session: Session) -> PluginManager:
    engine = build_engine(replace(settings))
    return PluginManager(
        plugins_dir=REPO_PLUGINS,
        plugins_data_dir=settings.plugins_data_dir,
        engine=engine,
    )


@pytest.fixture()
def loaded_plugin(manager: PluginManager, monkeypatch: pytest.MonkeyPatch) -> LoadedPlugin:
    """加载仓库里的真插件，并把任务状态清干净。

    ``_JOB`` 是模块全局，一个 pytest 进程里几个测试共用；不清的话上一个测试
    跑完的任务会显示在下一个测试的进度条里。
    """
    manager.load_all()
    status = manager.status(PLUGIN_ID)
    assert status.state == STATE_LOADED, status.error
    plugin = manager.get(PLUGIN_ID)
    assert plugin is not None
    monkeypatch.setattr(plugin.module, "_JOB", None)
    return plugin


@pytest.fixture()
def module(loaded_plugin: LoadedPlugin) -> Any:
    return loaded_plugin.module


@pytest.fixture()
def plugin(loaded_plugin: LoadedPlugin) -> Any:
    return loaded_plugin.instance


@pytest.fixture()
def host(loaded_plugin: LoadedPlugin) -> Any:
    return loaded_plugin.context.host


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    """一棵合成音乐目录：带标签的 mp3 + 没标签的 flac + 回收站副本 + 杂文件。"""
    root = tmp_path / "Music"
    album = root / "Synthetic Album"
    album.mkdir(parents=True)
    (album / "01 - Tagged.mp3").write_bytes(CASES["a_id3v23.mp3"]())
    # 只有填充块的 FLAC：容器读得动、一条标签都没有 -> 标题退回文件名
    (album / "02 - Bare.flac").write_bytes(flac([(1, bytes(8), True)]))
    (album / "cover.txt").write_text("not audio", encoding="utf-8")
    recycle = root / "$RECYCLE.BIN" / "Synthetic Album"
    recycle.mkdir(parents=True)
    (recycle / "01 - Tagged.mp3").write_bytes(CASES["a_id3v23.mp3"]())
    return root


def wait_for_job(module: Any, *, timeout: float = 60.0) -> dict[str, Any]:
    """等后台那一趟跑完，返回已经结束的任务快照。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = module._job_view()
        if view is not None and view["state"] != "running":
            return view
        time.sleep(0.02)
    raise AssertionError("扫描任务一直没结束")


def run_once(manager: PluginManager, module: Any, form: dict[str, str]) -> dict[str, Any]:
    """点一次按钮 → 等后台跑完 → 返回结束后的任务快照。"""
    manager.post(PLUGIN_ID, None, form)
    return wait_for_job(module)


def post_once(manager: PluginManager, form: dict[str, str]) -> str:
    """点一次按钮，拿回那一屏 HTML（插件返回 None 在这里算失败）。"""
    html = manager.post(PLUGIN_ID, None, form)
    assert html is not None
    return html


# ------------------------------------------------------------------- 加载 / 页面


def test_music_import_page_offers_both_pickers(plugin: Any, module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "supports_native_picker", lambda: True)
    html = plugin.render_page(None)
    assert 'name="action" value="pick_folder"' in html
    assert 'name="action" value="pick_files"' in html
    assert 'name="action" value="run"' in html
    assert ".mp3" in html and ".flac" in html  # 后缀名单来自宿主
    assert "深度扫描" in html


def test_page_without_native_picker_keeps_manual_paths(
    plugin: Any, module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """弹不出选择框的平台上不该画那两个按钮，但要留手填路径这条通道。"""
    monkeypatch.setattr(module, "supports_native_picker", lambda: False)
    html = plugin.render_page(None)
    assert 'value="pick_folder"' not in html
    assert "选择框" in html
    assert 'name="paths"' in html


def test_progress_block_polls_itself(plugin: Any, module: Any) -> None:
    """跑起来的那一栏自己每 POLL_SECONDS 秒替换一次（HTMX 轮询页面）。"""
    assert module._start_job(label="扫描文件夹", targets=["/music"]) is not None
    html = plugin.render_page(None)
    assert 'id="mi-scan-status"' in html
    assert 'hx-trigger="every 0.5s"' in html
    assert 'hx-select="#mi-scan-status"' in html


def test_finished_block_stops_polling(plugin: Any, module: Any) -> None:
    job = module._start_job(label="扫描文件夹", targets=["/music"])
    assert job is not None
    module._finish_job(job["id"], state="done", report={"summary": "扫了 2 个音频文件，读到 2 条"})
    html = plugin.render_page(None)
    assert "hx-trigger" not in html
    assert "扫了 2 个音频文件，读到 2 条" in html


def test_running_job_does_not_show_the_previous_import_report(
    plugin: Any, module: Any, loaded_plugin: LoadedPlugin
) -> None:
    """新的一趟还在跑时，别把上一次留下的导入报告挂在进度条下面充数。"""
    loaded_plugin.context.write_state({"import_report": {"inserted": 99, "total": 99, "errors": []}})
    assert module._start_job(label="扫描文件夹", targets=["/music"]) is not None
    assert "新增 99" not in plugin.render_page(None)


def test_import_report_from_state_shows_without_a_job(
    plugin: Any, loaded_plugin: LoadedPlugin
) -> None:
    """进程刚重启（内存里没有任务）时，页面上还能看到上次导了多少条。"""
    loaded_plugin.context.write_state(
        {"import_report": {"inserted": 7, "total": 7, "errors": []}, "saved_at": "x"}
    )
    assert "新增 7" in plugin.render_page(None)


# ------------------------------------------------------------------- 选择框


def test_parse_targets_accepts_both_separators(module: Any) -> None:
    assert module.parse_targets("  /a/b \n/c|/a/b\n\n") == ["/a/b", "/c"]


def test_existing_targets_drops_missing(module: Any, tmp_path: Path) -> None:
    assert module.existing_targets([str(tmp_path), "/没有这个东西"]) == [str(tmp_path)]


def test_choose_paths_without_native_picker(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "supports_native_picker", lambda: False)
    paths, error = module.choose_paths("folder")
    assert paths == []
    assert "填" in error  # 直接告诉用户改用路径框


def test_choose_paths_folder_reads_osascript_result(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "supports_native_picker", lambda: True)
    monkeypatch.setattr(module, "_run_osascript", lambda lines: ("/Music/我的专辑\n", ""))
    assert module.choose_paths("folder") == (["/Music/我的专辑"], "")


def test_choose_paths_files_splits_every_line(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "supports_native_picker", lambda: True)
    seen: list[list[str]] = []

    def fake_run(lines: list[str]) -> tuple[str, str]:
        seen.append(lines)
        return "/a/1.mp3\n/a/2.flac\n", ""

    monkeypatch.setattr(module, "_run_osascript", fake_run)
    assert module.choose_paths("files") == (["/a/1.mp3", "/a/2.flac"], "")
    assert any("multiple selections" in line for line in seen[0])  # 多选是文件框的开关
    assert any("choose folder" in line for line in module._SCRIPT_FOLDER)


def test_choose_paths_cancel_is_not_an_error(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按「取消」返回 -128：页面不该报错，也不该起任务。"""
    monkeypatch.setattr(module, "supports_native_picker", lambda: True)
    monkeypatch.setattr(module, "_run_osascript", lambda lines: ("", ""))
    assert module.choose_paths("folder") == ([], "")


def test_choose_paths_reports_osascript_failure(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "supports_native_picker", lambda: True)
    monkeypatch.setattr(module, "_run_osascript", lambda lines: ("", "系统选择框出错：退出码 1"))
    paths, error = module.choose_paths("folder")
    assert paths == [] and "退出码" in error


# ------------------------------------------------------------------- 扫描 + 入库


def test_run_action_scans_and_imports(
    manager: PluginManager, module: Any, host: Any, library: Path
) -> None:
    view = run_once(manager, module, {"action": "run", "paths": str(library)})
    assert view["state"] == "done", view["error"]
    report = view["report"]
    assert (report["scanned"], report["item_count"], report["failed"]) == (2, 2, 0)
    assert report["tagless"] == 1  # 那个 FLAC 一条标签都没有
    assert report["manifest_path"].endswith("music_scan.json")
    assert Path(report["manifest_path"]).is_file()

    imported = view["import_report"]
    assert imported["inserted"] == 2 and imported["source"] == "music_files"
    assert host.count_items(kind="music") == 2
    assert {item["title"] for item in host.list_items(kind="music")} == {"合成标题", "Bare"}
    # 有标签读标签，没标签拿文件名兜底（去掉开头的曲目号）


def test_scan_runs_in_the_background(
    manager: PluginManager,
    module: Any,
    host: Any,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点下去就先回带进度条的页面，不用等扫完才回。"""
    real_scan = host.scan_audio

    def slow(targets: Any, **kwargs: Any) -> Any:
        time.sleep(0.3)
        return real_scan(targets, **kwargs)

    monkeypatch.setattr(host, "scan_audio", slow)
    html = post_once(manager, {"action": "run", "paths": str(library)})
    assert "正在读文件" in html
    assert "每 0.5 秒" in html
    view = wait_for_job(module)
    assert view["scanned"] == 2
    assert any("已读" in note for note in view["notes"])
    assert view["state"] == "done"


def test_pick_folder_starts_a_scan(
    manager: PluginManager, module: Any, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "choose_paths", lambda kind: ([str(library)], ""))
    view = run_once(manager, module, {"action": "pick_folder"})
    assert view["label"] == "扫描文件夹"
    assert view["state"] == "done" and view["import_report"]["inserted"] == 2


def test_pick_files_only_reads_what_was_picked(
    manager: PluginManager,
    module: Any,
    host: Any,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picked = [str(library / "Synthetic Album" / "01 - Tagged.mp3")]
    monkeypatch.setattr(module, "choose_paths", lambda kind: (picked, ""))
    view = run_once(manager, module, {"action": "pick_files"})
    assert view["label"] == "导入所选文件"
    assert view["report"]["item_count"] == 1
    assert view["import_report"]["inserted"] == 1
    assert host.count_items(kind="music") == 1  # 回收站那份副本没被扫进来


def test_cancelled_picker_starts_nothing(
    manager: PluginManager, module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "choose_paths", lambda kind: ([], ""))
    html = post_once(manager, {"action": "pick_folder"})
    assert module._job_view() is None
    assert "没能执行" not in html


def test_picker_error_shows_up(
    manager: PluginManager, module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "choose_paths", lambda kind: ([], "打不开系统选择框"))
    html = post_once(manager, {"action": "pick_files"})
    assert "打不开系统选择框" in html
    assert module._job_view() is None


def test_missing_paths_are_reported(
    manager: PluginManager, module: Any, tmp_path: Path
) -> None:
    """路径填错/盘没插：页面直接说清楚，不起任务。"""
    missing = str(tmp_path / "没插这个盘")
    html = post_once(manager, {"action": "run", "paths": missing})
    assert "都不存在" in html
    assert module._job_view() is None


def test_blank_paths_are_reported(manager: PluginManager, module: Any) -> None:
    html = post_once(manager, {"action": "run", "paths": "  "})
    assert "请先用选择框挑" in html


def test_unknown_action_is_rejected(manager: PluginManager, module: Any) -> None:
    html = post_once(manager, {"action": "drop_table"})
    assert "不认识的 action" in html


def test_second_click_while_running_is_refused(
    plugin: Any, module: Any, tmp_path: Path
) -> None:
    """同一时间只跑一趟：进度条还没走完再点，页面直接说清楚。"""
    assert module._start_job(label="扫描文件夹", targets=[str(tmp_path)]) is not None
    html = plugin.handle_post(None, {"action": "run", "paths": str(tmp_path)})
    assert "已经有一趟扫描在跑" in html


def test_scan_failure_keeps_the_page_useful(
    manager: PluginManager,
    plugin: Any,
    module: Any,
    host: Any,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫描是文件系统边界：炸了只写进任务状态，页面还得说清楚为什么。"""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("盘没插")

    monkeypatch.setattr(host, "scan_audio", boom)
    view = run_once(manager, module, {"action": "run", "paths": str(library)})
    assert view["state"] == "error"
    assert "扫描失败" in view["error"] and "盘没插" in view["error"]
    assert "扫描失败" in plugin.render_page(None)


def test_import_failure_keeps_the_manifest(
    manager: PluginManager,
    module: Any,
    host: Any,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入库炸了不该把扫好的清单一起丢掉：页面上要给出清单路径，能手动导入。"""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("库锁住了")

    monkeypatch.setattr(host, "import_file", boom)
    view = run_once(manager, module, {"action": "run", "paths": str(library)})
    assert view["state"] == "error" and "导入失败" in view["error"]
    assert Path(view["report"]["manifest_path"]).is_file()


# ------------------------------------------------------------------- 判重 / 留底


def test_importing_the_same_folder_twice_changes_nothing(
    manager: PluginManager, module: Any, host: Any, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一批文件第二次应当全是「未变化」，不多出一条。"""
    assert run_once(manager, module, {"action": "run", "paths": str(library)})["import_report"]["inserted"] == 2
    monkeypatch.setattr(module, "_JOB", None)
    second = run_once(manager, module, {"action": "run", "paths": str(library)})["import_report"]
    assert (second["inserted"], second["updated"], second["unchanged"]) == (0, 0, 2)
    assert host.count_items(kind="music") == 2


def test_results_are_remembered(
    manager: PluginManager, plugin: Any, module: Any, loaded_plugin: LoadedPlugin, library: Path
) -> None:
    run_once(manager, module, {"action": "run", "paths": str(library)})
    state = loaded_plugin.context.read_state(default={})
    assert state["scan"]["item_count"] == 2
    assert state["import_report"]["inserted"] == 2
    assert state["saved_at"]
    assert "上次扫描" in plugin.render_page(None)


def test_last_paths_land_in_config(
    manager: PluginManager, plugin: Any, module: Any, loaded_plugin: LoadedPlugin, library: Path
) -> None:
    """选过的路径留在配置里：下次打开页面框里就有，不用重新打一遍。"""
    run_once(manager, module, {"action": "run", "paths": str(library)})
    assert loaded_plugin.context.config["last_paths"] == [str(library)]
    assert str(library) in plugin.render_page(None)


# ------------------------------------------------------------------- 规范


def test_music_import_requires_audio_and_import(manager: PluginManager) -> None:
    manager.load_all()
    status = manager.status(PLUGIN_ID)
    assert status.state == STATE_LOADED, status.error
    assert status.title == "本地音乐导入"
    assert status.has_page
    assert status.requires == ("audio", "import")
    assert status.logs and "已启动" in status.logs[0]


def test_plugin_only_uses_the_sdk() -> None:
    """规范：插件只能用标准库 + ``omnishelf.plugins.api``，不能碰宿主内部模块。"""
    source = (REPO_PLUGINS / "music_import" / "plugin.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.M)
    assert imports, "总得 import 点什么"
    allowed_std = {
        "__future__",
        "pathlib",
        "shutil",
        "subprocess",
        "sys",
        "threading",
        "time",
        "typing",
        "uuid",
    }
    for name in imports:
        if name == "omnishelf.plugins.api":
            continue
        assert not name.startswith("omnishelf"), f"插件不该 import 宿主内部模块: {name}"
        assert name.split(".")[0] in allowed_std, name
    assert "from omnishelf.plugins.api import Plugin" in source


def test_no_real_paths_in_the_plugin() -> None:
    """插件里不该写死任何人的音乐目录或真实资料库文件名（盘由用户自己选）。"""
    root = REPO_PLUGINS / "music_import"
    files = list(root.rglob("*.py")) + list(root.rglob("*.html")) + list(root.rglob("*.toml"))
    files.append(root / "README.md")
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text, path
        assert "资料库.xml" not in text, path


def test_metadata_and_files_present() -> None:
    root = REPO_PLUGINS / "music_import"
    text = (root / "metadata.toml").read_text(encoding="utf-8")
    for key in ("id", "title", "version", "description", "entry", "requires"):
        assert re.search(rf"^{key}\s*=", text, re.M), key
    assert (root / "templates" / "page.html").is_file()
    assert (root / "README.md").is_file()
