# 插件开发指南与规范

插件用来给 OmniShelf 加功能：抓某个来源的收藏、做数据可视化、批量清洗、导出成别的格式……
宿主（本体程序）负责存储、路由、页面骨架；插件负责「额外那件事」。

现成模板：**`plugins/kind_chart/`**（分类统计条状图），本文所有例子都来自它。
想开工最省事的办法：

```bash
cp -r plugins/kind_chart plugins/my_plugin
$EDITOR plugins/my_plugin/metadata.toml     # 改 id / title / description
$EDITOR plugins/my_plugin/plugin.py         # 写 class MyPlugin(Plugin): ...
# 刷新 http://127.0.0.1:8765/plugins 就能看到（热重载，不用重启）
```

---

## 1. 目录与文件约定

```
plugins/                        # 插件代码：进主仓库、进 git
└── my_plugin/                  # 一个目录 = 一个插件
    ├── metadata.toml           # 必须：插件说明书 + 加载入口声明
    ├── plugin.py               # 默认入口文件（可用 metadata 里的 entry 改名）
    ├── templates/              # 可选：自己的 Jinja 模板（用 self.render() 渲染）
    │   └── page.html
    ├── static/                 # 可选：静态文件（目前不会自动挂载，先内联）
    └── README.md               # 可选：给人看的说明

plugins_data/                   # 插件运行时数据：**不进 git**（已在 .gitignore）
└── my_plugin/
    ├── config.toml             # self.config / self.save_config() 读写
    └── state.json              # self.read_state() / self.write_state() 读写
```

* 目录名默认就是插件 id（`metadata.toml` 里可以显式写 `id`）。
* 以 `.` 或 `_` 开头的目录、以及**没有 `metadata.toml` 的目录**会被忽略（可以放心放文档/素材）。
* 运行时数据**只能**写到 `self.data_dir`（也就是 `plugins_data/<id>/`）；插件目录里的东西
  视为只读代码，重建容器/重新 clone 时会没有。

### 插件带自己的目录结构

`plugin.py` 只是**入口**，插件大到自己想拆模块完全可以拆——`plugins/copymanga_fetch/` 和
`plugins/picamanga_fetch/` 里就各放着一份自己的抓取工具包（`copymanga/`、`picamanga/`，
这些子目录没有 `metadata.toml`，所以不会被当成插件）。两条注意：

* **入口文件是被 `exec` 的，插件目录不在 `sys.path` 上**，所以要用同目录的包得先挂一下：

  ```python
  _PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
  if _PLUGIN_DIR not in sys.path:
      sys.path.insert(0, _PLUGIN_DIR)

  from copymanga import CopymangaClient, ExportConfig   # noqa: E402
  ```

* **随插件分发的第三方/外部工具有别改**：要它配合什么，就在插件这一侧绕
  （`copymanga_fetch` 要每页进度，就往 client 外面套一层记录进度的壳——那套包只按名字用 client，
  换成谁都能跑）。真要改工具才办得到的事，宁可停下来重新评估。放一份 `VENDOR.md` 写清来源版本、
  哪些文件没带进来，再用一个「逐个文件哈希」的测试把「一行没改」钉死。
* **例外：上游那份根本不合规矩时，改成规范的样子，但要说清改了啥**。`picamanga_fetch` 拿到的
  是一份脚本式的备份工具（读 `config.json` 里的真实凭据、自己落盘令牌、模块名随手乱起），
  只能重构成和 `copymanga` 同一套目录/文档规范才能用。这种「不是逐字节复制」的包就别用哈希冻结了
  ——它由自己的测试覆盖，`VENDOR.md` 里逐条记下方差（改了哪些、删了哪些能力、为什么）。
* **凭据和真实数据不要跟着进来**：上游目录里的 `config.json`、`data/`、示例文档常常带着
  真实 token 和真实收藏，只拷代码，别拷那些。

### metadata.toml

