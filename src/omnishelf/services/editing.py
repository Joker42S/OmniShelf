"""把 Web 表单数据转成 item 更新。

放在 service 层是为了让路由保持「薄适配器」：路由只负责解析 HTTP、调用本模块、
然后重定向；所有字段解析、类型转换与校验规则都在这里。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from .. import repository as repo
from ..kinds import ExtraFieldError, coerce_value, get_kind
from ..models import Item


class ItemFormError(ValueError):
    """表单里的值不合法（year/rating/extra 类型不符等）。"""


def _clean(value: Any) -> str | None:
    """去空白；空字符串视作未填写。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_year(raw: Any) -> int | None:
    text = _clean(raw)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ItemFormError(f"year 不是整数: {text!r}") from exc


def _parse_rating(raw: Any) -> float | None:
    text = _clean(raw)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ItemFormError(f"rating 不是数字: {text!r}") from exc


def parse_extra(form: Mapping[str, Any], item: Item) -> dict[str, Any]:
    """按 kinds.py 的声明解析 ``extra.<name>`` 表单项。

    * 未声明的字段不会出现在表单里，但既有的未声明字段会原样保留。
    * 类型不符 -> ``ItemFormError``（整次提交失败，不写库）。
    """
    kind_def = get_kind(item.kind)
    extra: dict[str, Any] = {}
    for field_def in kind_def.extra_fields:
        raw = form.get(f"extra.{field_def.name}")
        if field_def.type == "bool":
            extra[field_def.name] = raw is not None
            continue
        text = _clean(raw)
        if text is None:
            continue
        if field_def.type == "list[str]":
            extra[field_def.name] = [part.strip() for part in text.split(",") if part.strip()]
            continue
        try:
            extra[field_def.name] = coerce_value(field_def, text)
        except ExtraFieldError as exc:
            raise ItemFormError(str(exc)) from exc

    for key, value in (item.extra or {}).items():
        if key not in extra and kind_def.field(key) is None:
            extra[key] = value
    return extra


def update_status_and_rating(
    session: Session,
    item: Item,
    *,
    status: Any = None,
    rating: Any = None,
) -> Item:
    """列表页行内编辑：只改 status / rating（留空即清空）。"""
    clean_status = _clean(status)
    raw_rating = _clean(rating)
    parsed_rating = None if raw_rating is None else _parse_rating(raw_rating)
    try:
        return repo.update_item(session, item.id, status=clean_status, rating=parsed_rating)
    except ValueError as exc:
        raise ItemFormError(str(exc)) from exc


def update_item_from_form(
    session: Session,
    item: Item,
    form: Mapping[str, Any],
    *,
    section: str | None = None,
) -> Item:
    """把一张「表单字段 -> 值」的映射应用到 item。

    详情页把「通用信息」和「专属字段」拆成了两个表单（井字布局），靠隐藏字段
    ``section`` 区分这次提交改哪一块：

    * ``core``  —— 只改通用字段（标题/副标题/创作者/年份/评分/状态/备注/标签）；
    * ``extra`` —— 只改专属字段（``extra``），通用字段一律不动；
    * 省略 / ``all`` —— 两块都改（保持旧行为，API 与测试用）。

    这样两个表单互不干扰：保存通用信息不会清空 extra，保存专属字段也不会清空标题。
    """
    chosen = (section or _clean(form.get("section")) or "all").strip().lower()
    if chosen not in {"all", "core", "extra"}:
        raise ItemFormError(f"未知的 section: {chosen!r}（可选 core / extra / all）")

    changes: dict[str, Any] = {}
    if chosen in {"all", "core"}:
        changes.update(
            {
                "subtitle": _clean(form.get("subtitle")),
                "creators": repo.clean_creators_from_form(form.get("creators")),
                "notes": _clean(form.get("notes")),
                "year": _parse_year(form.get("year")),
                "rating": _parse_rating(form.get("rating")),
                "status": _clean(form.get("status")),
            }
        )
        title = _clean(form.get("title"))
        if title:
            changes["title"] = title
    if chosen in {"all", "extra"}:
        changes["extra"] = parse_extra(form, item)

    try:
        updated = repo.update_item(session, item.id, **changes)
    except ValueError as exc:  # status / rating 等 repository 层校验
        raise ItemFormError(str(exc)) from exc

    raw_tags = form.get("tags")
    if raw_tags is not None:  # 专属字段表单里没有 tags，所以不会动标签
        repo.set_tags(session, updated.id, str(raw_tags).split(","))
    return updated
