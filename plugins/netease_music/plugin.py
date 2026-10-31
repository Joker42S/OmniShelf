"""网易云音乐收藏导入：把「我喜欢的音乐」抓成可直接导入 OmniShelf 的 JSON。

数据全由本机 ``ncm-cli`` 取回（前提：已安装并配置好 appid / 私钥）：

* **登录**（页面「扫码登录」/ ``omnishelf plugin netease_music login``）——
  先 ``ncm-cli login --check``，未登录就执行 ``ncm-cli login`` 拿到一次性链接；
  页面在浏览器里把它画成二维码（链接同时保留可点），用户用网易云 App 扫码确认，
  插件轮询到登录成功就结束；
* **抓取**（页面按钮 / ``omnishelf plugin netease_music sync``）——
  ``playlist created --limit 1`` 找收藏歌单的**加密 id**（``id``，不是 ``originalId``），
  再 ``playlist tracks`` 按 offset 翻页。已有快照时默认**增量**：歌单按收藏时间倒序，
  遇到一整页都是快照里的歌就停；``--full`` 才整份重抓（能发现取消收藏，但慢）；
* **入库**——抓完落快照，再交给内置 importer ``netease_music``：
  ``external_ids.netease_music`` 用歌曲的 ``originalId`` 判重，重复导入只会「未变化」。

快照位置：``plugins_data/netease_music/netease_favorites.json``（只抓取时保留，
之后也能到上传页手动导入）。

实现上遵守插件规范：

* 继承 ``omnishelf.plugins.api.Plugin``（**插件唯一允许 import 的宿主模块**）；
* 只走 ``ncm-cli`` 子进程（stdlib ``subprocess``），插件自己不发 HTTP；
* 运行时数据只写 ``ctx.data_dir``；抓取在**点按钮之后于后台线程**跑，
  页面靠 HTMX 轮询刷新进度；
* **登录链接是敏感的、一次性的**：只显示在页面上（二维码/链接）或由 CLI 打印给用户，
  **不进日志、不进快照、不进异常文本**；ncm-cli 自己的 appid/私钥本插件完全不碰。
"""

from __future__ import annotations

# 宿主的插件加载器是 exec 单个入口文件，插件目录不是 Python 包，
# 所以先把插件目录补进 sys.path（和 steam_fetch / asmr_playlist 同一套路）。
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from netease import (  # noqa: E402
    DEFAULT_LOGIN_TIMEOUT,
    MAX_PAGE_SIZE,
    NeteaseClient,
    NeteaseError,
    NeteaseLoginError,
    NeteaseLoginTimeout,
)
from netease.models import LoginPrompt, LoginStatus, Playlist  # noqa: E402

from omnishelf.plugins.api import Plugin, QuickUpdateResult, cmd  # noqa: E402  （宿主 SDK）

#: 内置 importer 的名字（歌曲快照的形状见 src/omnishelf/importers/netease.py）
IMPORTER = "netease_music"
#: 快照文件名
SNAPSHOT_FILE = "netease_favorites.json"

#: 表单/配置默认值
DEFAULTS: dict[str, Any] = {
    #: 每次向 ncm-cli 请求多少首（1~500）。500 在实机 1 万首歌单上约 21 次请求。
    "page_size": MAX_PAGE_SIZE,
}

#: 页面轮询间隔（秒）。登录要等用户扫码，不用刷太快。
POLL_SECONDS = 2.0

#: 任务超过这个时长就当作卡住，允许再点一次（登录本身 300 秒会自己超时）
JOB_TIMEOUT = 3600.0


# ---------------------------------------------------------------------------
# 配置 / 客户端 / 快照
# ---------------------------------------------------------------------------

