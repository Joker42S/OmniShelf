"""netease 模块测试替身：假 ncm-cli 子进程 + 合成响应。

``netease`` 随 ``plugins/netease_music`` 分发（宿主执行的是插件入口文件，插件目录
不是 Python 包），所以先把插件目录补进 ``sys.path`` 再 import——和
``tests/steam_synth.py`` 对 ``steam_client`` 的做法一致。

约定：**测试绝不真的执行 ncm-cli**，所有命令都由 :class:`FakeRunner` 回答；
数据是合成的（假加密 id、``.invalid`` 域名），不含任何真实收藏。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from omnishelf.config import repo_root

PLUGIN_DIR = repo_root() / "plugins" / "netease_music"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from netease.client import CommandResult  # type: ignore[import-not-found]  # noqa: E402

#: 两条歌单各有一个加密 id 和一个 originalId，测试盯着「用 id、不用 originalId」
FAVORITE_ID = "0123456789ABCDEF0123456789ABCDEF"
CREATED_ID = "FEDCBA9876543210FEDCBA9876543210"
FAVORITE_ORIGINAL_ID = 20002
CREATED_ORIGINAL_ID = 10001

LOGIN_URL = "https://music.example.invalid/login/one-shot"


def json_result(
    command: Sequence[str],
    payload: Mapping[str, Any],
    *,
    returncode: int = 0,
    stderr: str = "",
) -> CommandResult:
    return CommandResult(
        tuple(command),
        returncode,
        json.dumps(payload, ensure_ascii=False),
        stderr,
    )


def text_result(
    command: Sequence[str],
    stdout: str,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> CommandResult:
    return CommandResult(tuple(command), returncode, stdout, stderr)


def login_status(logged_in: bool, message: str = "") -> dict[str, Any]:
    return {
        "success": logged_in,
        "message": message or ("已登录" if logged_in else "未登录，请执行 ncm-cli login 完成登录"),
    }


def login_prompt_payload(
    url: str = LOGIN_URL, message: str = "后台轮询已启动，请点击链接登录"
) -> dict[str, Any]:
    return {
        "success": True,
        "qrCodeUrl": url,
        "clickableUrl": url,
        "message": message,
    }


def created_payload(*, track_count: int = 3, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = records if records is not None else [
        {
            "originalId": CREATED_ORIGINAL_ID,
            "id": CREATED_ID,
            "name": "Synthetic Created List",
            "trackCount": 1,
            "specialType": 0,
        },
        {
            "originalId": FAVORITE_ORIGINAL_ID,
            "id": FAVORITE_ID,
            "name": "Synthetic Favorites",
            "trackCount": track_count,
            "specialType": 5,
        },
    ]
    return {
        "code": 200,
        "subCode": None,
        "message": None,
        "data": {"recordCount": len(rows), "records": rows},
    }


def track(number: int) -> dict[str, Any]:
    return {
        "originalId": 900000 + number,
        "id": f"{number:032X}",
        "name": f"Synthetic Track {number}",
        "duration": 180000,
        "artists": [{"name": f"Synthetic Artist {number}"}],
        "album": {"name": "Synthetic Album"},
        "liked": True,
    }


def tracks_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": 200, "subCode": "200", "message": None, "data": rows}


class FakeRunner:
    """可调用对象，满足 ``CommandRunner``；记录收到的每条完整 argv。"""

    def __init__(
        self,
        handler: Callable[[Sequence[str], float], CommandResult],
    ) -> None:
        self.handler = handler
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, command: Sequence[str], timeout: float) -> CommandResult:
        self.calls.append(list(command))
        self.timeouts.append(timeout)
        return self.handler(command, timeout)


class FakeClock:
    """``sleep`` 推进时间，让扫码轮询的测试不用真的等。"""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


def command_args(command: Sequence[str]) -> list[str]:
    """去掉 argv[0]（可执行文件），只看 ncm-cli 的参数。"""
    return list(command)[1:]


def is_check(command: Sequence[str]) -> bool:
    return command_args(command)[:2] == ["login", "--check"]


def is_login(command: Sequence[str]) -> bool:
    return command_args(command)[:1] == ["login"] and not is_check(command)


def is_created(command: Sequence[str]) -> bool:
    return command_args(command)[:2] == ["playlist", "created"]


def is_tracks(command: Sequence[str]) -> bool:
    return command_args(command)[:2] == ["playlist", "tracks"]


def option(command: Sequence[str], name: str) -> str:
    """取 ``--name value`` 里的 value（没有就空串）。"""
    args = command_args(command)
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return ""
