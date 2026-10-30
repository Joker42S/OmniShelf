"""插件页面：列表、插件自己的页面、热重载、一键快速更新。

* ``GET  /plugins``                        插件列表（默认网格视图，``?view=detail`` 看详情）
* ``POST /plugins/reload``                 重载（全部，或表单里指定某个插件）
* ``POST /plugins/quick-update``           对勾选的插件跑一次增量更新 + 导入（后台线程）
* ``GET  /plugins/quick-update/status``    快速更新的进度/结果（HTMX 轮询）
* ``GET  /plugins/{id}``                   插件自己的页面（调用它的 ``render_page``）
* ``POST /plugins/{id}``                   插件页面上的表单提交（``handle_post``）

每次进来都会先做一次「指纹变化检查」，所以**改完插件代码刷新页面就生效**。
插件跑在宿主进程里、能读能写，属于**受信任代码**：装插件等于装程序，别装来路不明的。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ...plugins.manager import STATE_ERROR, PluginError, PluginManager
from ..deps import plugin_manager_of, templates_of

router = APIRouter()

#: 快速更新状态区多久轮询一次（秒）
QUICK_POLL_SECONDS = 2
QUICK_VIEWS = ("grid", "detail")


@dataclass
class _QuickItem:
    id: str
    title: str
    state: str = "pending"  # pending / running / done / incomplete / failed
    summary: str = ""
    details: str = ""


@dataclass
class _QuickJob:
    id: str
    items: list[_QuickItem]
    running: bool = True
    started_at: str = ""
    finished_at: str | None = None
    error: str | None = None


@dataclass
class _QuickState:
    """挂在 ``app.state`` 上的快速更新状态（一个进程同时只跑一批）。"""

    lock: threading.Lock = field(default_factory=threading.Lock)
    job: _QuickJob | None = None


def _clock() -> str:
    return time.strftime("%H:%M:%S")


def _quick_state(request: Request) -> _QuickState:
    state: _QuickState | None = getattr(request.app.state, "quick_update_state", None)
    if state is None:
        state = _QuickState()
        request.app.state.quick_update_state = state
    return state


def _job_snapshot(state: _QuickState) -> dict[str, Any] | None:
    with state.lock:
        job = state.job
        if job is None:
            return None
        return {
            "id": job.id,
            "running": job.running,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "error": job.error,
            "items": [asdict(item) for item in job.items],
        }


def _base_context(request: Request, manager: PluginManager) -> dict[str, Any]:
    view = (request.query_params.get("view") or "grid").strip().lower()
    if view not in QUICK_VIEWS:
        view = "grid"
    return {
        "plugins": manager.list_plugins(),
        "interfaces": list(manager.interfaces()),
        "plugins_dir": str(manager.plugins_dir),
        "plugins_data_dir": str(manager.plugins_data_dir),
        "view": view,
        "job": _job_snapshot(_quick_state(request)),
        "poll_seconds": QUICK_POLL_SECONDS,
        "error": None,
        "reloaded": request.query_params.getlist("reloaded"),
    }


@router.get("/plugins", response_class=HTMLResponse)
def plugins_page(
    request: Request,
    manager: PluginManager = Depends(plugin_manager_of),
) -> HTMLResponse:
    """插件列表（默认网格；``?view=detail`` 是原来的详情视图）。"""
    changed = manager.reload_changed()
    templates = templates_of(request)
    context = _base_context(request, manager)
    if changed and not context["reloaded"]:
        context["reloaded"] = changed
    return templates.TemplateResponse(request=request, name="plugins.html", context=context)


@router.post("/plugins/reload")
async def reload_plugins(
    request: Request,
    manager: PluginManager = Depends(plugin_manager_of),
) -> Response:
    """重载插件（表单里给 ``plugin_id`` 就只重载它）。"""
    form = await request.form()
    plugin_id = str(form.get("plugin_id") or "").strip()
    error: Optional[str] = None
    try:
        if plugin_id:
            manager.load(plugin_id)
            reloaded = [plugin_id]
        else:
            manager.discover()
            reloaded = manager.reload()
    except PluginError as exc:
        reloaded, error = [], str(exc)
    if error:
        templates = templates_of(request)
        context = _base_context(request, manager)
        context["error"] = error
        return templates.TemplateResponse(
            request=request, name="plugins.html", context=context, status_code=400
        )
    from urllib.parse import urlencode

    target = "/plugins"
    if reloaded:
        target += "?" + urlencode([("reloaded", item) for item in reloaded])
    return RedirectResponse(url=target, status_code=303)


def _collect_quick_options(
    manager: PluginManager, selected: list[str], form: Any
) -> dict[str, dict[str, str]]:
    """从表单里挑出 ``quick_param:<插件>:<参数>``，只保留插件声明过的参数。

    没填的空值不传（让插件自己决定用保存的配置还是报缺参数）。
    """
    wanted = {plugin_id: {param.name for param in manager.status(plugin_id).quick_params}
              for plugin_id in selected}
    options: dict[str, dict[str, str]] = {}
    for key, value in form.multi_items():
        text_key = str(key)
        if not text_key.startswith("quick_param:"):
            continue
        plugin_id, _, name = text_key[len("quick_param:"):].partition(":")
        if plugin_id not in wanted or name not in wanted[plugin_id]:
            continue
        text = str(value)
        if text.strip():
            options.setdefault(plugin_id, {})[name] = text
    return options


def _run_quick_update(
    manager: PluginManager,
    state: _QuickState,
    job_id: str,
    items: list[_QuickItem],
    options_by_plugin: dict[str, dict[str, str]],
) -> None:
    """后台线程：按顺序跑选中的插件，一个失败不影响后面的。"""
    try:
        for item in items:
            with state.lock:
                if state.job is None or state.job.id != job_id:
                    return  # 被更新的一批顶掉了
                item.state = "running"
            try:
                result = manager.quick_update(item.id, options_by_plugin.get(item.id))
                with state.lock:
                    item.state = "done" if result.ok else "incomplete"
                    item.summary = result.summary
                    item.details = result.details
            except Exception as exc:  # 插件出错只算它自己失败
                with state.lock:
                    item.state = "failed"
                    item.summary = str(exc) or type(exc).__name__
    finally:
        with state.lock:
            if state.job is not None and state.job.id == job_id:
                for item in state.job.items:
                    if item.state in {"pending", "running"}:
                        item.state = "failed"
                        item.summary = item.summary or "这次没跑到"
                state.job.running = False
                state.job.finished_at = _clock()


def _render_quick_status(request: Request) -> HTMLResponse:
    state = _quick_state(request)
    return templates_of(request).TemplateResponse(
        request=request,
        name="_plugin_quick_status.html",
        context={"job": _job_snapshot(state), "poll_seconds": QUICK_POLL_SECONDS},
    )


@router.post("/plugins/quick-update")
async def quick_update(
    request: Request,
    manager: PluginManager = Depends(plugin_manager_of),
) -> Response:
    """对表单里勾选的插件跑一次快速增量更新（后台线程 + 状态轮询）。"""
    form = await request.form()
    selected = [
        str(value).strip()
        for value in form.getlist("quick_plugin")
        if str(value).strip()
    ]
    selected = list(dict.fromkeys(selected))
    options_by_plugin = _collect_quick_options(manager, selected, form)
    state = _quick_state(request)

    job_id: str | None = None
    items: list[_QuickItem] = []
    with state.lock:
        busy = state.job is not None and state.job.running
        if not busy:
            now = _clock()
            if selected:
                titles = {status.id: status.title for status in manager.list_plugins()}
                items = [
                    _QuickItem(id=plugin_id, title=titles.get(plugin_id, plugin_id))
                    for plugin_id in selected
                ]
                job = _QuickJob(id=uuid.uuid4().hex[:8], items=items, started_at=now)
                job_id = job.id
            else:
                job = _QuickJob(
                    id=uuid.uuid4().hex[:8],
                    items=[],
                    running=False,
                    started_at=now,
                    finished_at=now,
                    error="没有选中任何插件：先在上面勾上要更新的插件。",
                )
            state.job = job

    if job_id is not None:
        # 各插件自己的参数只活在这个线程的栈上：不写状态文件、不进日志、不回显。
        threading.Thread(
            target=_run_quick_update,
            args=(manager, state, job_id, items, options_by_plugin),
            name=f"quick-update-{job_id}",
            daemon=True,
        ).start()

    if request.headers.get("HX-Request"):
        return _render_quick_status(request)
    return RedirectResponse(url="/plugins", status_code=303)


@router.get("/plugins/quick-update/status", response_class=HTMLResponse)
def quick_update_status(request: Request) -> HTMLResponse:
    """只要快速更新那一块（HTMX 轮询，页面不整屏闪）。"""
    return _render_quick_status(request)


@router.get("/plugins/{plugin_id}", response_class=HTMLResponse)
def plugin_page(
    request: Request,
    plugin_id: str,
    manager: PluginManager = Depends(plugin_manager_of),
) -> HTMLResponse:
    """插件自己的页面。"""
    manager.reload_changed()
    templates = templates_of(request)
    status = manager.status(plugin_id)
    if status.state != "loaded" and status.state != STATE_ERROR:
        return templates.TemplateResponse(
            request=request,
            name="plugin.html",
            context={"status": status, "body": None, "error": f"没有这个插件（或没加载）: {plugin_id}"},
            status_code=404,
        )
    try:
        body = manager.page(plugin_id, request)
    except PluginError as exc:
        return templates.TemplateResponse(
            request=request,
            name="plugin.html",
            context={"status": status, "body": None, "error": str(exc)},
            status_code=500,
        )
    return templates.TemplateResponse(
        request=request,
        name="plugin.html",
        context={"status": status, "body": body, "error": None},
    )


@router.post("/plugins/{plugin_id}")
async def plugin_post(
    request: Request,
    plugin_id: str,
    manager: PluginManager = Depends(plugin_manager_of),
) -> Response:
    """插件页面里的表单提交。"""
    templates = templates_of(request)
    status = manager.status(plugin_id)
    form = await request.form()
    try:
        body = manager.post(plugin_id, request, form)
    except PluginError as exc:
        return templates.TemplateResponse(
            request=request,
            name="plugin.html",
            context={"status": status, "body": None, "error": str(exc)},
            status_code=400,
        )
    if body is None:
        return RedirectResponse(url=f"/plugins/{plugin_id}", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="plugin.html",
        context={"status": status, "body": body, "error": None},
    )
