"""Steam 收藏抓取：把 Steam 游戏库 / 愿望单抓成可直接导入 OmniShelf 的 JSON。

两份快照是**同一套 item 结构**（抓取时总会给每款补全商店信息：逐款请求 + 并发 + 本地缓存），共用一个
``steam`` importer；只有顶层 ``list`` 与条目 ``status``（done / wishlist）不同。

抓取之后有两种用法：

* **抓取并导入**（页面上的按钮 / ``omnishelf plugin steam_fetch sync``）——
  抓完直接调宿主的 import 接口入库，走和上传页完全一样的链路；
* **只抓取**（``--dry-run`` / 页面「只抓取」），快照落在插件自己的数据目录：

      plugins_data/steam_fetch/steam_owned.json      <- importer ``steam``
      plugins_data/steam_fetch/steam_wishlist.json   <- importer ``steam``

实现上遵守插件规范：

* 继承 ``omnishelf.plugins.api.Plugin``（**插件唯一允许 import 的宿主模块**）；
* 只用标准库 + 随插件分发的 ``steam_client/`` 工具包（纯标准库实现）；
* 运行时数据只写 ``ctx.data_dir``（api 详情缓存在 ``<data_dir>/steam_cache/``）；
* 抓取在**点按钮之后于后台线程**跑（页面靠 HTMX 轮询刷新进度），不在 ``start()`` 里做重活；
* api_key 可以存进本地 ``config.toml``（不进 git），但**绝不进日志、页面回显、
  快照文件和异常文本**——脱敏由本插件自己做（照 douban_fetch 的 redact 口径）。
  代理地址带 ``user:pass@`` 口令时同理：日志和报错里只留 ``协议://主机:端口``。

数据口径（和设计文档一致）：

* 玩家资料 / 游戏库 / 愿望单**每次直接请求 Steam**，不落盘；
* 只有商店信息（两份列表的名称/价格/封面链接/发行日期/开发者/类型/简介）
  走本地缓存，且**按商店区域分目录**（``steam_cache/apps/{cc}/{appid}.json``，
  US 与 CN 的价格互不冒领），``force_refresh`` 只作用于这一层；
  封面只保存链接（URL），不下载图片；
* 空数据不等于无数据：接口回空响应时按「游戏信息不可用或未公开」报告，**不写快照**，
  免得界面显示「0 款游戏」；
* 网络出口只有一个（``SteamWebAPI._open``）：配了 ``proxy`` 就**只走它**（显式的
  ``ProxyHandler`` 会顶掉环境变量里那份，不会被系统环境半路换掉），
  留空才走 urllib 默认——那时 ``HTTPS_PROXY`` 之类的环境变量照旧生效，没设就是直连。
"""

from __future__ import annotations

# 之所以要往 sys.path 里补插件目录：宿主的插件加载器是 exec 单个入口文件，
# 插件目录不是 Python 包（和 copymanga_fetch / picamanga_fetch 同一套路）。
import html
import json
import re
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Mapping

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from steam_client import SteamClient                      # noqa: E402
from steam_client.errors import (                          # noqa: E402
    SteamAuthError,
    SteamDataUnavailableError,
    SteamError,
)
from steam_client.transport import proxy_without_credentials  # noqa: E402

from omnishelf.plugins.api import Plugin, QuickUpdateResult, cmd              # noqa: E402  （宿主 SDK）

LISTS: dict[str, dict[str, str]] = {
    "owned": {"cn": "游戏库", "status": "done", "importer": "steam", "file": "steam_owned.json"},
    "wishlist": {"cn": "愿望单", "status": "wishlist", "importer": "steam", "file": "steam_wishlist.json"},
}

#: 页面/配置里 ``lists`` 选择的展开顺序
LIST_ORDER = ("owned", "wishlist")

#: 表单/配置的默认值
DEFAULTS: dict[str, Any] = {
    #: Steam Web API key。**会存进本地 config.toml**（不进 git），但绝不进日志/快照/异常。
    "api_key": "",
    "steamid64": "",
    "vanity": "",
    "cc": "US",
    #: 代理地址。和 api_key 一样只进本地 config.toml；带 ``user:pass@`` 时口令不进日志/报错。
    "proxy": "",
}

#: 表单里代理写坏了的提示（和 picamanga 用同一句话，用户在这两个插件之间来回跳不用重新学）
BAD_PROXY = "代理地址要写成 http://127.0.0.1:10809 这样（留空 = 不指定代理）"
#: urllib 的 ProxyHandler 认的代理协议（SOCKS 要写全协议头，见 normalize_proxy）
PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")

