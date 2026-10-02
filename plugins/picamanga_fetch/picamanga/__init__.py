"""picamanga —— 哔咔（picacomic）个人收藏夹导出工具包。

典型用法::

    from picamanga import FavoritesExporter, PicamangaClient, PicamangaConfig

    cfg = PicamangaConfig(host="https://picaapi.picacomic.com",
                          mode="incremental", token="<你的令牌>")
    exporter = FavoritesExporter(PicamangaClient(cfg))
    report = exporter.sync()
    print(report.added, report.updated)

只想拿列表、不落文件::

    from picamanga import fetch_favourites

    items = fetch_favourites(email="me@example.com", password="...")
"""

from __future__ import annotations

from typing import List

from .client import PicamangaClient
from .config import BASE_URL, PicamangaConfig, load_config
from .errors import ApiError, AuthError, ConfigError, PicamangaError
from .exporter import FavoritesExporter
from .models import ComicItem, SyncReport
from .store import JsonStore

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # 配置
    "PicamangaConfig",
    "load_config",
    "BASE_URL",
    # 客户端
    "PicamangaClient",
    # 编排
    "FavoritesExporter",
    "JsonStore",
    # 数据模型
    "ComicItem",
    "SyncReport",
    # 异常
    "PicamangaError",
    "AuthError",
    "ApiError",
    "ConfigError",
    # 便捷函数
    "fetch_favourites",
]


def fetch_favourites(**kwargs) -> List[ComicItem]:
    """一行代码拿收藏夹列表（不落地文件）。

    等价于::

        cfg = PicamangaConfig(**kwargs)
        with PicamangaClient(cfg) as client:
            return list(client.iter_comics())

    支持的参数与 :class:`~picamanga.config.PicamangaConfig` 一致，
    常用：``token`` / ``email`` / ``password`` / ``host`` / ``sort`` / ``timeout`` / ``proxy``。

    :return: ``list[ComicItem]``
    """
    cfg = PicamangaConfig(**kwargs).validate()
    with PicamangaClient(cfg) as client:
        return list(client.iter_comics())