```toml
[plugin]
id = "my_plugin"          # 可选，默认取目录名
title = "我的插件"         # 展示名（默认取 id）
version = "0.1.0"         # 可选
description = "一句话说明"  # 可选，列表页会显示
author = "你的名字"        # 可选
homepage = "https://…"    # 可选
entry = "plugin.py"       # 可选，入口文件（相对插件目录）；可写 "plugin.py:MyPlugin" 指定类名
requires = ["query"]      # 可选，声明用到的宿主接口：query / import / audio（见第 4 节）
```

一段最小可用的 `metadata.toml` 就一行：`[plugin]`（标题会退回目录名）。

---

## 2. 插件基类与生命周期

插件就是一个类：**继承 `Plugin`（在 `omnishelf.plugins.api` 里），实现 `start()` / `stop()`**。

```python
# plugins/my_plugin/plugin.py
from omnishelf.plugins.api import Plugin


class MyPlugin(Plugin):
    def start(self) -> None:
        self.log("准备好了")          # 日志会显示在插件列表页

    def stop(self) -> None:
        self.log("收工")

    def render_page(self, request=None) -> str:      # 可选：插件自己的页面
        return "<p>你好</p>"

    def handle_post(self, request=None, form=None):  # 可选：处理页面上的表单
        return None                                  # None = 跳回页面
```

| 成员 | 必须 | 何时调用 | 说明 |
| --- | --- | --- | --- |
| `start()` | **是**（抽象方法） | 插件加载时（进程启动 / 首次访问 / 重载后） | 读配置、做准备、注册东西 |
| `stop()` | **是**（抽象方法） | 卸载 / 重载前 / 进程退出时 | 释放资源、做收尾 |
| `render_page(request)` | 否 | 打开 `/plugins/<id>` 时 | 返回 HTML 片段或整页；不实现就没有页面 |
| `handle_post(request, form)` | 否 | 插件页面里提交表单时（POST `/plugins/<id>`） | 返回 HTML 直接回显；返回 `None` 跳回页面 |

* `start`/`stop` 是**抽象方法**：没实现的话插件实例化就会失败，列表页会告诉你缺哪个方法
  （不会再出现「跑起来才发现少了一个函数」）。
* `start` 里抛异常 → 该插件状态为出错，**不影响其它插件**。
* `stop` 里抛异常会被记录，但不会阻断卸载流程。
* 子类如果自定义 `__init__`，**必须先调用** `super().__init__(ctx)`。
* 一个入口文件里只有一个 `Plugin` 子类时，宿主自动用它；有多个就必须在 metadata 里写明：
  `entry = "plugin.py:MyPlugin"`。

## 3. `ctx` 给你什么

基类把 `ctx` 包了一层：下面这些都可以直接 `self.xxx` 用（内部就是 `self.ctx.xxx`），
`self.ctx` 本身也随时可用。想自己封装状态就直接写 `self.anything`。

`ctx` 的类型定义在 `src/omnishelf/plugins/context.py`，从 `omnishelf.plugins.api` 可以 import 到。

| 成员 | 说明 |
| --- | --- |
| `self.plugin_id` / `self.title` | 自己的 id / 标题 |
| `self.code_dir` | 插件代码目录（只读） |
| `self.data_dir` | `plugins_data/<id>/`（可写，会自动建目录） |
| `self.host` | **宿主提供的数据查询接口**（只读，见下节） |
| `self.config` | 配置 dict（`plugins_data/<id>/config.toml`，加载时自动读好） |
| `self.save_config(dict)` | 覆盖保存配置（TOML；字符串/数字/布尔/数组都能写） |
| `self.ctx.load_config()` | 重新从磁盘读配置 |
| `self.read_state(name="state.json", default=None)` | 读自己的 JSON 数据 |
| `self.write_state(data, name="state.json")` | 写自己的 JSON 数据 |
| `self.state_file(name)` | 拿自己的数据文件路径（想用 csv/sqlite 也行） |
| `self.render("page.html", **values)` | 用插件 `templates/` 里的模板渲染，模板里能直接 `{% raw %}{{ ctx }}{% endraw %}`、`{% raw %}{{ plugin }}{% endraw %}` |
| `self.log("…")` | 记一条日志：**同时**进两处——内存里最近 200 条（插件列表页可展开）和宿主的 `data/logs/plugins/<id>.log`（`/logs` 页实时看，重启也还在） |

