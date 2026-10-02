"""运行日志页：宿主统一收集的插件日志，可以实时盯着看。

* ``GET /logs``       整页（选插件、选级别、选尾部行数、自动刷新开关）
* ``GET /logs/tail``  只要日志那一块（HTMX 定时轮询，页面不整屏闪）

日志落在 ``data/logs/plugins/<插件 id>.log``（见
:class:`omnishelf.plugins.logstore.PluginLogStore`）：插件自己说的行和宿主记的
加载/卸载/出错**按时间排在同一个文件里**，这里读的就是它。

两件事这个页面**不**负责，别在这儿找：

* 历史流水 —— 文件是「本次运行」的，宿主启动即清空；抓取到底入库了什么看
  上传页的导入日志（``import_logs``）；
* 凭据 —— 打码是插件在 ``ctx.log()`` 之前自己做的事，宿主不猜口令长什么样。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...plugins.logstore import DEFAULT_TAIL, PluginLogStore
from ...plugins.manager import PluginManager
from ..deps import plugin_manager_of, templates_of

router = APIRouter()

LEVEL_ALL = "all"
#: 下拉框里的级别；比 logging 那套少，页面够用就行。
LEVELS = (LEVEL_ALL, "info", "warning", "error")
TAIL_CHOICES = (100, DEFAULT_TAIL, 500, 2000)
POLL_SECONDS = 2
MAX_LIMIT = 5000

#: 一行日志的头：``时间 | 级别 | 谁说的 | 正文``。
_LINE_HEAD = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (\S+?)\s*\| (.*?) \| (.*)$"
)


def _context(
    request: Request, manager: PluginManager
) -> tuple[dict[str, Any], Optional[PluginLogStore]]:
    store = manager.log_store
    try:
        limit = int(request.query_params.get("limit") or DEFAULT_TAIL)
    except ValueError:
        limit = DEFAULT_TAIL
    limit = limit if limit in TAIL_CHOICES else DEFAULT_TAIL
    level = (request.query_params.get("level") or LEVEL_ALL).lower()
    if level not in LEVELS:
        level = LEVEL_ALL
    plugin = (request.query_params.get("plugin") or LEVEL_ALL).strip()
    follow = request.query_params.get("follow", "1") not in {"0", "off", "false", ""}

    plugins = [
        {"id": status.id, "title": status.title} for status in manager.list_plugins()
    ]
    logged = store.logged_plugins() if store else []
    known = {item["id"] for item in plugins}
    plugins.extend({"id": pid, "title": pid} for pid in logged if pid not in known)

    return (
        {
            "plugins": plugins,
            "plugin": plugin,
            "levels": LEVELS,
            "level": level,
            "limits": TAIL_CHOICES,
            "limit": limit,
            "follow": follow,
            "poll_seconds": POLL_SECONDS,
            "logs_dir": str(store.logs_dir) if store else None,
            "enabled": store is not None,
        },
        store,
    )


def _entries(store: PluginLogStore, plugin: str, *, level: str, limit: int) -> list[dict[str, str]]:
    """把日志尾部读成「一条一条」，多行消息算一条（续行跟着它上面那条）。"""
    targets = (
        store.logged_plugins()
        if plugin == LEVEL_ALL  # 「全部」这个取值就是「所有有日志的插件」
        else [plugin]
    )
    collected: list[dict[str, Any]] = []
    for plugin_id in targets:
        current: Optional[dict[str, Any]] = None
        for raw in store.tail(plugin_id, limit=limit):
            head = _LINE_HEAD.match(raw)
            if head:
                stamp, name, origin, _message = head.groups()
                current = {
                    "time": stamp,
                    "level": name.lower(),
                    "origin": origin,
                    "lines": [raw],
                }
                collected.append(current)
            elif current is not None:
                current["lines"].append(raw)
            else:  # 不是这个格式的行（插件写到一半被截断之类），原样挂着
                current = {"time": "", "level": "", "origin": "", "lines": [raw]}
                collected.append(current)

    if level != LEVEL_ALL:
        collected = [entry for entry in collected if entry["level"] == level]
    collected.sort(key=lambda entry: entry["time"])
    return [
        {
            "time": entry["time"],
            "level": entry["level"],
            "origin": entry["origin"],
            "text": "\n".join(entry["lines"]),
            "kind": entry["level"] if entry["level"] in {"info", "warning", "error"} else "other",
        }
        for entry in collected[-max(1, min(limit, MAX_LIMIT)):]
    ]


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    manager: PluginManager = Depends(plugin_manager_of),
) -> HTMLResponse:
    """运行日志页。"""
    context, store = _context(request, manager)
    context["entries"] = (
        _entries(store, context["plugin"], level=context["level"], limit=context["limit"])
        if store
        else []
    )
    context["query"] = request.query_params
    return templates_of(request).TemplateResponse(
        request=request, name="logs.html", context=context
    )


@router.get("/logs/tail", response_class=HTMLResponse)
def logs_tail(
    request: Request,
    manager: PluginManager = Depends(plugin_manager_of),
) -> HTMLResponse:
    """只有日志那一块，给页面轮询用（整页太重，两秒刷一次会闪）。"""
    context, store = _context(request, manager)
    context["entries"] = (
        _entries(store, context["plugin"], level=context["level"], limit=context["limit"])
        if store
        else []
    )
    return templates_of(request).TemplateResponse(
        request=request, name="_log_lines.html", context=context
    )
