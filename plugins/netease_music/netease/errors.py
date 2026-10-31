"""netease 模块的异常。

原则：异常文本只描述「哪条命令、什么输出」，**不包含任何凭据**。
本模块从不读取 appid / 私钥 / 登录令牌，它们由 ncm-cli 自己保管。
扫码登录链接属于一次性的登录会话，不写进异常文本（需要展示给用户时走
``LoginPrompt``，由调用方决定在哪一屏显示，不要落日志）。
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Any

#: 报错里截取多少字符的输出，够定位问题又不至于把整页响应搬进日志
SHORT_LIMIT = 300


def _short(text: str, limit: int = SHORT_LIMIT) -> str:
    """把多行输出压成一行并截断（给异常文本用）。"""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _command_label(command: Sequence[str]) -> str:
    return shlex.join(str(part) for part in command)


class NeteaseError(Exception):
    """netease 模块所有异常的基类。"""


class NcmCliNotFound(NeteaseError):
    """本机找不到 ncm-cli 可执行文件。"""

    def __init__(self, binary: str) -> None:
        super().__init__(
            f"找不到 ncm-cli（{binary}）。先安装并配置 appid/私钥："
            "npm install -g @music163/ncm-cli && ncm-cli configure"
        )
        self.binary = binary


class NcmCliError(NeteaseError):
    """ncm-cli 以非 0 退出，或命令执行超时。"""

    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        detail = _short(stderr) or _short(stdout) or "没有输出"
        super().__init__(
            f"{_command_label(command)} 执行失败（退出码 {returncode}）：{detail}"
        )
        self.command = tuple(command)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class NcmCliOutputError(NeteaseError):
    """命令退出码是 0，但输出不是能解析的 JSON。"""

    def __init__(
        self,
        command: Sequence[str],
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        detail = _short(stdout) or _short(stderr) or "没有输出"
        super().__init__(f"{_command_label(command)} 的输出不是 JSON：{detail}")
        self.command = tuple(command)
        self.stdout = stdout
        self.stderr = stderr


class NeteaseLoginError(NeteaseError):
    """未登录、登录状态失效，或登录命令明确失败。"""


class NeteaseLoginTimeout(NeteaseLoginError):
    """等用户扫码超时。

    ``prompt`` 里还留着这次生成的链接，调用方可以继续展示给用户；
    重新调用 ``login()`` 会生成一条新链接。
    """

    def __init__(self, timeout: float, prompt: Any = None) -> None:
        super().__init__(
            f"等待扫码登录超时（{timeout:g} 秒）。重新调用登录会生成新链接。"
        )
        self.timeout = timeout
        self.prompt = prompt


class NeteaseApiError(NeteaseError):
    """网易云接口在 JSON 里回了非 200 的 ``code``，或返回结构对不上。"""

    def __init__(self, message: str, *, code: Any = None) -> None:
        self.code = code
        if code is None:
            super().__init__(f"网易云接口返回错误：{message}")
        else:
            super().__init__(f"网易云接口返回错误（code={code}）：{message}")