配置示例（`kind_chart` 的做法，注意**默认值 + 兜底**，用户把配置写坏了也不能炸）：

```python
DEFAULTS = {"bar_color": "#3b82f6", "max_bars": 12}

def effective_config(self):
    config = dict(DEFAULTS)
    config.update(self.config or {})         # 用户配置覆盖默认
    try:
        config["max_bars"] = max(1, min(int(config["max_bars"]), 50))
    except (TypeError, ValueError):
        config["max_bars"] = DEFAULTS["max_bars"]
    return config
```

---

## 4. 宿主暴露的接口：查询（`query`）+ 导入（`import`）+ 音频（`audio`）

方法返回的都是普通 `dict`/基本类型（不是 ORM 对象），所以你不用（也不该）依赖宿主的
数据库结构。方法全部同步、自己开短会话，直接调就行。

### 4.1 查询（`requires = ["query"]`）

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `self.host.stats()` | `{"total", "deleted", "by_kind", "by_status", "with_cover", "tags", "pending_merges"}` | 全局汇总，`by_status` 里「没有状态」那档的键是 `"unset"` |
| `self.host.kinds()` | `[{"name", "label", "fields": [...]}]` | 所有 kind 及其中文名、专属字段定义 |
| `self.host.statuses()` | `[{"name", "label"}]` | 可选状态（含 `unset`） |
| `self.host.all_tags()` | `["科幻", …]` | 全部标签（排序好的） |
| `self.host.count_items(**filters)` | `int` | 按条件数条目 |
| `self.host.list_items(**filters)` | `[dict, …]` | 按条件取条目（默认 100 条，单次最多 500 条） |
| `self.host.get_item(item_id)` | `dict` 或 `None` | 取单条 |

`filters` 与列表页/CLI 是同一套：`kind`、`status`、`tag`、`title_prefix`、`search`、`creator`（多值创作者里任意一个命中）、
`rating_min`、`rating_max`、`created_from`、`created_to`、`tags_include`、`tags_exclude`、
`sort`（`title`/`rating`/`year`/`created_at`/`updated_at`）、`descending`、`limit`、`offset`。
不认识的键会被忽略（不会把你的插件搞崩）。

```python
def render_page(self, request=None):
    stats = self.host.stats()
    labels = {k["name"]: k["label"] for k in self.host.kinds()}
    top_movies = self.host.list_items(kind="movie", rating_min=9, sort="rating", limit=5)
    return f"<p>共 {stats['total']} 条，高分电影 {len(top_movies)} 部</p>"
```

### 4.2 导入（`requires = ["import"]`）

抓来的数据要入库，就调宿主的导入接口——**走的是和 CLI / 上传页完全一样的链路**：
来源级判重、写 `import_logs`、原始文件存档，插件不需要（也不应该）自己去碰数据库。

```python
def handle_post(self, request=None, form=None):
    snapshot = self.data_dir / "douban_movie_collect.json"
    result = self.import_file("douban_movies", snapshot)   # 等价于 self.host.import_file(...)
    self.log(f"新增 {result['inserted']}，错误 {result['error_count']}")
    return f"<p>新增 {result['inserted']} 条</p>"
```

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `self.import_file(importer, path, *, archive=True)` | `dict` | 用内置 importer 导入一个文件；`archive=True` 时把原始文件按来源存档到 `data/imports/<来源>/` |
| `self.importer_names()` | `["douban_movies", …]` | 宿主当前注册的 importer 名字 |

返回的摘要含：`total` / `inserted` / `updated` / `skipped` / `unchanged` / `error_count` /
`warning_count` / `errors` / `warnings` / `log_id` / `archive_path` / `summary`。
文件不存在 → `FileNotFoundError`；importer 名字写错 → `ValueError`（消息里会列出可用的名字）。
导入本身**不会因为单条记录错误而失败**（记录级错误进 `errors`），所以拿到 200 不代表 0 错误，
要自己看 `error_count`。

