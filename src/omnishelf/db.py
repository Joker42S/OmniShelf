"""引擎、会话与基于 ``PRAGMA user_version`` 的迁移执行器。

迁移不是 ``create_all()``：``migrations/NNN_name.sql`` 按编号顺序执行，
执行成功后把 ``PRAGMA user_version`` 推进到该编号。
SQL 文件内部语句保持幂等（IF NOT EXISTS），因此重复执行是安全的。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, event
from sqlmodel import Session, create_engine

from .config import Settings

_MIGRATION_RE = re.compile(r"^(\d+)_[A-Za-z0-9_\-]+\.sql$")


class MigrationError(RuntimeError):
    """迁移文件缺失、编号重复或 SQL 执行失败。"""


def migration_files(migrations_dir: Path) -> list[tuple[int, Path]]:
    """返回 ``[(version, path), ...]``，按 version 升序。"""
    if not migrations_dir.is_dir():
        raise MigrationError(f"迁移目录不存在: {migrations_dir}")
    found: dict[int, Path] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if match is None:
            raise MigrationError(f"迁移文件名不符合 NNN_name.sql: {path.name}")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"迁移编号重复 {version}: {found[version].name} / {path.name}")
        if version <= 0:
            raise MigrationError(f"迁移编号必须为正整数: {path.name}")
        found[version] = path
    if not found:
        raise MigrationError(f"迁移目录里没有 .sql 文件: {migrations_dir}")
    return sorted(found.items())


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def current_version(db_path: Path) -> int:
    """当前 schema 版本；库文件不存在时返回 0。"""
    if not db_path.exists():
        return 0
    conn = _connect(db_path)
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def run_migrations(settings: Settings) -> int:
    """把库推进到最新版本，返回最终版本号。重复调用是幂等的。"""
    plan = migration_files(settings.migrations_dir)
    latest = plan[-1][0]
    version = current_version(settings.db_path)
    if version > latest:
        raise MigrationError(
            f"数据库版本 {version} 高于代码里的最新迁移 {latest}，拒绝降级。"
        )
    pending = [(v, p) for v, p in plan if v > version]
    if not pending:
        return version

    conn = _connect(settings.db_path)
    try:
        for target, path in pending:
            sql = path.read_text(encoding="utf-8")
            try:
                conn.executescript(sql)
            except sqlite3.Error as exc:  # pragma: no cover - 取决于 SQL 内容
                raise MigrationError(f"迁移 {path.name} 执行失败: {exc}") from exc
            conn.execute(f"PRAGMA user_version = {int(target)}")
            conn.commit()
    finally:
        conn.close()
    return latest


def init_db(settings: Settings) -> int:
    """建目录 + 执行迁移。CLI ``init`` 与测试都走这里。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.attachments_dir.mkdir(parents=True, exist_ok=True)
    return run_migrations(settings)


def checkpoint(settings: Settings) -> None:
    """把 WAL 里的改动合并进主库文件。

    WAL 是独立的 ``-wal`` 文件，而 ``data/.gitignore`` 忽略它——如果提交 data/ 之前
    不落盘，``collection.db`` 的快照可能缺少还在 WAL 里的改动。``data-commit`` 会先调这个。
    """
    if not settings.db_path.exists():
        return
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def build_engine(settings: Settings, *, echo: bool = False) -> Engine:
    """构造 SQLite 引擎。``:memory:`` 也可以，用于极端场景的单元测试。"""
    if str(settings.data_dir) == ":memory:":
        url = "sqlite://"
    else:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{settings.db_path.as_posix()}"
    engine = create_engine(url, echo=echo, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.close()

    return engine


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """事务性会话：正常提交，异常回滚。"""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