#: 进度轮询间隔（秒）。Steam Web API 很快，轮询太密只是白费请求。
POLL_SECONDS = 1.0

#: 在跑的任务超过这个时长就当作卡住，允许用户再点一次
JOB_TIMEOUT = 1800.0


# ---------------------------------------------------------------------------
# 配置与脱敏
# ---------------------------------------------------------------------------

def normalize_proxy(value: Any) -> str:
    """代理地址：留空 = 不指定代理；``127.0.0.1:10809`` 这种没写协议的自动补 ``http://``。

    补协议是因为本地代理软件（Clash/V2Ray 的 HTTP 混合端口）通常只给人一个 ``IP:端口``，
    而 urllib 的 ``ProxyHandler`` 拿到没有协议的串会直接忽略它——不补的效果是「悄悄没走代理」。
    所以 SOCKS 端口必须自己写全 ``socks5://IP:端口``。
    带空格、协议不在 urllib 支持名单里的都当非法，返回空串由调用方决定报错还是忽略。
    """
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    filled = text if "://" in text else f"http://{text}"
    parts = urllib.parse.urlsplit(filled)
    if parts.scheme not in PROXY_SCHEMES or not parts.netloc:
        return ""
    return filled


def proxy_label_of(proxy: str) -> str:
    """给人看的那句「走代理 http://127.0.0.1:7890」或「直连」；口令绝不进这行。"""
    return f"走代理 {proxy_without_credentials(proxy)}" if proxy else "直连"


def effective_config(ctx: Any) -> dict[str, Any]:
    """默认值 + 用户配置，每个字段夹到合法范围（配置写坏了也不能炸）。"""
    config = dict(DEFAULTS)
    for key, value in (ctx.config or {}).items():
        if key in config:
            config[key] = value
    config["api_key"] = str(config["api_key"] or "").strip()
    config["steamid64"] = str(config["steamid64"] or "").strip()
    config["vanity"] = str(config["vanity"] or "").strip()
    cc = str(config.get("cc") or "").strip().upper()
    config["cc"] = cc if len(cc) == 2 and cc.isalpha() else "US"
    config["proxy"] = normalize_proxy(config.get("proxy"))
    return config


def redact(text: Any, api_key: str) -> str:
    """把回显/日志里的明文 key 抹掉（***）。宿主不知道哪个串是口令，线出了插件没人兜底。"""
    message = str(text if text is not None else "")
    if api_key:
        message = message.replace(api_key, "***")
    return message


def auth_label(api_key: str) -> str:
    """只报「有没有」，值一个都不露。"""
    return f"已保存（{len(api_key)} 字符，不进 git；日志中一律 ***）" if api_key else "未配置"


def target_steamid(config: Mapping[str, Any], steamid: str = "", vanity: str = "") -> tuple[str, str]:
    """返回 (steamid64 或待解析的 vanity, 类型标记 "sid"/"vanity"/"")。"""
    sid = str(steamid or config.get("steamid64") or "").strip()
    if sid:
        return sid, "sid"
    name = str(vanity or config.get("vanity") or "").strip()
    return (name, "vanity") if name else ("", "")


# ---------------------------------------------------------------------------
# 抓取核心（纯函数，单测直接调）
# ---------------------------------------------------------------------------

def store_url(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}/"


def resolve_sid(client: SteamClient, target: str, kind: str, log: Any) -> str:
    """17 位数字直接用；否则按 vanity 解析。"""
    if kind == "sid":
        from steam_client.transport import require_steamid64

        return require_steamid64(target)
    sid = client.user.resolve_vanity_url(target)
    log(f"vanity {target} -> {sid}")
    return sid


def persona_note(client: SteamClient, sid: str, api_key: str, log: Any) -> str:
    """档案名只用于报告展示；私密档案不影响抓列表。"""
    try:
        summary = client.user.get_player_summary(sid)
    except SteamDataUnavailableError:
        log("玩家资料不可用或未公开（不影响游戏库/愿望单，除非整个档案是私密的）")
        return ""
    except SteamError as exc:
        log(f"玩家资料读取失败：{redact(exc, api_key)}")
        return ""
    return summary.persona_name or ""


_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")