> 需要发网络请求、注册定时任务时怎么办？这些接口还没开（`net` / `tasks`）。
> 抓取类的插件现在可以自己用标准库 `urllib` 做（`douban_fetch` 和 `asmr_playlist` 就是这么干的，
> 后者还演示了「配置里持久化 base url / id / 代理」和「调用内置 importer 直接入库」），
> 也可以像 `copymanga_fetch` / `picamanga_fetch` 那样带一份自己的抓取工具包（见第 1 节），等 `net` 接口开了再切过去。
> 想清楚插件到底需要什么，宿主侧补齐接口后再用
> （插件只依赖 SDK，宿主加接口时你的插件不用动）。

**抓取类插件的代理按同一套约定做**（`asmr_playlist`、`picamanga_fetch` 是现成的样子）：
配置项叫 `proxy`，留空 = 不在代码里指定（urllib 仍认 `HTTP(S)_PROXY` 环境变量，没设就是直连）；
只写 `IP:端口` 补成 `http://`，SOCKS 要用户自己写全协议头；认不出来的地址**当场报错**，
别悄悄换成直连——那一栏本来就是给「直连不通」用的，静默降级只会剩下一句莫名的失败。
请求出口收在一个函数里（`asmr_playlist.open_response` / `PicamangaClient._open`），
配了代理就 `build_opener(ProxyHandler(...))`：显式的 handler 会顶掉环境变量里那份，路由不会被半路换掉。
代理地址不是凭据，可以和站点地址一起存进配置；报告里要有一行写这次走的哪条路。
唯一没做这项的是 `copymanga_fetch`——它那份工具是**逐字节冻结**的外部代码，加不进去，
需要代理时用环境变量（`HTTPS_PROXY=http://127.0.0.1:10809 omnishelf plugin copymanga_fetch …`）。

### 4.3 音频（`requires = ["audio"]`）

「哪些后缀算音乐、哪些目录要跳过、没标签时标题从哪来」这套口径**在宿主这边**，
插件只负责「挑哪些路径」和「把结果讲清楚」。所以插件扫出来的和
`omnishelf scan-music` 扫出来的必然一致，两边不会各长一套规则。

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `self.audio_suffixes()` | `[".aac", ".flac", …]` | 宿主认作音乐文件的后缀（选择框/说明用同一份名单） |
| `self.scan_audio(targets, *, out_path, progress=None)` | `dict` | 深度扫描这些目录/文件并把清单写成 JSON；`out_path` 请落在 `self.data_dir` 里（那是插件唯一可写的目录） |

摘要含：`manifest_path` / `roots` / `scanned` / `item_count` / `tagless` / `failed` /
`errors` / `errors_hidden` / `started_at` / `finished_at` / `summary`。
拿到 `manifest_path` 就能 `self.import_file("music_files", …)` 入库——扫描是 `audio`、
入库是 `import`，两个接口配合着用一个。

`progress` 每读一个文件回调一次 `(已读个数, 当前路径)`。几千首歌要读几十秒到几分钟，
**别在请求线程里跑**（起后台线程 + 进度条，写法见第 5 节）。

现成的样子是 `plugins/music_import/`，它顺手演示了**怎么弹系统选择框**：

* 标准库 `subprocess` 起 macOS 的 `osascript`（`choose folder` /
  `choose file with multiple selections allowed`），argv 固定、**不经过 shell**；
* 用户按「取消」是 `-128`：那不是失败，页面什么都不用显示；
* 给一个超时（这一栏等的是人，不是机器，不该把请求线程挂死）；
* 平台不支持就退回「页面上手填路径」——功能比按钮重要。

音乐按**内容**判重（标题 + 专辑 + 艺术家，见 `development.md` 的「判重与来源」一节），所以同一批文件
重复扫不会堆条目，第二次全是「未变化」，用户可以放心重点按钮。

---

## 5. 插件自己的页面

* 打开 `/plugins/<id>` → 宿主调用 `render_page(ctx, request)`，把返回的 HTML 放进站点布局里
  （导航、样式都在）。你也可以返回一个完整的 HTML 文档，宿主照样塞进去。
