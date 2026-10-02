-- 003_creators.sql -- 创作者从单值改成多值（JSON 数组）。
--
-- 背景：一个作品经常有多个创作者（ASMR 的「社团 + 多个声优」、
-- 图书的「作者 A、作者 B」……），单列字符串拼起来不利于按作者筛选。
-- 现在 items.creators 是 JSON 数组（元素是字符串），空数组表示「没有创作者」。
--
-- 老数据：已有的 creator 字符串原样放进数组的第一个元素（**不做拆分**：那时候
-- 拆没拆是来源决定的，这里乱猜会把「A / B」这种本来就是一个名字的情况切坏）。
--
-- 注意：SQLite 的 ADD COLUMN 不能带 IF NOT EXISTS，靠 PRAGMA user_version 保证只跑一次。

ALTER TABLE items ADD COLUMN creators TEXT NOT NULL DEFAULT '[]';

UPDATE items
   SET creators = json_array(creator)
 WHERE creator IS NOT NULL AND trim(creator) <> '';

ALTER TABLE items DROP COLUMN creator;
