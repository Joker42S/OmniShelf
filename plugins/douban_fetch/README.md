# 豆瓣收藏抓取（douban_fetch）

把豆瓣的 **看过/读过/玩过**（以及在看/在读/在玩、想看/想读/想玩）抓成 JSON，
再由你在 **上传** 页导入 OmniShelf。

> 两种用法：**抓取并导入**（页面上的按钮 / 命令行 `fetch_import`）抓完直接调宿主的
> `import` 接口入库，走的就是上传页那条链路（来源判重、写 `import_logs`、原始文件存档）；
> **只抓取**则只落快照，之后到 `/upload` 页手动导入。

## 怎么用

1. 打开 `/plugins/douban_fetch`；
2. 填 **豆瓣用户 ID**（个人主页 URL 里那串数字，不是昵称）、选媒体与列表；
   抓自己的列表建议顺手把 **豆瓣登录 Cookie** 填上（见下面那一节），不然
   标了「仅自己可见」的条目会安静地抓不到；
3. 页码框默认是 `0`（全抓）；列表大、先想试水就填 `1` 只抓第一页，或 `47-` 从第 47 页抓到底；
   抓完这一趟直接入库就点旁边的 **抓取并导入**（不用再去上传页）；
4. 抓完页面会给出文件路径，例如
   `plugins_data/douban_fetch/douban_movie_collect.json`；
5. （只抓取时）到 **上传** 页，importer 选 `douban_movies` / `douban_books` / `douban_games`，传这个文件。

### 命令行

```bash
omnishelf plugin douban_fetch                     # 看命令与参数说明
omnishelf plugin douban_fetch fetch_import -h     # 详细帮助（参数说明来自 docstring）
omnishelf plugin douban_fetch fetch_import --user 12345678                    # 按配置默认：全抓
omnishelf plugin douban_fetch fetch_import --user 12345678 --pages 1          # 只抓 1 页并导入
omnishelf plugin douban_fetch fetch_import --user 12345678 --pages 0 --delay 5   # 全抓（慢）
omnishelf plugin douban_fetch fetch_import --user 12345678 --pages 1 --dry-run   # 只抓不入库
```

命令行也能带 Cookie：`--cookie 'dbcl2="12345678:xxxx"; ck=yy'`。
**留空就用配置里存的那个**（和页面上那一栏同一条语义）。命令行参数会留在 shell 历史里，
长期用建议在页面上存成配置；输出里只报字段名和登录态，不回报值。

三个列表对应的 importer：

| 媒体 | 抓取到的文件 | 上传时选的 importer |
| --- | --- | --- |
| 电影/剧集 | `douban_movie_collect.json` | `douban_movies` |
| 读书 | `douban_book_collect.json` | `douban_books` |
| 游戏 | `douban_game_collect.json` | `douban_games` |

## 配置项（存在 `plugins_data/douban_fetch/config.toml`，不进 git）

| 项 | 说明 | 默认 |
| --- | --- | --- |
| `user` | 豆瓣用户 ID | 空（必须填） |
| `overrides` | 给某个媒体单独指定用户 ID，例如 `{movie = "123"}` | 空 |
| `media` / `status` | 页面默认选中的媒体与列表 | `movie` / `collect` |
| `pages` | 页码范围：`1` / `1-5` / `47-`（从第 47 页抓到底，续抓用它）/ `0`（全抓） | `0`（全抓） |
| `incremental` | 是否默认增量 | `true` |
| `overrides.<媒体>` | 给某个媒体单独指定用户 ID（抓取表单会显示当前媒体的那个） | 空 |
| `delay` | 请求间隔秒数（夹在 1~30） | `2.5` |
| `cookie` | 豆瓣登录 Cookie（至少要含 `dbcl2`）。抓「仅自己可见」的条目要靠它 | 空（游客） |

## 登录 Cookie：为什么需要、怎么填

**为什么**：豆瓣允许把单个条目设成「仅自己可见」。游客视角下这些条目**不出现在列表里**，
而且**连站点总数都会少报**——实测同一个列表：游客 `看过(1395)` / 登录 `看过(1402)`，
差的那条正是标了仅自己可见的《异兽魔都 第二季》（`35028938`：游客的第 4 页 14 条，
登录后同一页 15 条、它就在里面）。详情页更直接：游客拿到的是一份 2.9KB 的壳，登录后是 100KB+ 的真页面。
也就是说，**不填 Cookie 的「抓全」是会静默少数据的**。

**怎么填**（抓取表单里的「豆瓣登录 Cookie」那一栏）：

1. 用浏览器登录豆瓣；
2. 开发者工具 → Application/存储 → Cookies → 复制 `dbcl2`（必需）和 `ck`；
3. 粘进来，三种形状都认：整串 Header 值 `dbcl2="…"; ck=…`、两列导出（`dbcl2<TAB>值` 一行一个）、
   或者只贴 `dbcl2` 的裸值 `12345678:xxxx`（会自动补上名字）。粘整页 HTML 会被拒绝。

