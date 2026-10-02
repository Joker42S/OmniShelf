"""路径与运行期配置。

设计原则：
  * 一切路径使用 ``pathlib.Path``。
  * 没有全局可变状态；需要配置的地方通过参数注入 ``Settings``。
  * 环境变量只在这里读取，其它模块不直接碰 ``os.environ``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

ENV_DATA_DIR = "OMNISHELF_DATA_DIR"
ENV_MIGRATIONS_DIR = "OMNISHELF_MIGRATIONS_DIR"
ENV_FIXTURES_DIR = "OMNISHELF_FIXTURES_DIR"
ENV_PLUGINS_DIR = "OMNISHELF_PLUGINS_DIR"
ENV_PLUGINS_DATA_DIR = "OMNISHELF_PLUGINS_DATA_DIR"
ENV_GIT_BIN = "OMNISHELF_GIT_BIN"

DB_FILENAME = "collection.db"
ATTACHMENTS_DIRNAME = "attachments"
IMPORTS_DIRNAME = "imports"
LOGS_DIRNAME = "logs"
PLUGINS_DIRNAME = "plugins"
PLUGINS_DATA_DIRNAME = "plugins_data"

#: 内部 rating 上限；UI 展示层可另行缩放。内部统一 REAL 0-10，None = 未评分。
RATING_MAX = 10.0
RATING_MIN = 0.0


def package_root() -> Path:
    """``src/omnishelf`` 目录。"""
    return Path(__file__).resolve().parent


def repo_root() -> Path:
    """主 repo 根目录（包含 pyproject.toml 的那一层）。"""
    return Path(__file__).resolve().parents[2]


def env(name: str, default: str | None = None) -> str | None:
    """读环境变量，**优先 OMNISHELF_\***，兼容 1.0 之前用过的 ``COLLECTION_*``。

    正式名是 omnishelf，但老环境变量继续有效（含 ``COLLECTION_GIT_BIN`` 这种
    为了绕开系统 git 而设的），免得升级后脚本突然失效。
    """
    value = os.environ.get(name)
    if value is None and name.startswith("OMNISHELF_"):
        value = os.environ.get("COLLECTION_" + name[len("OMNISHELF_") :])
    return default if value is None else value


def default_data_dir() -> Path:
    configured = env(ENV_DATA_DIR)
    if configured:
        return Path(configured).expanduser().resolve()
    return repo_root() / "data"


def default_migrations_dir() -> Path:
    configured = env(ENV_MIGRATIONS_DIR)
    if configured:
        return Path(configured).expanduser().resolve()
    repo_migrations = repo_root() / "migrations"
    if repo_migrations.is_dir():
        return repo_migrations
    packaged = package_root() / "migrations"
    if packaged.is_dir():
        return packaged
    return repo_migrations


def default_fixtures_dir() -> Path:
    configured = env(ENV_FIXTURES_DIR)
    if configured:
        return Path(configured).expanduser().resolve()
    return repo_root() / "tests" / "fixtures"


@dataclass(frozen=True, slots=True)
class Settings:
    """一次运行所需的全部路径/参数。用 ``with_data_dir`` 派生新实例。"""

    data_dir: Path
    migrations_dir: Path
    fixtures_dir: Path
    plugins_dir: Path = field(default_factory=lambda: Path("plugins"))
    plugins_data_dir: Path = field(default_factory=lambda: Path("plugins_data"))
    host: str = "127.0.0.1"
    port: int = 8765
    rating_max: float = RATING_MAX
    git_bin: str = "git"

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / ATTACHMENTS_DIRNAME

    @property
    def imports_dir(self) -> Path:
        """导入的原始文件按来源存档的地方：``data/imports/<source>/<原文件名>``。"""
        return self.data_dir / IMPORTS_DIRNAME

    @property
    def logs_dir(self) -> Path:
        """宿主自己的运行日志：``data/logs/``（和 ``imports/`` 一样是宿主产物）。"""
        return self.data_dir / LOGS_DIRNAME

    @property
    def plugin_logs_dir(self) -> Path:
        """插件日志由宿主统一收集：``data/logs/plugins/<插件 id>.log``。"""
        return self.logs_dir / PLUGINS_DIRNAME

    def with_data_dir(self, data_dir: Path | str) -> Settings:
        return replace(self, data_dir=Path(data_dir).expanduser().resolve())

    def cover_rel_path(self, item_id: str, suffix: str = ".jpg") -> str:
        """封面相对 ``attachments/`` 的路径：ULID 前 4 位分桶 ``ab/cd/<ulid>.jpg``。"""
        if len(item_id) < 4:
            raise ValueError(f"item id 太短，无法分桶: {item_id!r}")
        bucket_a, bucket_b = item_id[0:2], item_id[2:4]
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        return f"{bucket_a}/{bucket_b}/{item_id}{suffix}"

    def cover_abs_path(self, rel_path: str) -> Path:
        return self.attachments_dir / rel_path


def load_settings(data_dir: Path | str | None = None) -> Settings:
    """读取环境变量并构造 ``Settings``。显式传入的 ``data_dir`` 优先。"""
    resolved = Path(data_dir).expanduser().resolve() if data_dir is not None else default_data_dir()
    git_bin = env(ENV_GIT_BIN, "git") or "git"
    plugins_dir = Path(env(ENV_PLUGINS_DIR) or (repo_root() / PLUGINS_DIRNAME)).expanduser().resolve()
    plugins_data_dir = Path(
        env(ENV_PLUGINS_DATA_DIR) or (repo_root() / PLUGINS_DATA_DIRNAME)
    ).expanduser().resolve()
    return Settings(
        data_dir=resolved,
        migrations_dir=default_migrations_dir(),
        fixtures_dir=default_fixtures_dir(),
        plugins_dir=plugins_dir,
        plugins_data_dir=plugins_data_dir,
        git_bin=git_bin,
    )