* 页面里的表单 `POST` 到 `/plugins/<id>` → 宿主调用 `handle_post(ctx, request, form)`：
  返回 HTML 就直接回显，返回 `None` 就跳回页面。
* 慢活（比如翻页抓取）别压在 `handle_post` 里等：起个后台线程把进度写进模块内的任务状态，
  页面里那一小块用 HTMX 自己轮询自己（`hx-get` + `hx-select` 同一个 id）就能出进度条。
  `douban_fetch` / `asmr_playlist` / `copymanga_fetch` / `picamanga_fetch` 就是这个写法，可以直接照。
  点完按钮回的那一屏**一律先画进度条**（哪怕后台已经抢先跑完），结果交给下一次轮询——
  不然秒级失败会直接把报告塞进 POST 响应，进度条那条路径反而永远没被验证过。
* 任务状态是进度条唯一的依据，所以**线程的入口要兜底**：任何异常都得把任务标成结束，
  不然那一栏会一直转到超时为止。
* 活分几步的（比如「扫完再入库」），任务要跑到**整条链路结束**才落成 `done`：
  中途标结束会让页面先显示「结束」、下一次轮询才冒出后半截结果，看着像丢了数据
  （`music_import` 就是这么做的：扫描期间一路 `running`，入库回来才 `done`）。
* 要认证的抓取：**令牌可以落盘（等同本地 cookie），密码不要**——不落盘、不进日志、
  不回显；服务端返回体里可能带上口令，回显或写日志之前先把它抹掉。
* 表单里放 `<input type="hidden" name="…">` 也可以；`form` 是普通 dict（多值只取第一个）。
* 需要样式就在 HTML 里 `<style>`（`<article class="plugin-body">` 里已限制不溢出卡片）。
* 想画图：直接生成 **内联 SVG** 最省事（`kind_chart` 就是这么做的），不依赖任何前端库。

页面模板里可以拿到 `ctx`（`PluginContext`）和 `plugin`（`PluginMetadata`）两个变量，
也可以把数据直接通过 `self.render("page.html", **values)` 传进去。

---

## 6. 把功能挂到命令行（`@cmd`）

页面之外，插件还能往命令行里挂命令，用户这样调用：

```bash
omnishelf plugin                                  # 看有哪些插件、各有什么命令
omnishelf plugin <插件名>                          # 看这个插件的命令与参数说明
omnishelf plugin <插件名> <命令> [--参数 ...]       # 执行（加 -h 看详细帮助）
```

写法就是给方法加 `@cmd`：

```python
from omnishelf.plugins.api import Plugin, cmd


class MyPlugin(Plugin):
    def start(self) -> None: ...
    def stop(self) -> None: ...

    @cmd("fetch_import", help="抓取并导入")          # 一句话说明（列表里显示）
    def fetch_import(
        self,
        media: str = "movie",        # 有默认值 -> --media（可省略）
        pages: str = "",
        status: str = "collect",
        user: str = "",
        delay: float = 0.0,
        incremental: bool = True,    # bool 默认 True -> --incremental/--no-incremental
        dry_run: bool = False,       # bool 默认 False -> --dry-run 开关
    ) -> str:
        """抓取豆瓣的收藏列表，并（默认）直接导入 OmniShelf。

        等价于页面上的「抓取并导入」按钮：抓完调宿主的 import 接口入库。

        media: 媒体类型：movie / book / game
        pages: 页码范围：1 / 1-5 / 47- / 0（全抓）；留空用配置默认
        user: 豆瓣用户 ID；留空用插件配置里的
        dry_run: 只抓取不导入
        """
        ...
```

**参数表由方法签名自动生成**（这就是「在方法注释里写清楚参数」的落地方式）：

| 签名 | 命令行 |
| --- | --- |
| 没有默认值，如 `name: str` | 位置参数（必填）：`... greet Ada` |
| 有默认值，如 `media: str = "movie"` | 选项：`--media book`（可省略） |
| `bool` 默认 `False`，如 `dry_run: bool = False` | 开关：`--dry-run` |
| `bool` 默认 `True`，如 `incremental: bool = True` | 开关对：`--incremental` / `--no-incremental` |
| 下划线参数名 `page_size` | 两种写法都认：`--page-size` / `--page_size` |

