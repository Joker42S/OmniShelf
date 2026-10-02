"""插件页面：列表、插件自己的页面、热重载。

* ``GET  /plugins``               插件列表（装了什么、什么状态、打开页面）
* ``POST /plugins/reload``        重载（全部，或表单里指定某个插件）
* ``GET  /plugins/{id}``          插件自己的页面（调用它的 ``render_page``）
* ``POST /plugins/{id}``          插件页面上的表单提交（``handle_post``）

每次进来都会先做一次「指纹变化检查」，所以**改完插件代码刷新页面就生效**。
插件跑在宿主进程里、能读能写，属于**受信任代码**：装插件等于装程序，别装来路不明的。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ...plugins.manager import STATE_ERROR, PluginError, PluginManager
from ..deps import plugin_manager_of, templates_of

router = APIRouter()


def _base_context(request: Request, manager: PluginManager) -> dict[str, Any]:
    return {
        "plugins": manager.list_plugins(),
        "interfaces": list(manager.interfaces()),
        "plugins_dir": str(manager.plugins_dir),
        "plugins_data_dir": str(manager.plugins_data_dir),
        "error": None,
        "reloaded": request.query_params.getlist("reloaded"),
    }


@router.get("/plugins", response_class=HTMLResponse)
def plugins_page(
    request: Request,
    manager: PluginManager = Depends(plugin_manager_of),
) -> HTMLResponse:
    """插件列表。"""
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
