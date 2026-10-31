# 豆瓣收藏抓取（douban_fetch）

把豆瓣的**看过/读过/玩过**（以及在看/在读/在玩、想看/想读/想玩）抓取为 JSON 快照，再导入 OmniShelf。

两种用法：**抓取并导入**（页面按钮 / 命令行 `fetch_import`）抓取完成后直接调用宿主导入接口入库，与上传页走同一条链路；**只抓取**仅保存快照，之后到 `/upload` 页手动导入。

## 使用方法

1. 打开 `/plugins/douban_fetch`；
2. 填写**豆瓣用户 ID**（个人主页地址中的数字，不是昵称），选择媒体与列表；抓取自己的列表建议填写**豆瓣登录 Cookie**（见下节），否则标记为「仅自己可见」的条目不会被抓到；
3. 页码默认 `0`（全抓）；想先小范围验证可填 `1` 只抓第一页，或 `47-` 从第 47 页抓到底（分段抓取）；
4. 抓取完成后页面会显示文件路径，例如 `plugins_data/douban_fetch/douban_movie_collect.json`；
5. （只抓取时）到**上传**页，importer 选 `douban_movies` / `douban_books` / `douban_games` 上传该文件。

### 命令行

```bash
omnishelf plugin douban_fetch                     # 查看命令与参数说明
omnishelf plugin douban_fetch fetch_import -h     # 详细帮助
omnishelf plugin douban_fetch fetch_import --user 12345678                       # 按配置默认：全抓
omnishelf plugin douban_fetch fetch_import --user 12345678 --pages 1            # 只抓 1 页并导入
omnishelf plugin douban_fetch fetch_import --user 12345678 --pages 0 --delay 5  # 全抓（放慢）
omnishelf plugin douban_fetch fetch_import --user 12345678 --pages 1 --dry-run  # 只抓不入库
```

命令行可用 `--cookie 'dbcl2="12345678:xxxx"; ck=yy'` 携带 Cookie；留空则使用配置中已保存的 Cookie（与页面同一条语义）。命令行参数会留在 shell 历史里，长期使用建议在页面保存配置；输出只显示字段名与登录状态，不显示 Cookie 值。

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
| `overrides.<媒体>` | 给某个媒体单独指定用户 ID（抓取表单会显示当前媒体对应的那一个） | 空 |
| `media` / `status` | 页面默认选中的媒体与列表 | `movie` / `collect` |
| `pages` | 页码范围：`1` / `1-5` / `47-`（从第 47 页抓到底，续抓用它）/ `0`（全抓） | `0`（全抓） |
| `incremental` | 是否默认增量 | `true` |
| `delay` | 请求间隔秒数（1~30） | `2.5` |
| `cookie` | 豆瓣登录 Cookie（至少含 `dbcl2`）。抓「仅自己可见」的条目需要它 | 空（游客） |

## 登录 Cookie

**为什么需要**：豆瓣允许把单个条目设为「仅自己可见」。游客身份下这些条目不出现在列表里，连站点报告的总数也相应减少，因此不填 Cookie 的「抓全」会静默缺失数据。

**如何获取**（抓取表单的「豆瓣登录 Cookie」栏）：

1. 浏览器登录豆瓣；
2. 开发者工具 → Application/存储 → Cookies → 复制 `dbcl2`（必需）与 `ck`；
3. 粘贴到输入框，支持三种形式：整串 Header 值（`dbcl2="…"; ck=…`）、两列导出（`dbcl2<TAB>值`，一行一个）、或 `dbcl2` 的裸值（自动补上字段名）。粘贴整页 HTML 会被拒绝。

**保存与安全**：首次抓取时保存到 `plugins_data/douban_fetch/config.toml`（该目录不进 git）。之后输入框留空即沿用已存值；勾选「清除已存的 Cookie」后不再携带。Cookie 值不回显（输入框为密码型），不写入日志、页面、快照或详情文件，这些位置只出现 `dbcl2`、`ck` 等字段名。

**失效提示**：Cookie 过期时豆瓣不报错，只是按游客渲染页面。插件会检测页面登录状态，发现游客视角时会在明细与报告中标注，提示更换有效的 Cookie。

## 两个容易误解的点

### 1. 抓到的条数比豆瓣报的少

差额有两种来源：

* 豆瓣中已删除或隐藏的条目：仍计入站点总数，但列表页不渲染；
* 标记为「仅自己可见」的条目：只有携带登录 Cookie 才能抓到，游客身份下的差额不能都当作「已删除」理解。

页面只在快照覆盖到最后一页时才显示该差额，抓取进行中显示 `—`。

### 2. 快照是累积的，不是每次覆盖

同一个「媒体 + 列表」对应一个文件，分几次抓取（如先 1-20 页、再 21-40 页）会合并为一份，重复条目按 `subject_id` 去重更新。中途被限速打断后，从断点续抓也能拼出完整快照。

## 限速

* 豆瓣的反爬拦截页出现时，插件会立即停止并提示，不会以短间隔硬性重试（那样可能加重限制）；建议等待 30 分钟以上再用增量续抓；
* 每个请求都带站点 `Referer`；默认间隔 2.5 秒；
* 抓取大列表（几十页、上千条）建议把间隔调到 `5` 秒以上，一次避免连续抓取三个列表；
* 页码填 `0` 会一直翻到列表末尾，不会中途截断；连续翻 20 页会自动暂停 60 秒后继续（进度栏会显示）。

## 数据文件

* `douban_<媒体>_<状态>.json`：快照，即内置 importer 认的格式；
* `douban_<媒体>_<状态>.detail.json`：附抓取元信息（停止原因、耗时等），用于排查问题；
* 逐页明细记录在宿主运行日志（`/logs` 页）。

## 已知限制

* 电影的 `year` 抓不到（列表页没有稳定位置），留空而不是猜测；
* 游戏的 `platforms` 中平台与类型混在一起（豆瓣在同一 `/` 串中排列）；导入时日期之前的段会进入标签，切分只按两侧带空格的斜杠进行，不会拆开类型名；
* 书的 `publisher` 由「年份前一段」推断，原始串保留在 `extra.pub` 可回查；
* 抓取在后台运行：页面立即显示进度条（页数 / 条数 / 耗时），期间可切换页面；全抓几十页需要几分钟，可用页码范围分段抓取（`1-20`、`21-40`）。
