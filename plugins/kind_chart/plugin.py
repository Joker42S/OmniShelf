"""分类统计图：把每个 kind 的条目数量画成条状图。

这个插件同时是**插件模板**：想看一个新插件该怎么写，照着这个目录抄就行
（`metadata.toml` + `plugin.py` + `templates/`）。它演示了：

* 继承 ``Plugin``（来自 ``omnishelf.plugins.api``，**插件唯一允许 import 的宿主模块**）；
* ``start()`` / ``stop()`` —— 基类的两个抽象方法，必须实现；
* ``self.host`` —— 宿主提供的**只读数据查询接口**（这里用了 ``stats()``/``kinds()``/``statuses()``）；
* ``self.config`` / ``self.save_config()`` —— 配置存在 ``plugins_data/kind_chart/config.toml``；
* ``self.write_state()`` —— 持久化自己的数据（这里是最近一次渲染时间）；
* ``self.render("page.html", ...)`` —— 用插件自己 ``templates/`` 里的模板渲染页面；
* ``render_page()`` / ``handle_post()`` —— 提供页面、处理页面上的表单；
* ``@cmd("top")`` —— 把功能也挂到命令行：``omnishelf plugin kind_chart top --limit 5``。

约定：插件只用标准库 + 这个 SDK，**不要 import 宿主的其它模块**
（``omnishelf.repository`` 之类都是内部实现，宿主重构会把你弄坏）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from omnishelf.plugins.api import Plugin, cmd

#: 默认配置（用户没配置过就用这个）
DEFAULTS: dict[str, Any] = {
    "bar_color": "#3b82f6",
    "show_status": True,
    "max_bars": 12,
}


def effective_config(ctx: Any) -> dict[str, Any]:
    """默认值 + 用户配置（配置文件里没有的键用默认）。"""
    config = dict(DEFAULTS)
    for key, value in (ctx.config or {}).items():
        config[key] = value
    try:
        config["max_bars"] = max(1, min(int(config["max_bars"]), 50))
    except (TypeError, ValueError):
        config["max_bars"] = DEFAULTS["max_bars"]
    if not str(config.get("bar_color", "")).startswith("#"):
        config["bar_color"] = DEFAULTS["bar_color"]
    return config


def build_rows(pairs: list[tuple[str, int]], *, limit: int) -> list[dict[str, Any]]:
    """把 (标签, 数量) 变成画图需要的数据（宽度按最大值归一）。"""
    rows = [(label, int(count)) for label, count in pairs if count]
    rows.sort(key=lambda item: (-item[1], item[0]))
    total = sum(count for _, count in rows) or 1
    biggest = max((count for _, count in rows), default=1) or 1
    return [
        {
            "label": label,
            "count": count,
            "percent": round(count * 100 / total, 1),
            "width": round(count * 100 / biggest, 2),
        }
        for label, count in rows[:limit]
    ]


def _render(ctx: Any, request: Any, **overrides: Any) -> str:
    """插件页面：两个条状图（kind 分布 + 状态分布）。"""
    stats = ctx.host.stats()
    config = effective_config(ctx)
    kind_labels = {item["name"]: item["label"] for item in ctx.host.kinds()}
    status_labels = {item["name"]: item["label"] for item in ctx.host.statuses()}

    kind_rows = build_rows(
        [(kind_labels.get(name, name), count) for name, count in stats["by_kind"].items()],
        limit=int(config["max_bars"]),
    )
    status_rows = (
        build_rows(
            [(status_labels.get(name, name), count) for name, count in stats["by_status"].items()],
            limit=int(config["max_bars"]),
        )
        if config["show_status"]
        else []
    )

    ctx.write_state(
        {
            "last_render": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "total": stats["total"],
        }
    )
    return ctx.render(
        "page.html",
        stats=stats,
        kind_rows=kind_rows,
        status_rows=status_rows,
        config=config,
        saved=bool((request.query_params.get("saved") if request is not None else None)),
    )


def _handle_post(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    """保存配置（页面上的表单）。"""
    values: dict[str, Any] = {
        "bar_color": str(form.get("bar_color") or DEFAULTS["bar_color"]).strip(),
        "show_status": form.get("show_status") is not None,  # 复选框：没勾就是 False
        "max_bars": form.get("max_bars") or DEFAULTS["max_bars"],
    }
    try:
        values["max_bars"] = max(1, min(int(str(values["max_bars"])), 50))
    except ValueError:
        values["max_bars"] = DEFAULTS["max_bars"]
    ctx.save_config(values)
    ctx.log(f"配置已保存: {values}")
    return _render(ctx, request)


# ------------------------------------------------------------------- 插件类


class KindChart(Plugin):
    """按 kind 画条状图（插件模板 / 示例）。

    契约很简单：继承 ``Plugin``，实现 ``start()`` / ``stop()``；
    想提供页面就再覆盖 ``render_page()``，想处理表单就覆盖 ``handle_post()``。
    下面这些方法只是把上面那几个「纯函数」和 ``self.ctx`` 接起来——
    你也可以把逻辑直接写在方法里，两种写法都行。
    """

    def render_page(self, request: Any = None) -> str:
        return _render(self.ctx, request)

    def handle_post(self, request: Any = None, form: Mapping[str, Any] | None = None) -> str:
        return _handle_post(self.ctx, request, form or {})

    # ------------------------------------------------------------------ 命令行
    @cmd("top", help="在命令行里打印各分类条目数排行")
    def top(self, limit: int = 10) -> str:
        """打印每个 kind 的条目数量（从多到少）。

        limit: 最多显示几个分类
        """
        stats = self.host.stats()
        labels = {item["name"]: item["label"] for item in self.host.kinds()}
        rows = [(labels.get(name, name), count) for name, count in stats["by_kind"].items()]
        rows.sort(key=lambda item: (-item[1], item[0]))
        width = max((len(label) for label, _ in rows), default=4)
        lines = [f"共 {stats['total']} 条 · 待复核 {stats['pending_merges']}"]
        for label, count in rows[: max(1, limit)]:
            lines.append(f"  {label:<{width}}  {count:>6}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """加载时调一次：这里只需要打个日志（配置由宿主在实例化前读好）。"""
        config = effective_config(self.ctx)
        self.log(f"{self.title} 已启动（柱子颜色 {config['bar_color']}）")

    def stop(self) -> None:
        """卸载/重载/进程退出时调一次：这里没有需要释放的资源。"""
        self.log("已停止")
