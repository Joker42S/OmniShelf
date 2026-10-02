"""音乐文件导入：用**系统选择框**挑本地音乐，读标签生成清单，再交给宿主入库。

页面上两个按钮，对应两种挑法：

* **选一个文件夹**：深度遍历整个目录，把里面所有音乐文件读一遍；
* **选多个文件**：只读选中的那几个文件。

两条路都是「读标签 → 写清单 JSON → ``import_file("music_files", 清单)``」，
和 ``omnishelf scan-music`` 走的是同一套扫描口径（认哪些后缀算音乐、跳过回收站那类
垃圾目录、没有标签时拿文件名兜底），所以插件扫出来的结果和命令行完全一致。

**插件不碰标签解析，也不碰数据库**：读文件调宿主的 ``audio`` 接口
（``ctx.host.scan_audio``），入库调 ``import`` 接口（``ctx.host.import_file``）。
清单写在 ``plugins_data/music_import/`` 下面，那里是插件唯一可写的目录。

选择框是系统级的：macOS 上用 ``osascript`` 起「访达」的挑选对话框（只用标准库的
``subprocess``，路径不经过 shell，所以没有注入问题）。别的平台上没有这个对话框，
页面会退化成手填路径——功能一样能跑。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from omnishelf.plugins.api import Plugin

#: 进度条刷新间隔（秒）。扫描是本地读文件，比联网抓取快得多，轮询可以密一点。
POLL_SECONDS = 0.5

#: 在跑的扫描超过这个时长就当作卡住，允许用户再点一次（一晚上还没扫完的基本是拔了盘）。
JOB_TIMEOUT = 3600.0

#: 系统选择框最多等多久（用户走开泡茶也不该把请求线程挂死）。
PICK_TIMEOUT = 120.0

#: 每读这么多文件往宿主日志写一行（几万个文件逐个写会把日志刷满）。
LOG_EVERY = 500

#: 清单的文件名：同一时间只跑一个任务，所以固定一个名字，最后一次的结果留着。
MANIFEST_NAME = "music_scan.json"

_PAGE = "/plugins/music_import"

# ---------------------------------------------------------------------------
# 系统选择框
# ---------------------------------------------------------------------------

#: 选文件夹：返回 POSIX 路径（带空格的路径 osascript 会原样给回来，不用转义）。
_SCRIPT_FOLDER = ['POSIX path of (choose folder with prompt "选择要扫描的音乐文件夹")']

#: 选多个文件：AppleScript 给的是 alias 列表，逐个转成 POSIX 路径、一行一个。
_SCRIPT_FILES = [
    'set chosen to (choose file with prompt "选择要导入的音乐文件" with multiple selections allowed)',
    "set output to \"\"",
    "repeat with picked in chosen",
    "    set output to output & (POSIX path of picked) & linefeed",
    "end repeat",
    "return output",
]


def supports_native_picker() -> bool:
    """这台机器能不能弹出系统选择框（目前只有 macOS 这一条路）。"""
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def _run_osascript(lines: list[str]) -> tuple[str, str]:
    """跑一段 AppleScript，返回 ``(输出, 错误)``；用户按取消时两个都是空。"""
    argv = ["osascript"]
    for line in lines:
        argv += ["-e", line]
    try:
        proc = subprocess.run(  # noqa: S603 - 固定 argv，不经过 shell
            argv, capture_output=True, text=True, timeout=PICK_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        return "", f"选择框等了 {int(PICK_TIMEOUT)} 秒没回应，已取消"
    except OSError as exc:
        return "", f"打不开系统选择框：{exc}"
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        # -128 = user canceled：这不是错误，页面什么都不显示
        if "-128" in stderr:
            return "", ""
        return "", f"系统选择框出错：{stderr or f'退出码 {proc.returncode}'}"
    return proc.stdout.strip(), ""


def choose_paths(kind: str) -> tuple[list[str], str]:
    """打开系统选择框，返回 ``(路径列表, 错误说明)``。

    ``kind`` 是 ``folder``（单选一个目录）或 ``files``（多选文件）。
    取消时返回两个空值——那不是失败，只是用户没选。
    """
    if not supports_native_picker():
        return [], "这个系统上没有 macOS 的访达选择框，请在下面的框里直接填路径"
    text, error = _run_osascript(_SCRIPT_FOLDER if kind == "folder" else _SCRIPT_FILES)
    if error or not text:
        return [], error
    lines = [line.strip() for line in text.splitlines()]
    return [line for line in lines if line], ""


def parse_targets(text: str) -> list[str]:
    """把页面上填的路径拆成列表（一行一个，也认用 ``|`` 分隔的写法）。"""
    raw = text.replace("|", "\n")
    seen: list[str] = []
    for line in raw.splitlines():
        path = line.strip()
        if path and path not in seen:
            seen.append(path)
    return seen


def existing_targets(paths: list[str]) -> list[str]:
    """只留下真的还在的东西：路径写错或盘没插，页面该直接说出来。"""
    return [path for path in paths if Path(path).exists()]


# ---------------------------------------------------------------------------
# 扫描任务（后台线程 + 进度条）
# ---------------------------------------------------------------------------
#
# 扫一个几千首歌的库要几十秒到几分钟，占着请求线程浏览器只会转圈。所以
# handle_post 登记任务 + 起线程后**立刻**返回带进度条的页面，页面每 POLL_SECONDS
# 秒替换一次进度块；``scan_audio(progress=…)`` 每读一个文件回写一次计数。

_JOBS_LOCK = threading.Lock()
_JOB: dict[str, Any] | None = None


def _start_job(*, label: str, targets: list[str]) -> dict[str, Any] | None:
    """登记一次扫描；已经有在跑的就返回 ``None``（同一插件同时只跑一个）。"""
    global _JOB
    with _JOBS_LOCK:
        current = _JOB
        if (
            current is not None
            and current["state"] == "running"
            and time.time() - current["started_at"] < JOB_TIMEOUT
        ):
            return None
        _JOB = {
            "id": uuid.uuid4().hex[:12],
            "label": label,
            "state": "running",
            "started_at": time.time(),
            "elapsed": 0.0,
            "targets": list(targets),
            "scanned": 0,
            "note": "正在打开第一个文件…",
            "notes": [],
            "report": None,
            "import_report": None,
            "error": "",
        }
        return dict(_JOB)


def _job_view() -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOB
        if job is None:
            return None
        view = dict(job)
        view["notes"] = list(job["notes"])
    if view["state"] == "running":
        view["elapsed"] = round(time.time() - view["started_at"], 1)
    return view


def _update_job(job_id: str, **values: Any) -> None:
    with _JOBS_LOCK:
        job = _JOB
        if job is None or job["id"] != job_id:
            return
        job.update(values)
        job["elapsed"] = round(time.time() - job["started_at"], 1)


def _finish_job(
    job_id: str,
    *,
    state: str = "done",
    report: dict[str, Any] | None = None,
    import_report: dict[str, Any] | None = None,
    error: str = "",
    log: Callable[[str], None] | None = None,
) -> None:
    with _JOBS_LOCK:
        job = _JOB
        if job is None or job["id"] != job_id:
            return
        job["state"] = state
        job["report"] = report
        job["import_report"] = import_report
        job["error"] = error
        job["elapsed"] = round(time.time() - job["started_at"], 1)
        note = error or (report or {}).get("summary") or "结束"
        job["note"] = note
        job["notes"].append(note)
        if log is not None:
            log(f"{job['label']}：{note}（用时 {job['elapsed']} 秒）")


def _progress(job_id: str, log: Callable[[str], None]) -> Callable[[int, Path], None]:
    """把「又读了一个文件」同时送给进度条和宿主日志（日志按 LOG_EVERY 节流）。"""

    def update(count: int, path: Path) -> None:
        message = f"已读 {count} 个文件（当前：{path.name}）"
        with _JOBS_LOCK:
            job = _JOB
            if job is not None and job["id"] == job_id:
                job["scanned"] = count
                job["note"] = message
                job["notes"].append(message)
                del job["notes"][:-40]
                job["elapsed"] = round(time.time() - job["started_at"], 1)
        if count % LOG_EVERY == 0:
            log(f"已读 {count} 个文件")

    return update


def _remember(ctx: Any, payload: dict[str, Any]) -> None:
    """把最近一次的扫描/导入结果写进 ``plugins_data/music_import/state.json``。

    进度条只在跑的时候看得见，任务状态也只在内存里；用户回头想知道
    「上次到底导了多少条」得有个地方查，所以在这里留一份。
    """
    key = "import_report" if "inserted" in payload else "scan"
    state = ctx.read_state(default={}) or {}
    state[key] = payload
    state["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ctx.write_state(state)


def _scan_worker(ctx: Any, job_id: str, targets: list[str], out_path: Path) -> None:
    """后台线程里跑的那一趟：扫描 -> 入库。任何异常都只写进任务状态。

    任务一直到**入库结束**才落成 ``done``：进度条轮询的是任务状态，要是扫完就标
    结束，页面会显示「扫描结束」却没有导入结果，入库报告晚一拍才冒出来。
    """
    log = ctx.log
    try:
        summary = ctx.host.scan_audio(
            targets, out_path=out_path, progress=_progress(job_id, log)
        )
    except Exception as exc:  # 扫描是文件系统边界：只记账，不炸线程
        _finish_job(job_id, state="error", error=f"扫描失败：{type(exc).__name__}: {exc}", log=log)
        return
    _update_job(job_id, report=summary, note="扫描完了，正在入库…")
    _remember(ctx, summary)

    try:
        imported = ctx.host.import_file("music_files", summary["manifest_path"])
    except Exception as exc:
        _finish_job(
            job_id,
            state="error",
            report=summary,
            error=f"导入失败：{type(exc).__name__}: {exc}（清单已经写好，能手动导入）",
            log=log,
        )
        return
    _remember(ctx, imported)
    _finish_job(job_id, state="done", report=summary, import_report=imported, log=log)
    log(f"导入结果：{imported.get('summary', '')}")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


def _render(ctx: Any, request: Any, *, error: str = "") -> str:
    job = _job_view()
    state = ctx.read_state(default={}) or {}
    # 正在跑的这一趟还没出结果时，别拿上一次留下的导入报告充数
    if job is not None:
        import_report = job["import_report"]
    else:
        import_report = state.get("import_report")
    return ctx.render(
        "page.html",
        job=job,
        error=error,
        import_report=import_report,
        config=ctx.config,
        suffixes=ctx.host.audio_suffixes(),
        native_picker=supports_native_picker(),
        poll_seconds=POLL_SECONDS,
        page=_PAGE,
        data_dir=str(ctx.data_dir),
        last=state.get("scan"),
    )


def _start(ctx: Any, request: Any, label: str, paths: list[str], missing: list[str]) -> str:
    """校验路径 → 登记任务 → 起后台线程 → 立刻回页面（带进度条）。"""
    targets = existing_targets(paths)
    if not targets:
        shown = "、".join(missing) if missing else "（空）"
        return _render(ctx, request, error=f"这些路径都不存在（盘没插还是打错了）：{shown}")

    job = _start_job(label=label, targets=targets)
    if job is None:
        return _render(
            ctx, request, error="已经有一趟扫描在跑了，等它的进度条走完再点。"
        )
    out_path = ctx.data_dir / MANIFEST_NAME
    threading.Thread(
        target=_scan_worker,
        args=(ctx, job["id"], targets, out_path),
        name="music-import-scan",
        daemon=True,
    ).start()
    ctx.save_config({"last_paths": targets})
    ctx.log(f"{label}：开始扫描 {len(targets)} 个目标")
    return _render(ctx, request)


#: 页面上的两个按钮 → 两种挑选方式（``folder`` 单选目录，``files`` 多选文件）
_PICKERS: dict[str, tuple[str, str]] = {
    "pick_folder": ("folder", "扫描文件夹"),
    "pick_files": ("files", "导入所选文件"),
}


def _handle_post(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    action = str(form.get("action") or "").strip()

    if action in _PICKERS:
        kind, label = _PICKERS[action]
        paths, error = choose_paths(kind)
        if error:
            return _render(ctx, request, error=error)
        if not paths:  # 用户按了取消
            return _render(ctx, request)
        return _start(ctx, request, label, paths, [])

    if action == "run":
        filled = parse_targets(str(form.get("paths") or ""))
        if not filled:
            return _render(ctx, request, error="请先用选择框挑，或者在框里填上路径。")
        missing = [path for path in filled if not Path(path).exists()]
        return _start(ctx, request, "按填的路径扫描", filled, missing)

    return _render(ctx, request, error=f"不认识的 action: {action!r}")


# ---------------------------------------------------------------------------
# 插件类
# ---------------------------------------------------------------------------


class MusicImport(Plugin):
    """本地音乐文件导入：系统选择框挑文件夹 / 挑文件，读标签后入库。"""

    def start(self) -> None:
        hint = (
            "系统选择框可用"
            if supports_native_picker()
            else "这个平台没有 macOS 选择框，页面上手填路径也能用"
        )
        self.log(f"{self.title} 已启动（{hint}）")

    def stop(self) -> None:
        self.log("已停止")

    def render_page(self, request: Any = None) -> str:
        return _render(self.ctx, request)

    def handle_post(self, request: Any = None, form: Mapping[str, Any] | None = None) -> str:
        return _handle_post(self.ctx, request, form or {})
