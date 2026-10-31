"""ncm-cli 的命令行封装：扫码登录 + 读取「我喜欢的音乐」歌单。

这个模块只做三件事：

1. **登录**：先 ``ncm-cli login --check`` 查状态，未登录时执行 ``ncm-cli login``
   拿到二维码/登录链接，再轮询 ``--check`` 直到用户确认（或超时）；
2. **找收藏歌单**：``ncm-cli playlist created --limit 1`` 最多回两条记录，
   最后一条就是收藏歌单，取它的**加密 id**（``id`` 字段，不是 ``originalId``）；
3. **读歌曲**：``ncm-cli playlist tracks --playlistId <加密 id> --limit N --offset M``，
   传页码就只取那一页，不传页码就一路翻到短页/空页为止。需要增量时可以传
   ``known`` 判断函数：歌单按收藏时间倒序，遇到一整页都是已知歌曲就提前停下。

前提：ncm-cli 已安装，并在它自己的配置里保存了 appid / 私钥
（``ncm-cli configure``）。本模块**不读取、不保存、不传递任何凭据**；
登录链接属于一次性的登录会话，只放在 ``LoginPrompt`` 里交给调用方展示，
不要写进日志或状态文件。

playlist 命令依赖登录态：``check_login()`` 为假时 ncm-cli 会输出一句非 JSON 的
``TokenExpiredError``（退出码仍是 0）。所以歌单相关方法会先检查登录，
并把这类输出统一转成 ``NeteaseLoginError``。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import (
    NcmCliError,
    NcmCliNotFound,
    NcmCliOutputError,
    NeteaseApiError,
    NeteaseError,
    NeteaseLoginError,
    NeteaseLoginTimeout,
)
from .models import LoginPrompt, LoginResult, LoginStatus, Playlist

#: ``ncm-cli playlist tracks`` 的每页上限
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

#: 单条命令的超时（秒）；歌单翻页也是这个值
DEFAULT_TIMEOUT = 60.0
#: 等用户扫码的总时长（秒）
DEFAULT_LOGIN_TIMEOUT = 300.0
#: 扫码期间的检查间隔（秒）
DEFAULT_POLL_INTERVAL = 2.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

#: 这些字样出现在命令输出里 = 登录态有问题，不是数据问题
_LOGGED_OUT_MARKERS = (
    "未登录",
    "登录已过期",
    "登录过期",
    "登录失效",
    "tokenexpired",
    "token expired",
    "invalid token",
    "not logged in",
    "please login",
    "unauthorized",
)


@dataclass(frozen=True)
class CommandResult:
    """一次子进程调用的结果（测试可以构造它来替换真实 CLI）。"""

    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class TrackFetch:
    """一次歌单遍历的结果（全量或增量）。"""

    rows: list[dict[str, Any]]
    requests: int
    #: True = 一直读到短页/空页；False = 遇到整页已知歌曲提前停了
    complete: bool

    @property
    def stopped_early(self) -> bool:
        return not self.complete


#: 跑一条 ncm-cli 命令；返回退出码与标准输出/错误，不负责判断成功与否
CommandRunner = Callable[[Sequence[str], float], CommandResult]


def run_ncm_cli(command: Sequence[str], timeout: float) -> CommandResult:
    """默认的命令执行器：固定 argv、不经过 shell、不读 stdin。

    ``ncm-cli login`` 在当前版本会立刻返回（它自己起后台轮询），所以这里不需要
    交互式终端；``stdin=DEVNULL`` 同时避免某些版本改成前台等待时把请求线程挂住。
    """
    argv = [str(part) for part in command]
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NcmCliNotFound(argv[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise NcmCliError(
            argv, -1, "", f"命令超过 {timeout:g} 秒没有返回"
        ) from exc
    return CommandResult(
        tuple(argv),
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )


def _short(text: Any, limit: int = 300) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


def _loads_json(text: str, command: Sequence[str]) -> Any:
    """解析命令输出里的 JSON。

    ncm-cli 正常时只输出一段 JSON；但版本升级或下面某条命令出错时可能先打一行
    日志/错误。依次尝试「整段」「最后几行」「首尾花括号之间」，都不行才报错。
    """
    cleaned = _ANSI_RE.sub("", str(text or "")).strip().lstrip("\ufeff")
    if not cleaned:
        raise NcmCliOutputError(command, text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for line in reversed(cleaned.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise NcmCliOutputError(command, text)


def _looks_logged_out(text: Any) -> bool:
    lowered = str(text or "").casefold()
    return any(marker in lowered for marker in _LOGGED_OUT_MARKERS)


def _first_url(text: Any) -> str:
    match = _URL_RE.search(str(text or ""))
    if not match:
        return ""
    return match.group(0).rstrip("，。,.);；")


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if text.lstrip("+-").isdigit():
        return int(text)
    return None


def _validate_page(page: int | None) -> int | None:
    if page is None:
        return None
    if isinstance(page, bool) or not isinstance(page, int):
        raise ValueError("page 必须是正整数；不传表示取全量")
    if page < 1:
        raise ValueError("page 从 1 开始；要取全量就别传 page")
    return page


def _validate_page_size(page_size: int) -> int:
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise ValueError("page_size 必须是整数")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size 必须在 1~{MAX_PAGE_SIZE} 之间")
    return page_size


def _prompt_from_payload(payload: Mapping[str, Any]) -> LoginPrompt:
    """从 ``ncm-cli login`` 的返回里取二维码 / 可点击链接。"""
    rows: list[Mapping[str, Any]] = [payload]
    nested = payload.get("data")
    if isinstance(nested, Mapping):
        rows.append(nested)

    message = ""
    qr_url = ""
    clickable = ""
    for row in rows:
        if not message:
            message = str(row.get("message") or row.get("msg") or "").strip()
        if not qr_url:
            qr_url = str(
                row.get("qrCodeUrl") or row.get("qr_code_url") or ""
            ).strip()
        if not clickable:
            clickable = str(
                row.get("clickableUrl")
                or row.get("clickable_url")
                or row.get("loginUrl")
                or row.get("url")
                or ""
            ).strip()
    url = clickable or qr_url or _first_url(message)
    return LoginPrompt(
        url=url,
        message=message,
        qr_url=qr_url or url,
        raw=dict(payload),
    )


def _assert_code_ok(payload: Mapping[str, Any]) -> None:
    """``code`` 不是 200 就报错；登录态的报错换成 ``NeteaseLoginError``。"""
    if "code" not in payload:
        return
    code = payload.get("code")
    if code is None or str(code).strip() == "200":
        return
    message = str(
        payload.get("message") or payload.get("msg") or "接口没有返回成功"
    ).strip() or "接口没有返回成功"
    if _looks_logged_out(message):
        raise NeteaseLoginError(message)
    raise NeteaseApiError(message, code=code)


def _track_rows(data: Any) -> list[dict[str, Any]]:
    """歌曲列表：正常是数组；也接受 ``{"records"/"songs"/"tracks"/"list": [...]}``。"""
    if data is None:
        return []
    rows = data
    if isinstance(data, Mapping):
        found: Any = None
        for key in ("records", "songs", "tracks", "list"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                found = candidate
                break
        if found is None:
            raise NeteaseApiError("歌曲列表不是数组（data 里没有 records/songs/tracks）")
        rows = found
    if not isinstance(rows, list):
        raise NeteaseApiError("歌曲列表不是数组")
    return [dict(row) for row in rows if isinstance(row, Mapping)]


class NeteaseClient:
    """``ncm-cli`` 的薄封装。所有方法同步执行，出错抛 :class:`NeteaseError` 子类。

    典型用法::

        client = NeteaseClient()
        result = client.login(on_prompt=print)   # 已登录会直接返回
        playlist = client.favorite_playlist()    # 「我喜欢的音乐」的加密 id
        page2 = client.favorite_tracks(page=2, page_size=100)
        everything = client.favorite_tracks()    # 不传 page = 全量

    ``runner`` / ``clock`` / ``sleep`` 只给测试替换用；正常使用不要传。
    """

    def __init__(
        self,
        binary: str = "ncm-cli",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        login_timeout: float = DEFAULT_LOGIN_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        runner: CommandRunner | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.binary = str(binary or "ncm-cli").strip() or "ncm-cli"
        self.timeout = self._positive_value(timeout, "timeout")
        self.login_timeout = self._positive_value(login_timeout, "login_timeout")
        self.poll_interval = self._positive_value(poll_interval, "poll_interval")
        self._runner = runner if runner is not None else run_ncm_cli
        self._clock = clock
        self._sleep = sleep

    @staticmethod
    def _positive_value(value: float, name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是正数") from exc
        if number <= 0:
            raise ValueError(f"{name} 必须是正数")
        return number

    # ---------------------------------------------------------------- 命令执行

    def _run(self, *parts: str, timeout: float | None = None) -> CommandResult:
        command = (self.binary, *parts)
        result = self._runner(command, self.timeout if timeout is None else timeout)
        if result.returncode != 0:
            raise NcmCliError(
                command, result.returncode, result.stdout, result.stderr
            )
        return result

    def _run_json(self, *parts: str, timeout: float | None = None) -> dict[str, Any]:
        result = self._run(*parts, timeout=timeout)
        payload = _loads_json(result.stdout, result.command)
        if not isinstance(payload, Mapping):
            raise NcmCliOutputError(result.command, result.stdout, result.stderr)
        return dict(payload)

    def _playlist_json(self, *parts: str) -> dict[str, Any]:
        """跑歌单命令；登录态失效时统一转成 ``NeteaseLoginError``。

        未登录时 ncm-cli 会打印 ``❌ TokenExpiredError: 登录已过期，请重新登录``
        并以 0 退出——不转的话会变成一句莫名其妙的「输出不是 JSON」。
        """
        try:
            payload = self._run_json(*parts)
        except (NcmCliError, NcmCliOutputError) as exc:
            text = f"{exc.stdout}\n{exc.stderr}"
            if _looks_logged_out(text):
                raise NeteaseLoginError(
                    "未登录或登录已过期：先调用 login() 完成扫码登录"
                ) from exc
            raise
        _assert_code_ok(payload)
        return payload

    # ---------------------------------------------------------------- 登录

    def check_login(self) -> LoginStatus:
        """只查登录状态，不发起登录。"""
        try:
            payload = self._run_json(
                "login", "--check", "--output", "json", timeout=min(self.timeout, 30.0)
            )
        except NcmCliOutputError as exc:
            text = f"{exc.stdout}\n{exc.stderr}"
            if _looks_logged_out(text):
                return LoginStatus(False, _short(text))
            raise
        success = payload.get("success")
        if isinstance(success, str):
            logged_in = success.strip().casefold() in ("true", "1", "yes")
        else:
            logged_in = success is True
        message = str(payload.get("message") or payload.get("msg") or "").strip()
        return LoginStatus(logged_in, message, payload)

    def require_login(self) -> LoginStatus:
        """确认已登录；没登录就抛 ``NeteaseLoginError``（不会自动弹链接）。"""
        status = self.check_login()
        if not status.logged_in:
            raise NeteaseLoginError(
                status.message or "未登录：先调用 login() 完成扫码登录"
            )
        return status

    def _start_login(self) -> dict[str, Any]:
        """执行 ``ncm-cli login``（它会立刻返回并起后台轮询）。"""
        command = (self.binary, "login", "--output", "json")
        result = self._run("login", "--output", "json")
        try:
            payload = _loads_json(result.stdout, command)
        except NcmCliOutputError:
            # 万一某个版本无视 --output 打了人类可读的二维码：只要能抠到链接就继续
            url = _first_url(result.stdout)
            if not url:
                raise
            return {
                "success": True,
                "clickableUrl": url,
                "message": "请打开链接完成登录",
            }
        if not isinstance(payload, Mapping):
            raise NcmCliOutputError(command, result.stdout, result.stderr)
        return dict(payload)

    def login(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
        on_prompt: Callable[[LoginPrompt], None] | None = None,
        on_status: Callable[[LoginStatus, int], None] | None = None,
    ) -> LoginResult:
        """登录；已登录则直接返回，否则生成二维码链接并等用户确认。

        ``wait=False`` 只生成链接、不等待（之后用 :meth:`check_login` 自己轮询）。
        ``on_prompt`` 在拿到链接时调用一次——页面/终端必须**当场**把它显示出来，
        用户扫码后 ``ncm-cli`` 的后台轮询才会把登录态写回去。
        ``on_status`` 每次检查登录状态后调用，参数是 ``(状态, 第几次检查)``。
        等满 ``timeout``（默认 ``login_timeout``）仍未登录会抛
        :class:`NeteaseLoginTimeout`；异常里不带链接文本，链接在 ``exc.prompt``。
        """
        started = self._clock()
        status = self.check_login()
        if status.logged_in:
            return LoginResult(
                logged_in=True,
                prompt=None,
                elapsed=0.0,
                message=status.message or "已登录",
                raw=status.raw,
            )

        payload = self._start_login()
        if payload.get("success") is False:
            message = str(payload.get("message") or "ncm-cli 没能生成登录链接")
            raise NeteaseLoginError(message)
        prompt = _prompt_from_payload(payload)
        if on_prompt is not None:
            on_prompt(prompt)

        if not wait:
            return LoginResult(
                logged_in=False,
                prompt=prompt,
                elapsed=0.0,
                message=prompt.message,
                raw=payload,
            )

        wait_timeout = self.login_timeout if timeout is None else self._positive_value(
            timeout, "timeout"
        )
        deadline = started + wait_timeout
        polls = 0
        last = status
        while self._clock() < deadline:
            self._sleep(self.poll_interval)
            polls += 1
            last = self.check_login()
            if on_status is not None:
                on_status(last, polls)
            if last.logged_in:
                return LoginResult(
                    logged_in=True,
                    prompt=prompt,
                    elapsed=round(self._clock() - started, 1),
                    message=last.message or "登录成功",
                    raw=last.raw,
                )
        raise NeteaseLoginTimeout(wait_timeout, prompt)

    # ---------------------------------------------------------------- 歌单

    def favorite_playlist(self) -> Playlist:
        """取「我喜欢的音乐」歌单（加密 id）。

        ``playlist created --limit 1`` 会返回最多两条记录（自建歌单 + 收藏歌单），
        最后一条是收藏歌单；这里只认它的 ``id``，**不是** ``originalId``。
        """
        self.require_login()
        payload = self._playlist_json(
            "playlist",
            "created",
            "--limit",
            "1",
            "--offset",
            "0",
            "--output",
            "json",
        )
        data = payload.get("data")
        records: Any = data.get("records") if isinstance(data, Mapping) else None
        if records is None:
            records = payload.get("records")
        if not isinstance(records, list):
            raise NeteaseApiError(
                "歌单列表结构不对（data.records 不是数组）",
                code=payload.get("code"),
            )
        if not records:
            raise NeteaseApiError("账号下没有歌单（data.records 为空）", code=payload.get("code"))
        row = records[-1]
        if not isinstance(row, Mapping):
            raise NeteaseApiError("最后一条歌单记录不是对象", code=payload.get("code"))
        encrypted_id = str(row.get("id") or "").strip()
        if not encrypted_id:
            raise NeteaseApiError(
                "歌单记录缺少加密 id（id 字段；不要用 originalId）",
                code=payload.get("code"),
            )
        return Playlist(
            id=encrypted_id,
            original_id=_int_or_none(row.get("originalId")),
            name=str(row.get("name") or "").strip(),
            track_count=_int_or_none(row.get("trackCount")),
            raw=dict(row),
        )

    def _tracks_page(
        self, playlist_id: str, *, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        payload = self._playlist_json(
            "playlist",
            "tracks",
            "--playlistId",
            playlist_id,
            "--limit",
            str(limit),
            "--offset",
            str(offset),
            "--output",
            "json",
        )
        if "data" not in payload:
            raise NeteaseApiError("歌单歌曲返回里没有 data 字段", code=payload.get("code"))
        return _track_rows(payload.get("data"))

    def playlist_tracks(
        self,
        playlist_id: str,
        page: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        *,
        total: int | None = None,
        on_page: Callable[[int, int, int | None], None] | None = None,
    ) -> list[dict[str, Any]]:
        """读指定歌单的歌曲（原始 JSON 行，保持接口顺序）。

        传 ``page`` 就只取那一页（``offset = (page - 1) * page_size``）；
        不传就翻到**短页或空页**为止，返回全量。
        ``total`` 只用来判断「短页是不是服务端限了每页数量」和给进度条当分母，
        不会让全量抓取提前停（歌单总数可能过期）。
        ``on_page(第几次请求, 已取条数, total)`` 每次请求后回调一次。

        要做增量（只看最新的几页）用 :meth:`fetch_playlist_tracks`。
        """
        encrypted_id = str(playlist_id or "").strip()
        if not encrypted_id:
            raise ValueError("playlist_id 不能为空（要传加密 id）")
        page = _validate_page(page)
        size = _validate_page_size(page_size)
        expected = total if isinstance(total, int) and not isinstance(total, bool) and total > 0 else None

        if page is not None:
            rows = self._tracks_page(
                encrypted_id, offset=(page - 1) * size, limit=size
            )
            if on_page is not None:
                on_page(page, len(rows), expected)
            return rows

        return self.fetch_playlist_tracks(
            encrypted_id,
            page_size=size,
            total=expected,
            on_page=on_page,
        ).rows

    def fetch_playlist_tracks(
        self,
        playlist_id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        total: int | None = None,
        on_page: Callable[[int, int, int | None], None] | None = None,
        known: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> TrackFetch:
        """从头遍历歌单；传 ``known`` 时按增量取。

        ``known(row)`` 返回该行是否已经在本地快照里。网易云「我喜欢的音乐」按
        **收藏时间倒序**排列，新歌在最前面，所以某一页的每一行都是已知歌曲时，
        后面的都可以不再请求（``complete=False``）；返回的 ``rows`` 仍包含这一页
        的已知行，调用方可以据此合并快照。

        不传 ``known`` 或歌单顺序被改过时，用 ``page=None`` 的正常全量遍历
        （``complete=True``）兜底——增量发现不了「取消收藏」的旧歌。
        """
        encrypted_id = str(playlist_id or "").strip()
        if not encrypted_id:
            raise ValueError("playlist_id 不能为空（要传加密 id）")
        size = _validate_page_size(page_size)
        expected = total if isinstance(total, int) and not isinstance(total, bool) and total > 0 else None

        collected: list[dict[str, Any]] = []
        offset = 0
        request_no = 0
        complete = True
        while True:
            rows = self._tracks_page(encrypted_id, offset=offset, limit=size)
            request_no += 1
            collected.extend(rows)
            if on_page is not None:
                on_page(request_no, len(collected), expected)
            if not rows:
                break
            if known is not None and all(known(row) for row in rows):
                complete = False
                break
            offset += len(rows)
            if len(rows) < size:
                # 正常到末页了；但如果总数还没到，说明服务端把 limit 压小了，继续往后取
                if expected is None or len(collected) >= expected:
                    break
        return TrackFetch(collected, request_no, complete)

    def favorite_tracks(
        self,
        page: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        *,
        on_page: Callable[[int, int, int | None], None] | None = None,
    ) -> list[dict[str, Any]]:
        """读收藏歌单（「我喜欢的音乐」）的歌曲。

        传 ``page`` 与 ``page_size`` 取一页；**不传 page 返回全量**。
        内部先确认登录，再用 ``playlist created --limit 1`` 找收藏歌单的加密 id。
        """
        page = _validate_page(page)
        size = _validate_page_size(page_size)
        playlist = self.favorite_playlist()
        return self.playlist_tracks(
            playlist.id,
            page=page,
            page_size=size,
            total=playlist.track_count,
            on_page=on_page,
        )


__all__ = [
    "CommandResult",
    "CommandRunner",
    "DEFAULT_LOGIN_TIMEOUT",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_TIMEOUT",
    "MAX_PAGE_SIZE",
    "NeteaseClient",
    "NeteaseError",
    "TrackFetch",
    "run_ncm_cli",
]
