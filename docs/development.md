# 开发指南

给改 OmniShelf 代码的人看：开发环境、分层约定、目录结构、kind 注册表、**判重与来源的口径**、
怎么写 importer、字段口径、导入的存档与审计、排错速查、性能边界、发布流程。

使用者文档看 [`README.md`](../README.md)；写插件看
[`plugin-development.md`](plugin-development.md)（插件目录约定、metadata、钩子、SDK 接口、红线）。

> 早先还有两份**开发过程记录**（一份逐条记设计取舍的决策日志、一份从真实导出到落库的实操手册）。
> 它们记的是实现过程与个人数据形态，不适合随仓库分发，已经移出 git；
> **需要长期遵守的约定全部归档在本文件里**——改代码前先看第 2、6、8 节，那里是规则的出处。

---

## 1. 开发环境

```bash
git clone <repo> && cd collection
uv sync --dev                     # 建 .venv + 装依赖（含 pytest/mypy）
uv run pytest                     # 全部离线、不碰真实 data/、不联网
./.venv/bin/python -m mypy        # 类型检查
./.venv/bin/omnishelf demo        # 用合成数据跑一遍端到端（临时目录，结束即清理）
cp -r plugins/kind_chart plugins/scratch   # 想写插件时照模板抄
```

* 配置在 `pyproject.toml`：`testpaths = ["tests"]`、`addopts = "-q --strict-markers"`，
  mypy 检查范围是 `src/omnishelf` + `tests`（非 strict，但开了
  `warn_unused_ignores` / `check_untyped_defs`）。
* 测试**全部**用 `tmp_path` 下的独立数据目录（见 `tests/conftest.py`），不会动你的 `data/`。
* **运行期与测试都不许联网**：`tests/conftest.py` 里给 `urlopen` / `build_opener` 装了兜底替身，
  任何测试真发请求就直接失败（抓取类插件的测试自己搭假站点）。踩过一次——写登录失败测试时忘了
  装假站点，那条测试真的把凭据发到了生产接口上，而且两种路径都恰好"通过"，看不出问题。
* **仓库里不许出现真实收藏数据**。`tests/fixtures/` 与 `tests/plugin_fixtures/` 全是合成样本
  （假 id、`.invalid` 域名）；插件的真实凭据与抓取结果只落 `plugins_data/` 和 `data/`，都在忽略名单里。
* 代码风格：中文注释与 docstring；改行为要同时改测试，并把口径写进本文件（别只留在提交信息里）。

## 2. 分层与三条铁律

```
CLI / Web 路由    只做参数解析与渲染，不写业务逻辑
   ↓
services/        导入、编辑、封面、导出、筛选解析、评审、日志、git 编排
   ↓
repository.py    唯一直接读写数据库的地方（时间戳、ULID、判重规则都在这里）
   ↓
models.py        SQLModel 表定义 + kinds.py 的 kind/字段注册表
```

导入这条链路固定是三段，**破了会被现有测试挡住**：

1. `fetch()` 只读文件、只做「行 → dict」，**不转换类型**（`"1982"` 就是字符串）；
2. `normalize()` 只做「字段映射 + 换算」，**不碰数据库、不做判重**；
3. 类型/范围校验（`ItemDraft` + `kinds.coerce_extra`）、判重、写库、写 `import_logs`
   全部由 `services/import_.py` 负责。

```
你的文件 → fetch() → RawRecord → normalize() → ItemDraft
         → coerce_extra 校验 → 判重（见第 6 节）→ items / external_ids / tags / import_logs
```

其它硬性约定：

* 所有路径用 `pathlib`；所有时间戳是 UTC ISO8601 **秒精度 + `+00:00`**
  （`2026-10-02T13:59:00+00:00`，不用 `Z`、不带微秒），定长字符串可直接按字典序排序，由 repository 维护。
