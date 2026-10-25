# steam_fetch —— Steam 收藏抓取插件

把 Steam 的**游戏库**和**愿望单**抓成可直接导入 OmniShelf 的 JSON 快照，
一键入「游戏」书架（游戏库 → `done`，愿望单 → `wishlist`）。

抓取核心是随插件分发的 `steam_client/` 工具包（设计文档见仓库讨论记录；
纯标准库实现，不新增任何依赖）。

## 能拿到哪些信息

游戏库和愿望单是**同一套 item 结构**：抓取时两份都会补全商店信息，没有开关。
商店信息按 appid **逐款**取（`appdetails` 的逗号批量已被 Steam 回 400，实测见下文「口径」），
靠**最多 8 路并发 + 7 天本地缓存**压时间：第二次抓同一区域基本全走缓存，一条请求都不发。
两份快照只有顶层 `list` 与条目 `status`（done / wishlist）不同，共用一个 `steam` importer。

| 快照字段 | 来源 | 入库去处 |
| --- | --- | --- |
| `name` | 游戏库接口 / 商店信息 | `title` |
| `playtime_forever`（分钟） | 游戏库接口（愿望单为 null） | `extra.playtime_hours`（小时） |
| `cover_url` | 商店信息 `header_image` | `extra.cover_url`——**只存链接，不下载图片** |
| `release_date` | 商店信息 | `year`（抽 4 位年）+ `extra.release_date` 原串 |
| `price` | 商店信息（按所选区域） | `extra.info` |
| `developers` | 商店信息 | `creators`（创作者） |
| `genres` | 商店信息 | `tags`（标签） |
| `short_description` | 商店信息（清成纯文本） | `notes`（备注） |
| `appid` | 两份接口都有 | `external_ids("steam", appid)`（幂等去重键） |

## 用之前要准备的东西

| 输入 | 谁提供 | 说明 |
| --- | --- | --- |
| Steam Web API key | 你 | <https://steamcommunity.com/dev/apikey> 申请，注册域名随便填（key 只在服务端调用时用） |
| steamid64 或 vanity | 你 | 个人主页那串 **17 位数字**；或自定义 URL 名（如 `gabelogannewell`），二选一 |
| 代理（可选） | 你 | 直连不通时填本地代理，如 `http://127.0.0.1:10809`；只写 `IP:端口` 按 HTTP 代理对待，SOCKS 要写全 `socks5://IP:端口` |

key 存在 `plugins_data/steam_fetch/config.toml`（**不进 git**），
但它**绝不进日志、页面回显、快照文件和异常文本**——一律 `***`。
代理地址如果带 `user:pass@` 口令，同样只存本地配置，日志和报错里只留 `协议://主机:端口`。

## 用法

### 页面

插件列表页打开「Steam 收藏抓取」：填 key + 目标 → 「只抓取」或「抓取并导入」。
抓取在后台线程跑，进度条每 1 秒自己刷新。
页面上填的 **SteamID64 / vanity / 商店区域 / 代理点一次抓取就记住**（下次打开还是它）；
「抓哪些」和「重新拉取商店信息」只作用于这一次抓取。
进度栏里会写明这次走的哪条路（`网络：直连` / `网络：走代理 http://127.0.0.1:10809`）。

### 命令行

```bash
# 干跑：只抓 + 落快照，不写库
.venv/bin/omnishelf plugin steam_fetch sync --dry-run

# 抓全部并入库
.venv/bin/omnishelf plugin steam_fetch sync --steamid 76561197960287998

# 只抓愿望单，重新拉取商店信息（价格/发行日期没更新时用），区域换成国区
.venv/bin/omnishelf plugin steam_fetch sync --lists wishlist --force-refresh --cc CN

# 直连不通（商店域名半通不通那类）：这一次全程走本地代理
.venv/bin/omnishelf plugin steam_fetch sync --proxy http://127.0.0.1:10809
```

`--proxy` 留空 = 用配置里存的那个（配置里也空着就是直连）；填了却认不出来会直接报错，
不会悄悄退回直连——那正是「连不上但其实配错了」。

### 手动导入

快照落在 `plugins_data/steam_fetch/`，**两份都用 importer「steam」**：

```text
steam_owned.json      <- 上传页选 importer「steam」
steam_wishlist.json   <- 上传页选 importer「steam」
```

两份共用一条外部 ID 空间 `external_ids("steam", appid)`：
先入了愿望单、后来买了再导游戏库，会**更新成 done**，不会长出第二条。

## 口径（和 steam_client 的设计一致）

