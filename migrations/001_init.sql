-- 001_init.sql -- 初始 schema。
--
-- 约定：
--   * 所有时间戳为 ISO8601 UTC 字符串，由 repository 层维护。
--   * 所有 id 为 ULID（26 字符文本），不使用自增主键。
--   * 本文件中的语句必须保持幂等（IF NOT EXISTS），因为迁移以
--     sqlite3.executescript 执行，成功后才会推进 PRAGMA user_version。

CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    subtitle    TEXT,
    year        INTEGER,
    status      TEXT,
    rating      REAL,
    notes       TEXT,
    cover_path  TEXT,
    extra       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT
);

CREATE TABLE IF NOT EXISTS external_ids (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    url         TEXT,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS tags (
    item_id TEXT NOT NULL,
    tag     TEXT NOT NULL,
    PRIMARY KEY (item_id, tag)
);

CREATE TABLE IF NOT EXISTS import_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    started_at  TEXT,
    finished_at TEXT,
    total       INTEGER,
    inserted    INTEGER,
    updated     INTEGER,
    skipped     INTEGER,
    errors      TEXT
);

CREATE TABLE IF NOT EXISTS pending_merges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT,
    candidate_id TEXT,
    reason       TEXT,
    created_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_kind ON items (kind);
CREATE INDEX IF NOT EXISTS idx_items_status ON items (status);
CREATE INDEX IF NOT EXISTS idx_items_year ON items (year);
CREATE INDEX IF NOT EXISTS idx_external_ids_item_id ON external_ids (item_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags (tag);