* ID 是 ULID（`python-ulid`），只由 repository 生成，以字符串存库。
* 不用 `create_all()`，加表/加列一律写 `migrations/NNN_*.sql`（见第 4 节）。
* 单用户、单进程：没有并发写保护，别同时跑两个写进程。
* Windows 兼容做法：不 `shell=True`、不用符号链接、路径走 `pathlib`、
  git 用参数列表 + `cwd=`、文件读写显式 `encoding="utf-8"`。

## 3. 目录结构

```
collection/
├── pyproject.toml             # 依赖、pytest/mypy 配置、版本号（与 __init__.py 同步）
├── README.md                  # 使用者文档
├── docs/
│   ├── development.md         # 本文件：开发约定与规则出处
│   └── plugin-development.md  # 插件开发指南
├── migrations/00N_名称.sql    # 按编号顺序执行的 schema 变更
├── src/omnishelf/
│   ├── config.py              # 纯路径配置（pathlib）+ 环境变量
│   ├── db.py                  # engine / session / user_version 迁移 / checkpoint
│   ├── models.py              # SQLModel 表
│   ├── kinds.py               # kind 注册表（extra 字段声明 + 校验 + 判重策略）
│   ├── audiotags.py           # 本地音频标签读取（标准库手解 ID3/M4A/FLAC/WAV/AIFF + 时长）
│   ├── music_scan.py          # 音乐文件扫描（后缀名单、跳垃圾目录、清单 JSON）
│   ├── repository.py          # 唯一读写库的地方：时间戳、ULID、判重、creators 切分
│   ├── demo.py                # omnishelf demo：合成数据端到端
│   ├── cli.py
│   ├── services/
│   │   ├── import_.py         # fetch→normalize→dedupe→upsert + import_logs
│   │   ├── review.py          # pending_merges 的 merge/skip
│   │   ├── covers.py          # 封面落盘（ULID 分桶）+ 文件/cover_path 联动
│   │   ├── editing.py         # Web 表单 → item 更新（字段解析与校验）
│   │   ├── filters.py         # 唯一的筛选解析层（Web 与 CLI 共用）
│   │   ├── export_.py         # JSON 导出
│   │   └── vcs.py             # data-commit 的 git 编排
│   ├── importers/
│   │   ├── base.py            # RawRecord / Importer 协议 + ItemDraft
│   │   ├── mapping.py         # 映射引擎（mapping / transforms / 空值处理 / a.b.c 点路径）
│   │   ├── json_file.py       # JSON importer + json_movies / json_books
│   │   ├── csv_file.py        # CSV importer 基类（标准库 csv；不预置具体 CSV importer）
│   │   ├── picamanga.py       # PicaComic 书架导出
│   │   ├── copymanga.py       # copymanga 书架导出
│   │   ├── douban.py          # 豆瓣看过/读过/玩过
│   │   ├── asmr_one.py        # asmr.one 播放列表/私人收藏
│   │   ├── music.py           # 本地音乐清单 + iTunes 资料库
│   │   └── registry.py        # importer 注册表（内置名单在这里，上传页下拉直接读它）
│   ├── plugins/               # 插件运行时
│   │   ├── api.py             # 插件 SDK（插件唯一允许 import 的宿主模块）
│   │   ├── metadata.py        # metadata.toml 解析
│   │   ├── context.py         # 交给插件的 ctx（配置/数据/渲染/日志）
│   │   ├── host.py            # 宿主暴露给插件的接口：query / import / audio
│   │   ├── logstore.py        # 统一日志落盘（data/logs/plugins/<id>.log）
│   │   └── manager.py         # 加载/停止/热重载/错误隔离
│   └── web/
│       ├── app.py             # 应用工厂（含静态资源内容哈希版本号）
│       ├── deps.py            # session / templates 依赖
│       ├── routes/{pages,upload,review,plugins,logs,api}.py
│       ├── templates/
│       └── static/            # app.css + tag-picker.js + img/cover-na.svg + vendor/*
├── plugins/<插件 id>/          # 随仓库分发的插件代码（进 git）
├── tests/
│   ├── fixtures/              # importer 用的合成样本
│   ├── plugin_fixtures/       # 插件抓取用的合成响应/HTML
│   └── test_*.py
├── plugins_data/              # 插件配置与持久化（不进 git）
└── data/                      # 独立 git 仓库（不进主仓库）：库、附件、导入原文、运行日志
```

