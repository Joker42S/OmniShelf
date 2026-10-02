"""插件管理器：发现 → 加载 → 热重载 → 停止。

插件 = :class:`omnishelf.plugins.api.Plugin` 的子类（实例化时宿主注入 ``ctx``）：

* ``start()`` —— 加载时调用一次（注册东西、读配置、做准备）；**抽象方法，必须实现**；
* ``stop()``  —— 卸载/重载/进程退出时调用一次（释放资源，做收尾）；**抽象方法，必须实现**；
* 可选 ``render_page(request) -> str``（插件自己的页面）与
  ``handle_post(request, form) -> str | None``（页面上的表单提交）。

类的定位：``metadata.toml`` 的 ``entry = "plugin.py:MyPlugin"``；只写 ``plugin.py`` 时
自动找文件里唯一的 ``Plugin`` 子类（0 个或多个都会给出明确错误）。

热重载：管理器给每个插件算一个**指纹**（metadata.toml + 入口文件 + templates/ 的
mtime 与大小），发现指纹变了就 ``stop`` 旧的、重新导入模块、``start`` 新的。
Web 请求前与 ``reload_changed()`` 都会检查，所以「改完插件刷新页面」就生效。
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import traceback
import types
import argparse
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import Engine

from ..repository import utc_now_iso
from .api import Plugin, PluginCommand, PluginError as SDKPluginError, plugin_commands
from .context import PluginContext
from .host import AVAILABLE_INTERFACES, HostAPI, QueryHostAPI
from .logstore import PluginLogStore
from .metadata import METADATA_FILENAME, PluginMetadata, PluginMetadataError, load_metadata

#: 状态
STATE_LOADED = "loaded"
STATE_ERROR = "error"
STATE_DISABLED = "disabled"
STATE_MISSING = "missing"  # 目录/元数据有问题，压根没加载

#: 宿主自己写进插件日志时，第三列显示谁（区别于插件说的行）。
HOST_LOG_ORIGIN = "宿主"


@dataclass
class PluginStatus:
    """一个插件当前的情况（给页面/CLI 用）。"""

    id: str
    title: str
    version: str = ""
    description: str = ""
    author: str = ""
    directory: str = ""
    state: str = STATE_MISSING
    error: str | None = None
    loaded_at: str | None = None
    requires: tuple[str, ...] = ()
    has_page: bool = False
    logs: list[str] = field(default_factory=list)
    #: 宿主把这个插件的日志落到了哪儿（没开日志就是 ``None``）。
    log_file: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == STATE_LOADED

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "directory": self.directory,
            "state": self.state,
            "error": self.error,
            "loaded_at": self.loaded_at,
            "requires": list(self.requires),
            "has_page": self.has_page,
            "log_file": self.log_file,
        }


@dataclass
class LoadedPlugin:
    """装载好的插件：元数据 + 上下文 + 模块 + **插件实例**。"""

    metadata: PluginMetadata
    context: PluginContext
    module: types.ModuleType
    instance: Plugin
    fingerprint: str
    loaded_at: str = ""
    logs: list[str] = field(default_factory=list)

    @property
    def has_page(self) -> bool:
        """插件是否提供了自己的页面（覆盖了 ``render_page``）。"""
        return type(self.instance).render_page is not Plugin.render_page


class PluginError(SDKPluginError):
    """插件加载/运行出错（页面上会显示这条消息）。"""


class PluginManager:
    """管理 ``plugins/`` 下所有插件。"""

    def __init__(
        self,
        *,
        plugins_dir: Path,
        plugins_data_dir: Path,
        host: HostAPI | None = None,
        engine: Engine | None = None,
        imports_dir: Path | None = None,
        enabled: Mapping[str, bool] | None = None,
        logs_dir: Path | None = None,
        log_rotate: bool = True,
    ) -> None:
        self.plugins_dir = Path(plugins_dir)
        self.plugins_data_dir = Path(plugins_data_dir)
        #: 宿主统一收集的插件日志；不传 ``logs_dir`` 就只在内存里留（测试/嵌入式用）。
        self.log_store = PluginLogStore(logs_dir, rotate=log_rotate) if logs_dir else None
        if host is None:
            if engine is None:
                raise ValueError("PluginManager 需要 host 或 engine 之一")
            # imports_dir 给插件用的 import 接口存档原始文件（和上传页一致）
            host = QueryHostAPI(engine, imports_dir=imports_dir)
        self.host = host
        self._enabled = dict(enabled or {})
        self._loaded: dict[str, LoadedPlugin] = {}
        self._errors: dict[str, str] = {}
        self._discovered: dict[str, PluginMetadata] = {}
        self._discover_errors: dict[str, str] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 发现
    def discover(self) -> dict[str, PluginMetadata]:
        """扫描 ``plugins/*/metadata.toml``（不加载代码）。"""
        found: dict[str, PluginMetadata] = {}
        errors: dict[str, str] = {}
        if self.plugins_dir.is_dir():
            for child in sorted(self.plugins_dir.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                if not (child / METADATA_FILENAME).is_file():
                    continue  # 没有 metadata 的目录直接忽略（例如放模板/文档）
                try:
                    metadata = load_metadata(child)
                except PluginMetadataError as exc:
                    errors[child.name] = str(exc)
                    continue
                found[metadata.id] = metadata
        self._discovered = found
        self._discover_errors = errors
        return dict(found)

    def metadata(self, plugin_id: str) -> PluginMetadata | None:
        if not self._discovered:
            self.discover()
        return self._discovered.get(plugin_id)

    # ------------------------------------------------------------ 指纹/热重载
    @staticmethod
    def fingerprint(metadata: PluginMetadata) -> str:
        """metadata + 入口文件 + templates/ 的指纹（变了就该重载）。"""
        digest = hashlib.sha256()
        paths: list[Path] = [metadata.directory / METADATA_FILENAME, metadata.entry_path]
        for extra in sorted(metadata.directory.rglob("*")):
            if extra.is_file() and extra.suffix in {".py", ".html", ".toml", ".css", ".js"}:
                paths.append(extra)
        for path in sorted(set(paths)):
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}".encode())
        return digest.hexdigest()[:16]

    def reload_changed(self) -> list[str]:
        """指纹变了的插件重载，返回被重载的 id 列表。

        **这是「自动」路径，永远不抛异常**：某个插件坏掉只记状态，
        不能让整张插件列表页/别的插件跟着崩（``reload()`` 是用户显式点的，会抛）。
        """
        changed: list[str] = []
        self.discover()
        for plugin_id, metadata in self._discovered.items():
            current = self.fingerprint(metadata)
            loaded = self._loaded.get(plugin_id)
            if loaded is None or loaded.fingerprint != current:
                self._safe_load(plugin_id)
                changed.append(plugin_id)
        return changed

    # ------------------------------------------------------------ 加载/卸载
    def _host_log(self, plugin_id: str, message: str, *, level: str = "info") -> None:
        """宿主自己记的一行，写进**同一个**插件的日志文件（第三列显示「宿主」）。

        这样看日志不用在两个文件之间跳：加载/重载/卸载/出错是宿主说的，
        抓取进行到哪一步是插件说的，串起来就是完整的一次运行。
        """
        if self.log_store is not None:
            self.log_store.write(plugin_id, message, level=level, origin=HOST_LOG_ORIGIN)

    def load(self, plugin_id: str) -> PluginStatus:
        metadata = self.metadata(plugin_id)
        if metadata is None:
            raise PluginError(f"没有这个插件: {plugin_id}")
        with self._lock:
            self._unload(plugin_id)
            loaded = self._import(metadata)
            self._loaded[plugin_id] = loaded
        self._host_log(plugin_id, f"加载完成：{metadata.title} v{metadata.version}")
        return self.status(plugin_id)

    def load_all(self) -> list[PluginStatus]:
        """加载全部插件；**单个插件出错只记状态**，不影响其它插件。"""
        self.discover()
        for plugin_id in self._discovered:
            self._safe_load(plugin_id)
        return self.list_plugins()

    def unload(self, plugin_id: str) -> None:
        with self._lock:
            self._unload(plugin_id)

    def stop_all(self) -> None:
        """停掉全部插件，并收掉宿主的日志句柄（进程退出前调一次）。"""
        with self._lock:
            for plugin_id in list(self._loaded):
                self._unload(plugin_id)
        if self.log_store is not None:
            self.log_store.close()

    def reload(self, plugin_id: str | None = None) -> list[str]:
        """重载一个插件；不传 id 就是全部。返回被重载的 id。"""
        self.discover()
        targets = [plugin_id] if plugin_id else list(self._discovered)
        done: list[str] = []
        for target in targets:
            if target not in self._discovered:
                continue
            self.load(target)
            done.append(target)
        return done

    def _unload(self, plugin_id: str) -> None:
        loaded = self._loaded.pop(plugin_id, None)
        if loaded is None:
            return
        try:
            loaded.instance.stop()
        except Exception as exc:  # 插件的 stop 出错不该影响宿主
            self._errors[plugin_id] = f"stop() 出错: {exc}"
            self._host_log(plugin_id, f"stop() 出错：{type(exc).__name__}: {exc}", level="error")
        module_name = loaded.module.__name__
        sys.modules.pop(module_name, None)
        self._forget_own_packages(loaded.metadata.directory)
        self._host_log(plugin_id, "已卸载（停止或重载）")

    @staticmethod
    def _forget_own_packages(directory: Path) -> None:
        """卸载时把这个插件**自己目录里的子包**也从 ``sys.modules`` 里清掉。

        入口文件由宿主直接编译执行，每次重载都是新代码；而它 ``from mypkg import ...``
        拿到的子包走的是普通 import，会一直缓存着。带工具包的插件（``copymanga_fetch`` /
        ``picamanga_fetch``）只清入口模块就会出现「入口是新的、子包是旧的」，
        表现为新参数传进旧类的一句 ``TypeError``，而且改回去也看不出问题在哪。
        """
        root = str(directory.resolve())
        for name, module in list(sys.modules.items()):
            candidates = [getattr(module, "__file__", None), *(getattr(module, "__path__", ()) or ())]
            paths = [str(Path(path).resolve()) for path in candidates if path]
            if any(path == root or path.startswith(root + os.sep) for path in paths):
                sys.modules.pop(name, None)

    def _find_class(self, metadata: PluginMetadata, module: types.ModuleType) -> type[Plugin]:
        """在插件模块里找 Plugin 子类（metadata 里写了类名就用它）。"""
        if metadata.class_name:
            candidate = getattr(module, metadata.class_name, None)
            if candidate is None:
                raise PluginError(
                    f"入口文件 {metadata.entry} 里没有 {metadata.class_name} "
                    f"（metadata.toml 的 entry = \"{metadata.entry_spec}\"）"
                )
            found = [candidate]
        else:
            found = [
                value
                for value in vars(module).values()
                if isinstance(value, type) and issubclass(value, Plugin) and value is not Plugin
            ]
        if not found:
            raise PluginError(
                f"入口文件 {metadata.entry} 里没有 Plugin 子类。"
                f"插件要写成：class MyPlugin(Plugin): ...（from omnishelf.plugins.api import Plugin）"
            )
        if len(found) > 1:
            names = ", ".join(sorted(cls.__name__ for cls in found))
            raise PluginError(
                f"入口文件 {metadata.entry} 里有多个 Plugin 子类（{names}），"
                f'请在 metadata.toml 里写明要哪个：entry = "{metadata.entry}:类名"'
            )
        return found[0]

    def _import(self, metadata: PluginMetadata) -> LoadedPlugin:
        """导入插件模块、实例化它的 Plugin 子类并调用 start()。

        失败时抛 PluginError（单个插件坏掉不影响别的插件）。
        """
        entry = metadata.entry_path
        if not entry.is_file():
            raise PluginError(f"入口文件不存在: {metadata.entry}")
        module_name = f"omnishelf_plugins.{metadata.id}"
        # 直接编译源码执行：绕开 importlib 的 .pyc 缓存。
        # （同一秒内改插件、且文件大小没变时，pyc 缓存会让热重载拿到旧代码；
        #   顺带也不会往插件目录里写 __pycache__。）
        try:
            source = entry.read_text(encoding="utf-8")
        except OSError as exc:
            raise PluginError(f"读不了入口文件 {metadata.entry}: {exc}") from exc
        module = types.ModuleType(module_name)
        module.__file__ = str(entry)
        sys.modules[module_name] = module

        context = PluginContext(
            metadata=metadata,
            data_dir_path=self.plugins_data_dir / metadata.id,
            host=self.host,
            log_store=self.log_store,
        )
        context.load_config()
        try:
            exec(compile(source, str(entry), "exec"), module.__dict__)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginError(f"导入插件代码失败: {type(exc).__name__}: {exc}") from exc

        plugin_class = self._find_class(metadata, module)
        try:
            instance = plugin_class(context)
        except TypeError as exc:  # 抽象方法没实现 / __init__ 忘了 super()
            sys.modules.pop(module_name, None)
            raise PluginError(
                f"实例化 {plugin_class.__name__} 失败：{exc}\n"
                f"（插件要继承 Plugin 并实现 start()/stop()；自定义 __init__ 记得 super().__init__(ctx)）"
            ) from exc
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginError(f"实例化 {plugin_class.__name__} 失败: {type(exc).__name__}: {exc}") from exc

        if not isinstance(instance, Plugin):  # pragma: no cover - _find_class 已保证
            sys.modules.pop(module_name, None)
            raise PluginError(f"{plugin_class.__name__} 不是 Plugin 子类")

        loaded = LoadedPlugin(
            metadata=metadata,
            context=context,
            module=module,
            instance=instance,
            fingerprint=self.fingerprint(metadata),
            loaded_at=utc_now_iso(),
        )
        try:
            instance.start()
        except Exception as exc:
            sys.modules.pop(module_name, None)
            detail = traceback.format_exc(limit=3)
            raise PluginError(f"start() 出错: {type(exc).__name__}: {exc}\n{detail}") from exc
        self._errors.pop(metadata.id, None)
        return loaded

    def _safe_load(self, plugin_id: str) -> None:
        """给 load_all 用：单个插件出错只记状态。"""
        try:
            self.load(plugin_id)
        except PluginError as exc:
            self._errors[plugin_id] = str(exc)
            self._host_log(plugin_id, f"加载失败：{exc}", level="error")

    # ------------------------------------------------------------ 查询/状态
    def get(self, plugin_id: str) -> LoadedPlugin | None:
        return self._loaded.get(plugin_id)

    def list_plugins(self) -> list[PluginStatus]:
        if not self._discovered and not self._discover_errors:
            self.discover()
        statuses: list[PluginStatus] = []
        for plugin_id, message in sorted(self._discover_errors.items()):
            statuses.append(
                PluginStatus(id=plugin_id, title=plugin_id, directory=str(self.plugins_dir / plugin_id),
                             state=STATE_ERROR, error=f"metadata 有问题: {message}")
            )
        for plugin_id, metadata in sorted(self._discovered.items()):
            statuses.append(self.status(plugin_id, metadata=metadata))
        return statuses

    def status(self, plugin_id: str, *, metadata: PluginMetadata | None = None) -> PluginStatus:
        meta = metadata or self.metadata(plugin_id)
        loaded = self._loaded.get(plugin_id)
        error = self._errors.get(plugin_id)
        status = PluginStatus(
            id=plugin_id,
            title=meta.title if meta else plugin_id,
            version=meta.version if meta else "",
            description=meta.description if meta else "",
            author=meta.author if meta else "",
            directory=str(meta.directory) if meta else "",
            requires=meta.requires if meta else (),
            state=STATE_LOADED if loaded else (STATE_ERROR if error else STATE_MISSING),
            error=error,
            loaded_at=loaded.loaded_at if loaded else None,
            has_page=loaded.has_page if loaded else False,
            logs=loaded.context.logs if loaded else [],
            log_file=str(self.log_store.path_for(plugin_id)) if self.log_store else None,
        )
        if not loaded and not error and self._enabled.get(plugin_id) is False:
            status.state = STATE_DISABLED
        return status

    def page(self, plugin_id: str, request: Any) -> str:
        """调用插件的 render_page；插件不在/没实现/抛错都给明确错误。"""
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            status = self.status(plugin_id)
            if status.state == STATE_ERROR:
                raise PluginError(status.error or "插件加载失败")
            raise PluginError(f"插件未加载: {plugin_id}")
        try:
            return str(loaded.instance.render_page(request))
        except Exception as exc:
            raise PluginError(f"render_page() 出错: {type(exc).__name__}: {exc}") from exc

    def post(self, plugin_id: str, request: Any, form: Mapping[str, Any]) -> str | None:
        """调用插件的 handle_post（没实现就当没事发生）。"""
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            raise PluginError(f"插件未加载: {plugin_id}")
        try:
            result = loaded.instance.handle_post(request, dict(form))
        except Exception as exc:
            raise PluginError(f"handle_post() 出错: {type(exc).__name__}: {exc}") from exc
        return None if result is None else str(result)

    def check_requires(self, metadata: PluginMetadata) -> list[str]:
        """返回插件声明了但宿主没提供的接口名（给页面提示用）。"""
        return [name for name in metadata.requires if name not in AVAILABLE_INTERFACES]

    def interfaces(self) -> Iterable[str]:
        return AVAILABLE_INTERFACES

    # ------------------------------------------------------------ 命令行命令
    def commands(self, plugin_id: str) -> list[PluginCommand]:
        """列出插件用 ``@cmd`` 注册的命令（没加载/没注册就是空）。"""
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            return []
        return plugin_commands(type(loaded.instance))

    def run_command(self, plugin_id: str, name: str, argv: Sequence[str] = ()) -> Any:
        """执行插件的命令行命令，返回它的返回值。

        参数表由方法签名推出来（见 :func:`build_command_parser`）；
        ``-h`` / ``--help`` 会打印该命令的说明（含 docstring 里的参数说明）。
        """
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            raise PluginError(f"插件未加载: {plugin_id}")
        commands = {item.name: item for item in self.commands(plugin_id)}
        command = commands.get(name)
        if command is None:
            available = ", ".join(sorted(commands)) or "(没有注册任何命令)"
            raise PluginError(f"插件 {plugin_id} 没有命令 {name!r}；可用: {available}")
        method = getattr(loaded.instance, command.method, None)
        if method is None:  # pragma: no cover - 命令是刚内省出来的
            raise PluginError(f"插件 {plugin_id} 的命令 {name!r} 找不到方法 {command.method}")
        parser = build_command_parser(plugin_id, command, method)
        namespace = parser.parse_args(list(argv))
        try:
            return method(**vars(namespace))
        except SystemExit:  # pragma: no cover - argparse 自己会退出
            raise
        except Exception as exc:
            raise PluginError(f"命令 {name} 执行出错: {type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# 插件命令行：把方法签名翻译成 argparse（参数说明取 docstring 里的「参数名: 说明」）
# ---------------------------------------------------------------------------

def _doc_helps(doc: str) -> dict[str, str]:
    """从 docstring 里解析 ``参数名: 说明`` 行，作为每个参数的 help。"""
    helps: dict[str, str] = {}
    for line in doc.splitlines():
        head, sep, tail = line.strip().partition(":")
        name = head.lstrip("-*` ").strip()
        if sep and name and " " not in name and tail.strip():
            helps.setdefault(name, tail.strip())
    return helps


def _arg_type(annotation: Any, default: Any) -> Any:
    """把类型标注/默认值翻译成 argparse 的 type。"""
    for candidate in (annotation, type(default) if default is not inspect.Parameter.empty else None):
        if candidate in (int, float, str):
            return candidate
    return str


def build_command_parser(
    plugin_id: str, command: PluginCommand, method: Any
) -> argparse.ArgumentParser:
    """按方法签名生成 argparse 解析器。

    * 没有默认值的参数 → 位置参数（必填）
    * 有默认值的参数 → ``--参数名``（可省略，用默认值）；下划线会同时给出连字符写法，
      例如 ``dry_run`` 两个都能用：``--dry-run`` / ``--dry_run``
    * ``bool`` 且默认 False → 开关；默认 True → ``--参数名/--no-参数名``
    """
    parser = argparse.ArgumentParser(
        prog=f"omnishelf plugin {plugin_id} {command.name}",
        description=command.doc or command.help or command.name,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    helps = _doc_helps(command.doc)
    for name, param in inspect.signature(method).parameters.items():
        if name == "self":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        help_text = helps.get(name, "")
        dashed = name.replace("_", "-")
        spellings = [f"--{dashed}"] + ([f"--{name}"] if dashed != name else [])
        if isinstance(param.default, bool):
            if param.default is True:
                parser.add_argument(
                    f"--{dashed}",
                    dest=name,
                    action=argparse.BooleanOptionalAction,
                    default=True,
                    help=help_text or f"默认开启（--no-{dashed} 关闭）",
                )
            else:
                parser.add_argument(
                    *spellings, dest=name, action="store_true", default=False, help=help_text
                )
            continue
        if param.default is inspect.Parameter.empty:
            parser.add_argument(name, type=_arg_type(param.annotation, None), help=help_text)
        else:
            parser.add_argument(
                *spellings,
                dest=name,
                default=param.default,
                type=_arg_type(param.annotation, param.default),
                help=help_text,
            )
    return parser
