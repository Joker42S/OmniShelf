# ASMR.ONE 收藏抓取（`asmr_playlist`）

从 asmr.one 的**公开播放列表**或**私人收藏**抓取作品，保存为可直接导入 OmniShelf 的 JSON 快照，并可一键导入（内置 importer **`asmr_one`**，与上传页/命令行走同一条导入链路）。

功能要点：

* 公开播放列表（无需登录）与私人收藏（需登录）两种抓取目标，抓取方式相同；
* 增量 / 全量 / 指定页码范围三种抓取方式，页面上即可选择；
* 重复导入不会产生重复条目（按作品 ID 判重，只会「未变化」）；
* 抓取在后台运行，有进度条与抓取报告；中断后可从断点续抓，已抓到的数据不会丢失。

## 使用方法

网页：顶部导航 **插件** → **ASMR.ONE 收藏抓取**（`/plugins/asmr_playlist`）。

接口地址默认 `https://api.asmr.one/api/`，填到 `/api/` 即可，其余部分按抓取目标自动补全（也可以填写带 `id=…` 的完整地址）。

**公开播放列表**（无需登录）：

1. 填写**播放列表 ID**（asmr.one 播放列表页地址中的 UUID）；
2. 需要时填写**本地代理**（如 `http://127.0.0.1:10809`；留空为直连）；
3. 点击**抓取并导入**。

**私人收藏**（需登录）：

1. 勾选**抓取私人收藏**，页面展开认证输入项；
2. 填写 **authorization** 令牌（浏览器请求头中的 Bearer 令牌，`Bearer` 前缀写不写均可），或填写用户名与密码，由插件登录换取令牌；
3. 点击**抓取并导入**。私人收藏会连同评分一起抓取，并导入为条目的评分。

### 命令行

```bash
# 播放列表，增量（默认）：遇到整页都已在快照中即停止，适合日常更新
omnishelf plugin asmr_playlist fetch_import \
  --playlist-id 1a2b3c4d-5e6f-4a1b-8c2d-9e0f1a2b3c4d \
  --proxy http://127.0.0.1:10809 --pages 0

# 全量：从头到尾重新抓取
omnishelf plugin asmr_playlist fetch_import --pages 0 --no-incremental

# 指定范围 / 只抓某一页 / 只抓取不导入
omnishelf plugin asmr_playlist fetch_import --pages 1-2
omnishelf plugin asmr_playlist fetch_import --pages 7
omnishelf plugin asmr_playlist fetch_import --pages 1-3 --dry-run

# 私人收藏：令牌与用户名密码二选一（密码会留在 shell 历史里，长期使用建议令牌）
omnishelf plugin asmr_playlist fetch_import --target favorites --authorization <Bearer 令牌>
omnishelf plugin asmr_playlist fetch_import --target favorites --username <账号> --password <密码>
```

## 认证说明

* **令牌优先**：登录成功后立即将令牌保存到本地配置，供下次直接复用；密码不落盘、不写入日志、不回显，仅在本次抓取中使用。
* **令牌失效**（接口返回 401/403）时，若本次填写了用户名密码，会自动重新登录并重试该页（一次抓取中最多重新登录一次）。
* 公开播放列表与私人收藏各存各的快照，互不覆盖。

## 抓取方式

| 目的 | 页码 | 增量 |
| --- | --- | --- |
| 全量 | `0` | 关 |
| 增量（推荐日常） | `0` | 开 |
| 只更新最新 N 页 | `1-N` | 开或关 |
| 只抓某一页 / 从某页抓到底 | `7` / `12-` | 开或关 |

两种列表都是**新条目在前**（播放列表按加入时间、私人收藏按评价更新时间倒序），因此「增量 = 从第 1 页向后翻、遇到整页已知即停止」不会遗漏。快照是**累积**的：本次抓到的数据覆盖同 ID 的旧数据，其余保留。

## 配置（`plugins_data/asmr_playlist/config.toml`）

页面的「保存默认值」写入；抓取时的接口地址、抓取目标、播放列表 ID、代理与认证也会自动保存。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `https://api.asmr.one/api/` | 接口根地址，其余部分按抓取目标自动补全 |
| `target` | `playlist` | `playlist` 公开播放列表 / `favorites` 私人收藏 |
| `playlist_id` | 空 | 公开播放列表 ID（抓私人收藏时用不到） |
| `authorization` | 空 | Bearer 令牌；抓私人收藏时优先使用 |
| `username` | 空 | 登录账号（**密码不落盘**，每次现填） |
| `proxy` | 空（直连） | 如 `http://127.0.0.1:10809`；未写协议时会自动补 `http://` |
| `pages` | `0` | 默认页码范围（`0` 全抓；`12-` 从第 12 页抓到底） |
| `page_size` | `100` | 每页条数 1~100（接口上限 100） |
| `delay` | `0.5` | 请求间隔秒数 |
| `incremental` | `true` | 默认是否增量 |
| `keep_raw` | `false` | 保留接口返回的全部字段（见下） |
| `timeout` | `30` | 单次请求超时秒数 |

## 数据文件与导入

* 快照：`plugins_data/asmr_playlist/asmr_playlist_<播放列表 id>.json`（私人收藏为 `asmr_playlist_my_favorites.json`），格式即 importer `asmr_one` 认的格式；
* 元信息：同目录 `*.meta.json`，页面列表使用；逐页明细记录在宿主运行日志（`/logs` 页）；
* 导入按作品 ID（`works[].id` → `external_ids.asmr_one`）判重，同一批作品重复导入只会「未变化」；
* 也可以到 `/upload` 页选择 importer `asmr_one` 手动导入快照文件。

**`keep_raw`**：接口返回中有一部分站内统计数据是导入用不到的，默认丢弃以减小快照体积（约小三成）；需要完整留档时打开该选项。

## 已知边界

* 列表中被移除的作品不会从库中删除（导入只新增/更新，不删除）；
* 增量模式下老作品的字段更新（如新增标签）不会被察觉，需要刷新时用指定范围或全量抓取；
* 令牌被接口拒绝且未填用户名密码时，本次抓取会停止并提示重新认证；
* 各语言版本是**独立作品**（各有自己的 asmr.one ID），各自成条，不做关联；
* 状态（听过/在听）留空。