## 4. 迁移（数据库结构变更）

`migrations/NNN_名称.sql` 按编号顺序执行，跑过的记在 `PRAGMA user_version` 里。
新增一个迁移就加文件，**别去改老文件**；`ALTER TABLE` 没有 `IF NOT EXISTS`，靠版本号保证只跑一次。
SQL 文件内部语句保持幂等（`executescript` 会隐式提交、无法可靠回滚，所以用
「幂等 SQL + 最后推进版本号」而不是事务回滚）。库版本高于代码里的最新迁移时直接报错拒绝启动，
不做静默降级。数据搬运（例如把 `extra.author` 提到通用列）也写在迁移里，用 SQLite 的 JSON 函数即可。

已有：`001_init.sql`（一次写全的初始 schema）、`002_creator.sql`（把 `extra.author` / `extra.artist`
提升为通用列 `creator`）、`003_creators.sql`（`creator` 改成多值 `creators` 并删掉旧列）。
三个按编号顺序执行，新库也一样跑完——002/003 里的数据搬运写成幂等的，所以空库上跑也是无害的。

## 5. kind 注册表

`src/omnishelf/kinds.py` 是**唯一**声明 kind 与 `extra` 字段的地方：

```python
KINDS["movie"] = KindDef("movie", "影视", (
    FieldDef("director", "导演", "str"),
    FieldDef("cast", "主演", "list[str]"),
    FieldDef("runtime", "片长（分钟）", "int"),
    ...
))
```

通用字段（标题、副标题、创作者、年份、状态、评分、备注、封面）在 `items` 表里有独立列，
**不在这里声明**；这里只放该类型特有的。

| kind | 名称 | 跨来源查重 | 内容判重字段 |
| --- | --- | --- | --- |
| `book` | 图书 | 是 | — |
| `manga` | 漫画 | **否**（只查同源） | — |
| `movie` | 影视 | 是 | — |
| `game` | 游戏 | 是 | — |
| `asmr` | ASMR | 是 | — |
| `music` | 音乐 | 是（实际走内容判重） | `("album", "artists")` |

* 新增 kind / 字段：只改这个文件。`models / repository / CLI / Web` 都不硬编码 kind 清单。
* 详情页的「专属字段」表单按 `extra_fields` 渲染（`bool` → checkbox，`list[str]` → 逗号分隔）。
* `allowed extra types`：`str / int / float / bool / list[str]`。

## 6. 判重与来源

**源（source）= 这批数据来自哪里**，由 importer 的 `source` 决定。三个用处：
认出「这条我导过没有」、判断一个条目出现在哪些来源里、决定查重的范围。

> ⚠️ **一定要把来源的唯一标识映射进 `external_ids`**。有它，重复导入就是纯粹的更新；
> 没有它，只能靠标题/年份，标题稍有差异就被当成新条目。

导入时**按顺序**检查，命中即停：

| # | 检查 | 命中条件 | 语义 |
| --- | --- | --- | --- |
| 1 | **同源** | `(source, external_id)` 已在 `external_ids` | **更新**：外部数据权威，覆盖非空字段 |
| 2 | **非同源·精确** | `kind + title + year` 完全一致，且候选不是同源条目 | **合并**：只补空字段 + 合并 tags/external_ids |
| 3 | **非同源·模糊** | `kind + 标题` 相似 | **绝不自动合并**：存为新条目（candidate），写 `pending_merges`，本次计 `skipped` |

