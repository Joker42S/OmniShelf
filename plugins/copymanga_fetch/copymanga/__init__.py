"""copymanga —— 拷貝漫畫（copy4000.com）个人收藏导出工具包。

对外公开 API 见 ``docs/API.md``，典型用法::

    from copymanga import CopymangaClient, FavoritesExporter, ExportConfig

    cfg = ExportConfig(host="https://copy4000.com", mode="incremental",
                       token="<你的 token>")
    client = CopymangaClient(cfg)
    exporter = FavoritesExporter(client)
    report = exporter.run()
    print(report.added, report.updated)
"""

from .client import CopymangaClient
from .config import DEFAULT_HOST, ExportConfig, load_config
from .errors import ApiError, AuthError, ConfigError, CopymangaError
from .exporter import FavoritesExporter
from .models import CollectItem, SyncReport
from .store import JsonStore

__version__ = "2.0.0"

__all__ = [
    "__version__",
    # 配置
    "ExportConfig",
    "load_config",
    "DEFAULT_HOST",
    # 客户端
    "CopymangaClient",
    # 编排
    "FavoritesExporter",
    "JsonStore",
    # 数据模型
    "CollectItem",
    "SyncReport",
    # 异常
    "CopymangaError",
    "AuthError",
    "ApiError",
    "ConfigError",
    # 便捷函数
    "fetch_favorites",
]


def fetch_favorites(**kwargs):
    """一行代码拿收藏列表（不落地文件）。

    等价于::

        cfg = ExportConfig(**kwargs)
        client = CopymangaClient(cfg)
        return list(client.iter_collects())

    支持的参数与 :class:`~copymanga.config.ExportConfig` 一致，
    常用：``token`` / ``cookie`` / ``username`` / ``password`` /
    ``host`` / ``page_size`` / ``timeout``。

    :return: ``list[CollectItem]``
    """
    cfg = ExportConfig(**kwargs).validate()
    with CopymangaClient(cfg) as client:
        return list(client.iter_collects())
