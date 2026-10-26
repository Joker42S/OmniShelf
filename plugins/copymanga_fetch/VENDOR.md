# copymanga —— 随插件分发的第三方工具包

`plugins/copymanga_fetch/copymanga/` 是从外部项目 **逐字节复制** 过来的（版本 2.0.0，
纯标准库），不是本项目写的。放这里的理由：宿主的插件加载器只 exec 入口文件，
抓取逻辑没法既作为独立工具继续演进、又以「一个 .py 文件」的形式被插件 import。

规则只有一条：**不许偷偷改动这个目录里的代码**。

* 想改抓取行为，优先去改上游工具，然后把新版本原样复制过来；
* 需要工具配合什么，先在插件这一侧绕：进度靠套一层 client（`ProgressClient`），
  令牌自己存（`persist_token=False` + 插件的 `config.toml`）；
* 绕不过去、非改工具不可的（**代理就只能改在 HTTP 出口那**——`request()` 里直接调
  `urlopen`，外面没有任何可注入的口子），改完必须记进下面那张「本地补丁」表，
  并同步 `tests/test_plugin_copymanga_fetch.py` 里的哈希；
* 有一个测试
  （`tests/test_plugin_copymanga_fetch.py::test_vendored_tool_only_carries_the_documented_local_patch`）
  逐文件比对校验和：没记档的文件保持与上游逐字节一致，记了档的文件比对补丁后的哈希，
  多改一行就会红。

## 本地补丁

| 补丁 | 动了哪些文件 | 上游 | 现在 | 为什么 |
|---|---|---|---|---|
| 代理 | `config.py` / `client.py` / `cli.py` / `__init__.py`（docstring 一行） | 只有 `urllib.request.urlopen(req, timeout, context)` 一处直连，没有任何代理参数 | `ExportConfig.proxy` 字段 + `proxy_target` 属性（`127.0.0.1:10809` 这种裸地址按 HTTP 代理补 `http://`），HTTP 出口收进 `CopymangaClient._open`：填了代理就只走显式 `ProxyHandler`（顶掉环境变量那份），留空仍走 urllib 默认（认 `HTTPS_PROXY`）；`--proxy` / `COPYMANGA_PROXY` 一并加上 | 上游域名在不少网络环境里直连就超时（2026-10-25 靠临时挂 `HTTPS_PROXY` 才跑通）。口径和 `picamanga` / `steam_client` 对齐：一个 `proxy` 字段，别做「代理不通自动退回直连」那种两头不讨好的兜底 |

上游那 6 个没在表里的文件（`__main__.py` / `errors.py` / `exporter.py` / `formats.py` /
`models.py` / `store.py`）依旧逐字节等于上游 v2.0.0。

上游工具自带 `README.md` / `docs/API.md`（接口协议、`ExportConfig` 字段表、八种用法示例），
**没有**跟着复制过来——那些文档的示例里嵌了真实的 token 和账号密码。需要看的话去原项目。
同理，原项目目录里的 `config.json`（真实凭据）和 `data/`（真实收藏）也不进仓库。

工具还有自己的**默认目录**（相对它所在的包算）：`plugins/copymanga_fetch/data/` 和
`plugins/copymanga_fetch/config.json`。插件用不到它们（`build_export_config` 把 `data_dir`
指到 `plugins_data/<id>/`，`config_path` 干脆不填、`persist_token=False`），
但**别在插件目录里直接跑 `python -m copymanga`**——那样真实收藏和真实凭据就落在这两份路径里。
主仓库的 `.gitignore` 把这两个路径都屏蔽了（`data/` 是通用规则，`config.json` 点名），作为第二道保险。

## 复制了哪些文件

| 文件 | 作用 |
|---|---|
| `__init__.py` | 公开符号 + `fetch_favorites()` |
| `__main__.py` / `cli.py` | 工具的命令行入口（插件不用，页面/CLI 走库接口） |
| `config.py` | `ExportConfig`、模式/排序/接口路径常量、`load_config()` |
| `client.py` | HTTP、token 校验、账号密码登录、分页 |
| `exporter.py` | `FavoritesExporter`：增量/全量同步 + 排序不变量校验 + 导出 |
| `store.py` | `JsonStore`：本地状态库（水位线、取消收藏、书架位次） |
| `models.py` | `CollectItem` / `SyncReport` |
| `formats.py` | JSON / CSV / Markdown / Excel 写出（xlsx 需要 openpyxl） |
| `errors.py` | 异常类型 |