def effective_config(ctx: Any) -> dict[str, Any]:
    """默认值 + 用户配置，把 ``page_size`` 夹到 1~500（配置写坏了也不能炸）。"""
    config = dict(DEFAULTS)
    for key, value in (ctx.config or {}).items():
        if key in config:
            config[key] = value
    raw_size = config.get("page_size")
    try:
        size = int(raw_size) if raw_size is not None else MAX_PAGE_SIZE
    except (TypeError, ValueError):
        size = MAX_PAGE_SIZE
    config["page_size"] = max(1, min(size, MAX_PAGE_SIZE))
    return config


def make_client() -> NeteaseClient:
    """建一个 ncm-cli 客户端。单独抽出来方便测试整体替换。"""
    return NeteaseClient()


def prompt_view(prompt: LoginPrompt | None) -> dict[str, str] | None:
    if prompt is None:
        return None
    return {"url": prompt.url, "message": prompt.message, "qr_url": prompt.qr_url}


def login_state(client: NeteaseClient) -> dict[str, Any]:
    """查登录状态；``ncm-cli`` 找不到/执行失败也返回一个可显示的 dict，不炸页面。"""
    try:
        status: LoginStatus = client.check_login()
    except NeteaseError as exc:
        return {"logged_in": False, "message": "", "error": str(exc)}
    return {"logged_in": status.logged_in, "message": status.message, "error": ""}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return None


def artist_names(raw: Mapping[str, Any]) -> list[str]:
    """取主艺术家（``artists``，不是包含友情参与的 ``fullArtists``）。"""
    value = raw.get("artists") or raw.get("ar") or []
    pieces = value if isinstance(value, (list, tuple)) else [value]
    names: list[str] = []
    for piece in pieces:
        name = _text(piece.get("name")) if isinstance(piece, Mapping) else _text(piece)
        if name and name not in names:
            names.append(name)
    return names


def album_name(raw: Mapping[str, Any]) -> str | None:
    album = raw.get("album") or raw.get("al")
    if isinstance(album, Mapping):
        return _text(album.get("name"))
    return _text(album)


def item_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    """ncm-cli 的原始歌曲行 -> 快照行（importer ``netease_music`` 认的形状）。

    只留导入要用的字段：原始行里大量播放权限字段对书架没用，一首歌就上百个键。
    """
    song_id = raw.get("originalId")
    if song_id is None:
        song_id = raw.get("song_id")
    parsed_id = _int_or_none(song_id)
    millis = _int_or_none(raw.get("duration")) or _int_or_none(raw.get("dt"))
    return {
        "song_id": parsed_id if parsed_id is not None else _text(song_id),
        "name": _text(raw.get("name")) or _text(raw.get("title")),
        "artists": artist_names(raw),
        "album": album_name(raw),
        "duration_seconds": round(millis / 1000) if millis and millis > 0 else None,
    }


def unique_song_count(items: list[dict[str, Any]]) -> int:
    """按 ``song_id`` 去重；没有 id 的行各自算一首（宁多勿吞）。"""
    seen: set[Any] = set()
    missing = 0
    for index, item in enumerate(items):
        song_id = item.get("song_id")
        if song_id is None or song_id == "":
            missing += 1
        else:
            seen.add(str(song_id))
    return len(seen) + missing


def item_key(item: Mapping[str, Any]) -> tuple[str, ...]:
    """快照行的身份键：优先歌曲 id；没有 id 时退到标题 + 专辑 + 艺术家。"""
    song_id = item.get("song_id")
    if song_id is not None and str(song_id).strip():
        return ("id", str(song_id))
    artists = item.get("artists") or []
    if isinstance(artists, str):
        artist_key = artists
    else:
        artist_key = "\x1f".join(str(name) for name in artists)
    return (
        "fields",
        str(item.get("name") or ""),
        str(item.get("album") or ""),
        artist_key,
    )


def load_snapshot(data_dir: Path) -> dict[str, Any] | None:
    """读已有快照；不存在或内容坏了返回 None（调用方按全量处理）。"""
    path = data_dir / SNAPSHOT_FILE
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    playlist = payload.get("playlist")
    return {
        "playlist": dict(playlist) if isinstance(playlist, Mapping) else {},
        "items": [dict(item) for item in items if isinstance(item, Mapping)],
    }


