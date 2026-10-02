"""列表筛选条件的解析、校验与 URL 生成。

Web 与 CLI 共用这一层，路由/命令里不写业务逻辑：

* ``parse_filters()`` 把「表单/命令行里的原始字符串」变成 ``ListFilters``：
  校验评分、日期、排序键，并把**本地日期**转成库里的 UTC ISO8601 边界
  （库里存的都是 UTC，直接字符串比大小即可）；
* ``ListFilters.repo_kwargs()`` 得到传给 ``repository.list_items/count_items`` 的参数；
* ``ListFilters.to_query()`` / ``sort_links()`` / ``active_filters()`` 负责生成
  分页、排序表头、可移除筛选标签用的 URL（保留其余筛选条件）。

三条筛选来源合并成同一个 ``ListFilters``：**基础筛选**（kind/status/tag/标题前缀）、
**搜索**（只搜标题）、**高级筛选**（评分区间/入库日期区间/创作者/tag 包含与排除）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

__all__ = [
    "FilterError",
    "ListFilters",
    "SORT_LABELS",
    "DEFAULT_SORT",
    "DEFAULT_DESCENDING",
    "parse_filters",
    "default_descending",
    "utc_bounds_for_local_date",
]


class FilterError(ValueError):
    """筛选条件不合法（评分不是数字、日期格式不对、排序键不存在…）。"""


#: 可排序字段 -> 表头文字。顺序就是列表里的显示顺序。
SORT_LABELS: dict[str, str] = {
    "title": "标题",
    "rating": "评分",
    "year": "年份",
    "created_at": "添加时间",
    "updated_at": "更新时间",
}
DEFAULT_SORT = "created_at"

#: 每个字段「第一次点击表头」时的方向：时间类默认新→旧，名称默认 A→Z，评分默认高→低
_DEFAULT_DESCENDING: dict[str, bool] = {
    "created_at": True,
    "updated_at": True,
    "title": False,
    "rating": True,
    "year": True,
}
DEFAULT_DESCENDING = _DEFAULT_DESCENDING[DEFAULT_SORT]

_TZ = datetime.now().astimezone().tzinfo


def _storage_iso(moment: datetime) -> str:
    """转成库里那种定长、可排序的 UTC 字符串（秒精度）。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def utc_bounds_for_local_date(text: str, *, end: bool = False) -> str:
    """把 ``YYYY-MM-DD``（也接受完整 ISO）解释成**本地时区**的那一天，返回 UTC 字符串。

    库里存 UTC，用户想的是本地日期；按本地 00:00:00 / 23:59:59 取边界，
    这样「今天」不会因为时差漏掉刚入库的条目。
    """
    raw = str(text).strip()
    if not raw:
        raise FilterError("日期不能为空")
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            moment = datetime.combine(day, time.max if end else time.min)
        else:
            parsed = datetime.fromisoformat(raw)
            moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=_TZ)
    except ValueError as exc:
        raise FilterError(f"日期格式不对: {raw!r}（要 YYYY-MM-DD）") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_TZ)
    return _storage_iso(moment)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_float(value: Any, *, label: str) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise FilterError(f"{label} 要是数字，收到 {text!r}") from exc


def _parse_bool(value: Any, *, default: bool) -> bool:
    text = _clean(value)
    if text is None:
        return default
    lowered = text.lower()
    if lowered in {"1", "true", "yes", "on", "desc", "descending", "↓"}:
        return True
    if lowered in {"0", "false", "no", "off", "asc", "ascending", "↑"}:
        return False
    raise FilterError(f"排序方向只能是 asc / desc，收到 {text!r}")


def _as_tuple(values: Iterable[Any] | str | None) -> tuple[str, ...]:
    """多值参数：去空、去重、保持顺序。

    两种写法都接受：重复参数（``?tags_include=a&tags_include=b``，页面链接用这种）
    和逗号分隔（``?tags_include=a,b``，标签筛选的文本框提交这种；中英文逗号都认）。
    """
    if values is None:
        return ()
    if isinstance(values, str):
        raw: Sequence[Any] = [values]
    else:
        raw = list(values)
    seen: dict[str, None] = {}
    for item in raw:
        text = _clean(item)
        if text is None:
            continue
        for part in _split_tag_text(text):
            seen.setdefault(part, None)
    return tuple(seen)


def _split_tag_text(text: str) -> list[str]:
    """逗号分隔的标签文本 -> 干净的标签列表（中英文逗号都算分隔符）。"""
    return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]