口径一句话：**同源只认唯一标识，标题只用于非同源。** 来源给了标识，标识就是身份——
同源两条记录即使标题完全一样，只要标识不同就是**不同作品**，各自入库、不进取待复核、不互相覆盖。

第 2/3 步「跟谁比」由 kind 策略决定：

* `cross_source_duplicates=True`（默认）——跨来源比较，命中进待复核，人工确认后才合并。
* `cross_source_duplicates=False`（漫画）——**只查同源内部**。不同来源之间不考虑重复，
  免得每次导入都冒出一堆跨源误报。
* `identity_fields=("album", "artists")`（音乐）——**内容判重**：标题 + 这几个 `extra` 字段
  整体相同就算同一个作品，命中就地合并（只补空字段），**替代**第 2、3 两条规则。
  本地文件没有稳定 ID（同一首歌能被拷成任意个文件），而模糊标题匹配对它是负担
  （同一首歌收在不同专辑里本来就是两条正常数据，走模糊匹配会把待复核刷屏）。

标题怎么算「匹配」（`repository.title_keys()` / `titles_match()`，不是字符串相等）：
去掉括号内容（翻译组、版本、水印）→ 按 `|` `/` 切段 → 每段归一化（去标点空白 + `casefold()`）→
任一 key **全等**，或两边都 ≥ 6 字符时**互相包含**。短标题（`Dune` vs `Dune Messiah`）不会因为
「包含」被误判；命中只会进取待复核，**合并永远由人在 review 里决定**。

其它细节与边界：

* **「来源未知」的条目**（手工录入、没有任何 external_id）在两种策略下都参与标题比对，
  否则手输过的条目会被重复导一遍。
* 命中但**值没有任何变化** → 记 `unchanged`（不是 `updated`），不调用 `update_item`，`updated_at` 不动。
  重复导入因此是幂等的：条目数据一行都不变，只有 `import_logs` 追加一行审计记录。
* `review merge --keep X` 会恢复（un-soft-delete）保留侧，把另一侧的 external_ids / tags / 空字段
  并入后软删除，并删掉同一对条目的所有待复核行；`review skip` 只删待复核行，两个条目都留着。
* **已知边界**：默认策略下，一条 item 挂了两个来源的标识之后，两个来源每次导入都会覆盖同一批
  标量字段（后导入的赢）。漫画不会遇到（不跨源合并，一条只有一个来源）；真接入
  「同一作品确实存在于多个来源」的类型时再决定要不要引入主来源概念。
* 软删除的条目若持有 external_id，再次导入同一条会新建 item 并「抢走」该 external_id（不报错）。
* 模糊匹配在 Python 侧做（取同 kind 未删除条目后比较），保证和 `normalize_title` 完全一致；
  上万条时它会变成明显瓶颈，改法是加一列 `title_norm` + 索引、把比较挪到 SQL。

## 7. 写一个新的 importer

内置的 importer 名字（上传页下拉读的就是这份，`registry.names()`）：

```
asmr_one  copymanga  douban_books  douban_games  douban_movies  itunes_xml
json_books  json_movies  music_files  picamanga
```

`csv_file.py` 只有基类，**不预置具体 CSV importer**（各家表头都不一样，写 20 行子类更准）。

```python
from omnishelf.importers.json_file import JsonFileImporter
from omnishelf.importers.registry import register

@register("json_games")
class GameJsonImporter(JsonFileImporter):
    source = "json_games"
    kind = "game"
    mapping = {
        "title": ["title", "name"],
        "year": ["year"],
        "creators": ["developer"],
        "extra.platform": ["platforms"],
        "external_ids.igdb": ["igdb_id"],
        "tags": ["genres"],
    }
```

**注册与加载**：`@register("名字")` 之外，模块必须被 `load_builtin_importers()` 导入到
（`json_file.py` / `csv_file.py` 里的类自动加载；写在别的模块里要把模块名加进那个 import 列表）。
注册名在**整个进程/pytest 会话里必须唯一**，同名不同类会在收集阶段直接抛
`ValueError: importer 名称重复`。

