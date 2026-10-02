"""HTTP 客户端：请求封装、token 获取、账号密码登录、分页拉取收藏。

站点接口的关键点（从前端 ``person.js`` 的 http 封装还原）：

* 所有接口挂在同一个域名下，前端用的是 ``location.host``，所以这里用 ``config.host``。
* 请求必须带 ``Authorization: Token <token>`` 头，只带 Cookie 会返回 401。
* 还要带 ``platform: 2``。
"""

from __future__ import annotations

import base64
import json
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import ENDPOINTS, SHELF_ORDERING, ExportConfig, save_token_to_config
from .errors import ApiError, AuthError
from .models import CollectItem

LOGIN_KB = "/api/kb/web/login"
LOGIN_V1 = "/api/v1/login"
USER_INFO = "/api/v2/web/user/info"


class CopymangaClient:
    """拷贝漫画接口客户端。

    典型用法::

        from copymanga import CopymangaClient, ExportConfig

        client = CopymangaClient(ExportConfig(token="<token>"))
        client.ensure_authenticated()
        print(client.user["nickname"])

        for rows, total in client.pages("comics", free_type=1):
            ...

    :param config: :class:`~copymanga.config.ExportConfig`，省略则用默认值。
    :param overrides: 直接覆盖配置字段，例如 ``CopymangaClient(token="xx")``。
    """

    def __init__(self, config: Optional[ExportConfig] = None, **overrides: Any) -> None:
        cfg = config or ExportConfig()
        if overrides:
            cfg = cfg.merge(**overrides)
        self.config = cfg.validate()
        self.user: Dict[str, Any] = {}
        self._token: Optional[str] = self._extract_token()

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    def __enter__(self) -> "CopymangaClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        """释放资源（当前用 urllib，无连接池，留作扩展）。"""

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------

    @property
    def token(self) -> Optional[str]:
        """当前使用的 token（可能为 ``None``）。"""
        return self._token

    def _extract_token(self) -> Optional[str]:
        """按 token > cookie > 环境顺序找现成凭据（不发起登录）。"""
        if self.config.token:
            return self.config.token.strip()

        cookie = (self.config.cookie or "").strip()
        if cookie:
            for part in cookie.split(";"):
                key, _, value = part.partition("=")
                if key.strip() == "token" and value.strip():
                    return value.strip().strip('"')
        return None

    def login(self, username: Optional[str] = None, password: Optional[str] = None) -> str:
        """用账号密码换取 token。

        免费站走 ``/api/kb/web/login``；前端逻辑是
        ``salt = 6 位随机数字``、``password = base64(明文密码 + "-" + salt)``。
        登录成功后会得到一个新的 token（旧 token 依然有效）。

        :return: 新的 token 字符串。
        :raises AuthError: 所有登录尝试都失败。
        """
        username = username or self.config.username
        password = password or self.config.password
        if not username or not password:
            raise AuthError("缺少用户名或密码")

        salt = str(random.randint(100000, 999999))
        masked = base64.b64encode(("%s-%s" % (password, salt)).encode("utf-8")).decode("ascii")
        base = self.config.base_url

        attempts: List[Tuple[str, Dict[str, Any]]] = [
            (base + LOGIN_KB, {"username": username, "password": masked, "salt": salt}),
            (
                base + LOGIN_KB,
                {
                    "username": username,
                    "password": masked,
                    "salt": salt,
                    "platform": 2,
                    "version": "",
                    "source": "freeSite",
                },
            ),
            (base + LOGIN_V1, {"username": username, "password": masked, "salt": salt, "platform": 2}),
        ]

        problems: List[str] = []
        for url, payload in attempts:
            try:
                resp = self.request(url, method="POST", data=payload, token=None, retries=1)
            except ApiError as exc:
                problems.append("%s -> %s" % (url, exc))
                continue

            found = None
            if isinstance(resp.get("results"), dict):
                found = resp["results"].get("token")
            if not found and isinstance(resp.get("data"), dict):
                found = resp["data"].get("token")
            if found:
                self._token = found
                return found
            problems.append("%s -> %s" % (url, json.dumps(resp, ensure_ascii=False)[:200]))

        raise AuthError("账号密码登录失败：\n  " + "\n  ".join(problems))

    def get_user_info(self) -> Dict[str, Any]:
        """读取当前账号信息（同时作为 token 的有效性校验）。

        :raises AuthError: token 无效或缺失。
        """
        if not self._token:
            raise AuthError("还没有 token")
        resp = self.request(self.config.base_url + USER_INFO, token=self._token, retries=1)
        results = resp.get("results")
        if not isinstance(results, dict):
            raise ApiError("用户信息返回格式异常：%s" % json.dumps(resp, ensure_ascii=False)[:200])
        self.user = results
        return results

    def ensure_authenticated(self) -> str:
        """确保手里有一个可用 token，并顺手缓存账号信息。

        流程：有 token 就校验一次；校验失败且有账号密码就重新登录再校验。
        自动续期成功后，如果配置里开着 ``persist_token``（默认）并且有
        ``config_path``，会把新 token 写回配置文件，下次运行就不必再登录。

        :return: 可用的 token。
        :raises AuthError: 既没有凭据，凭据也换不到有效 token。
        """
        if self._token:
            try:
                self.get_user_info()
                return self._token
            except AuthError:
                if not (self.config.username and self.config.password):
                    raise AuthError("token 已失效，且未提供 username/password 用于重新登录")

        if self.config.username and self.config.password:
            self.login()
            self.get_user_info()
            self._persist_token()
            return self._token  # type: ignore[return-value]

        raise AuthError(
            "缺少认证信息：请提供 token、cookie，或 username + password。"
            "token 获取方式见 docs/API.md。"
        )

    def _persist_token(self) -> bool:
        """把续期后的 token 写回配置文件（失败不影响同步）。"""
        if not (self.config.persist_token and self._token):
            return False
        return save_token_to_config(self.config.config_path, self._token)

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------

    def build_headers(self, token: Optional[str], extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构造与浏览器一致的请求头。"""
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "platform": "2",
            "Origin": self.config.base_url,
            "Referer": self.config.base_url + "/web/person/shujia",
        }
        if token:
            headers["Authorization"] = "Token " + token
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        token: Optional[str] = "__current__",  # type: ignore[assignment]
        retries: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """发一个请求并返回解析后的 JSON。

        :param params: 拼到 URL 上的查询参数。
        :param data: 表单体（非 GET）。
        :param token: 传 ``None`` 表示匿名请求；默认用当前 token。
        :raises AuthError: HTTP 401/403。
        :raises ApiError: 网络错误、非 JSON 响应、重试耗尽。
        """
        if token == "__current__":
            token = self._token
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)

        body = urllib.parse.urlencode(data).encode("utf-8") if data else None
        retries = self.config.retries if retries is None else retries
        timeout = self.config.timeout if timeout is None else timeout
        context = None if self.config.verify_ssl else ssl._create_unverified_context()

        last_err: Optional[Exception] = None
        for attempt in range(1, max(1, retries) + 1):
            req = urllib.request.Request(
                url, data=body, method=method.upper(), headers=self.build_headers(token)
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                    text = resp.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    raise ApiError(
                        "返回内容不是 JSON（可能被 Cloudflare 拦截）：%s" % text[:200]
                    )
                if isinstance(payload, dict) and payload.get("code") in (401, 403):
                    raise AuthError("接口返回 code=%s：%s" % (payload.get("code"), payload.get("message")))
                return payload
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                if exc.code in (401, 403):
                    raise AuthError("HTTP %s 认证失败：%s" % (exc.code, detail or "token 可能已失效"))
                last_err = ApiError("HTTP %s %s" % (exc.code, detail))
            except AuthError:
                raise
            except ApiError:
                raise
            except Exception as exc:  # 网络抖动 / 超时
                last_err = exc

            if attempt < max(1, retries):
                wait = self.config.retry_backoff * attempt
                if not self.config.quiet:
                    print("    ! 请求失败(%s)，%.1fs 后重试…" % (last_err, wait))
                time.sleep(wait)

        raise ApiError("请求失败：%s" % last_err)

    # ------------------------------------------------------------------
    # 收藏数据
    # ------------------------------------------------------------------

    def fetch_collect_page(
        self,
        kind: str = "comics",
        free_type: int = 1,
        offset: int = 0,
        limit: Optional[int] = None,
        *,
        ordering: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """取一页收藏。

        :param kind: ``"comics"``（漫画）或 ``"cartoons"``（動畫）。
        :param free_type: 接口的 free_type 分组，1 / 2（免费站两组返回同一批数据）。
        :param offset: 偏移量。
        :param limit: 每页条数，默认用配置里的 ``page_size``。
        :param ordering: 排序方式，默认用配置里的 ``ordering``
            （``-datetime_modifier`` = 按加到書架時間降序）。
        :return: ``(本页原始记录列表, 远端总数)``。
        """
        if kind not in ENDPOINTS:
            raise ApiError("未知收藏类型 %r，可选 %s" % (kind, list(ENDPOINTS)))
        limit = limit or self.config.page_size
        resp = self.request(
            self.config.base_url + ENDPOINTS[kind],
            params={
                "limit": limit,
                "offset": offset,
                "free_type": free_type,
                "ordering": ordering or self.config.ordering,
            },
            token=self._token,
        )
        results = resp.get("results") or {}
        rows = results.get("list") or []
        return rows, int(results.get("total") or 0)

    def pages(
        self,
        kind: str = "comics",
        free_type: int = 1,
        *,
        limit: Optional[int] = None,
        max_pages: Optional[int] = None,
        delay: Optional[float] = None,
    ) -> Iterator[Tuple[List[Dict[str, Any]], int]]:
        """逐页产出 ``(本页记录, 远端总数)``，直到取完。

        调用方可以在循环里 ``break``（增量模式就是这么提前收手的）。
        """
        limit = limit or self.config.page_size
        max_pages = self.config.max_pages if max_pages is None else max_pages
        delay = self.config.request_delay if delay is None else delay

        offset = 0
        page_no = 0
        total: Optional[int] = None
        while True:
            if max_pages is not None and page_no >= max_pages:
                break
            rows, total = self.fetch_collect_page(kind, free_type, offset, limit)
            page_no += 1
            if not rows:
                break
            yield rows, total
            offset += len(rows)
            if total and offset >= total:
                break
            if len(rows) < limit:
                break
            if delay:
                time.sleep(delay)

    def iter_collects(
        self,
        kind: Optional[str] = None,
        free_type: Optional[int] = None,
        *,
        limit: Optional[int] = None,
        dedupe: bool = True,
    ) -> Iterator[CollectItem]:
        """遍历收藏并产出 :class:`~copymanga.models.CollectItem`。

        不传 ``kind`` / ``free_type`` 时按配置里的 ``kinds`` / ``free_types`` 全走一遍，
        并按收藏 ID 去重（免费站 free_type=1/2 返回的是同一批数据）。

        当排序方式是 ``-datetime_modifier``（按加到書架時間降序，默认）时，
        产出的 ``shelf_position`` 就是真实书架位次（0 = 最新加入書架）；
        其它排序下该字段置 0，因为它与书架顺序无关。

        :param dedupe: ``False`` 时不去重，每次请求到的都产出。
        """
        kinds = (kind,) if kind else tuple(self.config.kinds)
        free_types = (free_type,) if free_type is not None else tuple(self.config.free_types)
        self.ensure_authenticated()

        shelf_ordered = self.config.ordering == SHELF_ORDERING
        seen: set = set()
        position = -1
        for k in kinds:
            for ft in free_types:
                for rows, _total in self.pages(k, ft, limit=limit):
                    for row in rows:
                        cid = str(row.get("uuid"))
                        if dedupe and cid in seen:
                            continue
                        seen.add(cid)
                        position += 1
                        yield CollectItem.from_api(
                            row,
                            host=self.config.base_url,
                            free_type=ft,
                            kind=k,
                            shelf_position=position if shelf_ordered else 0,
                        )
