"""插件上下文：插件与宿主之间唯一的那根线。

* 插件 -> 宿主：``ctx.host``（只读数据查询接口，见 ``host.py``）；
* 宿主 -> 插件：``start(ctx)`` / ``stop(ctx)`` / ``render_page(ctx, request)`` /
  ``handle_post(ctx, request, form)``；
* 插件自己的配置与持久化数据：``ctx.data_dir``（``plugins_data/<插件 id>/``，不进 git）。

插件运行在宿主进程里，所以它 ``import`` 到的东西和宿主一样；但**约定只用标准库**、
``ctx`` 提供的接口和 ``markupsafe`` 之类的既有依赖，别去碰宿主内部模块（那些不算接口）。
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .host import HostAPI
from .logstore import PluginLogStore
from .metadata import PluginMetadata

CONFIG_FILENAME = "config.toml"


@dataclass
class PluginContext:
    """交给插件的运行时对象（``start(ctx)`` 收到的就是它）。"""

    metadata: PluginMetadata
    #: 插件自己的数据目录；**读 ``data_dir`` 属性**时会自动建好（规范里承诺过）
    data_dir_path: Path
    host: HostAPI
    #: 宿主的日志收集（``None`` = 不落盘，只在内存里留最近 200 条）
    log_store: PluginLogStore | None = None
    _config: dict[str, Any] = field(default_factory=dict, repr=False)
    _log: list[str] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------ 基本信息
    @property
    def data_dir(self) -> Path:
        """``plugins_data/<插件 id>/``：读一下就保证目录存在，插件不用自己 mkdir。"""
        self.data_dir_path.mkdir(parents=True, exist_ok=True)
        return self.data_dir_path

    @property
    def plugin_id(self) -> str:
        return self.metadata.id

    @property
    def title(self) -> str:
        return self.metadata.title

    @property
    def code_dir(self) -> Path:
        """插件代码目录（只读；写东西请用 ``data_dir``）。"""
        return self.metadata.directory

    # ------------------------------------------------------------ 配置
    @property
    def config(self) -> dict[str, Any]:
        """插件配置（``plugins_data/<id>/config.toml``，第一次访问时读盘）。"""
        return self._config

    @property
    def config_path(self) -> Path:
        return self.data_dir / CONFIG_FILENAME

    def load_config(self) -> dict[str, Any]:
        """重新从磁盘读配置（文件不存在就是空配置）。"""
        path = self.config_path
        if path.is_file():
            try:
                loaded = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                loaded = {}
            self._config = dict(loaded)
        else:
            self._config = {}
        return self._config

    def save_config(self, values: Mapping[str, Any]) -> Path:
        """覆盖保存配置（TOML；字符串/数字/布尔/数组都能直接写）。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lines: list[str] = ["# 由插件通过 ctx.save_config() 写入；不进 git"]
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
        path = self.config_path
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._config = dict(values)
        return path

    # ------------------------------------------------------------ 数据
    def state_file(self, name: str = "state.json") -> Path:
        """插件自己的持久化文件路径（会自动建目录）。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        safe = Path(name).name or "state.json"
        return self.data_dir / safe

    def read_state(self, name: str = "state.json", default: Any = None) -> Any:
        path = self.state_file(name)
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def write_state(self, data: Any, name: str = "state.json") -> Path:
        path = self.state_file(name)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ------------------------------------------------------------ 渲染 / 日志
    def render(self, template_name: str, **values: Any) -> str:
        """用插件自己 ``templates/`` 目录里的 Jinja 模板渲染一段 HTML。"""
        directory = self.metadata.templates_dir
        loader = FileSystemLoader(str(directory if directory.is_dir() else self.code_dir))
        environment = Environment(loader=loader, autoescape=select_autoescape(["html", "xml"]))
        return environment.get_template(template_name).render(**values, ctx=self, plugin=self.metadata)

    def log(self, message: str, *, level: str = "info") -> None:
        """记一条插件日志：内存里留最近 200 条（插件列表页立刻能看到），
        同时交给宿主的 :class:`~omnishelf.plugins.logstore.PluginLogStore`
        落到 ``data/logs/plugins/<插件 id>.log``，进程重启也还在。

        **消息进这里之前先过你自己的** ``redact()``：宿主不知道你的口令长什么样，
        没法替你打码，这条线出了插件就没人兜底。
        """
        text = str(message)
        self._log.append(text)
        del self._log[:-200]
        if self.log_store is not None:
            self.log_store.write(self.metadata.id, text, level=level)

    @property
    def logs(self) -> list[str]:
        return list(self._log)


def _toml_value(value: Any) -> str:
    """够用就行的 TOML 值序列化（字符串/布尔/数字/数组）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'
