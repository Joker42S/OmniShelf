# Steam 收藏抓取（steam_fetch）

把 Steam 的**游戏库**和**愿望单**抓取为可直接导入 OmniShelf 的 JSON 快照，并可一键导入「游戏」书架（游戏库 → `done`，愿望单 → `wishlist`）。

抓取时会为两份列表补全商店信息：名称、封面链接、发行日期、开发者、类型标签、简介；游戏库另含游玩时长（愿望单没有）。封面只保存链接，不下载图片。**价格不写入快照**：同一款游戏的价格随区域和促销频繁变动，记录后很快过期。

两份快照共用同一个外部 ID 空间（按 appid 判重）：先入愿望单、后来购买了再导游戏库时，条目会更新为 `done`，不会产生第二条。

商店信息使用本地缓存（默认 7 天）：再次抓取同一区域时基本不需要重新请求；「重新拉取商店信息」可强制重取。愿望单条目的名称取自商店信息，未取到时会先用 appid 占位，重新抓取一次即可覆盖为正确名称（重新导入旧快照无法修复占位名）。

抓取核心是随插件分发的 `steam_client/` 工具包（纯标准库实现，不新增依赖），设计与实现口径见其源码注释。

## 准备

| 输入 | 说明 |
| --- | --- |
| Steam Web API key | 在 <https://steamcommunity.com/dev/apikey> 申请（注册域名可随意填写） |
| steamid64 或 vanity | 个人主页地址中的 **17 位数字**；或自定义 URL 名（如 `gabelogannewell`），二选一 |
| 代理（可选） | 直连不通时填本地代理，如 `http://127.0.0.1:10809`；只写 `IP:端口` 按 HTTP 代理处理，SOCKS 需写全 `socks5://IP:端口` |

key 保存在 `plugins_data/steam_fetch/config.toml`（**不进 git**），且不出现在日志、页面回显、快照文件和错误信息中。带 `user:pass@` 口令的代理地址同样只存本地配置，日志和报错只保留 `协议://主机:端口`。

## 使用方法

### 页面

插件列表页打开「Steam 收藏抓取」：填写 key 与目标，选择**只抓取**或**抓取并导入**。抓取在后台运行，进度自动刷新。

页面填写的 **SteamID64 / vanity / 商店区域 / 代理**在点击抓取后自动保存（下次打开仍然显示）；「抓哪些」和「重新拉取商店信息」只作用于本次抓取。进度栏会显示本次的网络路径（直连或代理）。

代理填写后，本次抓取的所有请求（API 与商店两个域名）都走该代理；留空为不指定，按系统/环境变量的代理设置，未设置即直连。填写的地址无法识别时会直接报错，不会静默改为直连。

### 命令行

```bash
# 只抓取并保存快照，不写库
.venv/bin/omnishelf plugin steam_fetch sync --dry-run

# 抓取全部并入库
.venv/bin/omnishelf plugin steam_fetch sync --steamid 76561197960287998

# 只抓愿望单，强制重取商店信息（价格/发行日期等未更新时），区域为国区
.venv/bin/omnishelf plugin steam_fetch sync --lists wishlist --force-refresh --cc CN

# 直连不通时：本次全程走本地代理
.venv/bin/omnishelf plugin steam_fetch sync --proxy http://127.0.0.1:10809
```

`--proxy` 留空时使用配置中保存的代理（配置也为空则直连）。

### 手动导入

快照保存在 `plugins_data/steam_fetch/`，两份都使用 importer **`steam`**：

```text
steam_owned.json      <- 上传页选 importer「steam」
steam_wishlist.json   <- 上传页选 importer「steam」
```