@dataclass(frozen=True)
class ListFilters:
    """一次列表查询的全部筛选条件（基础 + 搜索 + 高级）与排序。"""

    # 基础筛选
    kind: str | None = None
    status: str | None = None
    tag: str | None = None
    title_prefix: str | None = None
    # 搜索（只搜标题）
    search: str | None = None
    # 高级筛选
    creator: str | None = None
    rating_min: float | None = None
    rating_max: float | None = None
    created_from: str | None = None  # 用户输入的日期原文，用于回填表单
    created_to: str | None = None
    tags_include: tuple[str, ...] = ()
    tags_exclude: tuple[str, ...] = ()
    # 排序
    sort: str = DEFAULT_SORT
    descending: bool = DEFAULT_DESCENDING
    # 内部缓存（dataclass(frozen) 的 __post_init__ 计算）
    _created_from_utc: str | None = field(default=None, repr=False, compare=False)
    _created_to_utc: str | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ 查询
    def repo_kwargs(self) -> dict[str, Any]:
        """传给 ``repository.list_items/count_items`` 的过滤参数。"""
        return {
            "kind": self.kind,
            "status": self.status,
            "tag": self.tag,
            "title_prefix": self.title_prefix,
            "search": self.search,
            "creator": self.creator,
            "rating_min": self.rating_min,
            "rating_max": self.rating_max,
            "created_from": self._created_from_utc,
            "created_to": self._created_to_utc,
            "tags_include": self.tags_include,
            "tags_exclude": self.tags_exclude,
        }

    @property
    def has_basic(self) -> bool:
        return bool(self.kind or self.status or self.tag or self.title_prefix)

    @property
    def has_advanced(self) -> bool:
        return bool(
            self.creator
            or self.rating_min is not None
            or self.rating_max is not None
            or self.created_from
            or self.created_to
            or self.tags_include
            or self.tags_exclude
        )

    @property
    def is_filtered(self) -> bool:
        return self.has_basic or bool(self.search) or self.has_advanced

    # ------------------------------------------------------- 标签 / URL 生成
    def active_filters(self) -> list[dict[str, str]]:
        """当前生效的筛选（用于页面顶部的「可移除」标签）。"""
        chips: list[dict[str, str]] = []

        def add(label: str, clear: dict[str, Any]) -> None:
            chips.append({"label": label, "url": self.to_query(**clear)})

        if self.kind:
            add(f"类型 {self.kind}", {"kind": None})
        if self.status:
            add(f"状态 {self.status}", {"status": None})
        if self.tag:
            add(f"tag {self.tag}", {"tag": None})
        if self.title_prefix:
            add(f"标题前缀 {self.title_prefix}", {"title_prefix": None})
        if self.search:
            add(f"搜索 “{self.search}”", {"search": None})
        if self.creator:
            add(f"创作者含 {self.creator}", {"creator": None})
        if self.rating_min is not None:
            add(f"评分 ≥ {self.rating_min:g}", {"rating_min": None})
        if self.rating_max is not None:
            add(f"评分 ≤ {self.rating_max:g}", {"rating_max": None})
        if self.created_from:
            add(f"入库 ≥ {self.created_from}", {"created_from": None})
        if self.created_to:
            add(f"入库 ≤ {self.created_to}", {"created_to": None})
        for tag_name in self.tags_include:
            add(f"含 tag {tag_name}", {"tags_include": [t for t in self.tags_include if t != tag_name]})
        for tag_name in self.tags_exclude:
            add(
                f"排除 tag {tag_name}",
                {"tags_exclude": [t for t in self.tags_exclude if t != tag_name]},
            )
        return chips

    def to_query(self, **overrides: Any) -> str:
        """生成 ``/`` 上的查询串（保留其余条件）；没有条件时返回 ``/``。"""
        values: dict[str, Any] = {
            "kind": self.kind,
            "status": self.status,
            "tag": self.tag,
            "title_prefix": self.title_prefix,
            "search": self.search,
            "creator": self.creator,
            "rating_min": self.rating_min,
            "rating_max": self.rating_max,
            "created_from": self.created_from,
            "created_to": self.created_to,
            "tags_include": list(self.tags_include),
            "tags_exclude": list(self.tags_exclude),
            "sort": self.sort,
            "descending": self.descending,
        }
        values.update(overrides)
        values["sort"] = values.get("sort") or DEFAULT_SORT
        if values["sort"] not in SORT_LABELS:
            values["sort"] = DEFAULT_SORT

        params: list[tuple[str, str]] = []
        for key in ("kind", "status", "tag", "title_prefix", "search", "creator"):
            text = _clean(values.get(key))
            if text:
                params.append((key, text))
        for key in ("rating_min", "rating_max"):
            number = values.get(key)
            if number is not None and number != "":
                params.append((key, f"{float(number):g}"))
        for key in ("created_from", "created_to"):
            text = _clean(values.get(key))
            if text:
                params.append((key, text))
        for key in ("tags_include", "tags_exclude"):
            for item in _as_tuple(values.get(key)):
                params.append((key, item))
        page = values.get("page")
        if page:
            params.append(("page", str(int(page))))
        per_page = values.get("per_page")
        if per_page:
            params.append(("per_page", str(int(per_page))))
        sort = str(values["sort"])
        descending = bool(values["descending"])
        if sort != DEFAULT_SORT or descending != DEFAULT_DESCENDING:
            params.append(("sort", sort))
            params.append(("dir", "desc" if descending else "asc"))
        return "/?" + urlencode(params) if params else "/"

    def sort_links(self) -> list[dict[str, Any]]:
        """表头排序链接：当前列再点一次就反向，其它列用它们的默认方向。"""
        links: list[dict[str, Any]] = []
        for name, label in SORT_LABELS.items():
            active = name == self.sort
            descending = (not self.descending) if active else _DEFAULT_DESCENDING[name]
            links.append(
                {
                    "name": name,
                    "label": label,
                    "url": self.to_query(sort=name, descending=descending),
                    "active": active,
                    "descending": descending,
                    "indicator": ("↓" if self.descending else "↑") if active else "↕",
                }
            )
        return links