**JSON 支持的结构**：`[...]`、`{"items": [...]}`（也认 `records` / `data` / `works`）；
非 dict 的行静默忽略；编码默认 UTF-8，`JsonFileImporter(path, encoding="gbk")` 可换。

### 7.1 映射目标

| 目标 | 落库位置 | 备注 |
| --- | --- | --- |
| `kind` `title` `subtitle` `year` `status` `rating` `notes` | `items` 对应列 | 只有这 7 个核心字段；`cover_path` 不能从映射写 |
| `creators` | `items.creators` | 给数组，或给字符串按第 8 节的规则切分 |
| `extra.<name>` | `extra` JSON | 是否合法由 `kinds.py` 决定 |
| `external_ids.<来源>` | `external_ids` 表 | **强烈建议填**，见第 6 节 |
| `external_id` | 同上，来源名用 importer 的 `source` | 单来源时的简写 |
| `tags` | `tags` 表 | `"a, b"` 或 `["a","b"]` |

写别的目标会在导入时报 `映射目标无法识别`。候选键支持 `a.b.c` **点路径**（先字面量键，再下钻），
所以豆瓣那种把平台/简介塞在 `extra` 子对象里的 payload 也能取到。

### 7.2 空值、空格与 transforms

| 规则 | 行为 |
| --- | --- |
| `None` / `""` / `"   "` | 视为「没有这个值」→ 继续尝试下一个候选字段 |
| `0` / `False` / `[]` | **是有效数据**，不当成空 |
| 字符串首尾空白 | 自动 `strip()`（首尾空格正是「同一条被当成两条」的常见来源） |
| CSV 空单元格 / 缺失列 | `""` 与 `None` 都当成「没有值」，落到 NULL 而不是报错 |

要关掉前两个行为：`skip_blank=False` / `strip_values=False`。

`transforms` 的 key 是**目标字段**，value 是函数或查表 dict：

```python
transforms = {
    "rating": lambda v: float(v) * 2,                          # 5 星制 -> 0-10
    "status": {"看过": "done", "在看": "doing", "想看": "wishlist"},
    "extra.platform": lambda v: [p.strip() for p in v.split("|")],
    "year": lambda v: v.year if hasattr(v, "year") else v,     # Excel 把年份存成日期单元格
}
```

查表未命中的值原样透传，随后被 `status` 校验拦下报错——**不会静默变成空**。
transform **返回 `None` = 这个字段没有值**，直接跳过（不写进 draft）。

### 7.3 校验规则（写 mapping 时对着这张表）

| 字段 | 规则 | 违反时 |
| --- | --- | --- |
| `title` | 去空白后不能为空 | 整条报错跳过 |
| `kind` | 必须在 `kinds.py` 注册 | 整条报错跳过 |
| `year` | 整数；`"1995"` 能自动转，`"1995年"`/`"MMXX"` 不行 | 整条报错跳过 |
| `status` | `wishlist` / `doing` / `done` / `dropped`，大小写不敏感 | 整条报错跳过 |
| `rating` | REAL，**必须 0–10**（**不裁剪**，越界直接报错） | 整条报错跳过 |
| `extra` 已声明字段 | 按声明的 type 宽松转换（数字字符串 → int/float，`true/false/1/0` → bool） | 类型不符 → 整条报错跳过 |
| `extra` 未声明字段 | 记 **warning**，值**原样保留** | 不报错 |
| `extra` 必填字段 | 只有声明 `required=True` 的才要求必填 | 整条报错跳过 |

单条记录失败**不中断整批**；明细在 `import_logs.errors`（JSON 数组，元素带 `level`：`error` / `warning`）。
`omnishelf import` 默认退出码 0（部分失败是常态），加 `--strict` 才在有错误时返回 1。

## 8. 字段口径

这些是用户拍过板的口径，改之前先对齐：

