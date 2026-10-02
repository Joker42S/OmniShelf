"""迁移：幂等、user_version、schema 内容。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import Session

from omnishelf.config import Settings, load_settings, repo_root
from omnishelf.db import (
    MigrationError,
    current_version,
    init_db,
    migration_files,
    run_migrations,
)


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def _indexes(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def test_init_creates_schema_and_version(settings: Settings) -> None:
    version = init_db(settings)
    assert version == 3
    assert current_version(settings.db_path) == 3
    assert settings.db_path.is_file()
    assert {
        "items",
        "external_ids",
        "tags",
        "import_logs",
        "pending_merges",
    } <= _tables(settings.db_path)
    # 002 加的通用列
    conn = sqlite3.connect(str(settings.db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    finally:
        conn.close()
    assert "creators" in columns
    assert {
        "idx_items_kind",
        "idx_items_status",
        "idx_items_year",
        "idx_external_ids_item_id",
        "idx_tags_tag",
    } <= _indexes(settings.db_path)


def test_init_is_idempotent(settings: Settings) -> None:
    first = init_db(settings)
    second = init_db(settings)
    third = run_migrations(settings)
    assert first == second == third == 3
    assert current_version(settings.db_path) == 3


def test_run_migrations_without_changes_keeps_version(settings: Settings) -> None:
    init_db(settings)
    before = current_version(settings.db_path)
    assert run_migrations(settings) == before


def test_current_version_on_missing_file(tmp_path: Path) -> None:
    assert current_version(tmp_path / "nope.db") == 0


def test_migration_files_sorted(settings: Settings) -> None:
    plan = migration_files(settings.migrations_dir)
    assert [version for version, _ in plan] == sorted(version for version, _ in plan)
    assert plan[0][0] == 1
    assert plan[0][1].name == "001_init.sql"


def test_bad_migration_filename(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    settings = load_settings(tmp_path / "data")
    broken = Settings(
        data_dir=settings.data_dir,
        migrations_dir=migrations,
        fixtures_dir=settings.fixtures_dir,
    )
    with pytest.raises(MigrationError):
        migration_files(migrations)
    with pytest.raises(MigrationError):
        run_migrations(broken)


def test_duplicate_migration_numbers(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (migrations / "001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError):
        migration_files(migrations)


def test_refuses_downgrade(settings: Settings) -> None:
    init_db(settings)
    conn = sqlite3.connect(str(settings.db_path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(MigrationError):
        run_migrations(settings)


def test_second_migration_applies(tmp_path: Path) -> None:
    """加一个 002 迁移，确认只执行新文件并推进版本号。"""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    base = load_settings(tmp_path / "data")
    settings = Settings(
        data_dir=base.data_dir,
        migrations_dir=migrations,
        fixtures_dir=base.fixtures_dir,
    )
    assert run_migrations(settings) == 1
    (migrations / "002_add_col.sql").write_text(
        "ALTER TABLE t ADD COLUMN note TEXT;", encoding="utf-8"
    )
    assert run_migrations(settings) == 2
    assert current_version(settings.db_path) == 2


def test_002_backfills_creator_from_extra(tmp_path: Path) -> None:
    """002 迁移把既有的 extra.author / extra.artist 搬进通用列 creator，并清理 extra。"""
    repo_migrations = repo_root() / "migrations"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    shutil.copy(repo_migrations / "001_init.sql", migrations / "001_init.sql")
    base = load_settings(tmp_path / "data")
    settings = Settings(
        data_dir=base.data_dir, migrations_dir=migrations, fixtures_dir=base.fixtures_dir
    )
    assert run_migrations(settings) == 1  # 只有 001 时停在 v1

    conn = sqlite3.connect(str(settings.db_path))
    conn.execute(
        "insert into items (id, kind, title, extra, created_at, updated_at) "
        "values ('01', 'manga', 'T', '{\"author\": \"某人\", \"volumes\": 3}', 'x', 'x')"
    )
    conn.execute(
        "insert into items (id, kind, title, extra, created_at, updated_at) "
        "values ('02', 'music', 'M', '{\"artist\": \"歌手\"}', 'x', 'x')"
    )
    conn.execute(
        "insert into items (id, kind, title, extra, created_at, updated_at) "
        "values ('03', 'manga', 'N', '{}', 'x', 'x')"
    )
    conn.commit()
    conn.close()

    shutil.copy(repo_migrations / "002_creator.sql", migrations / "002_creator.sql")
    assert run_migrations(settings) == 2

    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    rows = {row["id"]: row for row in conn.execute("select * from items")}
    conn.close()

    assert rows["01"]["creator"] == "某人"
    assert json.loads(rows["01"]["extra"]) == {"volumes": 3}  # author 已从 extra 移除
    assert rows["02"]["creator"] == "歌手"
    assert json.loads(rows["02"]["extra"]) == {}
    assert rows["03"]["creator"] is None  # 没有作者的就是空（creator 可为空）
    assert current_version(settings.db_path) == 2


def test_checkpoint_makes_wal_changes_visible_in_the_db_file(
    settings: Settings, session: Session, tmp_path: Path
) -> None:
    """WAL 里的改动必须能通过 checkpoint 落进主库文件（data-commit 依赖这一点）。"""
    from omnishelf import repository as repo
    from omnishelf.db import build_engine, checkpoint

    repo.create_item(session, kind="movie", title="WAL Test")
    session.commit()

    # 用连接池外的独立引擎再写一条，确保 WAL 里有内容
    engine = build_engine(settings)
    with engine.begin() as sql_conn:
        sql_conn.execute(
            text(
                "insert into items (id, kind, title, extra, created_at, updated_at) "
                "values ('01WALTEST0000000000000000', 'movie', 'WAL Two', '{}', 'x', 'x')"
            )
        )
    engine.dispose()

    checkpoint(settings)

    # 只拷主库文件（不带 -wal / -shm），内容必须完整
    copy = tmp_path / "only-main.db"
    shutil.copy(settings.db_path, copy)
    raw = sqlite3.connect(str(copy))
    try:
        titles = {row[0] for row in raw.execute("select title from items")}
    finally:
        raw.close()
    assert {"WAL Test", "WAL Two"} <= titles
    assert not (tmp_path / "only-main.db-wal").exists()


def test_checkpoint_on_missing_db_is_noop(tmp_path: Path) -> None:
    from omnishelf.db import checkpoint

    base = load_settings(tmp_path / "nope")
    checkpoint(base)  # 不该抛异常


def test_003_creators_becomes_a_json_list(tmp_path: Path) -> None:
    """003 迁移把单值 creator 变成多值 creators（JSON 数组），不做拆分。"""
    repo_migrations = repo_root() / "migrations"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name in ("001_init.sql", "002_creator.sql"):
        shutil.copy(repo_migrations / name, migrations / name)
    base = load_settings(tmp_path / "data")
    settings = Settings(
        data_dir=base.data_dir, migrations_dir=migrations, fixtures_dir=base.fixtures_dir
    )
    assert run_migrations(settings) == 2

    conn = sqlite3.connect(str(settings.db_path))
    conn.execute(
        "insert into items (id, kind, title, creator, created_at, updated_at) "
        "values ('01', 'manga', 'T', '某人 / 另一个人', 'x', 'x')"
    )
    conn.execute(
        "insert into items (id, kind, title, creator, created_at, updated_at) "
        "values ('02', 'movie', 'M', null, 'x', 'x')"
    )
    conn.commit()
    conn.close()

    shutil.copy(repo_migrations / "003_creators.sql", migrations / "003_creators.sql")
    assert run_migrations(settings) == 3

    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    rows = {row["id"]: row for row in conn.execute("select * from items")}
    columns = {row[1] for row in conn.execute("pragma table_info(items)")}
    conn.close()

    assert "creator" not in columns and "creators" in columns
    # 老值原样进数组（**不拆分**："A / B" 可能本来就是一个名字）
    assert json.loads(rows["01"]["creators"]) == ["某人 / 另一个人"]
    assert json.loads(rows["02"]["creators"]) == []
    assert current_version(settings.db_path) == 3
