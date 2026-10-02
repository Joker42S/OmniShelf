"""配置对象与配置加载。

优先级（高 → 低）：命令行参数 > 环境变量 > 配置文件 > 内置默认值。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import ConfigError

DEFAULT_HOST = "https://copy4000.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: 同步模式
#:   - ``incremental`` 增量：只往头部翻页，遇到全是旧条目即停，快
#:   - ``full``        全量：遍历所有页，重建数据集并标记已取消收藏的条目
MODES: Tuple[str, ...] = ("incremental", "full")

#: 输出格式
FORMATS: Tuple[str, ...] = ("json", "csv", "md", "xlsx")

#: 站点支持的排序方式（对应页面右上角下拉框的三个选项）
#:
#:   ``-datetime_modifier`` 按「加到書架時間」降序 —— 前端 label 就叫这个名字，
#:                          也是增量同步唯一安全的排序：新收藏必然落在头部。
#:   ``-datetime_updated``  按作品更新时间降序（与书架顺序无关，不能用于增量）
#:   ``-datetime_browse``   按阅读时间排序（同上）
#:
#: .. warning::
#:    实测服务端会**静默忽略**非法的 ordering 值并退回默认排序，不会报错。
#:    所以这里做白名单校验，避免写错参数后悄悄降级。
ORDERINGS: Tuple[str, ...] = ("-datetime_modifier", "-datetime_updated", "-datetime_browse")

#: 唯一能保证「新收藏一定在头部」的排序，增量同步必须用它
SHELF_ORDERING = "-datetime_modifier"

#: 站点支持的收藏类型 -> 接口路径
ENDPOINTS: Dict[str, str] = {
    "comics": "/api/v3/member/collect/comics",
    "cartoons": "/api/v3/member/collect/cartoons",
}

KIND_LABEL = {"comics": "漫画", "cartoons": "動畫"}

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)

DEFAULT_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config.json")
DEFAULT_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

#: 环境变量名 -> ExportConfig 字段名
ENV_MAP = {
    "COPYMANGA_HOST": "host",
    "COPYMANGA_MODE": "mode",
    "COPYMANGA_ORDERING": "ordering",
    "COPYMANGA_TOKEN": "token",
    "COPYMANGA_COOKIE": "cookie",
    "COPYMANGA_USERNAME": "username",
    "COPYMANGA_PASSWORD": "password",
    "COPYMANGA_DATA_DIR": "data_dir",
    "COPYMANGA_PAGE_SIZE": "page_size",
    "COPYMANGA_TIMEOUT": "timeout",
}


def _as_tuple(value: Any, cast=str) -> Tuple:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("，", ",").split(",")]
        return tuple(cast(p) for p in parts if p)
    if isinstance(value, Iterable):
        return tuple(cast(v) for v in value)
    return (cast(value),)


@dataclass
class ExportConfig:
    """一次同步/导出任务的全部可调参数。

    所有字段都有默认值，通常只需要填 ``token``（或 ``username``/``password``）。

    :param host: 网站域名，默认 ``https://copy4000.com``。
        接口路径挂在同一域名下，换镜像站时改这里即可。
    :param mode: ``"incremental"``（增量，默认）或 ``"full"``（全量）。
    :param token: 登录 token，来自浏览器 Cookie 的 ``token`` 字段。
    :param cookie: 整条 Cookie 字符串，脚本会自动取出 ``token``。
    :param username: 用户名，无 token 时用于换 token。
    :param password: 密码，无 token 时用于换 token。
    :param page_size: 每页条数，默认 100（接口实测可到 500）。
    :param max_pages: 安全阀，单次每种类型最多翻多少页，``None`` 不限。
    :param timeout: 单次请求超时秒数。
    :param retries: 失败重试次数。
    :param retry_backoff: 重试退避基数，第 n 次等待 ``retry_backoff * n`` 秒。
    :param request_delay: 翻页之间的间隔秒数，避免请求过快。
    :param user_agent: 请求 UA。
    :param verify_ssl: 是否校验 TLS 证书。
    :param data_dir: 本地数据目录，存放状态库与导出文件。
    :param store_name: 状态库文件名。
    :param formats: 导出格式，默认仅 ``("json",)``。
    :param snapshot: 是否把本次结果再另存一份带时间戳的快照。
    :param include_raw: 是否在 JSON 里保留接口原始返回。
    :param kinds: 要抓的收藏类型，``("comics", "cartoons")``。
    :param free_types: 接口的 free_type 分组，``(1, 2)``。
    :param quiet: 静默模式，不打印进度。
    """

    # —— 站点与同步模式 ——
    host: str = DEFAULT_HOST
    mode: str = "incremental"

    #: 收藏列表排序方式，见 :data:`ORDERINGS`。
    #: 增量同步依赖「新收藏在头部」，因此只能用 ``-datetime_modifier``（默认）。
    ordering: str = SHELF_ORDERING
    #: 是否在同步过程中校验排序不变量；校验失败会自动降级为全量扫描，避免漏数据。
    enforce_ordering: bool = True

    # —— 认证 ——
    token: Optional[str] = None
    cookie: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    #: token 自动续期后，是否把新 token 写回配置文件（``config_path``）。
    #: 实测站点会在重新登录后吊销旧 token，把这个设为 ``True`` 可以避免每次运行都重新登录。
    persist_token: bool = True
    #: 配置文件的来源路径，由 :func:`load_config` 自动填入；手工构造时可不填。
    config_path: Optional[str] = None

    # —— 请求行为 ——
    page_size: int = 100
    max_pages: Optional[int] = None
    timeout: float = 40.0
    retries: int = 3
    retry_backoff: float = 1.5
    request_delay: float = 0.3
    user_agent: str = DEFAULT_UA
    verify_ssl: bool = True

    # —— 本地存储与输出 ——
    data_dir: str = DEFAULT_DATA_DIR
    store_name: str = "copymanga_favorites.json"
    formats: Tuple[str, ...] = ("json",)
    snapshot: bool = True
    include_raw: bool = True

    # —— 抓取范围 ——
    kinds: Tuple[str, ...] = ("comics", "cartoons")
    free_types: Tuple[int, ...] = (1, 2)

    # —— 日志 ——
    quiet: bool = False

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """去掉尾部斜杠的站点地址。"""
        return self.host.rstrip("/")

    @property
    def store_path(self) -> str:
        """状态库（JSON）的绝对/相对路径。"""
        return os.path.join(self.data_dir, self.store_name)

    @property
    def export_dir(self) -> str:
        """带时间戳的导出文件目录。"""
        return os.path.join(self.data_dir, "exports")

    @property
    def snapshot_dir(self) -> str:
        """快照目录。"""
        return os.path.join(self.data_dir, "snapshots")

    # ------------------------------------------------------------------
    # 构造与校验
    # ------------------------------------------------------------------

    @classmethod
    def field_names(cls) -> List[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self, *, mask_secrets: bool = True) -> Dict[str, Any]:
        """转成普通 dict；``mask_secrets=True`` 时把密码等替换成 ``***``。"""
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        if mask_secrets:
            for key in ("token", "password", "cookie"):
                if data.get(key):
                    data[key] = "***"
        return data

    def validate(self) -> "ExportConfig":
        """校验取值，非法直接抛 :class:`~copymanga.errors.ConfigError`。"""
        if not self.host or not self.host.strip():
            raise ConfigError("host（网站域名）不能为空")
        if self.mode not in MODES:
            raise ConfigError("mode 只能是 %s，收到 %r" % (" / ".join(MODES), self.mode))
        if self.ordering not in ORDERINGS:
            raise ConfigError(
                "ordering 只能是 %s，收到 %r。"
                "注意：服务端会静默忽略非法值并退回默认排序，所以这里直接拦下。"
                % (" / ".join(ORDERINGS), self.ordering)
            )
        if self.mode == "incremental" and self.ordering != SHELF_ORDERING:
            raise ConfigError(
                "增量模式（incremental）只能用 ordering=%r（按加到書架時間降序）。"
                "当前是 %r —— 这个排序下新收藏不一定在头部，增量会漏数据。"
                "要么改回 %r，要么用 mode='full'。"
                % (SHELF_ORDERING, self.ordering, SHELF_ORDERING)
            )
        bad_kinds = [k for k in self.kinds if k not in ENDPOINTS]
        if bad_kinds:
            raise ConfigError("kinds 不支持 %s，可选 %s" % (bad_kinds, list(ENDPOINTS)))
        bad_formats = [f for f in self.formats if f not in FORMATS]
        if bad_formats:
            raise ConfigError("formats 不支持 %s，可选 %s" % (bad_formats, list(FORMATS)))
        if self.page_size < 1:
            raise ConfigError("page_size 必须 >= 1")
        if self.token and not isinstance(self.token, str):
            raise ConfigError("token 必须是字符串")
        return self

    # ------------------------------------------------------------------
    # 合并
    # ------------------------------------------------------------------

    def merge(self, **overrides: Any) -> "ExportConfig":
        """返回一份覆盖了指定字段的新配置（``None`` 表示不覆盖）。"""
        clean = {k: v for k, v in overrides.items() if v is not None}
        unknown = [k for k in clean if k not in self.field_names()]
        if unknown:
            raise ConfigError("未知配置项：%s" % unknown)
        for key in ("kinds", "free_types", "formats"):
            if key in clean:
                cast = int if key == "free_types" else str
                clean[key] = _as_tuple(clean[key], cast)
        return replace(self, **clean)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any], *, base: Optional["ExportConfig"] = None) -> "ExportConfig":
        """从 dict/JSON 结构的映射构造配置。"""
        cfg = base or cls()
        return cfg.merge(**dict(mapping))

    @classmethod
    def from_file(cls, path: str, *, base: Optional["ExportConfig"] = None) -> "ExportConfig":
        """从 JSON 配置文件构造配置；文件不存在时返回 ``base`` 或默认值。"""
        if not path or not os.path.isfile(path):
            return base or cls()
        with open(path, encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ConfigError("配置文件 %s 不是合法 JSON：%s" % (path, exc)) from exc
        if not isinstance(data, dict):
            raise ConfigError("配置文件 %s 顶层必须是对象" % path)
        return cls.from_mapping(data, base=base)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *, base: Optional["ExportConfig"] = None) -> "ExportConfig":
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
    path: Optional[str] = None,
    overrides: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    use_env: bool = True,
) -> ExportConfig:
    """按「默认值 < 配置文件 < 环境变量 < overrides」的顺序合成配置。

    :param path: 配置文件路径，默认 ``<项目根>/config.json``。
    :param overrides: 最高优先级覆盖项，通常是命令行参数。
    :param env: 环境变量映射，默认 ``os.environ``。
    :param use_env: 是否读取环境变量。
    :return: 校验通过的 :class:`ExportConfig`。
    """
    path = path if path is not None else DEFAULT_CONFIG_PATH
    cfg = ExportConfig.from_file(path)
    if use_env:
        cfg = ExportConfig.from_env(env, base=cfg)
    if overrides:
        cfg = cfg.merge(**dict(overrides))
    # 数字型字段可能来自 env / 配置文件里的字符串
    cfg = cfg.merge(
        page_size=int(cfg.page_size),
        timeout=float(cfg.timeout),
        retries=int(cfg.retries),
        request_delay=float(cfg.request_delay),
        retry_backoff=float(cfg.retry_backoff),
        free_types=tuple(int(v) for v in cfg.free_types),
        config_path=(path if path and os.path.isfile(path) else cfg.config_path),
    )
    return cfg.validate()


def save_token_to_config(path: Optional[str], token: str) -> bool:
    """把续期后的 token 写回 JSON 配置文件。

    只改 ``token`` 一个字段，其余内容与缩进原样保留；文件不存在或不是合法 JSON
    时静默返回 ``False``（写不进去不该让整个同步失败）。

    :return: 是否真的写入了。
    """
    if not path or not token or not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("token") == token:
        return False

    data["token"] = token
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError:
        return False
    return True
