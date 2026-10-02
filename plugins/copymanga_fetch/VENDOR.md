# copymanga —— 随插件分发的第三方工具包

`plugins/copymanga_fetch/copymanga/` 是从外部项目 **逐字节复制** 过来的（版本 2.0.0，
纯标准库），不是本项目写的。放这里的理由：宿主的插件加载器只 exec 入口文件，
抓取逻辑没法既作为独立工具继续演进、又以「一个 .py 文件」的形式被插件 import。

规则只有一条：**不许改动这个目录里的任何代码**。

* 想改抓取行为，去改上游工具，然后把新版本原样复制过来；
* 插件这一侧（`plugin.py`）只负责配置、页面、后台任务、进度和导入，
  对工具的调用集中在 `ProgressClient`（转发 + 报进度）和 `build_export_config` 两处；
* 有一个测试（`tests/test_plugin_copymanga_fetch.py::test_vendored_tool_is_unmodified`）
  逐文件比对校验和，改动了就会红。

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
