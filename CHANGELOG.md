# 变更历史

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)；格式参考 Keep a Changelog。

## [未发布]

### 新增

* **豆瓣插件可以带登录 Cookie**：豆瓣允许把单个条目设成**仅自己可见**，游客视角不但列表里
  没有它，**连站点总数都会少报**——实测同一个列表游客 `看过(1395)` / 登录 `看过(1402)`，
  差的那条是《异兽魔都 第二季》（`35028938`：它的详情页游客只有 2.9KB 的壳、登录后是 100KB+，
  列表第 4 页游客 14 条、登录 15 条）。也就是说以前「填 0 抓全量」会**静默少一批数据**。
  抓取表单多一栏「豆瓣登录 Cookie」：整串 Header 值、两列导出（`dbcl2<TAB>值`）、
  只贴 `dbcl2` 裸值三种抄法都认，粘整页 HTML 会被拒；抓到一开始就存进
  `plugins_data/douban_fetch/config.toml`（和令牌同性质，这个目录不进 git），
  **之后留空即沿用**，勾「清除已存的 Cookie」才改回游客身份。命令行加 `--cookie`（同样留空沿用）。
  Cookie 过期时豆瓣不报错、只是安静地按游客渲染，所以插件会**读页面本身**（登录后才有
  「退出」链接、标题才会变成「我看过的影视」）并在发现是游客视角时写明「Cookie 没能换来登录态」；
  报告、快照详情和日志里的「认证」只出现**字段名和登录态**（`带 Cookie（dbcl2, ck）· 登录态`），
  值不回显（输入框是 password 型）、不进日志、不进快照。
  「已删除/隐藏差额」的说明也跟着改：游客身份抓的那一趟，差额里混着「仅自己可见」的条目。

* **统一日志 + `/logs` 实时日志页**：宿主自己记一份运行日志（`data/logs/`），
  插件的 `ctx.log()` 不再只存在内存里——同一句话既留在插件页可展开的最近 200 条，
  也写进 `data/logs/plugins/<插件 id>.log`，进程重启还在。
  顶部导航多一栏「日志」：`/logs` 每 2 秒刷一次、自动滚到最新，能按插件、级别（info/warning/error）
  和条数筛；插件列表页每个插件也有「看日志」直接跳过去。
  这个文件里**宿主和插件写在同一条时间线上**——加载完成、卸载、加载失败这些宿主行为
  第三列标「宿主」，插件自己记的标插件 id，于是「插件为什么没起来」不用去翻两个地方。
  文件代表**本次运行**：宿主启动后第一次写某个插件的日志会清掉旧内容（点「重载」不算启动，不会把
  刚打的日志抹），常驻服务写满 1 MB 自动轮转留 3 份。
  命令行（`omnishelf plugins …` / `omnishelf plugin <插件> <命令>`）**写的是同一个文件**——
  页面和 CLI 用的是同一套宿主对象与同一个日志句柄；一次性进程不做轮转，
  免得把常驻服务正在写的文件 rename 走、日志分叉成两份。
  脱敏仍然**完全归插件**：宿主不猜「哪个字符串在这个站点算口令」，落盘前由插件自己的
  `redact()` 处理（`docs/plugin-development.md` 第 8 节把它写成红线）。

* **插件 `douban_fetch`（豆瓣收藏抓取）**：抓看过/读过/玩过（及在看/想看等）三站列表，
  按 `subject_id` 累积成一份可直接用内置 importer 导入的 JSON。
  含反爬拦截页识别、增量续抓、快照索引与页面表单。

* **插件 `asmr_playlist`（ASMR.ONE 收藏抓取）**：从 asmr.one 的公开播放列表或自己的
  私人收藏抓 **全量 / 增量 / 指定范围**的作品，按 `works[].id` 累积成可直接导入的快照，
  并可直接入库（页面按钮 / `omnishelf plugin asmr_playlist fetch_import`）。
  配置自动记住，代理不通自动回退直连；支持续抓提示。
  私人收藏要认证：粘一个 `authorization` 令牌，或者填用户名密码现场登录（**密码不落盘**）；
  抓回来的自己打的分（`userRating`）会导入成条目评分。
  接口地址改成填根地址 `https://api.asmr.one/api/`，后面按抓取目标自动补全。

