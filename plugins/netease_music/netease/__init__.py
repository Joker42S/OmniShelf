"""netease —— 网易云音乐 ncm-cli 封装。

对外只有 :class:`NeteaseClient` 和它返回的几种数据类型 / 异常::

    from netease import NeteaseClient

    client = NeteaseClient()
    client.login(on_prompt=print)              # 已登录直接返回；否则打印链接并等待
    playlist = client.favorite_playlist()      # 收藏歌单的加密 id
    page = client.favorite_tracks(2, 100)      # 第 2 页，每页 100 首
    everything = client.favorite_tracks()      # 不传页码 = 全量
    fresh = client.fetch_playlist_tracks(      # 增量：遇到整页已知歌曲就停
        playlist.id, known=lambda row: row.get("originalId") in known_ids
    )

前提是 ncm-cli 已安装并在它自己的配置里保存了 appid / 私钥；本包不接触任何凭据。
"""

from __future__ import annotations

from .client import (
    DEFAULT_LOGIN_TIMEOUT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT,
    MAX_PAGE_SIZE,
    CommandResult,
    CommandRunner,
    NeteaseClient,
    TrackFetch,
    run_ncm_cli,
)
from .errors import (
    NcmCliError,
    NcmCliNotFound,
    NcmCliOutputError,
    NeteaseApiError,
    NeteaseError,
    NeteaseLoginError,
    NeteaseLoginTimeout,
)
from .models import LoginPrompt, LoginResult, LoginStatus, Playlist

__all__ = [
    "CommandResult",
    "CommandRunner",
    "DEFAULT_LOGIN_TIMEOUT",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_TIMEOUT",
    "MAX_PAGE_SIZE",
    "LoginPrompt",
    "LoginResult",
    "LoginStatus",
    "NcmCliError",
    "NcmCliNotFound",
    "NcmCliOutputError",
    "NeteaseApiError",
    "NeteaseClient",
    "NeteaseError",
    "NeteaseLoginError",
    "NeteaseLoginTimeout",
    "Playlist",
    "TrackFetch",
    "run_ncm_cli",
]