**docstring 就是帮助文本**：`-h` / `--help` 会显示它，其中「`参数名: 说明`」这样的行会被
逐条接到对应参数上（所以一个参数写一行，别用列表符号）。命令的返回值：字符串直接打印，
其它类型打成 JSON。

> 注意用 `-h` 看插件命令的帮助——`--help` 在 `omnishelf plugin <插件名>` 这一层是宿主自己的帮助。

## 7. 热重载与调试

* **改完插件刷新页面就生效**：宿主每次访问插件页面/列表前会算一次指纹
  （`metadata.toml` + 入口文件 + `templates/` 等文件的 mtime 与大小），变了就
  `stop` 旧的 → 重新加载 → `start` 新的。
* **插件自带的子包也会一起重载**：卸载时宿主会把这个目录下的模块从 `sys.modules` 里清掉。
  入口文件是宿主直接编译执行的（每次都是新代码），子包却走普通 import、会被缓存着——
  不清就会出现「入口是新的、子包是旧的」，表现为新参数传进旧类的一句
  `TypeError: XXXConfig.__init__() got an unexpected keyword argument '...'`。
  一个边角情况：子包的 `.pyc` 按「源码 mtime（整秒）+ 大小」失效，
  **同一秒**内改成**一样长**的内容，import 机制仍会拿旧代码（真实编辑很少同时满足这两条）。
* 命令行：`omnishelf plugins list` 看状态，`omnishelf plugins reload [id]` 手动重载。
* 页面上的「重载」按钮同理；重载失败会显示原因。
* **日志有三处可看，同一份内容**：插件列表页「插件日志」是内存里最近 200 条；
  `/logs` 页实时看 `data/logs/plugins/<id>.log`（2 秒一刷、自动滚到最新，可按插件/级别筛）；
  命令行 `omnishelf plugins …` 跑出来的日志写**同一个文件**，跟常驻服务读到的一块儿。
  宿主自己记的行（加载完成、卸载、加载失败）也在这个文件里，第三列写「宿主」。
  一行形状：`2026-10-14 11:51:29 | INFO  | 插件 id | 消息`。
* **这个文件代表「本次运行」**：宿主进程启动后第一次写某个插件的日志会把旧内容清掉
  （热重载不算新启动，不会重复清）。要翻历史流水去 `/logs` 之外的那份——入库记录在上传页的
  「导入日志」里。常驻 `serve` 写满 1 MB 会自动轮转（`.1`/`.2`/`.3`）；命令行是一次性进程，
  不轮转——把一个正被 `serve` 写着的文件改名，它的句柄会跟着旧 inode 继续往 `.1` 里写，
  日志就分叉成两份了。
* 调试建议：把异常信息 `self.log()` 出来；`render_page` 抛异常会变成 500 并在页面上显示原因，
  但**别把它当异常处理手段**（用户会看到错误页）。

---

## 8. 规范（红线）

**必须：**

1. 继承 `Plugin` 并实现 `start()` / `stop()` 两个抽象方法。
2. 配置与持久化数据写到 `self.data_dir`（`plugins_data/<id>/`）——那里不进 git。
3. 只使用 **Python 标准库** + **插件 SDK** `omnishelf.plugins.api`
   （`Plugin` / `PluginContext` / `PluginMetadata` / `HostAPI`）。
   **其它 `omnishelf.*` 一律不要 import**——它们是宿主内部实现，签名说改就改
   （这条有测试盯着：`tests/test_plugin_kind_chart.py::test_plugin_only_uses_the_sdk`）。
   `jinja2`/`markupsafe` 也允许（宿主已依赖，且通过 `self.render()` 用最省事）。
4. 出错要自己兜住：配置缺项/类型不对要有默认值；对外部数据要容错。
   一个插件坏掉不能影响别的插件，也不能把页面炸成白屏。
5. 写数据**只能**通过 `self.import_file()`（即宿主的 `import` 接口）走导入链路；
   不要在插件里自己连数据库、也不要绕过判重写文件——那会破坏来源级判重与审计。
   用了就声明 `requires = ["query", "import"]`。