* **插件 `copymanga_fetch`（拷贝漫画书架抓取）**：同步自己账号下的书架收藏（只抓漫画——
  書架上的「動畫」在库里没有对应类型，抓下来也无处可放），
  **增量**只翻到本地水位线、**全量**顺带标出已取消收藏的，落成本地状态库 + 一份可直接导入的
  JSON，再点「同步并导入」用 importer `copymanga` 入库（页面按钮 /
  `omnishelf plugin copymanga_fetch fetch_import`）。站点地址、token、同步方式填一次就记住；
  没有 token 就填用户名密码现场登录换令牌（**密码不落盘**，令牌会留下，
  下次令牌框空着就直接复用存着的那个）。
  抓取本身用的是一份**随插件分发、一行未改**的独立工具包（`plugins/copymanga_fetch/copymanga/`，
  见该目录边的 `VENDOR.md`）：插件只负责表单、进度、落盘和入库。

* **插件 `picamanga_fetch`（哔咔收藏夹抓取）**：同步自己在哔咔（Picacomic）收藏夹里的漫画，
  **增量**只翻到本地已有的位置（日常一两次请求）、**全量**遍历所有页并顺带标出已取消收藏的，
  落成本地状态库 + 一份可直接导入的 JSON，再点「同步并导入」用 importer `picamanga` 入库
  （页面按钮 / `omnishelf plugin picamanga_fetch fetch_import`）。接口地址、令牌、同步方式填一次
  就记住；没有令牌就填邮箱口令现场登录换令牌（**口令不落盘**，令牌会留下，下次令牌框空着就复用）。
  哔咔的接口不返回收藏时间，所以增量边界靠「**连续**命中若干条本地已有的」判定（默认 5 条，
  一条就收手会被「取消后重新收藏」骗过去）；再加一条不依赖编号的兜底不变量——远端总数涨了、
  头部却一条新收藏都没翻到，就自动改跑一遍全量，宁可多花几个请求也不静默漏数据。
  抓取用的工具包（`plugins/picamanga_fetch/picamanga/`）是把一份外部备份脚本**重构**成与
  `copymanga_fetch` 同一套目录与文档规范的样子：纯标准库、只走 `urllib`、自己不碰凭据文件，
  凭据与落盘都归插件那一侧；逐条差异记在该目录的 `VENDOR.md`。
  这一家的接口在很多网络环境里直连不通，所以页面上有一栏**代理**（命令行 `--proxy`、
  配置项 `proxy`）：填了就这次同步的每个请求都只走它（连登录那次一起），报告和进度栏都写明
  走的哪条路；只写 `IP:端口` 按 HTTP 代理补协议，SOCKS 要写全 `socks5://…`；
  填个认不出来的地址会当场拒绝，不会悄悄换成直连再跑一次。

* **抓取进度条**：`douban_fetch`、`asmr_playlist`、`copymanga_fetch` 与 `picamanga_fetch`
  的抓取都在后台跑，点完按钮页面立刻
  出现一栏进度条 —— 实时显示翻到第几页、抓到多少条、已用多少秒，自己定时刷新，
  期间可以切到别的页面，回来还在跑。没跑完之前再点会提示「已经有一个抓取在跑了」。

* **本地音频标签读取器**（`src/omnishelf/audiotags.py`）：只用标准库读 MP3 / M4A / FLAC /
  WAV / AIFF 的标签与时长，不引第三方标签库（`mutagen`、`tinytag` 都不在依赖里，多一个依赖
  就多一条「装不上就跑不了」的路）；每个文件只 seek/read 头部那几百字节，不整读，超大封面
  只会被跳过。收进来的**只有导入要用的那 11 项**（标题、副标题、评分、备注、年份、艺术家、
  专辑艺术家、专辑、编号、时长、原始文件名），没有流派、封面、编码器、碟号、总曲数。
  ID3v2 的 `POPM` 评分现实里有两套口径——按百分制写的和按满值 255 写的——以 100 为界分别换算；
  M4A 的 `rtng` 原子在 iTunes 语义里是**内容分级**不是星级，所以不猜、宁可留空。
  `.ogg` / `.opus` / `.wv` / `.ape` 明确不认（前者要先验页 CRC 再重组分包，成本与收益不成比例），
  宁可在报告里说明读不了，也不给一份空数据冒充有标签。
  任何单个文件解析失败都只进 `errors`、**不往外抛**：一次扫描要过几千个文件，
  不能因为某个文件的标签块写得歪就整批停下来。

