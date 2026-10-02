"""宿主统一收集插件日志：把 ``ctx.log()`` 收到的行落到 ``data/logs/plugins/<插件 id>.log``。

**为什么自己开文件写，而不是挂 ``logging`` 的 handler**：``logging.getLogger(name)``
的名字是进程级全局的，而一个进程里可能同时存在好几个 ``PluginManager``
（测试里给不同临时数据目录各起一个 ``TestClient`` 很常见）。共用同名 logger 就会让
一行日志同时写进两份文件，测试结果取决于「谁先 import 了谁」；按 store 实例各持句柄
则互不干涉，也顺带没有「重载忘摘 handler」这类泄漏。

**两个进程可能写同一份文件**（常驻的 ``omnishelf serve`` 和一次性的 ``omnishelf plugins <命令>``）：

* *清空* —— 一个 store 第一次碰某个插件的日志时截断一次，于是文件始终是
  「本次运行的可读日志」，跟插件那边 ``meta.json`` 里「上次是什么样」的状态各司其职；
* *轮转* —— 只在 ``rotate=True``（常驻进程）时做。命令行进程不轮转：
  把宿主正在写的文件 rename 走，宿主的句柄就会跟着 inode 继续往 ``.1`` 里写，日志凭空分叉。
"""

from __future__ import annotations

import os
import threading
from collections import deque
from datetime import datetime
from io import TextIOBase
from pathlib import Path
from typing import Iterable

LOG_SUFFIX = ".log"
#: 单个日志文件超过这个大小就轮转（只给常驻进程用）。
MAX_BYTES = 1_000_000
#: 轮转保留几份历史（``<id>.log.1`` … ``.3``）。
BACKUP_COUNT = 3
#: 页面/CLI 默认读的尾部行数。
DEFAULT_TAIL = 200

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_LEVEL_WIDTH = 5


class PluginLogStore:
    """一个宿主进程持有的插件日志集合。"""

    def __init__(self, logs_dir: Path | str, *, rotate: bool = True) -> None:
        self.logs_dir = Path(logs_dir)
        self.rotate = rotate
        self._handles: dict[str, TextIOBase] = {}
        self._sizes: dict[str, int] = {}
        #: 本次运行里已经截断过的插件（热重载重新打开句柄时不该再清一次）。
        self._reset: set[str] = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 路径
    def path_for(self, plugin_id: str) -> Path:
        return self.logs_dir / f"{_safe_name(plugin_id)}{LOG_SUFFIX}"

    def logged_plugins(self) -> list[str]:
        """已经有日志文件的插件 id（页面上的下拉框用）。"""
        if not self.logs_dir.is_dir():
            return []
        return sorted(
            path.name[: -len(LOG_SUFFIX)]
            for path in self.logs_dir.glob(f"*{LOG_SUFFIX}")
            if path.is_file()
        )

    # ------------------------------------------------------------ 写
    def write(
        self, plugin_id: str, message: str, *, level: str = "INFO", origin: str | None = None
    ) -> None:
        """追加一行（多行消息会把后续行对齐到第一行的正文列）。

        ``origin`` 是第三列显示谁：默认插件 id，宿主自己记账的行的传 ``"宿主"``
        ——两边写进**同一个文件**，看日志的人不用在两个文件之间来回跳。
        """
        stamp = datetime.now().astimezone().strftime(_TIME_FORMAT)
        prefix = f"{stamp} | {str(level).upper():<{_LEVEL_WIDTH}} | {origin or plugin_id} | "
        body = str(message).replace("\n", "\n" + " " * len(prefix))
        payload = f"{prefix}{body}\n"
        encoded = len(payload.encode("utf-8"))
        with self._lock:
            handle = self._open(plugin_id)
            if self.rotate and self._sizes[plugin_id] + encoded > MAX_BYTES:
                self._rotate(plugin_id)
                handle = self._handles[plugin_id]
            handle.write(payload)
            handle.flush()
            self._sizes[plugin_id] += encoded

    def _open(self, plugin_id: str) -> TextIOBase:
        handle = self._handles.get(plugin_id)
        if handle is not None:
            return handle
        path = self.path_for(plugin_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 只有本次运行**第一次**碰这个插件的日志才截断：热重载会 close 再 open，
        # 要是每次都清，改一次插件代码就把这次运行的过程抹掉了。
        fresh = plugin_id not in self._reset and path.is_file() and path.stat().st_size > 0
        handle = path.open("w" if fresh else "a", encoding="utf-8")
        self._handles[plugin_id] = handle
        self._sizes[plugin_id] = 0
        self._reset.add(plugin_id)
        return handle

    def _rotate(self, plugin_id: str) -> None:
        self._close_locked(plugin_id)
        path = self.path_for(plugin_id)
        for index in range(BACKUP_COUNT, 1, -1):
            previous = path.with_name(f"{path.name}.{index - 1}")
            if previous.is_file():
                os.replace(previous, path.with_name(f"{path.name}.{index}"))
        if path.is_file():
            os.replace(path, path.with_name(f"{path.name}.1"))
        self._handles[plugin_id] = path.open("a", encoding="utf-8")
        self._sizes[plugin_id] = 0

    # ------------------------------------------------------------ 读
    def tail(self, plugin_id: str, *, limit: int = DEFAULT_TAIL) -> list[str]:
        """读某个插件日志的尾部 ``limit`` 行（文件不存在就是空）。"""
        path = self.path_for(plugin_id)
        with self._lock:
            handle = self._handles.get(plugin_id)
            if handle is not None:
                handle.flush()
        if not path.is_file():
            return []
        try:
            with path.open(encoding="utf-8") as source:
                lines: Iterable[str] = (line.rstrip("\r\n") for line in source)
                return list(deque(lines, maxlen=max(0, int(limit))))
        except OSError:
            return []

    # ------------------------------------------------------------ 收尾
    def close(self, plugin_id: str | None = None) -> None:
        """关掉句柄；不传 id 就是全关（宿主退出/``stop_all`` 时）。"""
        with self._lock:
            targets = [plugin_id] if plugin_id else list(self._handles)
            for target in targets:
                if target:
                    self._close_locked(target)

    def _close_locked(self, plugin_id: str) -> None:
        handle = self._handles.pop(plugin_id, None)
        if handle is None:
            return
        try:
            handle.close()
        except OSError:
            pass
        self._sizes.pop(plugin_id, None)


def _safe_name(plugin_id: str) -> str:
    """插件 id 直接当文件名，去掉路径分隔符（id 规范里本来就只有 ``[a-z0-9_]``）。"""
    return Path(str(plugin_id)).name or "plugin"