def default_descending(sort: str = DEFAULT_SORT) -> bool:
    """某个排序字段「第一次点击」时的方向。"""
    return _DEFAULT_DESCENDING.get(sort, DEFAULT_DESCENDING)


def parse_filters(
    *,
    kind: Any = None,
    status: Any = None,
    tag: Any = None,
    title_prefix: Any = None,
    search: Any = None,
    creator: Any = None,
    rating_min: Any = None,
    rating_max: Any = None,
    created_from: Any = None,
    created_to: Any = None,
    tags_include: Iterable[Any] | str | None = None,
    tags_exclude: Iterable[Any] | str | None = None,
    sort: Any = None,
    dir: Any = None,
    descending: Any = None,
) -> ListFilters:
    """把原始输入解析成 ``ListFilters``（不合法就抛 ``FilterError``）。

    ``dir`` / ``descending`` 二选一：Web 用 ``?dir=asc|desc``，CLI 用 ``--reverse``。
    """
    sort_key = _clean(sort) or DEFAULT_SORT
    if sort_key not in SORT_LABELS:
        allowed = " / ".join(SORT_LABELS)
        raise FilterError(f"排序字段只能是 {allowed}，收到 {sort_key!r}")

    direction = _DEFAULT_DESCENDING[sort_key]
    if dir is not None:
        direction = _parse_bool(dir, default=direction)
    elif descending is not None:
        direction = bool(descending)

    minimum = _parse_float(rating_min, label="评分下限")
    maximum = _parse_float(rating_max, label="评分上限")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise FilterError(f"评分区间反了：下限 {minimum:g} 大于上限 {maximum:g}")

    raw_from = _clean(created_from)
    raw_to = _clean(created_to)
    utc_from = utc_bounds_for_local_date(raw_from) if raw_from else None
    utc_to = utc_bounds_for_local_date(raw_to, end=True) if raw_to else None
    if utc_from and utc_to and utc_from > utc_to:
        raise FilterError(f"入库日期区间反了：{raw_from} 晚于 {raw_to}")

    return ListFilters(
        kind=_clean(kind),
        status=_clean(status),
        tag=_clean(tag),
        title_prefix=_clean(title_prefix),
        search=_clean(search),
        creator=_clean(creator),
        rating_min=minimum,
        rating_max=maximum,
        created_from=raw_from,
        created_to=raw_to,
        tags_include=_as_tuple(tags_include),
        tags_exclude=_as_tuple(tags_exclude),
        sort=sort_key,
        descending=direction,
        _created_from_utc=utc_from,
        _created_to_utc=utc_to,
    )


def with_creator(filters: ListFilters, creator: str) -> ListFilters:
    """点列表里的创作者名：只改创作者这一个条件（其余筛选保留）。"""
    return replace(filters, creator=creator)


def mapping_of_filters(filters: ListFilters) -> Mapping[str, Any]:
    """调试/日志用：把筛选条件摊平成字典。"""
    return {
        key: value
        for key, value in filters.repo_kwargs().items()
        if value not in (None, "", (), [])
    }