* **缺就留空，不要猜。** 来源没给的 `year` / `rating` / 阅读状态一律留空；
  猜出来的值以后补不回来，空字段补得上。启发式解析（从一串无标签文本里挑人名）踩过的坑：
  会把演员名当成地区。
* **评分量程固定 0–10。** 来源用别的分级（5 星、百分制）时在 `transforms` 里换算，越界不裁剪、直接报错。
* **`creators` 是多值**（JSON 数组）。切分规则（`repository.split_creators()`，
  运行时/表单/CLI/importer 共用同一份）：
  - 括号**外**的 `,` `，` 和「两侧都有空白的 `/`」= 多个创作者；
  - 括号**里**的分隔符不拆（`Current Storage (momi, ぴょん吉)` 括号内是团体成员名单，属于名字本身）；
  - 斜杠两侧没有空白不拆（`horn/wood`、`ミニチュアキマイラ1/24 (J-2型)` 是一个名字）；
  - 顿号 `、` 不参与切分。
  归一化统一走 `clean_creators()`（去空白、去重、保序）。
* **远程封面统一叫 `extra.cover_url`**：所有来源的远程封面地址都用这个约定名，
  跨来源不用各写一个字段。
* **音乐只读 11 项**：标题、副标题、评分、备注、艺术家、专辑、专辑艺术家、年份、编号、时长、原始文件名。
  库里 `music` 没有对应列的（封面、流派、歌词、编码器、BPM、播放计数）**一概不读**——
  没人看的 extra 只是把垃圾从文件搬进数据库。
  另外两条硬要求：标签是标题的唯一来源，没标签才拿文件名兜底（只去后缀 + 去开头曲目号，
  **不按 ` - ` 拆 `Artist - Title`**），原始文件名无条件写进 `extra.filename`；
  `creators` 是「艺术家 + 专辑艺术家」合并去重，同时 `extra.artists` 与 `extra.album_artists`
  各存一份原文（判重的键是 `extra.artists`，不能被合并掉）。
* **作品是否完结**（`extra.finished`）和**你读到哪一步**（`status`）是两件事，不互相映射。
* `extra.marked_date`（来源侧的标记时间）比入库时间更有意义，值得留。

## 9. 导入的存档、审计与提交

* `omnishelf import` 把**原始文件原样复制**到 `data/imports/<来源>/<原文件名>`（按来源分目录，
  便于回溯「这批数据从哪个文件来」）；同名覆盖，旧版本留在 data 仓库的 git 历史里；
  已经在存档目录里的文件不会自己覆盖自己；结果里的 `archive_path` 字段就是它。
* 每次导入往 `import_logs` **追加一行**审计记录（来源、起止时间、各计数、errors 明细）。
  所以导入后 `data/collection.db` 一定会变一点（多一行日志），**条目数据本身是幂等的**。
* 库跑在 WAL 模式，改动可能只在被 gitignore 的 `collection.db-wal` 里。
  `omnishelf data-commit` 因此**先执行 `PRAGMA wal_checkpoint(TRUNCATE)`** 再交给 git；
  自己写脚本直接提交 `data/` 时也要记得这一步，否则快照会缺数据。