* 网络出口只有一个（`SteamWebAPI._open`）：`proxy` 填了就**只走它**——显式的 `ProxyHandler`
  会顶掉 urllib 从环境变量读来的那份，不会被系统环境半路换掉，而且 **api 与 store 两个域名
  共用同一条路**（只配一个等于没配：愿望单走 `api.steampowered.com`、商店信息走
  `store.steampowered.com`）。留空才走 urllib 默认，那时 `HTTPS_PROXY` 之类的环境变量照旧生效，
  没设就是直连。**没有**「代理不通自动退回直连」这种兜底——那样只会让报错看不出走的哪条路，
  连不上时异常里会带 `（经代理 协议://主机:端口）`；
* 玩家资料 / 游戏库 / 愿望单**每次直接请求 Steam**，不落盘；
* 只有**商店信息**（两份列表的名称/价格/封面链接/发行日期/开发者/类型/简介）
  走本地缓存 `plugins_data/steam_fetch/steam_cache/apps/{cc}/{appid}.json`——
  **按商店区域分目录**（价格随区域变化，US 与 CN 各存一份、互不冒领），默认 7 天，
  `--force-refresh` 只作用于这一层，且**失败不污染旧缓存**；
* 商店信息**逐款取，不批量**：``appdetails`` 的多 appid 逗号写法现在会被 Steam 直接回
  **HTTP 400**（2026-10-18 实测：``appids=730,570`` → 400 且 body 是 ``null``，``appids=730`` → 200），
  命中的缓存不发请求，剩下的**最多 8 路并发**；
  **碰到网络类失败（超时/断网/5xx 重试用尽）就熔断本次剩下的 id**——每个未命中的 id 最坏要付
  4 次超时，链路断了还硬排 300 个队，就会像 2026-10-18 那次一样挂半小时、结果全是空值。
  被熔断/失败的这些 appid 不写缓存，下次抓取会重新问；单条 ``success=false`` 只影响那一条；
* 愿望单接口只给 appid，**商店信息没取到时快照里名字先用 appid 占位**（游戏库不受影响，
  它自带名字）。这种条目入库后标题就是那串数字——**重导旧快照修不好**（值一模一样，算「未变化」），
  要等商店域名通着**重抓一次**：按 ``(steam, appid)`` 命中的是同一条，覆盖写回真名字，不会长出第二条；
* 想真正一次拿多个 app 的商店信息，唯一的官方批量接口是 ``IStoreBrowseService/GetItems/v1``
  （POST，要带 ``data_requestids``）。截至 2026-10-18 没有实测通过（探测回 ``{"response":{}}``），
  所以**没有**换过去；换之前要先确认它仍能给出封面/开发者/类型/简介这四样；
* 空数据 ≠ 无数据：接口回空响应时报「游戏信息不可用或未公开」并**不写快照**
  （多半是档案私密/受限，不是真没有）；
* 愿望单走 `IWishlistService/GetWishlist/v1`（`wishlistdata` 旧路径 2024-11 起已不可用）。

## steam_client 工具包（独立使用）

`steam_client/` 也可以脱离插件用（把 `plugins/steam_fetch` 加进 `sys.path` 即可）：

```python
from steam_client import SteamClient, SteamError

client = SteamClient(api_key=KEY, cache_path="/data/steam_cache")
client = SteamClient(api_key=KEY, cache_path="/data/steam_cache",
                     proxy="http://127.0.0.1:10809")     # 直连不通时：两个域名都走这条

sid = client.user.resolve_vanity_url("gabelogannewell")   # vanity -> steamid64
profile = client.user.get_player_summary(sid)             # 实时，不缓存
games = client.game.get_owned_games(sid)                  # 实时，不缓存
wish = client.game.get_wishlist(sid)                      # 实时，不缓存
details = client.game.enrich_apps(wish)                   # app 详情，走缓存 + 逐款并发（下标对齐）
details = client.game.enrich_apps(wish, force_refresh=True)  # 强制刷新（失败保留旧缓存）
by_id = client.game.get_app_details_map(wish)             # 只要 appid -> 详情这个映射
```

错误模型：`SteamAuthError` / `SteamNotFoundError` / `SteamRateLimitError` /
`SteamDataUnavailableError` / `SteamNetworkError` / `CacheError`，都继承 `SteamError`。
测试在 `tests/test_steam_client_*.py`（71 条，含 `test_steam_client_proxy.py` 15 条，
覆盖设计文档的 9 条验收用例）与 `tests/test_plugin_steam_fetch.py`（插件层 15 条）。
