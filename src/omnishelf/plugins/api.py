"""插件 SDK —— **插件唯一允许 import 的宿主模块**。

插件写成 :class:`Plugin` 的子类，宿主负责实例化并调用生命周期方法：

.. code-block:: python

    # plugins/my_plugin/plugin.py
    from omnishelf.plugins.api import Plugin

    class MyPlugin(Plugin):
        def start(self) -> None:
            self.log("准备好了")

        def stop(self) -> None:
            self.log("收工")

        def render_page(self, request) -> str:
            return "<p>你好</p>"

* ``start()`` / ``stop()`` 是**抽象方法**，没实现的话实例化就会失败（宿主会给出明确原因），
  不会像以前那样等到运行时才发现少写了一个函数；
* ``render_page()`` / ``handle_post()`` 有默认实现（不提供页面 / 不处理表单），按需覆盖；
* 需要自己的状态就直接写 ``self.xxx``；需要宿主能力用 ``self.ctx``；
  需要配置用 ``self.config`` / ``self.save_config()``。

**只有这个模块是接口**：``omnishelf.plugins.api`` 是对插件公开的稳定层，
宿主其它模块（``omnishelf.repository``、``omnishelf.services.*`` 等）都是内部实现，
插件里不要 import——它们的签名说改就改。这条有测试盯着
（``tests/test_plugin_kind_chart.py::test_plugin_only_uses_the_sdk``）。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from .context import PluginContext
from .host import AVAILABLE_INTERFACES, HostAPI
from .metadata import PluginMetadata

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注
    from starlette.requests import Request

__all__ = [
    "Plugin",
    "PluginContext",
    "PluginMetadata",
    "HostAPI",
    "AVAILABLE_INTERFACES",
    "PluginError",
    "PluginCommand",
    "cmd",
    "plugin_commands",
]

#: ``@cmd`` 往方法上挂的属性名
CMD_ATTR = "__plugin_command__"


@dataclass(frozen=True)
class PluginCommand:
    """插件注册到命令行的命令（由 :func:`cmd` 装饰器声明）。"""

    name: str
    method: str
    help: str = ""
    doc: str = ""
    params: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "method": self.method,
            "help": self.help,
            "doc": self.doc,
            "params": list(self.params),
        }


def cmd(name: str, *, help: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个方法注册成命令行命令：``omnishelf plugin <插件名> <name> [参数]``。

    ``name`` 是命令行里用的名字；``help`` 是一句话说明（列表里显示）。
    详细的参数说明写在方法自己的 docstring 里，一行一个 ``参数名: 说明``。
    """
    command = (name or "").strip()
    if not command:
        raise ValueError("@cmd 的命令名不能为空")

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, CMD_ATTR, {"name": command, "help": help.strip()})
        return func

    return decorate


def plugin_commands(cls: type) -> list[PluginCommand]:
    """列出某个插件类（会包含继承来的）用 ``@cmd`` 注册的方法。"""
    found: list[PluginCommand] = []
    seen: set[str] = set()
    for klass in reversed(cls.__mro__):
        for attr_name, value in vars(klass).items():
            marker = getattr(value, CMD_ATTR, None)
            if not isinstance(marker, dict) or marker["name"] in seen:
                continue
            seen.add(marker["name"])
            doc = inspect.getdoc(value) or ""
            found.append(
                PluginCommand(
                    name=marker["name"],
                    method=attr_name,
                    help=marker.get("help") or (doc.splitlines()[0] if doc else ""),
                    doc=doc,
                    params=tuple(_documented_params(doc)),
                )
            )
    return sorted(found, key=lambda item: item.name)


def _documented_params(doc: str) -> list[str]:
    """从 docstring 里抓 ``参数名: 说明`` 这样的行（用于展示/校验参数是否写了说明）。"""
    names: list[str] = []
    for line in doc.splitlines():
        head, sep, _ = line.strip().partition(":")
        if sep and head and " " not in head and not head.endswith(("。", ".")):
            names.append(head.lstrip("-*` "))
    return names


class PluginError(RuntimeError):
    """插件运行时错误（页面会把它显示给用户）。"""


