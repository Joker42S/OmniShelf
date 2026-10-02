# kind_chart（插件模板 / 示例）

按 `kind` 统计条目数量并画成条状图；顺带演示插件系统的全部要点（继承 `Plugin`、
配置、持久化、自己的页面、表单提交、`start`/`stop` 生命周期）。

当作模板用：复制这个目录改名，改 `metadata.toml` 里的 `id`/`title`/`description`，
然后改 `plugin.py` 里的 `class KindChart(Plugin)` 与 `templates/page.html`。

* 命令行：`omnishelf plugin kind_chart top --limit 5`（演示 `@cmd`）
* 代码进 git：`plugins/kind_chart/`
* 配置与数据不进 git：`plugins_data/kind_chart/`（`config.toml`、`state.json`）

开发规范见 `docs/plugin-development.md`。
