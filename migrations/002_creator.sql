-- 002_creator.sql -- 把「作者/创作者」提升为通用列。
--
-- 背景：`author` 原本是 book/manga 的 extra 字段（music 用 `artist`），但它其实是
-- 所有 kind 通用的概念，且列表页要展示。现在统一成 items.creator（可为空）。
--
-- 注意：ALTER TABLE ... ADD COLUMN 没有 IF NOT EXISTS，这条迁移靠
-- PRAGMA user_version 保证只执行一次（迁移执行器只会跑版本号更高的文件）。
-- 下面的数据搬运是幂等的（第二次跑时 extra 里已经没有 author/artist 了）。

ALTER TABLE items ADD COLUMN creator TEXT;

-- 把既有的 extra.author / extra.artist 搬进新列，并从 extra 里移除（避免两处重复）
UPDATE items SET creator = json_extract(extra, '$.author')
 WHERE creator IS NULL AND json_extract(extra, '$.author') IS NOT NULL;

UPDATE items SET creator = json_extract(extra, '$.artist')
 WHERE creator IS NULL AND json_extract(extra, '$.artist') IS NOT NULL;

UPDATE items SET extra = json_remove(extra, '$.author')
 WHERE json_extract(extra, '$.author') IS NOT NULL;

UPDATE items SET extra = json_remove(extra, '$.artist')
 WHERE json_extract(extra, '$.artist') IS NOT NULL;