class Plugin(ABC):
    """所有插件的基类。

    宿主实例化顺序：``Cls(ctx)`` → :meth:`start`。重载/退出时调用 :meth:`stop`。
    子类如果自定义 ``__init__``，**必须先调用** ``super().__init__(ctx)``。
    """

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ 便捷入口
    #: 下面这些只是 ``self.ctx`` 的转发，写了让插件代码短一点、也方便 IDE 补全。
    @property
    def metadata(self) -> PluginMetadata:
        """自己在 ``metadata.toml`` 里声明的信息。"""
        return self.ctx.metadata

    @property
    def plugin_id(self) -> str:
        return self.ctx.plugin_id

    @property
    def title(self) -> str:
        return self.ctx.title

    @property
    def host(self) -> HostAPI:
        """宿主提供的**只读数据查询接口**（现阶段只有它）。"""
        return self.ctx.host

    @property
    def config(self) -> dict[str, Any]:
        """插件配置（``plugins_data/<id>/config.toml``）。"""
        return self.ctx.config

    @property
    def data_dir(self) -> Path:
        """插件自己的可写目录（``plugins_data/<id>/``，不进 git）。"""
        return self.ctx.data_dir

    @property
    def code_dir(self) -> Path:
        """插件代码目录（只读）。"""
        return self.ctx.code_dir

    def save_config(self, values: Mapping[str, Any]) -> Path:
        return self.ctx.save_config(values)

    def import_file(self, importer: str, path: str | Path, *, archive: bool = True) -> dict[str, Any]:
        """把文件交给宿主的**内置 importer** 入库（``import`` 接口）。

        走的和 CLI/上传页完全一样的链路：来源级判重、写 ``import_logs``、原始文件存档。
        返回导入结果摘要 dict（``total`` / ``inserted`` / ``updated`` / ``skipped`` /
        ``unchanged`` / ``error_count`` / ``warning_count`` / ``log_id`` / ``archive_path`` / ``summary``）。
        """
        return self.host.import_file(importer, path, archive=archive)

    def importer_names(self) -> list[str]:
        """宿主当前注册的 importer 名字（想知道有没有自己要的那个时用）。"""
        return self.host.importer_names()

    def audio_suffixes(self) -> list[str]:
        """宿主认作音乐文件的后缀（``audio`` 接口，文件选择框用同一份名单）。"""
        return self.host.audio_suffixes()

    def scan_audio(
        self,
        targets: Sequence[str | Path],
        *,
        out_path: str | Path,
        progress: Callable[[int, Path], None] | None = None,
    ) -> dict[str, Any]:
        """读本地音乐文件的标签并写成清单 JSON（``audio`` 接口）。

        扫描口径（认哪些后缀、跳过哪些垃圾目录、无标签时怎么兜底）全在宿主那边，
        所以插件扫出来的和 ``omnishelf scan-music`` 一模一样。返回摘要 dict
        （``manifest_path`` / ``scanned`` / ``item_count`` / ``tagless`` / ``failed`` /
        ``errors`` / ``summary``），``manifest_path`` 交给
        ``self.import_file("music_files", ...)`` 入库。

        ``out_path`` 要写在插件自己的 ``data_dir`` 里；``progress(已读数, 当前文件)``
        每读一个文件回调一次，画进度条用（慢盘上这一步可能要几分钟，别在请求线程里跑）。
        """
        return self.host.scan_audio(targets, out_path=out_path, progress=progress)

    def render(self, template_name: str, **values: Any) -> str:
        """用插件自己 ``templates/`` 里的 Jinja 模板渲染 HTML。"""
        return self.ctx.render(template_name, **values)

    def read_state(self, name: str = "state.json", default: Any = None) -> Any:
        return self.ctx.read_state(name, default)

    def write_state(self, data: Any, name: str = "state.json") -> Path:
        return self.ctx.write_state(data, name)

    def state_file(self, name: str = "state.json") -> Path:
        return self.ctx.state_file(name)

    def log(self, message: str, *, level: str = "info") -> None:
        """记一条日志：插件列表页能看到最近 200 条，宿主同时落盘到
        ``data/logs/plugins/<插件 id>.log``。**写之前自己先打码**（口令/令牌别进这里）。"""
        self.ctx.log(message, level=level)

    # ------------------------------------------------------------------ 生命周期
    @abstractmethod
    def start(self) -> None:
        """加载时调用一次：读配置、做准备、注册东西。**必须实现**。"""

    @abstractmethod
    def stop(self) -> None:
        """卸载 / 重载 / 进程退出时调用一次：释放资源、做收尾。**必须实现**。"""

    # ------------------------------------------------------------------ 页面（可选）
    def render_page(self, request: "Request | None" = None) -> str:
        """返回插件自己页面的 HTML（宿主会套进站点布局）。

        不实现就没有页面（插件列表里显示「没有自己的页面」）。
        """
        return ""

    def handle_post(
        self, request: "Request | None" = None, form: Mapping[str, Any] | None = None
    ) -> str | None:
        """处理插件页面上的表单提交：返回 HTML 直接回显，返回 ``None`` 跳回页面。"""
        return None

    # ------------------------------------------------------------------ 其它
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} id={self.plugin_id!r}>"