* **`omnishelf scan-music`：把「一个文件夹」读成一份可导入的清单**。深度遍历目录（不跟随目录
  软链，免得在环里绕不出来），只认上面那些后缀，整个跳过回收站那类垃圾目录——外置盘上
  `$RECYCLE.BIN` 里常躺着几万份音乐文件的副本，扫进来就是成倍的重复。
  产出的是 **JSON 清单**而不是直接入库：「抓」和「放」分开，扫几千个文件很慢，中途断了清单还在，
  导入反复试也不用重扫（`--import` 想顺手入库就顺手）。
  标题一律取标签里的歌曲名，**没有标签才退回文件名**（去掉后缀和开头的曲目号；
  `"Artist - Title.mp3"` 这种**不按 ` - ` 拆**——标题里带破折号的歌不少，猜错会把标题切成半句），
  退回的那条在清单里标 `title_from: "filename"`，文件名本身照旧存进 `extra.filename` 备用。

* **内置 importer `music_files` / `itunes_xml`**（注册表现在有 10 个 importer）：两者产出的都是
  `kind=music`，共用同一套 11 项字段口径（`build_music_draft`），所以本地文件和 iTunes 资料库
  导进来的是同一种形状。`music_files` 认 `scan-music` 的清单，也认手写的 `{"items": [...]}`
  或 `[...]`；`itunes_xml` 用标准库 `plistlib` 直读「资料库导出 XML」，跳过 `Track Type` 不是
  `File` 的（流媒体链接、已被移除的文件）和播客节目，`Rating` 的百分制星级除以 10 落成 0-10，
  原始文件名从 `Location` URL 里取（`file://localhost/Users/…`、`file:///Users/…`、
  Windows 的 `file://localhost/D:/Music/…` 三种写法都吃得下）。
  艺术家和专辑艺术家**都算 `creators`**（听歌时「谁」是两种问法，书架上都是这一条的创作者），
  同时两个字段各留一份原文，否则合完就分不出谁是谁；多值切分（`"A & B"`、`"やなぎなぎ/麻枝准"`）
  复用 `split_creators()` 那套「括号里不拆」的规则，通过它新加的可选分隔符参数接进来，
  **没有放宽全局创作者切分规则**。

* **音乐判重看内容，不看 ID**：`kinds.py` 里 `music` 声明 `identity_fields=("album", "artists")`
  ——同一首歌可以被拷成任意个文件，本地文件没有稳定 ID 可依，所以标题 + 专辑 + 艺术家三者相同
  就判同一作品；`music_files` 因此**不写 `external_ids`**。iTunes 的记录有 `Persistent ID`
  （资料库重建前后都稳定，`Track ID` 会随重排变，只作兜底），写进 `external_ids.itunes`：
  同一份 XML 重复导入走 ID 认亲，拿 XML 去补本地文件导入的那一条走内容判重合并。

* **宿主 `audio` 接口 + 插件 `music_import`（本地音乐导入）**：宿主把「哪些后缀算音乐」和
  「读标签生成清单」开放成 `ctx.host.audio_suffixes()` / `ctx.host.scan_audio()`
  （插件 `requires = ["audio", "import"]`）。插件页面上两个按钮对应两种挑法——
  **选一个文件夹**（深度遍历）或**选多个文件**（只读选中的）——都用**系统选择框**：
  macOS 上走 `osascript` 起「访达」的挑选对话框（只用标准库 `subprocess`，路径不经过 shell
  所以没有注入问题），别的平台没有这个对话框就退化成手填路径，功能一样跑。
  扫描在后台进行、进度条实时显示读到第几个文件；清单落在 `plugins_data/music_import/`
  （插件唯一可写的目录）之后交给 `music_files` 入库。**插件不解析标签、不碰数据库**，
  所以插件扫出来的和 `omnishelf scan-music` 完全一致。