def plain_text(value: Any) -> str:
    """Steam 的 short_description 带 <br>/<b> 之类标签，入库前清成纯文本。"""
    text = _BR.sub("\n", str(value or ""))
    return html.unescape(_TAGS.sub("", text)).strip()


def store_fields(info: Any) -> dict[str, Any]:
    """从商店详情抽统一字段（info 为 None 时全部留空，行结构不变）。"""
    raw: Mapping[str, Any] = getattr(info, "raw", None) or {}
    return {
        "cover_url": info.header_image if info else None,
        "release_date": info.release_date if info else None,
        "developers": [str(item) for item in raw.get("developers") or [] if str(item).strip()],
        "genres": [str(item.get("description")) for item in raw.get("genres") or []
                   if isinstance(item, Mapping) and str(item.get("description") or "").strip()],
        "short_description": plain_text(raw.get("short_description")),
    }


def log_detail_gaps(stats: Mapping[str, Any], log: Any) -> None:
    """把「详情没取到」按原因分开说：商店说不给，还是网络没要回来。

    两种情况都会用 appid 占位、下次重抓覆盖回真名，但用户看到时能分清是该等商店恢复
    还是该查代理/网络。
    """
    unavailable = int(stats.get("unavailable") or 0)
    failed = int(stats.get("failed") or 0)
    if unavailable:
        log(f"其中 {unavailable} 个 app 详情不可用（下架或未公开，先用 appid 占位）")
    if failed:
        log(f"另有 {failed} 个 app 详情因网络问题没取到（已自动补跑，重抓会覆盖回真名）")


def owned_rows(client: SteamClient, sid: str, *, force_refresh: bool,
               cc: str, log: Any) -> list[dict[str, Any]]:
    games = client.game.get_owned_games(sid)
    log(f"游戏库抓到 {len(games)} 条，补全商店信息（逐款问，命中缓存跳过，最多 8 路并发）…")
    stats: dict[str, int] = {}
    infos = client.game.enrich_apps([game.appid for game in games],
                                    force_refresh=force_refresh, cc=cc, stats=stats)
    log_detail_gaps(stats, log)
    rows: list[dict[str, Any]] = []
    for game, info in zip(games, infos):
        row = {
            "appid": game.appid,
            "name": game.name or (info.name if info else None) or str(game.appid),
            "playtime_forever": game.playtime_forever,
            "playtime_2weeks": game.playtime_2weeks,
            "url": store_url(game.appid),
            "status": LISTS["owned"]["status"],
        }
        row.update(store_fields(info))
        rows.append(row)
    return rows


def wishlist_rows(client: SteamClient, sid: str, *, force_refresh: bool, cc: str,
                  log: Any) -> list[dict[str, Any]]:
    appids = client.game.get_wishlist(sid)
    log(f"愿望单抓到 {len(appids)} 个 appid，补全商店信息（逐款问，命中缓存跳过）…")
    stats: dict[str, int] = {}
    infos = client.game.enrich_apps(appids, force_refresh=force_refresh, cc=cc, stats=stats)
    log_detail_gaps(stats, log)
    rows: list[dict[str, Any]] = []
    for appid, info in zip(appids, infos):
        row = {
            "appid": appid,
            "name": (info.name if info else None) or str(appid),
            "playtime_forever": None,      # 愿望单没有时长；与游戏库同构，缺的留空
            "playtime_2weeks": None,
            "url": store_url(appid),
            "status": LISTS["wishlist"]["status"],
        }
        row.update(store_fields(info))
        rows.append(row)
    return rows


def build_snapshot(list_key: str, sid: str, persona: str, cc: str,
                   rows: list[dict[str, Any]], extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "list": list_key,
        "list_cn": LISTS[list_key]["cn"],
        "kind": "game",
        "source": "steam",
        "status": LISTS[list_key]["status"],
        "steamid": sid,
        "persona": persona,
        "cc": cc,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "items": rows,
    }
    payload.update(extra or {})
    return payload


def _atomic_write(path: Path, text: str) -> None:
    """先写 .part 再改名，避免宿主/用户读到写了一半的文件。"""
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_snapshot(data_dir: Path, payload: dict[str, Any]) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / LISTS[payload["list"]]["file"]
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def snapshot_index(data_dir: Path) -> list[dict[str, Any]]:
    """列出已抓快照概况（页面显示用）。"""
    rows: list[dict[str, Any]] = []
    for list_key, spec in LISTS.items():
        path = data_dir / spec["file"]
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({
            "file": path.name,
            "list_cn": payload.get("list_cn", spec["cn"]),
            "count": payload.get("count") or len(payload.get("items") or []),
            "fetched_at": payload.get("fetched_at", ""),
            "importer": spec["importer"],
            "size_kb": round(path.stat().st_size / 1024, 1),
        })
    return rows


