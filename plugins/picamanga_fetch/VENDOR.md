# picamanga —— 由外部哔咔备份工具重构而来的工具包

`plugins/picamanga_fetch/picamanga/` 来自一个外部项目 **pica-backup**（aiohttp 异步版，
含 `client.py` / `fav_backup.py` / `cli.py` / `constants.py` / `docs/api-notes.md`）。
本目录是它的**重构版**：接口协议、签名算法、增量边界判定的思路都沿用上游，
代码、模块划分和文档格式按本项目 `plugins/copymanga_fetch/copymanga/` 的规范重写了一遍，
目录结构和文件名一一对应（`config.py` / `client.py` / `exporter.py` / `store.py` /
`models.py` / `errors.py` / `cli.py` / `__main__.py` / `__init__.py`）。

所以这里和 copymanga 那一份的规则**不同**：那份是逐字节复制、有校验和冻结测试；
这一份是我们自己的代码，改动走正常 review，没有「不许改动」的限制。

## 有意改掉的东西

| 上游 | 现在 | 为什么 |
|---|---|---|
| `async` + `aiohttp` | 同步、只用标准库 `urllib` | 宿主规定插件只能 import 标准库 + SDK；抓取本来就跑在插件的后台线程里，异步没有收益还要多带一个依赖 |
| `use_proxy` + `proxy_url` 两个参数（请求时 `kwargs["proxy"] = proxy_url`） | 一个 `proxy` 字段，出口收在 `PicamangaClient._open` 里（`build_opener(ProxyHandler({"http", "https"}))`） | 两个开关合成一个：给了地址就走，没给就不走（留空时 urllib 仍认 `HTTPS_PROXY` 环境变量）。标准库没有 aiohttp 那个 `proxy=` 参数，只能自己建 opener；显式的 `ProxyHandler` 会顶掉环境变量里那份，路由不会被半路换掉 |
| 十几个接口（浏览 / 搜索 / 排行 / 章节 / 图片 / 推荐 / 评论 / 点赞 / 签到） | 只留换令牌的 `/auth/sign-in` 和取收藏的 `/users/favourite` | 备份收藏夹只需要这两个。其余删干净，别留没人测的代码 |
| 下载封面图片 | 只带着封面地址走 | 入库的是元数据；抓图片会凭空多出几个 GB |
| 每个账号一个子目录 + `delta` / `jsonl` / `manifest` 记账 | 一份 `picamanga_favorites.json` 状态库 | 单用户单账号的场景，那套账本只是负担；「同步到哪了」由本地条目顺序 + 连续命中判定承担 |
| 自带 `config.json`（真实邮箱 + 口令）+ `persist_token` 写回 | 不读也不写任何凭据文件 | 凭据只从参数 / 环境变量 / 调用方传入，落盘交给插件的 `plugins_data/<id>/config.toml`（令牌会存，口令不存）。工具目录里不会凭空多出真实凭据 |
| 额外导出 csv / md / xlsx | 只有状态库；给 OmniShelf 的导入文件由插件生成 | 和 copymanga 插件同样的决定：多三种格式只是多三份要维护的序列化代码 |
| `incremental --verify` 选项 | `mode="full"` | 全量模式本来就是「翻完 + 标记取消收藏」，不需要第二个入口 |
| 用 `_id` 时间戳是否单调来判定「是不是按收藏日期排序」 | 改判「远端总数涨了、但头部一条新收藏都没找到」 | **这是关键的一处**。`_id` 前 4 字节是这本漫画的上架时间，和用户什么时候收藏它没有关系；上游文档自己也承认，把它的单调性当排序键会把「账号批量整理收藏夹」留下的痕迹误判成排序规则变了。误判的代价是每次增量都白跑一遍全量 |
| 模块名 `constants.py` / `FavouriteStore` / `FavouriteBackup` / `pica_*` | `config.py` / `JsonStore` / `FavoritesExporter` / `picamanga_*` | 和 copymanga 那份对齐；文件名和内置 importer `picamanga` 同名，一眼看得出对应关系 |

上游 `docs/api-notes.md` 里的实测结论（签名要用**去掉前导斜杠**的路径、签名不对时服务端会回
一个假的 `{"code":200}` 空响应、单页固定 20 条、认证错误码 `1005`/`1008`、增量为什么不能
「命中一条就停」）都写进了各模块的注释里。那份文档里作者账号的**真实收藏条数**没有复制过来。

## 目录里的默认路径

`config.DEFAULT_DATA_DIR` 是相对本包算的 `plugins/picamanga_fetch/data/`。
插件用不到它（`build_export_config` 把 `data_dir` 指到 `plugins_data/picamanga_fetch/`），
但**别在插件目录里直接跑 `python -m picamanga`**——那样真实收藏就落在这个目录里。
主仓库的 `.gitignore` 有通用的 `data/` 规则作为第二道保险。

## 文件一览

| 文件 | 作用 |
|---|---|
| `__init__.py` | 公开符号 + `fetch_favourites()` |
| `__main__.py` / `cli.py` | 工具的命令行入口（插件不用，页面/CLI 走库接口） |
| `config.py` | `PicamangaConfig`、签名常量、模式/排序取值、`load_config()` |
| `client.py` | HMAC 签名、请求重试、登录换令牌、翻页取收藏夹 |
| `exporter.py` | `FavoritesExporter`：增量/全量同步 + 顺序前提校验 + 取消收藏标记 |
| `store.py` | `JsonStore`：本地状态库（扫描顺序、取消收藏标记、远端总数） |
| `models.py` | `ComicItem`（含 `to_export_dict()`，对齐内置 importer 的 19 个字段）/ `SyncReport` |
| `errors.py` | 异常类型 |
| `VENDOR.md` | 本文件 |
