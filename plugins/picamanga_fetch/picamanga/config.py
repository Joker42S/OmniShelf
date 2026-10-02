"""配置对象、接口常量与配置加载。

优先级（高 → 低）：命令行参数 > 环境变量 > 内置默认值。

本工具**不读配置文件**：凭据只从参数/环境变量进来，落盘由调用方决定
（OmniShelf 那一侧存进插件自己的 ``config.toml``）。这样工具目录里不会
凭空多出一份带真实令牌的 JSON。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import ConfigError

BASE_URL = "https://picaapi.picacomic.com"

# —— 请求签名 ——
# 这三个值是哔咔**官方 App 内置**的常量（客户端逆向所得，各家开源实现都用同一套），
# 用来给请求算 HMAC-SHA256 签名，不是任何人的账号或令牌。
API_KEY = "C69BAF41DA5ABD1FFEDC6D2FEA56B"
NONCE = "b1ab87b4800d4d4590a11701b8551afa"
SECRET_KEY = r"~d}$Q7$eIni=V)9\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"

# —— 请求头（与 App 一致，缺一项服务端会拒绝）——
APP_VERSION = "2.2.1.2.3.3"
APP_UUID = "defaultUuid"
APP_PLATFORM = "android"
APP_BUILD_VERSION = "44"
APP_CHANNEL = "2"
USER_AGENT = "okhttp/3.8.1"
ACCEPT_JSON = "application/vnd.picacomic.com.v1+json"

#: 本工具用到的只有两个接口
SIGN_IN_PATH = "/auth/sign-in"
FAVOURITE_PATH = "/users/favourite"

#: 认证失败时 ``error`` 字段的取值（HTTP 401 之外还有这两个业务码）
AUTH_ERROR_CODES: Tuple[str, ...] = ("1005", "1008")

#: 同步模式
#:   - ``incremental`` 增量：按「新收藏在前」翻页，连续命中本地已有条目即停，快
#:   - ``full``        全量：遍历所有页，本地有、远端没有的条目标记为已取消收藏
MODES: Tuple[str, ...] = ("incremental", "full")

#: 收藏列表接口（``/users/favourite?s=<sort>``）的排序值 → 含义。
#: 取值与官方 App 的 ``ComicSort`` 一致；``dd``/``da`` 是按**收藏日期**排序。
SORTS: Dict[str, str] = {
    "ua": "默认",
    "dd": "新到旧（按收藏日期）",
    "da": "旧到新（按收藏日期）",
    "ld": "最多爱心",
    "vd": "最多指名",
}

#: 唯一能保证「新收藏在头部」的排序，增量同步必须用它
NEWEST_FIRST_SORT = "dd"

#: 服务端单页固定返回 20 条（实测传 ``limit`` 无效，被忽略）。
#: 这里留着只是用来估算进度，不是可调参数。
PAGE_SIZE = 20

#: 增量扫描要**连续命中**多少个本地已有条目才判定「后面的都与本地一致」并停止翻页。
#:
#: 为什么不是「命中 1 个就停」：取消收藏后重新收藏会把那条刷新到列表头部，
#: 它后面紧跟的**全是**本地没有的新收藏，单次命中就停会把这些全部漏掉。
#: 要求连续命中可以挡掉这种跳变——重收藏只造成一次命中，连续计数随即归零。
DEFAULT_CONSECUTIVE_KNOWN_STOP = 5

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)

#: 工具的默认数据目录。**别在插件目录里直接跑 ``python -m picamanga``**——
#: 真实收藏会落进这里（主仓库 ``.gitignore`` 的 ``data/`` 规则是第二道保险）。
DEFAULT_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

#: 环境变量名 -> PicamangaConfig 字段名
ENV_MAP = {
    "PICAMANGA_HOST": "host",
    "PICAMANGA_MODE": "mode",
    "PICAMANGA_SORT": "sort",
    "PICAMANGA_TOKEN": "token",
    "PICAMANGA_EMAIL": "email",
    "PICAMANGA_PASSWORD": "password",
    "PICAMANGA_DATA_DIR": "data_dir",
    "PICAMANGA_MAX_PAGES": "max_pages",
    "PICAMANGA_DELAY": "request_delay",
    "PICAMANGA_TIMEOUT": "timeout",
    "PICAMANGA_RETRIES": "retries",
    "PICAMANGA_PROXY": "proxy",
}


@dataclass
class PicamangaConfig:
    """一次同步任务的全部可调参数。

    所有字段都有默认值，通常只需要填 ``token``（或 ``email``/``password``）。

    :param host: 接口地址，默认 ``https://picaapi.picacomic.com``。
    :param mode: ``"incremental"``（增量，默认）或 ``"full"``（全量）。
    :param sort: 收藏列表排序，取值见 :data:`SORTS`。增量只能用 ``dd``。
    :param token: 登录令牌，来自 App/网页的请求头 ``authorization``。
    :param email: 账号（哔咔的账号就是一个邮箱）。
    :param password: 口令，只用于换令牌。
    :param max_pages: 安全阀，单次最多翻多少页，``None`` 翻完为止。
    :param timeout: 单次请求超时秒数。
    :param retries: 失败重试次数。
    :param retry_backoff: 重试退避基数，第 n 次等待 ``retry_backoff * n`` 秒。
    :param request_delay: 翻页之间的间隔秒数，避免请求过快被风控。
    :param consecutive_known_stop: 增量扫描连续命中多少个本地已有条目就停手。
    :param proxy: 代理服务器地址，如 ``http://127.0.0.1:10809``。留空表示不在这里指定
        （这时走 urllib 的默认行为：认系统的 ``HTTPS_PROXY`` 等环境变量，没设就是直连）。
        填了就**只走这一个**，环境变量不再参与。
    :param verify_ssl: 是否校验 TLS 证书。
    :param enforce_sort: 是否校验「新收藏在头部」这一不变量，校验失败自动降级全量。
    :param data_dir: 本地数据目录，存放状态库。
    :param store_name: 状态库文件名。
    :param include_raw: 状态库里是否保留接口返回的全部字段。
    :param quiet: 静默模式，不打印进度。
    """

    # —— 站点与同步模式 ——
    host: str = BASE_URL
    mode: str = "incremental"
    sort: str = NEWEST_FIRST_SORT
    #: 是否在校验排序不变量；发现异常时改用全量扫描，避免静默漏数据。
    enforce_sort: bool = True

    # —— 认证 ——
    token: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None

    # —— 请求行为 ——
    max_pages: Optional[int] = None
    timeout: float = 30.0
    retries: int = 3
    retry_backoff: float = 1.5
    request_delay: float = 0.15
    consecutive_known_stop: int = DEFAULT_CONSECUTIVE_KNOWN_STOP
    proxy: str = ""
    verify_ssl: bool = True

    # —— 本地存储 ——
    data_dir: str = DEFAULT_DATA_DIR
    store_name: str = "picamanga_favorites.json"
    include_raw: bool = True

    # —— 日志 ——
    quiet: bool = False

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """去掉尾部斜杠的接口地址。"""
        return self.host.rstrip("/")

    @property
    def store_path(self) -> str:
        """状态库（JSON）的绝对/相对路径。"""
        return os.path.join(self.data_dir, self.store_name)

    @property
    def proxy_target(self) -> str:
        """交给 urllib 的代理地址；没写协议头的按 HTTP 代理对待（``127.0.0.1:10809`` 也能用）。

        空值返回空串，含义是「不在这里指定代理」——此时走 urllib 默认行为，
        认系统的 ``HTTP_PROXY`` / ``HTTPS_PROXY``，没设就是直连。
        """
        text = str(self.proxy or "").strip()
        if text and "://" not in text:
            text = "http://" + text
        return text

    # ------------------------------------------------------------------
    # 构造与校验
    # ------------------------------------------------------------------

    @classmethod
    def field_names(cls) -> List[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self, *, mask_secrets: bool = True) -> Dict[str, Any]:
        """转成普通 dict；``mask_secrets=True`` 时把令牌/口令替换成 ``***``。"""
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        if mask_secrets:
            for key in ("token", "password"):
                if data.get(key):
                    data[key] = "***"
        return data

    def validate(self) -> "PicamangaConfig":
        """校验取值，非法直接抛 :class:`~picamanga.errors.ConfigError`。"""
        if not self.host or not str(self.host).strip():
            raise ConfigError("host（接口地址）不能为空")
        if self.mode not in MODES:
            raise ConfigError("mode 只能是 %s，收到 %r" % (" / ".join(MODES), self.mode))
        if self.sort not in SORTS:
            raise ConfigError(
                "sort 只能是 %s，收到 %r" % (" / ".join(SORTS), self.sort)
            )
        if self.mode == "incremental" and self.sort != NEWEST_FIRST_SORT:
            raise ConfigError(
                "增量模式（incremental）只能用 sort=%r（按收藏日期新到旧），当前是 %r —— "
                "这个排序下新收藏不一定在头部，增量会漏数据。"
                "要么改回 %r，要么用 mode='full'。" % (NEWEST_FIRST_SORT, self.sort, NEWEST_FIRST_SORT)
            )
        if self.max_pages is not None and int(self.max_pages) < 1:
            raise ConfigError("max_pages 要么为空，要么 >= 1")
        if self.timeout < 1:
            raise ConfigError("timeout 必须 >= 1 秒")
        if self.retries < 1:
            raise ConfigError("retries 必须 >= 1")
        if self.consecutive_known_stop < 1:
            raise ConfigError("consecutive_known_stop 必须 >= 1")
        if self.token and not isinstance(self.token, str):
            raise ConfigError("token 必须是字符串")
        return self

    # ------------------------------------------------------------------
    # 合并
    # ------------------------------------------------------------------

    def merge(self, **overrides: Any) -> "PicamangaConfig":
        """返回一份覆盖了指定字段的新配置（``None`` 表示不覆盖）。"""
        clean = {k: v for k, v in overrides.items() if v is not None}
        unknown = [k for k in clean if k not in self.field_names()]
        if unknown:
            raise ConfigError("未知配置项：%s" % unknown)
        return replace(self, **clean)

    @classmethod
    def from_mapping(
        cls, mapping: Mapping[str, Any], *, base: Optional["PicamangaConfig"] = None
    ) -> "PicamangaConfig":
        cfg = base or cls()
        return cfg.merge(**dict(mapping))

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        base: Optional["PicamangaConfig"] = None,
    ) -> "PicamangaConfig":
        """从环境变量构造配置（只覆盖实际设置了值的变量）。"""
        env = env if env is not None else os.environ
        overrides: Dict[str, Any] = {}
        for env_key, field_name in ENV_MAP.items():
            value = env.get(env_key)
            if value:
                overrides[field_name] = value
        return (base or cls()).merge(**overrides)


def load_config(
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    use_env: bool = True,
) -> PicamangaConfig:
    """按「默认值 < 环境变量 < overrides」的顺序合成配置。

    :param overrides: 最高优先级覆盖项，通常是命令行参数。
    :param env: 环境变量映射，默认 ``os.environ``。
    :param use_env: 是否读取环境变量。
    :return: 校验通过的 :class:`PicamangaConfig`。
    """
    cfg = PicamangaConfig()
    if use_env:
        cfg = PicamangaConfig.from_env(env, base=cfg)
    if overrides:
        cfg = cfg.merge(**dict(overrides))
    # 数字型字段可能来自环境变量里的字符串；max_pages 的空值（""/0）表示「翻完为止」，
    # 所以这里用 replace 直接写，不走 merge（merge 会丢掉 None）。
    raw_max_pages = str(cfg.max_pages or "").strip()
    cfg = replace(
        cfg,
        timeout=float(cfg.timeout),
        retries=int(cfg.retries),
        request_delay=float(cfg.request_delay),
        consecutive_known_stop=int(cfg.consecutive_known_stop),
        max_pages=(None if raw_max_pages in ("", "0") else int(raw_max_pages)),
    )
    return cfg.validate()