def import_snapshot(ctx: Any, list_key: str, path: Path) -> dict[str, Any]:
    """把快照交给宿主导入链路。出错不抛给页面——抓取已经成功了，返回带 error 的摘要。"""
    importer = LISTS[list_key]["importer"]
    try:
        result = ctx.host.import_file(importer, path)
    except Exception as exc:
        return {"error": redact(f"{type(exc).__name__}: {exc}", str((ctx.config or {}).get("api_key") or "")),
                "list": list_key, "importer": importer}
    return dict(result) | {"list": list_key, "importer": importer, "path": str(path)}


def run_fetch(ctx: Any, *, steamid: str = "", vanity: str = "", lists: str = "all",
              cc: str = "", force_refresh: bool = False, proxy: str = "",
              progress: Any = None) -> dict[str, Any]:
    """抓 + 落快照（不导入）。``lists`` 是 owned/wishlist/all；progress(message) 回报进度。"""
    config = effective_config(ctx)
    api_key = str(config.get("api_key") or "")
    if not api_key:
        raise SteamAuthError("还没配 Steam Web API key：在页面顶部保存，或 steam_fetch 配置里填")
    chosen_cc = (cc or config["cc"]).upper()
    # 代理：命令行/表单留空 = 用配置里那个（和 cc 同一个口径）；
    # 亲手填了一个却认不出来就报错，别悄悄退回直连——那正是「连不上但其实配错了」。
    raw_proxy = str(proxy or "").strip()
    chosen_proxy = normalize_proxy(raw_proxy) if raw_proxy else str(config["proxy"])
    if raw_proxy and not chosen_proxy:
        raise ValueError(BAD_PROXY)
    network = proxy_label_of(chosen_proxy)
    target, kind = target_steamid(config, steamid, vanity)
    if not target:
        raise ValueError("要指定 SteamID64（个人主页那串 17 位数字）或 vanity（自定义 URL 名）")

    def log(message: str) -> None:
        message = redact(message, api_key)
        ctx.log(message)
        if progress is not None:
            progress(message)

    client = SteamClient(api_key=api_key, cache_path=ctx.data_dir / "steam_cache",
                         proxy=chosen_proxy)
    log(f"开始抓取：目标={target if kind == 'vanity' else 'steamid64 ' + target} "
        f"列表={lists} cc={chosen_cc} 网络={network}")
    sid = resolve_sid(client, target, kind, log)
    persona = persona_note(client, sid, api_key, log)

    wanted = list(LIST_ORDER) if lists == "all" else [part.strip() for part in str(lists).split(",") if part.strip()]
    bad = [item for item in wanted if item not in LISTS]
    if bad:
        raise ValueError(f"列表名不认识：{bad}（可选 owned / wishlist / all）")

    report: dict[str, Any] = {"sid": sid, "persona": persona, "cc": chosen_cc,
                              "network": network, "lists": [],
                              "auth_display": auth_label(api_key)}
    for list_key in wanted:
        log(f"抓取{LISTS[list_key]['cn']}…")
        try:
            if list_key == "owned":
                rows = owned_rows(client, sid, force_refresh=force_refresh,
                                  cc=chosen_cc, log=log)
            else:
                rows = wishlist_rows(client, sid, force_refresh=force_refresh, cc=chosen_cc, log=log)
        except SteamDataUnavailableError as exc:
            # 空数据 ≠ 无数据：可能是私密档案/被限制，按来源的文案报，不写快照
            report["lists"].append({"list": list_key, "list_cn": LISTS[list_key]["cn"],
                                    "unavailable": str(exc)})
            log(f"{LISTS[list_key]['cn']}：{exc}")
            continue
        payload = build_snapshot(list_key, sid, persona, chosen_cc, rows)
        path = save_snapshot(ctx.data_dir, payload)
        report["lists"].append({"list": list_key, "list_cn": LISTS[list_key]["cn"],
                                "count": len(rows), "file": str(path),
                                "importer": LISTS[list_key]["importer"]})
        log(f"{LISTS[list_key]['cn']} {len(rows)} 条已存 {path.name}")
    return report


