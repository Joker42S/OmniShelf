# 拷贝漫画书架抓取（`copymanga_fetch`）

把**自己拷贝漫画书架上的收藏**同步下来，存成可以直接导入的 JSON，
并（可选）用内置 importer **`copymanga`** 直接入库。

这个插件：

* 抓取/同步用的是**随插件分发的一份 copymanga 工具包**（`plugins/copymanga_fetch/copymanga/`，
  原样复制、一行没改，见 `VENDOR.md`）——插件只负责表单、进度、落盘和导入；
* **站点地址、token、账号、同步方式、翻页参数**都记在 `plugins_data/copymanga_fetch/config.toml`；
* **增量 / 全量**两种同步：增量日常一两次请求就够，全量能发现老条目的变化和取消收藏；
* 只抓**漫画**（工具也能抓動畫，但库里没有这个类型，也就没有能接它的 importer，所以不提供这个选项）；
* 抓完**一键导入**（走宿主的 `import` 接口，和上传页/CLI 完全同一条链路）；
* 有进度条（实时显示翻到第几页）、同步报告和本地文件列表。

## 用起来

网页：顶部导航 **插件** → **拷贝漫画书架抓取** → `/plugins/copymanga_fetch`

1. **站点地址**默认 `https://copy4000.com`，换镜像站时改这里（贴整条书架页地址也行，只取域名）；
2. **认证**二选一：
   * 粘 **token**——浏览器开发者工具（F12）→ Network → 请求头里的 `Authorization: Token <token>`，
     或者直接看 Cookie 里的 `token` 值；
   * 没有令牌就勾开下面那栏，填**用户名 + 密码**，插件登录换一个令牌（换到的令牌会留下，密码不会）；
3. 第一次先跑一次**全量**，把整个书架落下来；以后用**增量**就很快；
4. 点 **同步并导入**。

命令行同理：

```bash
# 第一次：全量同步并导入
omnishelf plugin copymanga_fetch fetch_import --token <登录 token> --mode full

# 日常：增量（默认），只翻到上次的位置——令牌已经在配置里了，不用再传
omnishelf plugin copymanga_fetch fetch_import

# 只同步不入库
omnishelf plugin copymanga_fetch fetch_import --dry-run

# 用账号密码登录（口令会留在 shell 历史里，介意就用 --token）
omnishelf plugin copymanga_fetch fetch_import --username <账号> --password <口令>
```

## 关于认证

* **token 优先**：令牌会存进本地配置，下次直接复用；用用户名密码登录成功的那一刻就当场存下来，
  不用等这次同步跑完（同步中途失败也不白登一次）。
* **表单里令牌留空 = 用配置里存着的那个**，不是「把它忘掉」：所以第一次密码登录之后，
  直接再点一次同步就行，不会被要求重新认证。要换账号就粘一个新令牌。
* **令牌过期**（接口回 401）时，这次填了用户名密码就自动重新登录再同步。
* **密码不落盘、不进日志、不回显**，只在本次同步里用一次。

## 两种同步方式

| 想要 | 方式 |
| --- | --- |
| 日常刷新（快） | 增量 |
| 刷新老条目的字段、找出取消收藏的 | 全量 |

接口默认按「**加到書架時間降序**」返回，所以新收藏一定排在最前面：增量从头部翻，
一整页都没超过本地水位线就停。同步过程还会实时校验这个排序——万一排序口径变了，
会自动改跑一遍全量并在报告里标出来，**宁可多花几个请求也不静默漏数据**。

## 配置（`plugins_data/copymanga_fetch/config.toml`）

页面上「保存默认值」写入；**同步时站点地址 / 认证 / 方式 / 翻页参数也会自动记住**。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `host` | `https://copy4000.com` | 站点地址 |
| `token` | 空 | 登录 token（**会存进本地配置**，和 Cookie 一个性质；表单留空就是用它） |
| `username` | 空 | 账号（**密码不在这个表里**，每次现填） |
| `mode` | `incremental` | 默认同步方式：`incremental` / `full` |
| `page_size` | `100` | 每页条数 1~500，越大请求越少 |
| `delay` | `0.3` | 翻页间隔秒数 |
| `timeout` | `40` | 单次请求超时秒数 |
| `include_raw` | `false` | 状态库里保留接口返回的全部字段（导入用不到，文件会大不少） |

## 落盘与导入

都在 `plugins_data/copymanga_fetch/` 下：

* `copymanga_favorites.json`——**状态库**：水位线、首次/最近见到、取消收藏标记都在里面，下次增量靠它；
* `copymanga_import.json`——**导入文件**：只放还没取消收藏的条目，形状正好是 importer
  `copymanga` 认的 `{"items": [...]}`；
* `copymanga_fetch.meta.json`——上次同步的概况（页面列文件时只读这个小文件）。
  只留状态（同步方式、页码、条数、时间），逐页流水不写在这儿——它在宿主的
  `data/logs/plugins/copymanga_fetch.log`，浏览器里 `/logs` 实时看。

导入调的是 `self.import_file("copymanga", 导入文件)`，**判重靠漫画自己的编号**
（`comic_uuid` → `external_ids.copymanga`），所以同一批重复导入只会「未变化」。
到 `/upload` 页选 importer `copymanga` 手动导入这个文件也可以（内容一模一样）。

书名 / 作者 / 题材 / 连载状态 / 最新话 / 读到第几话 / 更新时间 / 封面 / 详情页地址都会带上。

## 已知边界（有意为之）

* **取消收藏的作品不会从库里删掉**——导入接口只新增/更新，不删；它们只在状态库里打个标记，
  也不会再进导入文件；
* 增量只顺带刷新「翻到的那几页」里老条目的字段（最新话、人气等），没翻到的不动——
  要整库刷新老字段就跑一次全量；
* 只同步**漫画**：工具本身也能抓動畫，但库里没有这个类型（也就没有能接它的 importer），
  所以不给这个开关；同理不提供 CSV / Markdown / Excel 的额外导出——抓完直接入库，
  状态库和导入文件本来就是 JSON；
* 全量**没有单次页数上限**：一次点击就一路翻到末页，几千条的书架也只是慢一点；
  报告会写这次停在什么原因上（`complete` / `known-boundary` / `watermark-reached`）。