6. 插件跑在宿主进程里、权限和宿主一样：**不要执行来路不明的代码**，也不要写
   「需要用户把未知脚本放进来」的功能。
7. 抓取类的测试**不出网**：把 `urllib.request.urlopen` 换成假站点再跑（`tests/test_plugin_*_fetch.py`
   里各有一份 `install_fake_network`）；插件配了代理时请求走的是 `build_opener`，那份替身里
   也得一起换掉。忘了换会被 `tests/conftest.py` 的兜底夹具当场拦下——
   真站点的脸色不该决定测试通不通过，合成凭据也不该发出去。
8. 翻页抓取**不要用「这一页看不到分页」推断「已经抓完了」**：解析不出末页时，
   拿站点自己报的总条数换算出下限（每页条数向上取整），已经确认的总页数**只增不减**
   （`douban_fetch._reconcile_total_pages` 是现成的写法）；抓 HTML 猜结构迟早会猜错一次，
   猜小的那一下会静默少一批数据。走接口的插件（`copymanga_fetch` / `picamanga_fetch` /
   `asmr_playlist`）都以接口给的 `total` / `total_pages` 为准，别自己数页。
   同一个道理：「这次没抓完」和「数据在站点上被删了」要分清，
   差额（`deleted_or_hidden`）只在**这一趟真的翻到了尽头**时才报，抓到一半留空。
9. **限速不许用「中断抓取」来实现**。用户填了全抓，就得把数据抓完；「翻到第 N 页就停」
   这种自保上限只会静默少一批数据，报告还看着像正常收尾。四个抓取插件都没有单次页数上限
   （自己写的翻页循环里就没有这个判断；带工具包的两家把包里那个安全阀拨成 `None`，
   包本身一行都不改）。要照顾站点脸色就拉长页间间隔；**只有抓 HTML 的
   `douban_fetch` 需要额外插一段强制休息**（`REST_EVERY_PAGES` / `REST_SECONDS`：连翻 20 页
   歇 60 秒，歇完接着翻，进度栏写明）——走接口的另外三家风控没这么严，别顺手给它们加等待，
   那只会白白拖慢长列表。写「停」之前先回答：剩下那些谁来补？
   同理，起始页只留一处真相——页码范围（`47-` 就是第 47 页到底），别再加一个
   「从第几页开始」跟它抢。
10. **日志里不许出现凭据，打码是插件自己的活**。`self.log()` 写进的是
    `data/logs/plugins/<id>.log`——一份会长期留在磁盘上、还会显示在 `/logs` 页面上的文件，
    比聊天窗口更容易漏出去。宿主**不做兜底打码**（它猜不出「哪个字符串在这个站点算口令」，
    真去猜反而会把有用的消息涂花），所以消息**进 `self.log()` 之前**必须先过你自己的
    `redact()`：四个抓取插件各有一份，凡是可能带上令牌/口令的那条消息（包括站点原样回显的
    报错）都要过一遍再上报。页面进度条那份是内存里的，关掉就没了，日志这份不是。
11. **状态文件只管「上次抓到什么」，流水归日志**。`*.meta.json` / `*.detail.json` 里留
    target、mode、页码、条数、`fetched_at`、有没有抓全这类**状态**；每翻一页一行的
    `notes` / `page_notes` **不要**再写进这些文件——它们每次抓取整体覆盖、没人回头看，
    跟日志文件是同一份内容的两具尸体。
12. **认证状态要说得出「有没有、成不成」，但不能带值**。凭据这类东西失效时，站点往往
    **不报错**，只是安静地按未认证渲染——用户看到的是一份少了数据的成功报告（豆瓣就是这么
    把「仅自己可见」的条目连总数一起少报的）。所以：页面、日志、状态文件里给出
    `带 Cookie（dbcl2, ck）· 登录态` 这种**只有字段名和结论**的状态行，值本身
    不回显、不打码进日志（`redact()` 要在）；并且从**返回内容 itself** 判一次真伪
    （登录页才有退出链接之类），判定失败就在明细里写明。逐页明细走 `self.log()`（进度条照样从内存 report 读）。