## 10. 排错速查（文案都是实际输出）

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| `未注册的 importer: 'nope'（已注册: …）` | 名字拼错，或新模块没加进 `load_builtin_importers()` | 检查 `@register` 与模块导入 |
| `记录缺少 title（可用字段: director, overview, year）` | 映射的候选字段都没命中（或值全是空白） | 对着「可用字段」改 `mapping` 候选名 |
| `rating 必须在 0.0-10.0 之间，得到 12.0` | 量纲没换（5 星忘 ×2、百分制忘 ÷10） | 加 `transforms["rating"]` |
| `非法 status: 'finished'` | 源状态值不在四个合法值里 | 用 `transforms` 查表映射 |
| `year … unable to parse string as an integer` | `"1995年"` 这类脏年份 | transform 里抽数字，或让该条失败 |
| `kind=movie 未声明 extra 字段 'xxx'，已原样保留` | **只是 warning**，值已入库 | 想让它在详情页表单里出现就在 `kinds.py` 声明 |
| `JSON 顶层必须是数组或对象` / `JSON 对象里找不到 items/records/data/works 数组` | 文件形状不对 | 改结构，或自己写 `fetch()` |
| `文件编码不是 utf-8: xxx.csv` | 文件是 GBK 等 | 子类里 `encoding = "gbk"` |
| 第一列怎么都取不到值 | Excel 导出的 CSV 带 BOM，列名成了 `\ufeff名字` | 用默认 `encoding="utf-8-sig"`（内置 CSV 基类已是） |
| `fetch 失败: …` | 读文件阶段就挂了（整批 0 条） | 看 `import_logs.errors` 里那条 error |

看明细：`omnishelf import 文件 --importer 名字 --json | python -m json.tool`。

从小样本到全量的推荐顺序：先在临时目录试跑
（`omnishelf --data-dir /tmp/dryrun import …` → `review list` 看待复核比例 → `list --limit 20` 抽查），
满意再进真实库；全量前先 `omnishelf export` 备份一份。

## 11. 插件

插件的完整规范在 [`plugin-development.md`](plugin-development.md)。这里只列宿主侧的三条红线：

* 插件**只允许** import 标准库 + `omnishelf.plugins.api`（SDK），不碰宿主内部模块；
  需要宿主能力就走 `metadata.toml` 里声明的接口：`query`（只读查询）/ `import`（交文件给内置
  importer 入库）/ `audio`（读本地音乐文件标签）。
* 插件**只写** `plugins_data/<id>/`；配置里可以落 token/用户名，**口令永不落盘、永不进日志、
  永不出现在页面与异常文本里**；脱敏由插件自己做（宿主不知道哪个字符串在那个站点算口令）。
* 插件跑在宿主进程内，是**受信任代码**，没有沙箱。

## 12. 规模与性能

| 规模 | 现状 |
| --- | --- |
| 几千条 | 够。导入是单事务；同源有标识时不跑标题比对，重导全是「未变化」 |
| 浏览 | Web 列表已分页（`?page=`、`?per_page=`，默认每页 60、上限 200）；CLI `list --limit N --page N` |
| 上万条 | 规则 3 的模糊比对会成为瓶颈（每条新记录全表扫一次），改法见第 6 节最后一条 |

## 13. 不使用的东西

不引入 Node、Docker、Celery、Redis、Alembic、FTS5、向量库、DI 容器；
运行期不访问网络（HTMX / Pico.css 已内置在 `src/omnishelf/web/static/vendor/`）。
依赖表是刻意保持干净的：音频标签解析自己写（标准库字节切片），不引 mutagen / tinytag。

## 14. 发布流程

1. 改 `pyproject.toml` 的 `version` 与 `src/omnishelf/__init__.py` 的 `__version__`（两处必须一致，
   `tests/test_cli.py` 盯着），在 `CHANGELOG.md` 顶部加一节（写清楚破坏性变更）。
2. `uv sync --dev`（重装 editable 包，`uv.lock` 会跟着更新）。
3. `uv run pytest` + `./.venv/bin/python -m mypy` 全绿。
4. `git tag vX.Y.Z`，主 repo 与 data repo 各自提交/推送（**data 是私有仓库，别无脑公开**）。

## 15. 已知边界

* 单用户、单进程；没有并发写保护，WAL 只是缓解。
* 没有全文检索：列表过滤在 SQL 层（`repository._item_filters`），搜索只搜标题。
* 插件是受信任代码，没有沙箱、没有权限系统。
* 封面只做了「上传本地文件」；从 `extra.cover_url` 自动抓取还没有实现。
* Windows 未实机验证（开发机是 macOS），代码层面已按第 2 节的兼容性约定处理。