def build_snapshot(playlist: Playlist, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "music",
        "source": IMPORTER,
        "importer": IMPORTER,
        "playlist": {
            "id": playlist.id,
            "original_id": playlist.original_id,
            "name": playlist.name,
            "track_count": playlist.track_count,
        },
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "unique_count": unique_song_count(items),
        "items": items,
    }


def save_snapshot(data_dir: Path, payload: Mapping[str, Any]) -> Path:
    """先写 ``.part`` 再改名，避免宿主/用户读到写了一半的文件。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / SNAPSHOT_FILE
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def snapshot_index(data_dir: Path) -> list[dict[str, Any]]:
    """列出已抓快照概况（页面显示用）。"""
    path = data_dir / SNAPSHOT_FILE
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    playlist = payload.get("playlist") or {}
    return [{
        "file": path.name,
        "playlist": playlist.get("name") or "(未命名)",
        "count": payload.get("count") or len(payload.get("items") or []),
        "unique_count": payload.get("unique_count") or "",
        "fetched_at": payload.get("fetched_at", ""),
        "importer": payload.get("importer") or IMPORTER,
        "size_kb": round(path.stat().st_size / 1024, 1),
    }]


# ---------------------------------------------------------------------------
# 登录 / 抓取核心（纯函数，单测直接调）
# ---------------------------------------------------------------------------

def run_login(
    *,
    wait: bool = True,
    timeout: float | None = None,
    on_prompt: Any = None,
    on_status: Any = None,
    client: NeteaseClient | None = None,
) -> dict[str, Any]:
    """扫码登录；返回 ``{logged_in, message, elapsed, prompt}``。

    已登录时不会生成新链接；``wait=False`` 只拿到链接就返回。
    超时抛 :class:`NeteaseLoginTimeout`（``exc.prompt`` 里仍有链接，调用方可继续展示）。
    """
    client = client or make_client()
    seen: list[LoginPrompt] = []

    def prompt_sink(prompt: LoginPrompt) -> None:
        seen.append(prompt)
        if on_prompt is not None:
            on_prompt(prompt)

    result = client.login(
        wait=wait, timeout=timeout, on_prompt=prompt_sink, on_status=on_status
    )
    return {
        "logged_in": result.logged_in,
        "message": result.message,
        "elapsed": result.elapsed,
        "prompt": prompt_view(result.prompt or (seen[0] if seen else None)),
    }


def run_sync(
    ctx: Any,
    *,
    page_size: int | None = None,
    dry_run: bool = False,
    full: bool = False,
    progress: Any = None,
    client: NeteaseClient | None = None,
) -> dict[str, Any]:
    """抓收藏歌单 -> 落快照 ->（可选）交给宿主导入；返回报告 dict。

    已有同一歌单的快照时默认增量：从最新一页开始，遇到整页都是快照里的歌就停，
    把新歌合并到快照前面。``full=True`` 才整份重抓（能反映取消收藏）。
    """
    config = effective_config(ctx)
    size = int(page_size) if page_size is not None else int(config["page_size"])
    if not 1 <= size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size 必须在 1~{MAX_PAGE_SIZE} 之间，收到 {size}")

    def log(message: str) -> None:
        ctx.log(message)
        if progress is not None:
            progress(message)

    client = client or make_client()
    started = time.monotonic()
    log("检查登录状态…")
    client.require_login()
    log("读取收藏歌单（playlist created --limit 1）…")
    playlist = client.favorite_playlist()
    log(f"歌单：{playlist.name or '(未命名)'}（加密 id {playlist.id}；"
        f"接口元数据 {playlist.track_count if playlist.track_count is not None else '?'} 首）")

    existing = load_snapshot(ctx.data_dir)
    existing_items = existing["items"] if existing is not None else []
    existing_playlist_id = (
        str(existing["playlist"].get("id") or "") if existing is not None else ""
    )
    incremental = (
        not full
        and bool(existing_items)
        and existing_playlist_id == playlist.id
    )
    if full:
        log("全量抓取：会重新读完整个歌单")
    elif incremental:
        log(f"增量抓取：快照里已有 {len(existing_items)} 首，"
            "遇到整页已知歌曲就停止翻页（取消收藏的旧歌发现不了，需要 --full）")
    elif existing is not None:
        log("本地快照不属于这份歌单（或为空/损坏）：本次按全量抓取")

    known_keys = {item_key(item) for item in existing_items} if incremental else set()

    def is_known(row: Mapping[str, Any]) -> bool:
        return item_key(item_row(row)) in known_keys

    requests = 0

    def on_page(request_no: int, fetched: int, total: int | None) -> None:
        nonlocal requests
        requests = request_no
        approx = f" / 约 {total} 首" if total else ""
        log(f"第 {request_no} 次请求：已取 {fetched} 首{approx}")

    fetch = client.fetch_playlist_tracks(
        playlist.id,
        page_size=size,
        total=playlist.track_count,
        on_page=on_page,
        known=is_known if incremental else None,
    )
    rows = fetch.rows
    elapsed = round(time.monotonic() - started, 1)
    report: dict[str, Any] = {
        "playlist": {
            "id": playlist.id,
            "original_id": playlist.original_id,
            "name": playlist.name,
            "track_count": playlist.track_count,
        },
        "mode": "incremental" if incremental else "full",
        "checked": 0,
        "new_count": 0,
        "complete": fetch.complete,
        "count": 0,
        "unique_count": 0,
        "requests": requests,
        "elapsed": elapsed,
        "path": "",
        "empty": False,
        "snapshot_written": False,
        "import": None,
        "import_error": "",
    }

    if not rows:
        report["empty"] = True
        log("没有抓到歌曲：歌单为空或接口返回空（不写快照）")
        return report

    fetched_items = [item_row(row) for row in rows]
    if incremental:
        seen_keys = set(known_keys)
        new_items: list[dict[str, Any]] = []
        for item in fetched_items:
            key = item_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            new_items.append(item)
        # 读到了末尾就以接口为准（能发现取消收藏）；提前停止则把新歌并到旧快照前面
        items = fetched_items if fetch.complete else new_items + existing_items
    else:
        new_items = fetched_items
        items = fetched_items

    report.update({
        "checked": len(fetched_items),
        "new_count": len(new_items),
        "count": len(items),
        "unique_count": unique_song_count(items),
        "path": str(ctx.data_dir / SNAPSHOT_FILE),
        "elapsed": round(time.monotonic() - started, 1),
    })

    if incremental and not fetch.complete and not new_items:
        log(f"增量完成：没有新歌（检查 {len(fetched_items)} 首）；快照未变，跳过导入")
        return report

    payload = build_snapshot(playlist, items)
    path = save_snapshot(ctx.data_dir, payload)
    report.update({
        "count": len(items),
        "unique_count": payload["unique_count"],
        "path": str(path),
        "snapshot_written": True,
        "elapsed": round(time.monotonic() - started, 1),
    })
    summary = (f"{len(items)} 首（去重后 {payload['unique_count']} 首）"
               f" · {requests} 次请求 · {report['elapsed']} 秒 · 快照 {path.name}")
    if not incremental:
        log(f"抓取完成：{summary}")
    elif fetch.complete:
        log(f"增量完成（读完整份歌单）：检查 {len(fetched_items)} 首，"
            f"新增 {len(new_items)} 首 · {summary}")
    else:
        log(f"增量完成：检查 {len(fetched_items)} 首，新增 {len(new_items)} 首 · {summary}")

    if dry_run:
        log("只抓取不导入（--dry-run）；之后可到上传页选 importer "
            f"{IMPORTER} 导入快照")
        return report

    log(f"交给宿主导入（importer {IMPORTER}）…")
    try:
        result = ctx.host.import_file(IMPORTER, path)
    except Exception as exc:  # 抓取已经成功，快照不能因为导入失败而丢
        report["import_error"] = f"{type(exc).__name__}: {exc}"
        log(f"抓取成功但导入失败：{report['import_error']}（快照没丢，可到上传页手动导入）")
        return report
    report["import"] = dict(result)
    log(f"导入完成：新增 {result.get('inserted', 0)} · 更新 {result.get('updated', 0)} · "
        f"未变化 {result.get('unchanged', 0)} · 错误 {result.get('error_count', 0)}")
    return report


# ---------------------------------------------------------------------------
# 后台任务：点按钮 -> 起线程 -> 立刻回带进度的一屏
# ---------------------------------------------------------------------------

_JOBS_LOCK = threading.Lock()
_JOB: dict[str, Any] | None = None


def _start_job(*, kind: str, label: str, do_import: bool = False) -> dict[str, Any] | None:
    global _JOB
    with _JOBS_LOCK:
        current = _JOB
        if (current is not None and current["state"] == "running"
                and time.time() - current["started_at"] < JOB_TIMEOUT):
            return None
        _JOB = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "label": label,
            "do_import": do_import,
            "state": "running",
            "viewed": False,
            "started_at": time.time(),
            "elapsed": 0.0,
            "note": "正在启动 ncm-cli…" if kind == "sync" else "正在生成登录二维码…",
            "notes": [],
            "report": None,
            "error": "",
            "prompt": None,
            "logged_in": False,
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


def _finish_job(
    job_id: str,
    *,
    state: str = "done",
    note: str = "",
    report: dict[str, Any] | None = None,
    error: str = "",
    prompt: dict[str, str] | None = None,
    logged_in: bool = False,
) -> None:
    with _JOBS_LOCK:
        job = _JOB
        if job is None or job["id"] != job_id:
            return
        job["state"] = state
        job["report"] = report
        job["error"] = error
        job["logged_in"] = logged_in
        if prompt is not None:
            job["prompt"] = prompt
        job["elapsed"] = round(time.time() - job["started_at"], 1)
        job["note"] = error or note or "任务结束"


def _login_worker(ctx: Any, job_id: str) -> None:
    """后台扫码登录：先拿链接，再轮询到用户确认（或超时）。"""

    def on_prompt(prompt: LoginPrompt) -> None:
        with _JOBS_LOCK:
            job = _JOB
            if job is None or job["id"] != job_id:
                return
            job["prompt"] = prompt_view(prompt)
            job["note"] = "请用网易云音乐 App 扫描下方二维码，或点开登录链接；确认后自动继续"

    def on_status(status: LoginStatus, polls: int) -> None:
        with _JOBS_LOCK:
            job = _JOB
            if job is None or job["id"] != job_id:
                return
            job["note"] = (f"等待扫码确认…（第 {polls} 次检查："
                           f"{status.message or '未登录'}）")

    try:
        result = run_login(wait=True, on_prompt=on_prompt, on_status=on_status)
    except NeteaseLoginTimeout as exc:
        # 链接不放进日志；页面仍能继续展示（用户可能过一会儿才扫码）
        ctx.log(str(exc))
        _finish_job(job_id, state="error", error=str(exc),
                    prompt=prompt_view(getattr(exc, "prompt", None)))
        return
    except Exception as exc:  # ncm-cli 缺失、输出异常等
        message = f"登录失败：{type(exc).__name__}: {exc}"
        ctx.log(message)
        _finish_job(job_id, state="error", error=message)
        return
    _finish_job(job_id, state="done", logged_in=bool(result["logged_in"]),
                note=str(result["message"] or "登录成功"))


def _sync_worker(
    ctx: Any, job_id: str, *, page_size: int, do_import: bool, full: bool
) -> None:
    try:
        report = run_sync(
            ctx,
            page_size=page_size,
            dry_run=not do_import,
            full=full,
            progress=_progress_sink(job_id),
        )
    except Exception as exc:
        message = f"抓取失败：{type(exc).__name__}: {exc}"
        ctx.log(message)
        _finish_job(job_id, state="error", error=message)
        return
    if report.get("import_error"):
        ctx.log(f"抓取成功但导入失败：{report['import_error']}")
    _finish_job(job_id, state="done", report=report)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

def _render(
    ctx: Any,
    request: Any = None,
    *,
    error: str = "",
    notice: str = "",
    just_started: bool = False,
) -> str:
    config = effective_config(ctx)
    job = _job_view()

    if job is not None and job["kind"] == "login" and job.get("logged_in"):
        status = {"logged_in": True, "message": job.get("note") or "已登录", "error": ""}
    elif job is not None and job["state"] == "running":
        # 抓取任务能跑起来就说明登录有效；别再并发调一次 ncm-cli 抢接口。
        if job["kind"] == "login":
            status = {"logged_in": False, "message": job.get("note") or "等待扫码…", "error": ""}
        else:
            status = {"logged_in": True, "message": "登录状态正常（抓取中）", "error": ""}
    else:
        status = login_state(make_client())

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
        status=status,
        error=error,
        notice=notice,
        snapshots=snapshot_index(ctx.data_dir),
        data_dir=str(ctx.data_dir),
        importer=IMPORTER,
        max_page_size=MAX_PAGE_SIZE,
        poll_seconds=POLL_SECONDS,
        login_timeout=int(DEFAULT_LOGIN_TIMEOUT),
    )


def _page_size_from_form(form: Mapping[str, Any]) -> tuple[int | None, str]:
    """返回 (页大小, 错误)；没填返回 None 表示沿用配置。"""
    raw = str(form.get("page_size") or "").strip()
    if not raw:
        return None, ""
    try:
        size = int(raw)
    except ValueError:
        return None, f"每页数量要是整数，收到 {raw!r}"
    if not 1 <= size <= MAX_PAGE_SIZE:
        return None, f"每页数量必须在 1~{MAX_PAGE_SIZE} 之间，收到 {size}"
    return size, ""


def _handle_post(ctx: Any, request: Any, form: Mapping[str, Any]) -> str:
    action = str(form.get("action") or "save").strip()

    if action == "save":
        size, size_error = _page_size_from_form(form)
        if size_error:
            return _render(ctx, request, error=size_error)
        config = effective_config(ctx)
        if size is not None:
            config["page_size"] = size
        ctx.save_config({"page_size": config["page_size"]})
        ctx.log(f"默认配置已保存：每页 {config['page_size']} 首")
        return _render(ctx, request, notice="默认配置已保存。")

    if action == "login":
        state = login_state(make_client())
        if state.get("error"):
            return _render(ctx, request, error=str(state["error"]))
        if state["logged_in"]:
            return _render(ctx, request,
                           notice=f"已经登录了：{state['message'] or '状态正常'}")
        job = _start_job(kind="login", label="扫码登录网易云")
        if job is None:
            return _render(ctx, request, error="已经有一个任务在跑了，等它结束再点。")
        threading.Thread(
            target=_login_worker,
            args=(ctx, job["id"]),
            name=f"netease-login-{job['id']}",
            daemon=True,
        ).start()
        return _render(ctx, request, just_started=True)

    if action in ("sync", "sync_import"):
        size, size_error = _page_size_from_form(form)
        if size_error:
            return _render(ctx, request, error=size_error)
        state = login_state(make_client())
        if state.get("error"):
            return _render(ctx, request, error=str(state["error"]))
        if not state["logged_in"]:
            return _render(ctx, request,
                           error="还没登录：先点「扫码登录」完成登录，再抓取。")
        config = effective_config(ctx)
        chosen = size if size is not None else int(config["page_size"])
        if chosen != config["page_size"]:
            config["page_size"] = chosen
            try:
                ctx.save_config({"page_size": chosen})
            except OSError as exc:
                ctx.log(f"配置没能保存：{exc}")
        full = str(form.get("full") or "").strip().casefold() in (
            "1", "true", "yes", "on"
        )
        job = _start_job(
            kind="sync",
            label=f"我喜欢的音乐（{'全量' if full else '增量'}）",
            do_import=action == "sync_import",
        )
        if job is None:
            return _render(ctx, request, error="已经有一个任务在跑了，等它结束再点。")
        threading.Thread(
            target=_sync_worker,
            kwargs={"ctx": ctx, "job_id": job["id"], "page_size": chosen,
                    "do_import": action == "sync_import", "full": full},
            name=f"netease-sync-{job['id']}",
            daemon=True,
        ).start()
        return _render(ctx, request, just_started=True)

    return _render(ctx, request, error=f"不认识的 action: {action!r}")


# ---------------------------------------------------------------------------
# 生命周期 / 命令行
# ---------------------------------------------------------------------------

class NeteaseMusicImport(Plugin):
    """网易云音乐「我喜欢的音乐」抓取与导入插件。"""

    def start(self) -> None:
        config = effective_config(self.ctx)
        self.log(f"{self.title} 已启动：每页 {config['page_size']} 首，"
                 f"已有快照 {len(snapshot_index(self.ctx.data_dir))} 份；"
                 "登录状态请打开插件页面查看")

    def stop(self) -> None:
        self.log("已停止（没有常驻资源需要释放）")

    def render_page(self, request: Any = None) -> str:
        return _render(self.ctx, request)

    def handle_post(self, request: Any = None, form: Mapping[str, Any] | None = None) -> str:
        return _handle_post(self.ctx, request, form or {})

    # ------------------------------------------------------------------ 命令行

    @cmd("status", help="查看 ncm-cli 的登录状态")
    def status(self) -> str:
        """查看 ncm-cli 当前登录状态（不会发起登录）。"""
        state = login_state(make_client())
        if state.get("error"):
            return f"检查登录状态失败：{state['error']}"
        if state["logged_in"]:
            return f"已登录：{state['message'] or '状态正常'}"
        return f"未登录：{state['message'] or '需要扫码登录'}"

    @cmd("login", help="扫码登录网易云（打印链接并等待确认）")
    def login(self, wait: bool = True, timeout: float = 0) -> str:
        """扫码登录网易云：打印一次性登录链接，等用户在浏览器/App 里确认。

        wait: 是否等用户扫码确认；--no-wait 只打印链接就退出（之后用 status 检查）
        timeout: 等扫码的秒数；0 = 默认 300 秒
        """
        client = make_client()
        wait_timeout = timeout if timeout and timeout > 0 else None

        def on_prompt(prompt: LoginPrompt) -> None:
            print("请打开下面的链接完成登录（也可以用网易云 App 扫码）：", flush=True)
            print(f"  {prompt.url}", flush=True)

        try:
            result = run_login(
                wait=wait, timeout=wait_timeout, on_prompt=on_prompt, client=client
            )
        except NeteaseLoginTimeout as exc:
            return f"等待扫码超时（{exc.timeout:g} 秒）。重新执行 login 会生成新链接。"
        except NeteaseError as exc:
            raise RuntimeError(str(exc)) from exc

        if result["logged_in"]:
            return f"登录成功：{result['message'] or '已登录'}"
        return ("登录链接已打印。扫码确认后执行 "
                "`omnishelf plugin netease_music status` 检查登录状态。")

    @cmd("sync", help="抓取「我喜欢的音乐」并（默认）直接导入；有快照时默认增量")
    def sync(self, page_size: int = 0, dry_run: bool = False, full: bool = False) -> str:
        """抓取网易云「我喜欢的音乐」并（默认）直接导入 OmniShelf。

        page_size: 每次向 ncm-cli 请求多少首（1~500）；0 = 用插件配置（默认 500）
        dry_run: 只抓取不导入；快照照样落盘，之后可到上传页手动导入
        full: 整份重抓。默认有同一歌单的快照时只取新歌（遇到整页已知歌曲就停）；
              取消收藏/被移出的旧歌只有全量才能发现
        """
        config = effective_config(self.ctx)
        size = int(page_size) if page_size else int(config["page_size"])
        if not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size 必须在 1~{MAX_PAGE_SIZE} 之间，收到 {size}")
        try:
            report = run_sync(
                self.ctx,
                page_size=size,
                dry_run=dry_run,
                full=full,
                progress=self.log,
            )
        except NeteaseLoginError as exc:
            raise RuntimeError(
                f"未登录或登录已过期：{exc}。先运行 "
                "`omnishelf plugin netease_music login` 扫码登录。"
            ) from exc
        except NeteaseError as exc:
            raise RuntimeError(str(exc)) from exc

        playlist = report["playlist"]
        lines = [
            f"歌单：{playlist['name'] or '(未命名)'}（加密 id {playlist['id']}"
            + (f"；接口元数据 {playlist['track_count']} 首"
               if playlist.get("track_count") is not None else "")
            + "）"
        ]
        if report["empty"]:
            lines.append("没有抓到歌曲（歌单为空或接口返回空），未写快照。")
            return "\n".join(lines)

        if report["mode"] == "incremental":
            if report["snapshot_written"]:
                suffix = "（已读完整份歌单）" if report["complete"] else ""
                lines.append(
                    f"增量完成：检查 {report['checked']} 首 · 新增 {report['new_count']} 首"
                    f" · 快照共 {report['count']} 首（去重后 {report['unique_count']} 首）"
                    f" · {report['requests']} 次请求 · {report['elapsed']} 秒{suffix}"
                )
                lines.append(f"快照：{report['path']}（importer {IMPORTER}）")
            else:
                lines.append(
                    f"增量完成：没有新歌（检查 {report['checked']} 首）"
                    f" · {report['requests']} 次请求 · {report['elapsed']} 秒"
                )
        else:
            lines.append(
                f"抓取完成：{report['count']} 首（去重后 {report['unique_count']} 首）"
                f" · {report['requests']} 次请求 · {report['elapsed']} 秒"
            )
            lines.append(f"快照：{report['path']}（importer {IMPORTER}）")

        if not report["snapshot_written"]:
            lines.append("导入：没有新歌，已跳过")
        elif dry_run:
            lines.append("导入：已跳过（--dry-run）；到上传页选 importer "
                         f"{IMPORTER} 导入快照即可")
        elif report["import_error"]:
            lines.append(f"导入失败：{report['import_error']}"
                         "（快照没丢，可到上传页手动导入）")
        else:
            result = report["import"] or {}
            lines.append(
                f"导入：共 {result.get('total', 0)} 条 · 新增 {result.get('inserted', 0)}"
                f" · 更新 {result.get('updated', 0)} · 未变化 {result.get('unchanged', 0)}"
                f" · 跳过 {result.get('skipped', 0)} · 错误 {result.get('error_count', 0)}"
                f" · 日志 #{result.get('log_id', '?')}"
            )
        return "\n".join(lines)

    def quick_update(self, options: Mapping[str, Any] | None = None) -> QuickUpdateResult:
        """抓「我喜欢的音乐」并导入：有快照时只取新歌（遇到整页已知歌曲就停）。"""
        try:
            text = self.sync()
        except (RuntimeError, ValueError) as exc:
            return QuickUpdateResult(ok=False, summary=str(exc))
        problems: list[str] = []
        if "没有抓到歌曲" in text:
            problems.append("歌单为空，未写快照")
        if "导入失败" in text:
            problems.append("导入失败")
        header = next(
            (line.strip() for line in text.splitlines() if line.startswith("增量完成")),
            "",
        ) or next(
            (line.strip() for line in text.splitlines() if line.startswith("抓取完成")),
            "网易云收藏已同步",
        )
        summary = header + ("；" + "；".join(problems) if problems else "")
        return QuickUpdateResult(ok=not problems, summary=summary, details=text)