### 修复

* **带自己工具包的插件热重载不干净**：宿主重载时只丢掉入口模块，插件目录下自己的子包
  （`copymanga_fetch/copymanga/`、`picamanga_fetch/picamanga/`）还缓存着旧代码，
  于是「入口是新的、子包是旧的」——改了子包（例如给配置类加一个字段）刷新后就会出现
  `TypeError: PicamangaConfig.__init__() got an unexpected keyword argument 'proxy'`
  这类看不懂的报错。卸载时现在会把这些模块一并从 `sys.modules` 里清掉。
  **已经开着的服务要重启一次**才能拿到这个修复（修复本身在宿主里）。

* **豆瓣 `页码=0` 抓全量却只翻一页就停**：页面没渲染分页区块时，插件把「看不到分页」当成
  「只有 1 页」——先抓 1 页、再把页码改成 `0` 抓全量，第二趟照样停在第 1 页，报告写着
  「已到最后一页（1）」，看着像抓完了。总页数现在**只增不减**：页码条认出的末页、
  这一页推出来的、豆瓣标题里报的条数换算出的下限，三者取最大。
  顺带把「被删除/隐藏的差额」改成只有**这一趟真的翻到了尽头**才算（抓到一半时留空，
  免得那个数看着像抓漏）。页码输入框默认值也从 `1` 改成 `0`（全抓）：少翻几页可能
  永远少一批数据，多翻几页只是慢一点。

### 变更

* **抓取插件的 `*.meta.json` / `*.detail.json` 不再存逐页流水**（`notes` / `page_notes`）：
  这几个文件的作用是「上次抓到什么」的**状态卡**（目标、模式、页码、条数、抓没抓全、代理），
  而逐页明细每次抓取整体覆盖、之后没人回头看，跟新加的日志文件是同一份内容的两具尸体。
  现在这一份历史只有一个家——`data/logs/plugins/<插件 id>.log`（`/logs` 页看）；
  插件页面和命令行上的「同步明细 / 进度」照旧，从内存里的 report 读。
  同时四个抓取插件把「每翻完一页」那一行也上报给宿主日志（上报前先过插件自己的 `redact()`），
  所以关掉页面、甚至进程已经退出，那次抓取翻到第几页仍然查得到。

* **全量抓取不再被「单次页数上限」中断，四个抓取插件一起统一**（`douban_fetch` /
  `asmr_playlist` / `picamanga_fetch` / `copymanga_fetch`）：填全抓就得一路翻到末页，
  中途不会因为「翻够了」被掐断——上限把请求数当成了安全阀，代价是静默少一批数据。
  页面上的「单次页数上限」「起始页」两栏（后者原本只有豆瓣和 `asmr_playlist` 有）、
  配置项 `max_pages` / `start_page`、命令行
  `--max-pages` / `--start-page` 一起去掉（本地 `config.toml` 里残留的这两个旧键会被忽略，
  想彻底清掉就手工删掉那一行）。起页现在只由页码范围决定：续抓直接填 `47-`，
  分段就填 `1-20`、`21-40`；中途失败时报告会写明停在第几页、下一步该填什么。
  防限速只留给豆瓣一家——它抓的是网页、风控最严，每连翻 20 页强制歇 60 秒，歇完接着翻，
  进度栏会写明；另外三个走的是接口，只按填的翻页间隔走，不插强制休息。
  注意：如果 `plugins_data/douban_fetch/config.toml` 里以前手工存过 `pages = "1"`，
  它仍然优先于默认值——想让默认全抓就把它改成 `0`（存量配置优先是刻意的设计，不算回归）。

* `create_item()` / `update_item()` / `ItemDraft` / 导入映射 / 导出 JSON / 插件拿到的条目 dict：
  `creator` 字段统一改名并改为列表 `creators`（**破坏性**：读写这些接口的代码要跟着改）。