# ---------------------------------------------------------------------------
# 后台任务：点按钮 -> 起线程 -> 立刻回带进度条的一屏
# ---------------------------------------------------------------------------

_JOBS_LOCK = threading.Lock()
_JOB: dict[str, Any] | None = None


def _start_job(*, label: str, do_import: bool) -> dict[str, Any] | None:
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
            "note": "正在连接 Steam…",
            "notes": [],
            "report": None,
            "import_reports": None,
            "error": "",
        }
        return dict(_JOB)


def _job_view() -> dict[str, Any] | None:
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
    def update(message: str) -> None:
        with _JOBS_LOCK:
            job = _JOB
            if job is None or job["id"] != job_id:
                return
            job["note"] = message
            job["notes"].append(message)
            del job["notes"][:-40]
            job["elapsed"] = round(time.time() - job["started_at"], 1)
    return update


def _finish_job(job_id: str, *, state: str = "done", report: dict[str, Any] | None = None,
                import_reports: list[dict[str, Any]] | None = None, error: str = "") -> None:
    with _JOBS_LOCK:
        job = _JOB
        if job is None or job["id"] != job_id:
            return
        job["state"] = state
        job["report"] = report
        job["import_reports"] = import_reports
        job["error"] = error
        job["elapsed"] = round(time.time() - job["started_at"], 1)
        job["note"] = error or (report or {}).get("error") or "抓取结束"


def _fetch_worker(ctx: Any, job_id: str, kwargs: dict[str, Any], do_import: bool, api_key: str) -> None:
    """后台线程里跑的那一遍：抓 -> 落快照 ->（可选）导入 -> 结果写回任务。"""
    try:
        report = run_fetch(ctx, progress=_progress_sink(job_id), **kwargs)
        import_reports: list[dict[str, Any]] | None = None
        if do_import:
            import_reports = [
                import_snapshot(ctx, row["list"], Path(row["file"]))
                for row in report["lists"] if row.get("file")
            ]
            for item in import_reports:
                if item.get("error"):
                    ctx.log(f"抓取成功但导入失败（{item.get('list')}）：{item['error']}")
                else:
                    ctx.log(f"已导入 {item['list']}：新增 {item['inserted']}，更新 {item['updated']}，"
                            f"未变化 {item['unchanged']}，错误 {item['error_count']}")
        _finish_job(job_id, report=report, import_reports=import_reports)
    except Exception as exc:
        message = redact(f"抓取失败：{type(exc).__name__}: {exc}", api_key)
        ctx.log(message)
        _finish_job(job_id, state="error", error=message)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

def _render(ctx: Any, request: Any, *, error: str = "", just_started: bool = False) -> str:
    config = effective_config(ctx)
    job = _job_view()
    if job is not None and just_started:
        job["state"] = "running"        # 刚点完按钮的一律先画进度条，结果交给下一次轮询
    if job is not None and job["state"] != "running" and job["viewed"]:
        job = None                      # 结束且显示过了就别占地方
    elif job is not None and job["state"] != "running":
        _mark_job_viewed(job["id"])
    return ctx.render(
        "page.html",
        config=config,
        job=job,
        error=error,
        snapshots=snapshot_index(ctx.data_dir),
        data_dir=str(ctx.data_dir),
        auth_display=auth_label(str(config.get("api_key") or "")),
        has_key=bool(config.get("api_key")),
        poll_seconds=POLL_SECONDS,
    )


def _proxy_form_error(form: Mapping[str, Any]) -> str:
    """表单里的代理地址认不出来就报错；没填/清空不算错（那就是「不指定代理」）。"""
    raw = str(form.get("proxy") or "").strip()
    return BAD_PROXY if raw and not normalize_proxy(raw) else ""


def _save_config(ctx: Any, form: Mapping[str, Any]) -> dict[str, Any]:
    """以当前配置为底，只覆盖表单真的提交了的字段；api_key 单独有「清除」语义。"""
    base = effective_config(ctx)
    values = dict(base)
    for field in ("steamid64", "vanity", "cc", "proxy"):
        if field in form:
            values[field] = form[field]
    pasted = str(form.get("api_key") or "").strip()
    if form.get("clear_key") is not None:
        values["api_key"] = ""
    elif pasted:
        values["api_key"] = pasted
    return values


