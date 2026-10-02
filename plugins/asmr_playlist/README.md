# ASMR.ONE 收藏抓取（`asmr_playlist`）

从 **asmr.one 的公开播放列表**或**自己的私人收藏**抓作品，存成可直接导入 OmniShelf 的 JSON，
并（可选）直接用内置 importer **`asmr_one`** 入库。

这个插件：

* 用**标准库 `urllib`**（插件红线：只用标准库 + 插件 SDK）；
* **接口地址、播放列表 id、代理、认证**持久化在 `plugins_data/asmr_playlist/config.toml`，
  不用再改代码；
* **公开播放列表**（不用登录）和**私人收藏**（要登录）两种目标，抓法完全一样；
* **增量 / 全量 / 指定范围**三种抓法在页面上点点就能选；
* 抓完**一键导入**（走宿主的 `import` 接口，和上传页/CLI 完全同一条链路）；
* 有进度条、抓取报告、快照列表；中途失败时报告会给出「页码范围填 `N-` 接着抓」的提示。

## 用起来

网页：顶部导航 **插件** → **ASMR.ONE 收藏抓取** → `/plugins/asmr_playlist`

接口地址默认 `https://api.asmr.one/api/`——填到 `/api/` 就够了，
后面的部分按抓取目标自动补全（也可以整条写成带 `id=…` 的地址）。

**公开播放列表**（不用登录）：

1. 填**播放列表 ID**（asmr.one 播放列表页 URL 里那串 UUID）；
2. 填**本地代理**（如 `http://127.0.0.1:10809`；留空 = 直连）；
3. 点 **抓取并导入**。

**私人收藏**（要登录）：

1. 勾上 **抓取私人收藏**，下面会出现用户名 / 密码 / authorization 三个框；
2. 有令牌就粘 **authorization**（浏览器请求头里那一串，`Bearer` 前缀写不写都行）；
   没令牌就填**用户名 + 密码**，插件登录换一次令牌；
3. 点 **抓取并导入** —— 你给作品打过的分会一起抓回来，导入成条目的评分。

命令行同理：

```bash
# 播放列表，增量（默认）：全抓但遇到「整页都已在快照里」就停，适合日常更新
omnishelf plugin asmr_playlist fetch_import \
  --playlist-id 1a2b3c4d-5e6f-4a1b-8c2d-9e0f1a2b3c4d \
  --proxy http://127.0.0.1:10809 --pages 0

# 播放列表，全量：从头到尾重抓一遍
omnishelf plugin asmr_playlist fetch_import --pages 0 --no-incremental

# 只更新最新两页 / 只抓某一页 / 只抓不导入
omnishelf plugin asmr_playlist fetch_import --pages 1-2
omnishelf plugin asmr_playlist fetch_import --pages 7
omnishelf plugin asmr_playlist fetch_import --pages 1-3 --dry-run

# 私人收藏：令牌或用户名密码二选一（口令会进 shell 历史，长期用还是令牌省事）
omnishelf plugin asmr_playlist fetch_import --target favorites --authorization <Bearer 令牌>
omnishelf plugin asmr_playlist fetch_import --target favorites --username <账号> --password <口令>
```

## 私人收藏的认证

* **authorization 优先**：能用令牌就不用每次填密码；令牌会存进本地配置，下次直接复用。
  登录成功的那一刻就当场存下来，不用等这次抓取跑完（抓取中途失败也不白登一次）。
* **令牌过期**（接口回 401/403）时，如果这次填了用户名密码，会自动重新登录换一个令牌
  并重试那一页（一次抓取里只重登一次）。
* **密码不落盘、不进日志、不回显**，只在本次抓取里用一次；所以长期方案是让它换出来的
  那个令牌留在配置里。
* 私人收藏和播放列表各存各的快照，互不覆盖。

## 三种抓法

| 想要 | 页码 | 增量 |
| --- | --- | --- |
| 全量 | `0` | 关 |
| 增量（推荐日常） | `0` | 开 |
| 只更新最新 N 页 | `1-N` | 开或关 |
| 只抓某一页 / 从某页抓到底 | `7` / `12-` | 开或关 |

两种列表都是**新的在最前面**（播放列表按加入时间、私人收藏按评价更新时间倒序），
所以「增量 = 从第 1 页往后翻、遇到整页已知就停」不会漏。快照是**累积**的：
本次抓到的覆盖同 id 的老数据，其余保留。

## 配置（`plugins_data/asmr_playlist/config.toml`）

页面上「保存默认值」写入；**抓取时接口地址 / 抓取目标 / 播放列表 ID / 代理 / 认证也会自动记住**。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `https://api.asmr.one/api/` | 接口根地址，后面的部分按抓取目标自动补全 |
| `target` | `playlist` | `playlist` 公开播放列表 / `favorites` 私人收藏 |
| `playlist_id` | 空 | 公开播放列表 id（抓私人收藏时用不到） |
| `authorization` | 空 | Bearer 令牌；抓私人收藏时优先用它 |
| `username` | 空 | 登录用的账号（**密码不在这个表里**，每次现填） |
| `proxy` | 空（直连） | 如 `http://127.0.0.1:10809`；`127.0.0.1:10809` 这种没写协议的会自动补 `http://` |
| `pages` | `0` | 默认页码范围（`0` 全抓，一路翻到末页；`12-` 从第 12 页抓到底） |
| `page_size` | `100` | 每页条数 1~100（接口上限就是 100） |
| `delay` | `0.5` | 每次请求间隔秒数 |
| `incremental` | `true` | 默认是否增量 |
| `keep_raw` | `false` | 保留接口返回的全部字段（见下） |
| `timeout` | `30` | 单次请求超时秒数 |

## 落盘与导入

* 快照：`plugins_data/asmr_playlist/asmr_playlist_<播放列表 id>.json`，
  私人收藏那份叫 `asmr_playlist_my_favorites.json`
  —— 形状就是 importer 认的 `{"works": [...]}`（顶层另有说明来源的字段）；
  写的是**紧凑 JSON**（几千条约十几 MB；缩进写要多一倍，而它主要是机器读的）；
* 元信息：同目录 `*.meta.json`（缩进好读，页面列表用，不用读大文件）；
  只留状态（抓取目标、模式、页码、条数、有没有抓全），逐页流水不写在这儿——它在宿主的
  `data/logs/plugins/asmr_playlist.log`，浏览器里 `/logs` 实时看；
* 导入：`self.import_file("asmr_one", 快照)`，**判重靠 `works[].id` →
  `external_ids.asmr_one`**，所以同一批作品重复导入只会「未变化」；
* 到 `/upload` 页选 importer `asmr_one` 手动导入这个快照文件也可以（内容一模一样）。

### `keep_raw`

接口每条作品里 `translation_info` / `rank` / `rate_count_detail` / `work_attributes`
是 importer 明确**不导入**的站内数据，却占了约三成体积。默认丢掉它们，
需要留档就打开 `keep_raw`。

## 已知边界（有意为之）

* 列表里**被移除**的作品不会从库里删掉——导入接口只新增/更新，不删；
* 增量遇到「整页已知」就停，所以**老作品的字段更新**（例如补了新标签）不会被发现，
  想刷新就抓一次指定范围或全量；
* 令牌被接口拒了又没填用户名密码，这次抓取就停下来重新认证；
* 语言版本是**独立作品**（各有自己的 asmr.one id），按来源各自成条，不做关联
  （和内置 importer 的取舍一致）；
* 状态（听过/在听）留空：「加进收藏」不等于听过，不猜。