* **`data/` 不再是 submodule**：改成主仓库旁边的一个普通独立 git 仓库（主仓库 `.gitignore`
  忽略它）。主仓库的提交历史里不会再出现「submodule 指针更新」的记录，两个仓库各自
  commit / push。`omnishelf data-commit` 相应地只提交 `data/`，`--no-pointer` 选项已移除。
  换机器时改为 `git clone <数据仓库> data`。

* **删掉没人用的 enrich 骨架**：`omnishelf.services.enrich.run_enrichment()`、
  `omnishelf.enrichers` 里的 `Enricher` 协议从写下来那天起就只有一句「本阶段只有协议，没有实现」，
  仓库里没有任何实现，Web 和 CLI 也从不走它——只有测试在构造假 enricher 自己喂自己。
  现在整条删掉；真要补数据请走插件，或者在详情页手动填。

* **开发过程记录不再随仓库分发**：`docs/decisions.md`（决策日志）和 `docs/importing-real-data.md`
  （接入真实数据的实操手册）是**开发过程**的记录，会带进实现细节、试错和当时的真实数据规模，
  不适合放在给使用者看的仓库里，已经搬到仓库同级、不进 git 的本地目录。
  里边的内容分两类处理：**需要长期遵守的约定全部归档进 `docs/development.md`**
  （分层与铁律、kind 注册表、判重与来源、字段口径、importer 规则、排错速查、发布流程、已知边界），
  一次性过程细节留在本地。代码注释和文档里原先指向那两份文件的引用，改成指向
  `docs/development.md` 的**具名小节**（「判重与来源」「字段口径」），小节以后重新编号也不会指空。
  `.gitignore` 保留这两条忽略规则，是防一手：哪天又把这些内容写回 `docs/` 时不会被误提交。

* **补上 `LICENSE` 文件**：`pyproject.toml` 一直声明的是 MIT，但仓库里没有那份文本，
  公开仓库看过来就是「声明了许可却没有 LICENSE」。现在补齐（MIT，版权署名 OmniShelf）。

* **版本号回到 `0.1.0-alpha`**：`pyproject.toml` 与 `omnishelf.__version__` 从 `1.0.0` 改成
  `0.1.0-alpha`——核心闭环已经能用，但对外不叫正式版。已有的 `v1.0.0` 标签留在历史里不动；
  插件各自的 `metadata.toml` 版本是**独立计数**的，不受这次改动影响。

### 破坏性变更（插件作者）

* **插件从「两个孤立函数」改成继承抽象基类**：插件现在是
  `omnishelf.plugins.api.Plugin` 的子类，`start()` / `stop()` 是抽象方法，
  `render_page()` / `handle_post()` 有默认实现。旧写法的插件**不会再被加载**，
  会得到「没有 Plugin 子类」的明确错误。
  迁移（三行）：

  ```python
  # 旧：def start(ctx): ... / def stop(ctx): ...
  # 新：
  from omnishelf.plugins.api import Plugin          # ← 插件唯一允许 import 的宿主模块

  class MyPlugin(Plugin):
      def start(self): ...                          # 用 self.host / self.config / self.log() …
      def stop(self): ...
  ```

  `metadata.toml` 里的 `start = "..."` / `stop = "..."` 两个键已删除（用类名定位）；
  `entry` 可以写成 `"plugin.py:MyPlugin"`，只写文件名时宿主自动找唯一的 `Plugin` 子类。
  仓库内的 `plugins/kind_chart`（模板）与 `plugins/douban_fetch` 都已迁移。

### 修正

* 补齐文档/示例里漏改的旧名字。

* **粘贴令牌时带进来的引号和分隔符**：`copymanga_fetch` / `picamanga_fetch` 的表单把
  `token="xxx";`、`authorization: Bearer "xxx";` 这种连着抄下来的整串只剥一层，剩下的引号会
  跟着送去认证，表现为「令牌明明没错却认证失败」。现在一轮一轮剥到干净为止。

* **测试不再可能出网**：抓取类插件的测试如果忘了换掉 `urlopen`，以前会真的把（合成的）凭据发到
  对应站点上去。`tests/conftest.py` 加了一个兜底夹具，任何没装假站点的请求直接报错。