**建议：**

* 目录里放个 `README.md` 写清楚插件干什么、配置项有哪些。
* 声明 `requires = ["query"]`，方便宿主提示接口不满足的插件。
* 大循环/慢查询放在用户点按钮触发的路径上，别在 `start()` 里做重活（会拖慢启动）。
* 页面尽量返回片段 HTML（宿主会套布局），别去复制宿主的导航和样式。

---

## 9. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 列表页看不到我的插件 | 目录里没有 `metadata.toml`（没它会被忽略），或目录名以 `.`/`_` 开头 |
| 状态是「出错」，提示 `实例化 XXX 失败: Can't instantiate abstract class` | 类里没实现 `start()` 或 `stop()`（它们是抽象方法） |
| 状态是「出错」，提示 `没有 Plugin 子类` | 你写成了旧的「两个孤立函数」写法，或者忘了 `class X(Plugin)` |
| `/plugins/<id>` 404 | 插件没加载成功（先看列表页的错误），或 id 拼错 |
| 页面 500，写着 `render_page() 出错` | 你的渲染代码抛异常了，看提示里的类型和消息 |
| 改了代码没生效 | 正常情况下刷新即可；确认改的是 `entry` 指定的那个文件（默认 `plugin.py`）。仍不生效就点「重载」或 `omnishelf plugins reload`；重启进程是最后的办法（子包的 `.pyc` 在同一秒内改成一样长时仍可能是旧的，见第 7 节） |
| 抓取「已到最后一页（1）」，可站点明明报了几百条 | 分页区块没渲染出来时被当成了只有一页——见第 8 节第 8 条：总页数只增不减，末页认不出就用站点报的条数换算下限 |
| 填了 `0` 全抓，却停在第 20 页 | 不是中断，是豆瓣的节奏休息（连翻 20 页歇 60 秒，见第 8 节第 9 条）——歇完会继续翻，进度栏写明「强制休息」。抓取本身没有页数上限 |
| 一个文件里有两个 `Plugin` 子类 | 宿主会报错要求指明：`entry = "plugin.py:MyPlugin"` |
| 配置没保存 | 只有实现了 `handle_post` 才会处理表单；保存用 `self.save_config({...})` |
| 明明抓全了，站点报的条数还是比抓到的少 | 未认证视角下站点**自己就少报**（豆瓣把「仅自己可见」的条目连总数一起藏）。认证状态要能从报告里读出来，别让用户猜——见第 8 节第 12 条 |
| 昨天那次抓到第几页了？ | 进度条和「同步明细」都是内存里的，页面一关就没。过程在 `data/logs/plugins/<id>.log`，浏览器里看 `/logs`；文件代表**本次运行**，要更早的历史得看当时的页面或导入日志 |
| 想联网 | 等宿主开放 `net` 接口（先讨论）。在此之前，抓取类插件可以**只在用户点下去的时候**用标准库 `urllib` 请求（第 4 节末尾那几个插件就是这么做的）——运行期不碰网络这条底线是给本体定的，不是把抓取也禁掉 |

---

## 10. 宿主接口一览（将来的扩展位）

| 接口名 | 状态 | 内容 |
| --- | --- | --- |
| `query` | ✅ 已有 | 只读数据查询（本文第 4.1 节） |
| `import` | ✅ 已有 | 把文件交给内置 importer 入库（本文第 4.2 节） |
| `audio` | ✅ 已有 | 读本地音乐文件的标签、生成可导入的清单（本文第 4.3 节） |
| `write` | ❌ 未开放 | 直接新建/修改单条（需要设计冲突处理；目前请用 `import`） |
| `net` | ❌ 未开放 | 带超时/重试/限速的 HTTP 客户端（爬取来源用） |
| `tasks` | ❌ 未开放 | 定时/后台任务（增量抓取用） |
| `ui` | ❌ 未开放 | 在列表/详情页插入组件 |

插件通过 `requires = [...]` 声明自己需要哪些接口，`omnishelf plugins list` 与插件列表页
会提示缺失的接口。
