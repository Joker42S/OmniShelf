"""拷贝漫画（copy4000.com）书架收藏抓取：把私人书架抓成可直接导入 OmniShelf 的 JSON。

三件事：

1. **同步** 自己账号下的书架收藏（只抓漫画）。认证先用手上的 ``token``（Cookie 里那个
   ``token`` 字段），不行就用用户名+密码登录换一个。抓的是「按加到書架時間降序」的收藏列表，
   所以两种模式都成立：

   * **增量**（默认）：从最前面翻，一整页都没超过本地水位线就停 —— 日常刷新通常一两次请求；
   * **全量**：遍历所有页，本地有、远端已经没有的条目标记成「已取消收藏」（不删，方便回溯）。

2. **落两份文件**（都在 ``plugins_data/copymanga_fetch/``）：
   ``copymanga_favorites.json`` 是同步工具自己的状态库（水位线、首次/最近见到、取消收藏都记在里面，
   下次增量靠它）；``copymanga_import.json`` 是给宿主导入用的 —— 只含**还没取消收藏**的条目，
   形状正好是内置 importer ``copymanga`` 认的 ``{"items": [...]}``。

3. **导入**：调宿主的 ``import`` 接口（``self.import_file("copymanga", ...)``），
   走的是和上传页/CLI 完全一样的链路：来源级判重、写 ``import_logs``、原始文件存档。
   判重靠 ``comic_uuid`` → ``external_ids.copymanga``，所以重复导入同一批只会「未变化」。

抓取本身**不在这个文件里实现**：插件目录里带了一份 ``copymanga/`` 工具包（原样复制，一行没改，
见 ``VENDOR.md``），本文件只做「表单 → 配置 → 调用它 → 把结果变成页面/导入」这一层。
之所以要往 ``sys.path`` 里补插件目录：宿主的插件加载器是 exec 单个入口文件，
包与包之间的相对导入得先能找到根。

配置（``plugins_data/copymanga_fetch/config.toml``，页面上「保存默认值」或抓取时自动落盘）：

=================  ==========================================================
``host``           站点地址，默认 ``https://copy4000.com``；贴整条书架页 URL 也行
``token``          登录 token（**会存进本地配置**，和 Cookie 一个性质）
``username``       账号（配合页面上的密码框；密码本身**不落盘**）
``mode``           默认同步方式：``incremental`` 增量 / ``full`` 全量
``page_size``      每页条数（1~500，默认 100，越大请求越少）
``delay``          翻页之间的间隔秒数（默认 0.3）
``timeout``        单次请求超时秒数（默认 40）
``include_raw``    默认 false：状态库里不保留接口原始返回（体积能小一大半）
=================  ==========================================================

实现上遵守插件规范：

* 继承 ``omnishelf.plugins.api.Plugin``（**插件唯一允许 import 的宿主模块**）；
* 只用标准库 + 这个 SDK + 插件目录里自带的 ``copymanga`` 包；
* 运行时数据只写 ``ctx.data_dir``；
* 同步在**点按钮之后于后台线程**跑（页面靠轮询它刷新进度条），不在 ``start()`` 里做重活；
  点完按钮回的那一屏一律先画进度条，结果交给下一次轮询——秒级失败也不会把报告
  塞进 POST 响应后就把轮询链路绕过去；
* 出错自己兜住：配置写坏了有默认值 + 明确提示，认证失败/接口报错都不炸页面；
  密码在任何回显和日志里都会被抹掉。

已知边界（和 asmr_playlist 一样，是有意为之）：

* 列表里**被取消收藏**的作品不会从库里删掉——导入接口只新增/更新，不删；
  所以导入文件里干脆不放它们（状态库里仍然留着，带 ``removed`` 标记）；
* 增量同步只会顺带刷新「翻到的那几页」里老条目的字段（最新话、人气等），
  没翻到的老条目不动；要整库刷新老字段就抓一次全量；
* token 会过期：失效时填了用户名密码就自动重新登录一次，只留 token 的话换一个再抓；
* 只抓漫画：书架上的「動畫」在本库里没有对应的类型和 importer，抓下来也无处可放，
  所以工具支持它、插件却不提供这个选项。
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from omnishelf.plugins.api import Plugin, cmd

# 抓取逻辑在随插件分发的 copymanga 包里（原样复制，未改动）：入口文件是被 exec 的，
# 所以先把插件目录补进 sys.path，再 import 那个包。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from copymanga import (  # noqa: E402  （必须等上面那行把目录加进来）
    ApiError,
    AuthError,
    ConfigError,
    CopymangaClient,
    CopymangaError,
    ExportConfig,
    FavoritesExporter,
    JsonStore,
)
from copymanga.config import KIND_LABEL  # noqa: E402

DEFAULT_HOST = str(ExportConfig().host)
#: 内置 importer 的名字（书架类来源）
IMPORTER_NAME = "copymanga"
#: 状态库 / 导入文件 / 元信息（都在 ctx.data_dir 下）
STORE_NAME = "copymanga_favorites.json"
IMPORT_NAME = "copymanga_import.json"
META_NAME = "copymanga_fetch.meta.json"
#: 每页条数上限（工具实测接口支持到 500）
MAX_PAGE_SIZE = 500

MODE_LABEL = {"incremental": "增量", "full": "全量"}
#: 只抓漫画：书架上的「動畫」在库里没有对应类型，也就没有能接它的 importer
FETCH_KIND_LABEL = "漫画"
AUTH_REQUIRED = "抓自己的书架要先认证：粘一个 token，或者填用户名和密码"
BAD_HOST = "站点地址要写成 https://copy4000.com 这样（不能为空）"

DEFAULTS: dict[str, Any] = {
    "host": DEFAULT_HOST,
    "token": "",
    "username": "",
    "mode": "incremental",
    "page_size": 100,
    "delay": 0.3,
    "timeout": 40,
    "include_raw": False,
}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def normalize_host(value: Any) -> str:
    """站点地址：必须是 http(s)；贴了整条书架页 URL 就只取 ``协议://域名`` 那截。"""
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def normalize_token(value: Any) -> str:
    """把用户粘的 token 变成裸值：``Token xxx`` / ``Cookie 里那串`` 都认，只留 xxx。

    从抓包面板或 Cookie 里连着抄时常带上引号和分隔符（``token="xxx";`` 这种），
    所以前后这些字符要一轮一轮剥，单剥一轮会把剩下的引号带去认证。
    """
    text = str(value or "").strip()
    while True:
        stripped = text
        for prefix in ("Token ", "token ", "token=", "token;", "Bearer ", "bearer "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
                break
        stripped = stripped.strip("\"';,").strip()
        if stripped == text:
            return text
        text = stripped


def normalize_mode(value: Any) -> str:
    """同步方式：``full`` 之外一律当增量（页面的开关不勾就是 incremental）。"""
    text = str(value or "").strip().lower()
    return "full" if text.startswith("full") else "incremental"


def clamp_int(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def clamp_float(value: Any, default: float, *, low: float, high: float) -> float:
    try:
        return max(low, min(float(value), high))
    except (TypeError, ValueError):
        return default


def effective_config(ctx: Any) -> dict[str, Any]:
    """默认值 + 用户配置，每个字段都夹到合法范围（配置写坏了也不能炸）。"""
    config = dict(DEFAULTS)
    raw = ctx.config or {}
    for key, value in raw.items():
        if key in config:
            config[key] = value

    config["host"] = normalize_host(config.get("host")) or DEFAULT_HOST
    config["token"] = normalize_token(config.get("token"))
    config["username"] = str(config.get("username") or "").strip()
    config["mode"] = normalize_mode(config.get("mode"))
    config["page_size"] = clamp_int(config.get("page_size"), int(DEFAULTS["page_size"]),
                                    low=1, high=MAX_PAGE_SIZE)
    config["delay"] = clamp_float(config.get("delay"), float(DEFAULTS["delay"]),
                                  low=0.0, high=10.0)
    config["timeout"] = clamp_int(config.get("timeout"), int(DEFAULTS["timeout"]),
                                  low=5, high=120)
    config["include_raw"] = bool(config.get("include_raw", DEFAULTS["include_raw"]))
    return config


class _FakeContext:
    """只为复用 effective_config 的夹取逻辑（不碰磁盘）。"""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)


def redact(text: Any, *secrets: str) -> str:
    """把回显/日志里的敏感值抹掉（工具抛的异常可能带上 URL 或响应片段）。"""
    message = str(text if text is not None else "")
    for secret in secrets:
        value = str(secret or "")
        if len(value) >= 4:
            message = message.replace(value, "***")
    return message


# --------------------------------------------------------------------------
# 落盘：状态库 / 导入文件 / 元信息
# --------------------------------------------------------------------------

def store_path(data_dir: Path) -> Path:
    """同步工具自己的状态库（增量的水位线、取消收藏标记都在里面）。"""
    return data_dir / STORE_NAME


def import_path(data_dir: Path) -> Path:
    """给宿主 importer 的文件：只有还没取消收藏的条目。"""
    return data_dir / IMPORT_NAME


def meta_path(data_dir: Path) -> Path:
    """上次抓取的概况（页面列文件时只读这个小文件，不去解析大 JSON）。"""
    return data_dir / META_NAME


def _atomic_write(path: Path, text: str) -> None:
    """先写 .part 再改名，避免宿主/用户读到写了一半的文件。"""
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def build_import_payload(store: JsonStore, *, host: str, fetched_at: str) -> dict[str, Any]:
    """把状态库里的活跃条目整理成 importer ``copymanga`` 认的 ``{"items": [...]}``。

    排序按书架位次（0 = 最新加进书架）；接口原始返回不写进来——入库用不到它，
    留着只是让导入文件胖一倍。
    """
    items = sorted(store.active(), key=lambda item: (item.shelf_position, item.name))
    return {
        "source": f"{host.rstrip('/')}/web/person/shujia",
        "host": host.rstrip("/"),
        "fetched_at": fetched_at,
        "total": len(items),
        "items": [item.to_dict(include_raw=False) for item in items],
    }


def save_import_file(data_dir: Path, payload: Mapping[str, Any]) -> Path:
    """导入文件写**紧凑 JSON**（机器读的，人看结构和概况有旁边的元信息）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    target = import_path(data_dir)
    _atomic_write(target, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return target


def save_meta(data_dir: Path, report: Mapping[str, Any]) -> Path:
    """写状态卡：``notes`` 这类逐页流水**不进**这里（归宿主日志 ``data/logs/plugins/``）。"""
    target = meta_path(data_dir)
    slimmed = {key: value for key, value in report.items() if key != "notes"}
    _atomic_write(target, json.dumps(slimmed, ensure_ascii=False, indent=2))
    return target


def load_meta(data_dir: Path) -> dict[str, Any]:
    path = meta_path(data_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def local_files(data_dir: Path) -> list[dict[str, Any]]:
    """本地已有的文件（页面那张「抓完的东西」表）。"""
    rows: list[dict[str, Any]] = []
    for label, path in (("状态库", store_path(data_dir)), ("导入文件", import_path(data_dir))):
        if path.is_file():
            rows.append({
                "what": label,
                "name": path.name,
                "size_mb": round(path.stat().st_size / 1048576, 2),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(path.stat().st_mtime)),
            })
    return rows


def size_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / 1048576, 2)
    except OSError:
        return 0.0


# --------------------------------------------------------------------------
# 调用工具：进度代理 + 一次同步
# --------------------------------------------------------------------------

@dataclass
class SyncOutcome:
    """一次同步的结果（``report`` 是给页面/CLI 用的 dict，不含条目本体）。"""

    report: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class ProgressClient:
    """套在真正的客户端外面，只为了两件事：报进度、把换到的令牌当场交出来。

    ``FavoritesExporter`` 允许直接给它一个 client（这是工具文档里的用法二），
    同步过程只碰这里转发的几个成员，所以工具一行不用改就能拿到逐页进度。
    """

    def __init__(self, client: Any, *, on_progress: Any = None,
                 on_token: Any = None) -> None:
        self._client = client
        self._on_progress = on_progress
        self._on_token = on_token
        self.requests = 0
        self.rows_seen = 0
        self.remote_total = 0
        self.page_size = 0

    # —— 转发：exporter 会当成真正的 client 用 ——
    @property
    def config(self) -> Any:
        return self._client.config

    @config.setter
    def config(self, value: Any) -> None:
        self._client.config = value
        try:
            self.page_size = int(value.page_size)
        except (AttributeError, TypeError, ValueError):
            self.page_size = 0

    @property
    def token(self) -> Any:
        return self._client.token

    @property
    def user(self) -> Any:
        return self._client.user

    def close(self) -> None:
        self._client.close()

    def emit(self, message: str) -> None:
        if self._on_progress is not None:
            self._on_progress(message, self)

    def ensure_authenticated(self) -> Any:
        self.emit("正在确认登录状态…")
        token = self._client.ensure_authenticated()
        if self._on_token is not None:
            self._on_token(token)
        user = self.user or {}
        who = str(user.get("nickname") or user.get("username") or "?")
        self.emit(f"已登录：{who}")
        return token

    def fetch_collect_page(self, kind: str = "comics", free_type: int = 1,
                           offset: int = 0, limit: Any = None, *, ordering: Any = None) -> Any:
        rows, total = self._client.fetch_collect_page(
            kind, free_type, offset, limit, ordering=ordering)
        self.requests += 1
        self.rows_seen += len(rows)
        self.remote_total = max(self.remote_total, int(total or 0))
        label = KIND_LABEL.get(kind, kind)
        size = int(limit or self.page_size or 0)
        page_no = offset // size + 1 if size else self.requests
        self.emit(f"第 {self.requests} 次请求 · {label} 第 {page_no} 页 · "
                  f"这页 {len(rows)} 条 · 书架共 {self.remote_total} 条")
        return rows, total

    @property
    def expected_pages(self) -> int | None:
        """进度条的分母：按接口报的总数估算还要请求几次（估算，抓超了页面会夹住）。"""
        if not self.remote_total or not self.page_size:
            return None
        return max(1, math.ceil(self.remote_total / self.page_size))


def build_export_config(
    *, data_dir: Path, host: str, token: str, username: str, password: str,
    mode: str, page_size: int, delay: float,
    timeout: int, include_raw: bool,
) -> ExportConfig:
    """把插件这一侧的选择翻译成工具的 :class:`ExportConfig`。

    只抓漫画（``kinds``）：動畫没有对应的 importer，抓下来也无处可放。
    ``max_pages=None``：全量就一路翻到末页，没有单次页数上限（工具里那个安全阀留给它自己用）。
    刻意不填 ``config_path``／开着 ``persist_token=False``：令牌由插件自己存进
    ``config.toml``（工具的续期只会往 JSON 配置文件里写，那是它自己的目录约定）。
    """
    return ExportConfig(
        host=host.rstrip("/"),
        mode=normalize_mode(mode),
        token=normalize_token(token),
        username=str(username or "").strip(),
        password=str(password or ""),
        persist_token=False,
        data_dir=str(data_dir),
        store_name=STORE_NAME,
        page_size=int(page_size),
        max_pages=None,
        timeout=float(timeout),
        request_delay=float(delay),
        include_raw=bool(include_raw),
        kinds=("comics",),
        snapshot=False,
        quiet=True,
    ).validate()


def run_sync(
    ctx: Any,
    *,
    host: str,
    token: str,
    username: str,
    password: str,
    mode: str,
    page_size: int,
    delay: float,
    timeout: int,
    include_raw: bool,
    on_progress: Any = None,
) -> tuple[SyncOutcome | None, str]:
    """真正干活的那一步：配置 → 同步 → 写导入文件。

    返回 ``(结果, 错误信息)``；出错时结果=None，错误信息已经抹掉敏感值。
    ``on_progress(message, client)`` 每请求一页给一次（页面的进度条用它）。
    """
    secrets = (token, password)
    try:
        config = build_export_config(
            data_dir=ctx.data_dir, host=host, token=token, username=username,
            password=password, mode=mode, page_size=page_size,
            delay=delay, timeout=timeout,
            include_raw=include_raw,
        )
    except ConfigError as exc:
        return None, f"配置有问题：{redact(exc, *secrets)}"

    notes: list[str] = []

    def sink(message: str, client: Any = None) -> None:
        text = redact(message, *secrets)
        notes.append(text)
        # 打码之后才上报：这行会进宿主的日志文件，保证没有凭据是插件这边的责任。
        ctx.log(text)
        if on_progress is not None:
            on_progress(text, client)

    previous = JsonStore(str(store_path(ctx.data_dir))).load()
    known = previous.watermark()
    sink(f"本地状态库 {len(previous)} 条"
         + (f"（水位线 {known}）" if config.mode == "incremental" and known is not None else "")
         + f"，开始{'全量' if config.mode == 'full' else '增量'}同步…")

    inner = CopymangaClient(config)

    def keep_token(new_token: Any) -> None:
        """登录/续期换来的令牌**当场**存回配置：同步哪怕中途出事也不白登一次。"""
        value = normalize_token(new_token)
        if value and value != str(config.token or ""):
            _persist_token(ctx, value)

    wrapper = ProgressClient(inner, on_progress=sink, on_token=keep_token)
    # 套一层进度记录：exporter 只按名字用 client（duck typing），所以这里换掉它
    # 也照样跑——这样不改工具一行就能拿到每页的进度。
    exporter = FavoritesExporter(client=wrapper, config=config)  # type: ignore[arg-type]
    started = time.time()
    try:
        report = exporter.sync(mode=config.mode)
    except AuthError as exc:
        wrapper.emit(f"认证失败：{redact(exc, *secrets)}")
        return None, f"认证失败：{redact(exc, *secrets)}"
    except ApiError as exc:
        return None, f"接口出错：{redact(exc, *secrets)}"
    except ConfigError as exc:
        return None, f"配置有问题：{redact(exc, *secrets)}"
    except CopymangaError as exc:
        return None, f"同步失败：{redact(exc, *secrets)}"
    except Exception as exc:                        # 意外也不能炸页面
        ctx.log(f"同步失败：{type(exc).__name__}: {redact(exc, *secrets)}")
        return None, f"同步失败：{type(exc).__name__}: {redact(exc, *secrets)}"
    finally:
        keep_token(wrapper.token)
        inner.close()

    fetched_at = time.strftime("%Y-%m-%d %H:%M:%S")
    payload = build_import_payload(JsonStore(str(config.store_path)).load(),
                                   host=config.base_url, fetched_at=fetched_at)
    import_file = save_import_file(ctx.data_dir, payload)

    data = report.to_dict()
    counts = data.get("counts") or {}
    summary = {
        "mode": report.mode,
        "host": report.host,
        "user": dict(report.user or {}),
        "remote_total": int(counts.get("total_remote") or wrapper.remote_total or 0),
        "local_total": int(counts.get("total_local") or 0),
        "added": int(counts.get("added") or 0),
        "updated": int(counts.get("updated") or 0),
        "unchanged": int(counts.get("unchanged") or 0),
        "restored": int(counts.get("restored") or 0),
        "removed": int(counts.get("removed") or 0),
        "requests": report.pages,
        "fetched": report.fetched,
        "stop_reason": report.stop_reason,
        "ordering": report.ordering,
        "ordering_verified": report.ordering_verified,
        "fell_back_to_full": report.fell_back_to_full,
        "watermark": report.watermark,
        "elapsed": round(time.time() - started, 1),
        "import_items": len(payload["items"]),
        "import_file": str(import_file),
        "import_mb": size_mb(import_file),
        "store_file": str(config.store_path),
        "store_mb": size_mb(Path(config.store_path)),
        "include_raw": config.include_raw,
        "auth_note": "",
        "page_size": config.page_size,
        "fetched_at": fetched_at,
    }
    return SyncOutcome(report=summary, notes=notes[-60:]), ""


# --------------------------------------------------------------------------
# 导入
# --------------------------------------------------------------------------

def import_snapshot(ctx: Any, file_path: Path) -> dict[str, Any]:
    """把导入文件交给宿主的 **import 接口** 入库（importer ``copymanga``）。

    走的是和上传页/CLI 完全一样的链路（来源判重、写 import_logs、原始文件存档）。
    出错**不抛给页面**：同步已经成功了，返回带 ``error`` 的摘要让页面提示手动导入。
    """
    importer = IMPORTER_NAME
    try:
        available = list(ctx.host.importer_names())
        if importer not in available:
            return {
                "error": f"宿主没有注册 {importer} importer（可选：{', '.join(available) or '无'}）",
                "path": str(file_path),
                "importer": importer,
            }
        result = ctx.host.import_file(importer, file_path)
    except Exception as exc:                        # FileNotFoundError / ValueError / 写库错误
        return {"error": f"{type(exc).__name__}: {exc}",
                "path": str(file_path), "importer": importer}
    return dict(result) | {"importer": importer, "path": str(file_path)}


def importer_available(ctx: Any) -> bool:
    try:
        return IMPORTER_NAME in list(ctx.host.importer_names())
    except Exception:
        return False


def manga_count(ctx: Any) -> int | None:
    """库里现有多少条漫画（页面上的参考数字；查询接口出问题就不显示）。"""
    try:
        return int(ctx.host.count_items(kind="manga"))
    except Exception:
        return None


# --------------------------------------------------------------------------
# 同步任务：进度条要实时更新，所以放在后台线程里跑
# --------------------------------------------------------------------------
#
# 全量同步一个几百条的书架要翻好几页，账号密码登录还要多等一次请求；让它占着请求
# 线程等，浏览器只能转圈。所以 ``handle_post`` 只做校验 + 起线程 + 立刻返回带进度条的
# 页面，``run_sync(progress=…)`` 每请求一页把进度回写到这里，页面用 HTMX 定时轮询自己。
#
# 状态放在模块全局（进程内）：插件热重载会换掉模块 globals，正在跑的线程继续把文件
# 写完，但内存态不会留下「永远显示运行中」的脏状态。

_JOBS_LOCK = threading.Lock()
_JOB: dict[str, Any] | None = None

#: 在跑的任务超过这个时长就当作卡住，允许用户再点一次
JOB_TIMEOUT = 1800.0

#: 进度条刷新间隔（秒）
POLL_SECONDS = 1.0


def _start_job(*, label: str, do_import: bool) -> dict[str, Any] | None:
    """登记一次同步任务；已经有在跑的就返回 ``None``（同一插件同时只跑一个）。"""
    global _JOB
    with _JOBS_LOCK:
        current = _JOB
        if (current is not None and current["state"] == "running"
                and time.time() - current["started_at"] < JOB_TIMEOUT):
            return None
        _JOB = {
            "id": uuid.uuid4().hex[:12],
            "label": label,
            "do_import": do_import,
            "state": "running",
            "viewed": False,
            "started_at": time.time(),
            "elapsed": 0.0,
            "requests": 0,
            "expected_pages": None,
            "remote_total": None,
            "fetched_count": 0,
            "item_count": 0,
            "note": "正在确认登录状态…",
            "notes": [],
            "report": None,
            "import_report": None,
            "error": "",
        }
        return dict(_JOB)


def _job_view() -> dict[str, Any] | None:
    """给页面看的任务快照（进度条 + 最近几行明细）。"""
    with _JOBS_LOCK:
        job = _JOB
        if job is None:
            return None
        view = dict(job)
        view["notes"] = list(job["notes"])
    if view["state"] == "running":
        view["elapsed"] = round(time.time() - view["started_at"], 1)
    return view


def _mark_job_viewed(job_id: str) -> None:
    with _JOBS_LOCK:
        if _JOB is not None and _JOB["id"] == job_id:
            _JOB["viewed"] = True


def _progress_sink(job_id: str) -> Any:
    """``run_sync(on_progress=…)``：把每一次请求的进度写回任务状态。"""

    def update(message: str, client: Any = None) -> None:
        with _JOBS_LOCK:
            job = _JOB
            if job is None or job["id"] != job_id:
                return
            job["note"] = message
            job["notes"].append(message)
            del job["notes"][:-40]
            job["elapsed"] = round(time.time() - job["started_at"], 1)
            if client is None:
                return
            job["requests"] = int(getattr(client, "requests", 0) or 0)
            job["remote_total"] = int(getattr(client, "remote_total", 0) or 0) or None
            job["fetched_count"] = int(getattr(client, "rows_seen", 0) or 0)
            job["expected_pages"] = getattr(client, "expected_pages", None)
    return update


def _finish_job(
    job_id: str,
    *,
    state: str = "done",
    report: dict[str, Any] | None = None,
    import_report: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    with _JOBS_LOCK:
        job = _JOB
        if job is None or job["id"] != job_id:
            return
        job["state"] = state
        job["report"] = report
        job["import_report"] = import_report
        job["error"] = error
        job["elapsed"] = round(time.time() - job["started_at"], 1)
        job["item_count"] = int((report or {}).get("import_items") or 0)
        job["note"] = error or "同步结束"


# --------------------------------------------------------------------------
# 表单 / 页面
# --------------------------------------------------------------------------

def _validate_connection(host: str, token: str, username: str, password: str) -> str:
    """连接项的校验：返回错误信息（空串 = 通过）。"""
    if not host:
        return BAD_HOST
    if not token and not (username and password):
        return AUTH_REQUIRED
    return ""


def _form_host_error(form: Mapping[str, Any]) -> str:
    """表单里**明确填了**一个认不出来的地址时报错。

    夹取逻辑会把坏地址退回默认站点（配置被手改坏时页面不该炸），但用户亲手贴了一个
    地址却被悄悄换成 copy4000.com 就不行了——镜像站恰恰是手填地址换来的。
    """
    raw = str(form.get("host") or "").strip()
    return BAD_HOST if raw and not normalize_host(raw) else ""


def _settings_from_form(form: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """从表单读这次的同步参数（没提交的字段用配置里的默认值）。

    复选框不勾就压根不提交，所以那几个开关都配一个同名标记（``has_*``）：
    有标记才认「这次表里勾了什么」，没标记就是这张表不管这个开关。
    """
    chosen = dict(config)
    if "host" in form:
        raw_host = str(form.get("host") or "").strip()
        if raw_host:
            chosen["host"] = normalize_host(raw_host)
    if "token" in form:
        # 令牌框留空 = 「用配置里存着的那个」，不是「把它忘掉」：第一次密码登录换到的令牌
        # 就存在配置里，拿旧的一屏（框里还空着）再点一次不该被当成没认证。
        chosen["token"] = normalize_token(form.get("token")) or str(config.get("token") or "")
    if "username" in form:
        chosen["username"] = str(form.get("username") or "").strip()
    if "mode" in form:
        chosen["mode"] = normalize_mode(form.get("mode"))
    if str(form.get("has_raw") or ""):
        chosen["include_raw"] = form.get("include_raw") is not None
    for key in ("page_size", "timeout"):
        if key in form:
            chosen[key] = form.get(key)
    if "delay" in form:
        chosen["delay"] = form.get("delay")
    return effective_config(_FakeContext(chosen))


def _persist_connection(
    ctx: Any, *, host: str, token: str, username: str, mode: str,
    page_size: int, delay: float, timeout: int,
    include_raw: bool,
) -> None:
    """把这次用的东西固化进配置（用户不用每次重填）。

    刻意**不保存密码**：配置文件是给「下次点开还在」用的，口令这种东西一次一填。
    """
    values = dict(effective_config(ctx))
    values.update({
        "host": normalize_host(host) or DEFAULT_HOST,
        # 空令牌不许把登录换来的那个抹掉（表单值已经在 _settings_from_form 里兜过一层）
        "token": normalize_token(token) or str(values.get("token") or ""),
        "username": str(username or "").strip(),
        "mode": normalize_mode(mode),
        "page_size": int(page_size),
        "delay": float(delay),
        "timeout": int(timeout),
        "include_raw": bool(include_raw),
    })
    try:
        ctx.save_config(values)
    except OSError as exc:                        # 写不进磁盘不影响这次同步
        ctx.log(f"连接配置没能保存：{exc}")


def _persist_token(ctx: Any, token: str) -> None:
    """登录换来的 token 存回配置：下次直接用它，不用再登一次。

    只补这一个字段——密码照样不落盘。
    """
    values = dict(effective_config(ctx))
    values["token"] = normalize_token(token)
    try:
        ctx.save_config(values)
    except OSError as exc:
        ctx.log(f"token 没能保存：{exc}")


def _preview_text(path: Path, item_count: int, *, limit: int = 60) -> str:
    """导入文件预览：小文件才读，给人看结构用（文件本身是紧凑 JSON）。"""
    if item_count > limit:
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    rows = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return ""
    trimmed = [{key: row.get(key) for key in
                ("collect_id", "name", "authors", "status_label", "comic_uuid", "url")}
               for row in rows if isinstance(row, Mapping)]
    return json.dumps({"total": payload.get("total"), "items": trimmed},
                      ensure_ascii=False, indent=2)


def _render(
    ctx: Any,
    request: Any,
    *,
    report: dict[str, Any] | None = None,
    import_report: dict[str, Any] | None = None,
    error: str = "",
    notice: str = "",
    json_text: str = "",
    just_started: bool = False,
) -> str:
    config = effective_config(ctx)
    query = getattr(request, "query_params", None) or {}

    def form_value(key: str) -> Any:
        if key in query:
            return query.get(key)
        return config.get(key)

    form = {
        "host": form_value("host"),
        "token": config["token"],
        "username": config["username"],
        "mode": normalize_mode(form_value("mode")),
        "page_size": form_value("page_size"),
        "delay": form_value("delay"),
        "timeout": form_value("timeout"),
        "include_raw": config["include_raw"],
    }

    last = load_meta(ctx.data_dir)
    job = _job_view()
    if job is not None and just_started:
        # 刚点下按钮的这一屏一律先给进度条：哪怕后台已经抢先跑完，结果也留给
        # 进度条的下一次轮询，不然「点一次只看到一次结果」就说不准了。
        job["state"] = "running"
    if job is not None and job["state"] != "running":
        if job["viewed"]:
            job = None                            # 结束而且显示过了，就别再占一块地方
        else:
            # 抓完了：把进度条那一块换成正式报告（和以前同步返回时看到的是同一份东西）
            _mark_job_viewed(job["id"])
            if job["state"] == "done" and job["report"]:
                report = job["report"]
                import_report = job["import_report"]
                json_text = _preview_text(Path(report["import_file"]),
                                          int(report.get("import_items") or 0))
    return ctx.render(
        "page.html",
        config=config,
        form=form,
        last=last,
        files=local_files(ctx.data_dir),
        data_dir=str(ctx.data_dir),
        default_host=DEFAULT_HOST,
        importer=IMPORTER_NAME,
        importer_available=importer_available(ctx),
        manga_total=manga_count(ctx),
        report=report,
        import_report=import_report,
        error=error,
        notice=notice,
        json_text=json_text,
        job=job,
        poll_seconds=POLL_SECONDS,
    )


def _handle_post(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    action = str(form.get("action") or "save").strip()
    if action == "save":
        return _save_config(ctx, request, form)
    if action == "fetch":
        return _do_fetch(ctx, request, form)
    if action == "fetch_import":
        return _do_fetch(ctx, request, form, do_import=True)
    return _render(ctx, request, error=f"不认识的 action: {action!r}")


def _save_config(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    """保存默认值：以当前配置为底，只覆盖这个表单真的提交了的字段。"""
    host_error = _form_host_error(form)
    if host_error:
        return _render(ctx, request, error=host_error)
    chosen = _settings_from_form(form, effective_config(ctx))
    ctx.save_config(chosen)
    ctx.log(f"默认配置已保存：站点={chosen['host']} "
            f"认证={'有' if (chosen['token'] or chosen['username']) else '无'} "
            f"模式={MODE_LABEL[chosen['mode']]} "
            f"page_size={chosen['page_size']} delay={chosen['delay']} "
            f"include_raw={chosen['include_raw']}")
    return _render(ctx, request, notice="默认配置已保存")


def _do_fetch(
    ctx: Any, request: Any, form: Mapping[str, Any], *, do_import: bool = False
) -> str:
    """表单校验（同步）→ 起后台同步 → 立刻返回带进度条的页面。"""
    chosen = _settings_from_form(form, effective_config(ctx))
    token = str(chosen["token"])
    username = str(chosen["username"])
    password = str(form.get("password") or "").strip()

    connection_error = _form_host_error(form) or _validate_connection(
        chosen["host"], token, username, password)
    if connection_error:
        return _render(ctx, request, error=connection_error)

    _persist_connection(ctx, host=chosen["host"], token=token, username=username,
                        mode=chosen["mode"],
                        page_size=chosen["page_size"], delay=chosen["delay"],
                        timeout=chosen["timeout"],
                        include_raw=chosen["include_raw"])

    job = _start_job(
        label=f"{FETCH_KIND_LABEL}（{MODE_LABEL[chosen['mode']]}同步）",
        do_import=do_import,
    )
    if job is None:
        return _render(ctx, request,
                       error="已经有一个同步在跑了，等它的进度条走完再点。")

    def run_in_background() -> None:
        """线程入口：``_fetch_worker`` 之外的兜底。

        任务状态是进度条唯一的依据——线程要是带着异常死了却没人把 job 结掉，
        页面上那根条就会一直转到超时（30 分钟）为止。
        """
        try:
            _fetch_worker(ctx, job_id=job["id"], settings=chosen, token=token,
                          username=username, password=password, do_import=do_import)
        except Exception as exc:
            message = f"同步线程意外退出：{type(exc).__name__}: {redact(exc, token, password)}"
            ctx.log(message)
            _finish_job(job["id"], state="error", error=message)

    threading.Thread(
        target=run_in_background,
        name=f"copymanga-fetch-{job['id']}",
        daemon=True,
    ).start()
    return _render(ctx, request, just_started=True)


def _fetch_worker(
    ctx: Any,
    *,
    job_id: str,
    settings: Mapping[str, Any],
    token: str,
    username: str,
    password: str,
    do_import: bool,
) -> None:
    """后台线程里跑的那一次同步：认证 → 翻页 → 落文件 →（可选）导入 → 把结果写回任务。"""
    outcome, sync_error = run_sync(
        ctx,
        host=str(settings["host"]), token=token, username=username, password=password,
        mode=str(settings["mode"]),
        page_size=int(settings["page_size"]), delay=float(settings["delay"]),
        timeout=int(settings["timeout"]),
        include_raw=bool(settings["include_raw"]),
        on_progress=_progress_sink(job_id),
    )
    if outcome is None:
        ctx.log(f"同步失败：{sync_error}")
        _finish_job(job_id, state="error", error=sync_error)
        return

    report = dict(outcome.report)
    report["notes"] = list(outcome.notes)
    report["auth_note"] = _auth_note(report, token, username, password)
    try:
        save_meta(ctx.data_dir, report)
        ctx.log(f"同步完成：远端 {report['remote_total']} / 本地 {report['local_total']} 条"
                f"（新增 {report['added']}，更新 {report['updated']}，"
                f"未变化 {report['unchanged']}，取消收藏 {report['removed']}），"
                f"{report['requests']} 次请求 / {report['elapsed']} 秒，"
                f"导入文件 {report['import_items']} 条已存")

        import_report: dict[str, Any] | None = None
        if do_import:
            import_report = import_snapshot(ctx, Path(report["import_file"]))
            if import_report.get("error"):
                ctx.log(f"同步成功但导入失败：{import_report['error']}")
            else:
                ctx.log(f"已导入 {IMPORTER_NAME}：新增 {import_report['inserted']}，"
                        f"更新 {import_report['updated']}，未变化 {import_report['unchanged']}，"
                        f"错误 {import_report['error_count']}，日志 #{import_report['log_id']}")
        _finish_job(job_id, report=report, import_report=import_report)
    except Exception as exc:                      # 写盘/导入意外也得把任务结掉
        ctx.log(f"同步结果落盘失败：{type(exc).__name__}: {exc}")
        _finish_job(job_id, state="error",
                    error=f"同步结果落盘失败：{type(exc).__name__}: {exc}")


def _auth_note(report: Mapping[str, Any], token: str, username: str, password: str) -> str:
    """这次是靠什么认证上的（只说方式，绝不带上令牌和密码）。"""
    if str(token or "").strip():
        return "用了本地存的 token"
    if username and password:
        who = str((report.get("user") or {}).get("username") or username)
        return f"用账号 {who} 登录换的 token（已存进本地配置，下次不用再登录）"
    return ""


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def run_cli_sync(
    ctx: Any,
    *,
    mode: str = "",
    host: str = "",
    token: str = "",
    username: str = "",
    password: str = "",
    page_size: int = 0,
    delay: float = 0.0,
    timeout: int = 0,
    include_raw: bool = False,
    dry_run: bool = False,
) -> str:
    """命令行的主体：同步（+ 导入），返回给人看的多行文本。"""
    config = effective_config(ctx)
    chosen_host = normalize_host(host) or config["host"]
    chosen_token = normalize_token(token) if str(token or "").strip() else config["token"]
    chosen_username = str(username or "").strip() or config["username"]
    chosen_mode = normalize_mode(mode) if str(mode or "").strip() else config["mode"]
    chosen_page_size = int(page_size) if page_size else config["page_size"]
    chosen_delay = float(delay) if delay else float(config["delay"])
    chosen_timeout = int(timeout) if timeout else config["timeout"]
    chosen_raw = bool(include_raw) or config["include_raw"]

    connection_error = _validate_connection(chosen_host, chosen_token, chosen_username, password)
    if connection_error:
        raise ValueError(connection_error)

    _persist_connection(ctx, host=chosen_host, token=chosen_token, username=chosen_username,
                        mode=chosen_mode,
                        page_size=chosen_page_size, delay=chosen_delay,
                        timeout=chosen_timeout,
                        include_raw=chosen_raw)

    outcome, sync_error = run_sync(
        ctx, host=chosen_host, token=chosen_token, username=chosen_username, password=password,
        mode=chosen_mode, page_size=chosen_page_size,
        delay=chosen_delay, timeout=chosen_timeout,
        include_raw=chosen_raw,
    )
    if outcome is None:
        raise ValueError(sync_error)

    report = dict(outcome.report)
    report["notes"] = list(outcome.notes)
    report["auth_note"] = _auth_note(report, chosen_token, chosen_username, password)
    save_meta(ctx.data_dir, report)

    lines = [
        f"同步完成：{report['host']}（{MODE_LABEL.get(report['mode'], report['mode'])}，"
        f"{FETCH_KIND_LABEL}）",
        f"  远端      {report['remote_total']} 条 · 本地累计 {report['local_total']} 条"
        f"（水位线 {report['watermark'] if report['watermark'] is not None else '—'}）",
        f"  本次      新增 {report['added']} · 更新 {report['updated']} · "
        f"未变化 {report['unchanged']} · 恢复 {report['restored']} · "
        f"取消收藏 {report['removed']}",
        f"  请求      {report['requests']} 次 / {report['elapsed']} 秒 · {report['stop_reason']}",
        f"  认证      {report['auth_note'] or '用了本地存的 token'}",
        f"  状态库    {report['store_file']}（{report['store_mb']} MB）",
        f"  导入文件  {report['import_file']}（{report['import_mb']} MB，"
        f"{report['import_items']} 条，不含已取消收藏的）",
    ]
    if not report["ordering_verified"]:
        lines.append(f"  排序      ⚠ 这次没能确认「按加到書架時間降序」"
                     f"{'，已自动改跑全量' if report['fell_back_to_full'] else ''}")
    if report["notes"]:
        lines.append("  明细      " + "；".join(report["notes"][-6:]))

    if dry_run:
        lines.append("  导入      已跳过（--dry-run）；到上传页选 importer "
                     f"{IMPORTER_NAME} 导入上面那个文件")
        return "\n".join(lines)

    result = import_snapshot(ctx, Path(report["import_file"]))
    if result.get("error"):
        lines.append(f"  导入      失败：{result['error']}")
        lines.append("            （文件没丢，仍可到上传页手动导入）")
        return "\n".join(lines)
    lines.append(
        f"  导入      共 {result['total']} 条 · 新增 {result['inserted']} · "
        f"更新 {result['updated']} · 跳过 {result['skipped']} · "
        f"未变化 {result['unchanged']} · 错误 {result['error_count']} · "
        f"警告 {result['warning_count']}"
    )
    lines.append(f"            importer {result['importer']} · 导入日志 #{result['log_id']}"
                 + (f" · 已存档 {result['archive_path']}" if result.get("archive_path") else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------

class CopymangaFetch(Plugin):
    """拷贝漫画书架收藏抓取插件。

    抓取/同步本身在插件目录里的 ``copymanga/`` 包里（原样分发，未改动）；
    这个类只做宿主这一侧：配置、页面、后台任务、导入、命令行。
    """

    def start(self) -> None:
        config = effective_config(self.ctx)
        last = load_meta(self.ctx.data_dir)
        stored = JsonStore(str(store_path(self.ctx.data_dir))).load()
        self.log(f"{self.title} 已启动：站点 {config['host']}，"
                 f"认证 {'已配' if (config['token'] or config['username']) else '未配'}，"
                 f"默认 {MODE_LABEL[config['mode']]}，"
                 f"本地状态库 {len(stored)} 条"
                 + (f"（上次抓取 {last.get('fetched_at')}）" if last.get("fetched_at") else "")
                 + ("" if importer_available(self.ctx)
                    else f"（⚠ 宿主没有注册 importer {IMPORTER_NAME}）"))

    def stop(self) -> None:
        self.log("已停止（没有常驻资源需要释放）")

    def render_page(self, request: Any = None) -> str:
        return _render(self.ctx, request)

    def handle_post(self, request: Any = None, form: Mapping[str, Any] | None = None) -> str:
        return _handle_post(self.ctx, request, form or {})

    @cmd("fetch_import", help="同步拷贝漫画书架收藏并导入 OmniShelf")
    def fetch_import(
        self,
        mode: str = "",
        host: str = "",
        token: str = "",
        username: str = "",
        password: str = "",
        page_size: int = 0,
        delay: float = 0.0,
        timeout: int = 0,
        include_raw: bool = False,
        dry_run: bool = False,
    ) -> str:
        """同步拷贝漫画（copy4000.com）自己账号下的书架收藏，并（默认）直接导入 OmniShelf。

        等价于页面上的「抓取并导入」按钮：同步完调宿主的 import 接口入库，走的是
        importer ``copymanga``，和上传页/CLI 同一条链路（来源判重、写 import_logs、原始文件存档）。
        判重靠 ``comic_uuid``，所以重复导入同一批只会「未变化」。
        已取消收藏的条目只留在本地状态库里，不会被导入。

        mode: 同步方式：incremental 增量（默认，只翻到已知水位线就停）/ full 全量
              （遍历所有页，并把远端已经没有的条目标记成已取消收藏）
        host: 站点地址，如 https://copy4000.com；留空用插件配置里的
        token: 登录 token（浏览器 Cookie 里的 token 值）；留空用插件配置里的
        username: 账号（没有可用 token 时用它登录换 token）
        password: 密码（只在这次命令里用，不落盘；命令行会留在 shell 历史里，
                  介意的话改用 token）
        page_size: 每页条数 1~500；0 = 用插件配置（默认 100）
        delay: 翻页间隔秒数 0~10；0 = 用插件配置（默认 0.3）
        timeout: 单次请求超时秒数 5~120；0 = 用插件配置（默认 40）
        include_raw: 状态库里保留接口原始返回（更占体积）；不传 = 用插件配置
        dry_run: 只同步不导入；文件照样落盘，之后可以到上传页手动导入
        """
        return run_cli_sync(
            self.ctx, mode=mode, host=host, token=token, username=username, password=password,
            page_size=page_size, delay=delay, timeout=timeout,
            include_raw=include_raw, dry_run=dry_run,
        )
