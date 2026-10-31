"""netease 模块对外的数据形状。

``raw`` 留着命令/接口的原始返回，排查问题用；它不参与相等比较，也不进 ``repr``
（里面可能有整页歌单数据，回显起来又长又没用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class LoginStatus:
    """一次 ``ncm-cli login --check`` 的结论。"""

    logged_in: bool
    message: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class LoginPrompt:
    """扫码登录时给用户看的链接。

    ``url`` 是可直接点开的地址；``qr_url`` 是要画进二维码的内容（ncm-cli 当前
    两个字段给的是同一条短链）。这条链接等同于一次性的登录会话凭证：页面可以
    显示二维码/链接，**不要写进日志/状态文件**。
    """

    url: str
    message: str = ""
    qr_url: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class LoginResult:
    """``login()`` 的结果。``logged_in=False`` 只会出现在 ``wait=False`` 时。"""

    logged_in: bool
    prompt: LoginPrompt | None = None
    elapsed: float = 0.0
    message: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class Playlist:
    """收藏歌单（「我喜欢的音乐」）的基本信息。

    ``id`` 是**加密 id**（``id`` 字段，32 位十六进制），``playlist tracks`` 只认它；
    ``original_id`` 只是明文数字，不要拿去请求。
    """

    id: str
    original_id: int | None = None
    name: str = ""
    track_count: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