### 测试

* 834 条测试（全部离线、只用合成数据与临时目录），`mypy` 无告警。
  音乐这一摊新加 `tests/test_audiotags.py`、`tests/test_music_import.py`、
  `tests/test_plugin_music_import.py` 和一份合成的 iTunes 资料库导出 XML 夹具
  （假 `Persistent ID`、`.invalid` 域、假路径）。音频文件本身**不存二进制**：
  `tests/audio_synth.py` 现场拼出 MP3 / M4A / FLAC / WAV / AIFF 的字节——
  测试断言的就是「按这个协议写出来的标签，读回来该是什么」，构造器留在代码里，
  改协议细节时改它就够。

## [1.0.0] - 2026-10-12

**正式发布：项目定名 OmniShelf。**

### 破坏性变更

* **包名与命令改名**：Python 包 `collection` → `omnishelf`，命令行 `collection <cmd>` → `omnishelf <cmd>`，
  入口点 `omnishelf.cli:main`（旧命令不再存在，请改用 `omnishelf`）。
* **环境变量改名**：`COLLECTION_*` → `OMNISHELF_*`
  （`OMNISHELF_DATA_DIR` / `OMNISHELF_MIGRATIONS_DIR` / `OMNISHELF_FIXTURES_DIR` /
  `OMNISHELF_GIT_BIN` / `OMNISHELF_PLUGINS_DIR` / `OMNISHELF_PLUGINS_DATA_DIR`）。
  **旧名字仍然有效**（作为兼容回退），新名字优先。
* 数据库文件名**仍是 `collection.db`**：已有数据目录不用动。
* `repo.stats()` 里「没有状态」那一档的键统一为 `unset`（中文标签「未设置」）。

### 功能

* **收藏库**：单表 `items` + `extra` JSON 列；kind 注册表（图书/漫画/影视/音乐/游戏）声明类型专属字段。
* **导入**：`import` 支持多文件；原始文件按来源存档到 `data/imports/<来源>/`；内置 importer
  `picamanga`、`copymanga`、`douban_games`、`douban_movies`、`douban_books`
  与示例用的 `json_movies` / `json_books`（另有 CSV 基类）。
* **判重**：同源只认唯一标识，标题只用于非同源；模糊匹配进 `pending_merges` 待复核，绝不自动合并。
* **Web（零构建）**：列表（搜索 / 基础筛选 / 高级筛选：评分区间·入库日期·创作者·标签包含排除 /
  点表头排序 / 分页 / 封面缩略图 / 状态与评分行内编辑 / 创作者可点）、详情（四个 block 按井字排列，
  通用信息与专属字段分开）、封面上传、待复核、**上传数据页**、`/api/*` JSON 接口。
* **插件系统**：`plugins/<插件>/metadata.toml` 目录即插件，支持**热重载**；插件必须实现
  `start(ctx)`/`stop(ctx)`，可实现自己的页面（`render_page`/`handle_post`）；宿主通过 `ctx` 暴露
  **只读数据查询接口**；配置与数据写在 `plugins_data/`（不进 git）。示例/模板插件：`plugins/kind_chart/`。
* **数据版本化**：`data/` 作为独立（通常私有）repo 以 submodule 挂载；`data-commit` 前会
  `PRAGMA wal_checkpoint(TRUNCATE)`，保证提交出去的库快照完整。
* **CLI**：`init/import/list/show/edit/export/demo/serve/data-commit/review/plugins`，支持 `--json`。
* **离线**：运行期不访问外部网络；前端资源（HTMX / Pico.css）随仓库内置。

### 文档

* README 收敛为使用者文档；开发内容移到 [`docs/development.md`](docs/development.md)。
* 插件开发指南与规范：[`docs/plugin-development.md`](docs/plugin-development.md)。
* 当时还随仓库分发了两份开发过程记录（真实数据接入实操、设计决策记录），现在已移出仓库，
  其中需要长期遵守的约定归档进了 [`docs/development.md`](docs/development.md)（见上面「变更」）。

### 测试

* 411 条测试（全部离线、只用合成数据），`mypy` 无告警。