**它去哪了**：抓取一开始就把它存进 `plugins_data/douban_fetch/config.toml`（和 token 同性质，
这个目录不进 git）。之后那一栏**留空即沿用**已存的，勾「清除已存的 Cookie」才不再带。
值**不回显**（输入框是 password 型），也不写进日志、页面、快照或详情文件——
那些地方只出现 `dbcl2, ck` 这样的**字段名**。

**失效会告诉你**：Cookie 过期时豆瓣不报错，只是安静地按游客渲染。插件因此会看页面本身
（登录后才有「退出」链接、标题才会变成「我看过的影视」），一旦发现是游客视角就在明细里写明
「Cookie 没能换来登录态……请换一个还有效的 Cookie 再抓」，报告里的「认证」一行也会标注。

## 两个容易误解的点

### 1. 抓到的条数比豆瓣报的少 —— 已删除的条目，以及「仅自己可见」

差额有两种来源：

* 豆瓣里已被删除/隐藏的条目：仍算进站点总数（`看过(1395)`），但列表页不渲染。
  实测把前 48 页用 6 秒间隔**完整重抓一遍，新增 0 条**，说明本地那份就是列表页能给的完整集合。
* **标了「仅自己可见」的条目**：游客视角看不见，豆瓣报的总数也跟着少。
  这一类**只有带登录 Cookie 才抓得到**，所以游客身份下的差额不能当「都是删了的」来读。

页面只在「快照已经覆盖到最后一页」时才显示这个差额，抓到一半时显示 `—`，
免得看着像失败。

### 2. 快照是**累积**的，不是每次覆盖

同一个「媒体 + 列表」对应一个文件。分几次抓（比如先 1-20 页、再 21-40 页）
会合并成一份，重复抓会更新同一条（按 `subject_id` 去重）。
这样即使中途被限速打断，下次接着抓也能拼出完整快照。

## 抗限速

豆瓣会拦：

* **反爬拦截页**（约 3.2KB，`请点击下方按钮继续浏览`）——插件认出来就立即停止并提示，
  **不硬刚**（短间隔重试只会把 IP 拦到全站，这是踩过的坑）。等 30 分钟以上再增量续抓。
* **nginx 403**（读书站尤其敏感）——每个请求都带站点 `Referer`，并且默认间隔 2.5 秒。

抓大列表（几十页、上千条）建议把间隔调到 `5` 秒以上，一次别连续抓三个列表。
**翻页没有上限**：填 `0` 就一路翻到列表尽头，中途不会因「翻够了」被中断；
豆瓣的节奏靠每页之间的间隔，加上**连翻 20 页强制歇 60 秒**（歇完接着翻，进度栏会写明）。

## 数据格式

`douban_<媒体>_<状态>.json` 就是内置 importer 认的格式：

```json
{
  "media": "movie", "status": "done", "user": "12345678",
  "fetched_at": "2026-10-12 12:42:27",
  "auth": "cookie", "logged_in": true,
  "reported_count": 1402, "item_count": 45, "highest_page": 3, "total_pages": 93,
  "deleted_or_hidden": null,
  "items": [
    {
      "subject_id": "5996825", "title": "济公游记", "year": "",
      "stars": 5, "rating": 10, "marked_date": "2026-10-05",
      "url": "https://movie.douban.com/subject/5996825/",
      "cover": "https://img9.doubanio.com/...",
      "extra": {"intro": "…", "language": "汉语普通话", "playable": true}
    }
  ]
}
```

另有 `douban_<媒体>_<状态>.detail.json`：多一层 `fetch` 元信息
（停止原因、耗时、`deleted_or_hidden` 等），排查问题时看它。
「每页抓到几条」那一串逐页明细不在这里，它在宿主的运行日志里（`/logs` 页，
文件是 `data/logs/plugins/douban_fetch.log`）。

## 测试

```bash
.venv/bin/python -m pytest tests/test_plugin_douban_fetch.py tests/test_web_plugin_douban_fetch.py
```

测试**不出网**：HTTP 全部替换成 `tests/plugin_fixtures/douban_*.html`
（照真实页面结构写的），其中一条用例专门验证「插件产出的快照能被内置 importer 导入且重复导入幂等」。

## 已知限制

* 电影的 `year` 抓不到（列表页没有稳定位置放年份），留空而不是猜；
* 游戏的 `platforms` 里平台和类型是混在一起的（豆瓣在同一个 `/` 串里排的）；
* 书的 `publisher` 用「年份前一段」推，绝大多数对，原始串保留在 `extra.pub` 可回查；
* 抓取跑在**后台**：点完按钮页面立刻出现进度条（翻到第几页 / 抓到多少条 / 已用多久），
  期间可以切到别的页面，回来还在跑；全抓几十页仍要几分钟，不想等的就用页码范围分段（`1-20`、`21-40`）。