def _handle_post(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    action = str(form.get("action") or "save").strip()
    if action == "save":
        proxy_error = _proxy_form_error(form)
        if proxy_error:
            return _render(ctx, request, error=proxy_error)
        values = _save_config(ctx, form)
        checked = effective_config(_FakeContext(ctx, values))
        sid = str(checked["steamid64"])
        if sid and (not sid.isdigit() or len(sid) != 17):
            return _render(ctx, request, error=f"SteamID64 得是 17 位数字，收到 {sid!r}")
        ctx.save_config(checked)
        ctx.log(f"默认配置已保存：steamid64={checked['steamid64'] or '(未设置)'} "
                f"vanity={checked['vanity'] or '(未设置)'} cc={checked['cc']} "
                f"网络={proxy_label_of(checked['proxy'])} "
                f"key={auth_label(str(checked['api_key']))}")
        return _render(ctx, request)
    if action in ("fetch", "fetch_import"):
        return _do_fetch(ctx, request, form, do_import=action == "fetch_import")
    return _render(ctx, request, error=f"不认识的 action: {action!r}")


class _FakeContext:
    """只为复用 effective_config 的夹取逻辑（不碰磁盘）。"""

    def __init__(self, ctx: Any, config: Mapping[str, Any]) -> None:
        self.config = dict(config)


def _do_fetch(ctx: Any, request: Any, form: Mapping[str, Any], *, do_import: bool) -> str:
    proxy_error = _proxy_form_error(form)
    if proxy_error:
        return _render(ctx, request, error=proxy_error)
    config = effective_config(ctx)
    values = _save_config(ctx, form)
    checked = effective_config(_FakeContext(ctx, values))
    api_key = str(checked["api_key"])
    if not api_key:
        return _render(ctx, request, error="请先保存 Steam Web API key（页面顶部那一栏）")
    target, kind = target_steamid(checked, str(form.get("steamid64") or ""), str(form.get("vanity") or ""))
    if not target:
        return _render(ctx, request, error="请先填 SteamID64 或 vanity（二选一，SteamID64 优先）")
    if kind == "sid" and (not target.isdigit() or len(target) != 17):
        return _render(ctx, request, error=f"SteamID64 得是 17 位数字，收到 {target!r}")

    # 这次抓的目标/区域/钥匙都记住（和 copymanga 同一口径：点抓取就持久，坏值上面已经拦掉了）。
    # 「抓哪些」和「重新拉取商店信息」是本次开关，不进配置。
    if checked != config:
        try:
            ctx.save_config(checked)
        except OSError as exc:
            ctx.log(f"配置没能保存：{redact(exc, api_key)}")

    lists = str(form.get("lists") or "all")
    job = _start_job(
        label=f"{LISTS['owned']['cn']}/{LISTS['wishlist']['cn']}（{target}）",
        do_import=do_import,
    )
    if job is None:
        return _render(ctx, request, error="已经有一个抓取在跑了，等它的进度条走完再点。")

    kwargs = {
        "steamid": target if kind == "sid" else "",
        "vanity": target if kind == "vanity" else "",
        "lists": lists,
        "cc": checked["cc"],
        "proxy": checked["proxy"],
        "force_refresh": form.get("force_refresh") is not None,
    }
    threading.Thread(
        target=_fetch_worker,
        kwargs={"ctx": ctx, "job_id": job["id"], "kwargs": kwargs,
                "do_import": do_import, "api_key": api_key},
        name=f"steam-fetch-{job['id']}",
        daemon=True,
    ).start()
    return _render(ctx, request, just_started=True)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

class SteamFetch(Plugin):
    """Steam 收藏抓取插件。"""

    def start(self) -> None:
        config = effective_config(self.ctx)
        stored = snapshot_index(self.ctx.data_dir)
        self.log(f"{self.title} 已启动：目标 {config['steamid64'] or config['vanity'] or '(未设置)'}，"
                 f"key {auth_label(config['api_key'])}，网络 {proxy_label_of(config['proxy'])}，"
                 f"已有快照 {len(stored)} 份")

    def stop(self) -> None:
        self.log("已停止（没有常驻资源需要释放）")

    def render_page(self, request: Any = None) -> str:
        return _render(self.ctx, request)

    def handle_post(self, request: Any = None, form: Mapping[str, Any] | None = None) -> str:
        return _handle_post(self.ctx, request, form or {})

    # ------------------------------------------------------------------ 命令行
    @cmd("sync", help="抓取 Steam 游戏库/愿望单并（默认）直接导入")
    def sync(
        self,
        lists: str = "all",
        steamid: str = "",
        vanity: str = "",
        cc: str = "",
        proxy: str = "",
        force_refresh: bool = False,
        dry_run: bool = False,
    ) -> str:
        """抓取 Steam 收藏并（默认）直接导入 OmniShelf。

        lists: owned / wishlist / all（逗号分隔也行，如 "owned,wishlist"）
        steamid: SteamID64（个人主页那串 17 位数字）；留空用插件配置里的
        vanity: 自定义 URL 名（如 gabelogannewell）；steamid 没填时用它解析
        cc: 商店区域码（US/CN…），决定价格用哪个区的报价；留空用配置
        proxy: 代理服务器地址，如 http://127.0.0.1:10809；SOCKS 要写全协议头
               （socks5://IP:端口）。留空 = 用插件配置里的（配置里也空着就是直连）
        force_refresh: 重新拉取商店信息（价格/发行日期没更新时用；游戏库资料本身每次都是新的）
        dry_run: 只抓取不导入；快照照样落盘，之后可到上传页手动导入

        两份列表抓取时都会补全商店信息（名称/价格/封面链接/发行日期/
        开发者/类型/简介）：appdetails 只能逐款问（逗号批量会被回 400），
        靠最多 8 路并发 + 按区域分目录的 7 天本地缓存压时间，只存链接不下载图片。
        """
        config = effective_config(self.ctx)
        if not str(config.get("api_key") or ""):
            raise ValueError("要先配 Steam Web API key："
                             "在插件页面保存，或写进 plugins_data/steam_fetch/config.toml 的 api_key")
        try:
            report = run_fetch(
                self.ctx, steamid=steamid, vanity=vanity, lists=lists or "all",
                cc=cc, proxy=proxy, force_refresh=force_refresh,
                progress=_logging_progress(self.log),
            )
        except SteamError as exc:
            # 异常信息已经由库侧脱敏；这里再过一道保险
            raise RuntimeError(redact(exc, str(config["api_key"]))) from exc

        lines = [
            f"抓取完成：{report['persona'] or report['sid']}（{report['sid']}）· 区域 {report['cc']}",
            f"  认证      {report['auth_display']}",
            f"  网络      {report['network']}",
        ]
        for row in report["lists"]:
            if row.get("file"):
                lines.append(f"  {row['list_cn']}  {row['count']} 条 · 快照 {row['file']}")
            else:
                lines.append(f"  {row['list_cn']}  {row['unavailable']}（未写快照）")
        if dry_run:
            lines.append("  导入      已跳过（--dry-run）；到上传页选 importer 导入快照文件即可")
            return "\n".join(lines)
        for row in report["lists"]:
            if not row.get("file"):
                continue
            result = import_snapshot(self.ctx, row["list"], Path(row["file"]))
            if result.get("error"):
                lines.append(f"  导入 {row['list_cn']} 失败：{result['error']}（快照没丢，可到上传页手动导入）")
                continue
            lines.append(
                f"  导入 {row['list_cn']} 共 {result['total']} 条 · 新增 {result['inserted']} · "
                f"更新 {result['updated']} · 未变化 {result['unchanged']} · "
                f"错误 {result['error_count']} · importer {result['importer']} · 日志 #{result['log_id']}"
            )
        return "\n".join(lines)

    def quick_update(self, options: Mapping[str, Any] | None = None) -> QuickUpdateResult:
        """用当前配置整份重抓游戏库 + 愿望单并导入（Steam 没有增量模式）。"""
        try:
            text = self.sync(lists="all")
        except ValueError as exc:
            return QuickUpdateResult(ok=False, summary=str(exc))
        problems: list[str] = []
        if "未写快照" in text:
            problems.append("有列表没写快照（档案私密或接口抖动）")
        if "失败：" in text:
            problems.append("有列表导入失败")
        header = next(
            (line.strip() for line in text.splitlines() if line.strip().startswith("抓取完成")),
            "Steam 游戏库/愿望单已同步",
        )
        summary = header + ("；" + "；".join(problems) if problems else "")
        return QuickUpdateResult(ok=not problems, summary=summary, details=text)


def _logging_progress(log: Any) -> Any:
    """CLI 路径：把进度消息送进宿主统一日志（plugins_data 外的 data/logs/plugins/…）。"""

    def update(message: str) -> None:
        log(message)

    return update
