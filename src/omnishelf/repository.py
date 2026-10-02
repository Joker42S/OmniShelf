"""纯 CRUD 层。

规则：
  * 这里只做「读写数据库」，不做去重/合并决策（那些在 ``services``）。
  * 时间戳（``created_at`` / ``updated_at`` / ``deleted_at``）只在本层维护。
  * ID 由本层生成（ULID），调用方不能自己造 id。
  * CLI 与 Web 只允许通过本层或 services 访问数据库，不允许自己写查询。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from .config import RATING_MAX, RATING_MIN
from .kinds import get_kind
from .models import (
    STATUSES,
    UNSET_STATUS,
    ExternalId,
    ImportLog,
    Item,
    PendingMerge,
    Tag,
)

#: 归一化时被丢弃的标点/空白（中英文常见符号）。Python 侧与 SQL 侧共用这一份定义。
_PUNCT_CHARS = " \t\r\n-_.,:;!?'\"()[]{}<>·、。，：；！？“”‘’（）《》【】/~\\|@#$%^&*+=`"
_PUNCT_RE = re.compile("[" + re.escape(_PUNCT_CHARS) + "]+")

#: 括号里的内容通常是水印/翻译组/版本标记（``[中国翻訳] [DL版]``、``(作者)``），
#: 跨来源比对标题时要先去掉——不同站点加的后缀不一样。
_BRACKET_RE = re.compile(r"[\[【（(][^\[\]【】（）()]*[\]】）)]")
#: 同一作品在不同站点可能只保留标题的一段：``日文 | 中文`` → 其中一段。
_TITLE_SPLIT_RE = re.compile(r"[|｜/／]|\s+-\s+")
#: 只有两边都不短于这个长度，才允许用「互相包含」判定，避免短标题互相乱命中。
MIN_CONTAINMENT_LEN = 6


class ItemNotFoundError(LookupError):
    """按 id 找不到 item。"""


class PendingMergeNotFoundError(LookupError):
    """按 id 找不到 pending merge。"""


def utc_now_iso() -> str:
    """当前 UTC 时间，ISO8601 秒精度（定长、可直接字符串排序）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    """生成一个新的 ULID 字符串。"""
    from ulid import ULID

    return str(ULID())


def normalize_title(title: str) -> str:
    """归一化标题：去掉空白与标点、大小写折叠。"""
    return _PUNCT_RE.sub("", title).casefold()


@lru_cache(maxsize=16384)
def title_keys(title: str) -> frozenset[str]:
    """把标题拆成用于**跨来源**比对的键集合。

    一次导入里同一条标题会被反复比较（每条新记录都要扫一遍既有条目），
    所以结果做 LRU 缓存：返回 ``frozenset``（不可变，调用方不会改坏缓存）。

    同一个作品在不同站点的标题常常不一样，例如实测样本里：

    * ``星屑のカルテ | 星尘病历 [中国翻訳] [DL版]``（A 站）
    * ``星尘病历``（B 站）

    所以除了「整条标题归一化」之外，还要：

    1. 去掉括号内容（翻译组、版本、水印）；
    2. 按 ``|`` / ``/`` 等分隔符切段（不同站点常只保留一段）；
    3. 每段各自归一化；空串丢弃。
    """
    keys = {normalize_title(title)}
    stripped = _BRACKET_RE.sub(" ", title)
    keys.update(normalize_title(part) for part in _TITLE_SPLIT_RE.split(stripped))
    return frozenset(key for key in keys if key)


def titles_match(left: str, right: str) -> bool:
    """判断两个标题是否可能指同一作品（只用于「进待复核」，不用于自动合并）。

    命中条件（满足其一）：
      * 任一 title key 全等；
      * 两边都有 ≥ ``MIN_CONTAINMENT_LEN`` 的 key，且其中一个包含另一个
        （应对「A 站标题 = B 站标题 + 后缀」这类情况）。
    """
    left_keys, right_keys = title_keys(left), title_keys(right)
    if left_keys & right_keys:
        return True
    for a in left_keys:
        for b in right_keys:
            if (
                len(a) >= MIN_CONTAINMENT_LEN
                and len(b) >= MIN_CONTAINMENT_LEN
                and (a in b or b in a)
            ):
                return True
    return False


def validate_status(status: str | None) -> str | None:
    """校验并归一化 status（去空白 + 小写），与 ItemDraft 的规则保持一致。"""
    if status is None:
        return None
    text = str(status).strip().lower()
    if text not in STATUSES:
        raise ValueError(f"非法 status: {status!r}（可选: {', '.join(STATUSES)}）")
    return text


def validate_rating(rating: float | None) -> float | None:
    if rating is None:
        return None
    value = float(rating)
    if not (RATING_MIN <= value <= RATING_MAX):
        raise ValueError(f"rating 必须在 {RATING_MIN}-{RATING_MAX} 之间，得到 {value}")
    return value


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- items


def _split_external_id(entry: Sequence[str | None]) -> tuple[str, str, str | None]:
    """接受 ``(source, id)`` 或 ``(source, id, url)``。"""
    if len(entry) == 2:
        source, external_id = entry
        return str(source), str(external_id), None
    if len(entry) == 3:
        source, external_id, url = entry
        return str(source), str(external_id), None if url is None else str(url)
    raise ValueError(f"external id 需要 2 或 3 个元素，得到 {entry!r}")


def create_item(
    session: Session,
    *,
    kind: str,
    title: str,
    subtitle: str | None = None,
    creators: Sequence[str] | None = None,
    year: int | None = None,
    status: str | None = None,
    rating: float | None = None,
    notes: str | None = None,
    cover_path: str | None = None,
    extra: dict[str, Any] | None = None,
    tags: Iterable[str] = (),
    external_ids: Iterable[Sequence[str | None]] = (),
    item_id: str | None = None,
    now: str | None = None,
) -> Item:
    """插入一条 item（含 tags / external_ids）。kind 必须是注册过的。"""
    get_kind(kind)
    timestamp = now or utc_now_iso()
    item = Item(
        id=item_id or new_id(),
        kind=kind,
        title=title,
        subtitle=subtitle,
        creators=clean_creators(creators),
        year=year,
        status=validate_status(status),
        rating=validate_rating(rating),
        notes=notes,
        cover_path=cover_path,
        extra=dict(extra or {}),
        created_at=timestamp,
        updated_at=timestamp,
        deleted_at=None,
    )
    session.add(item)
    session.flush()
    for entry in external_ids:
        source, external_id, url = _split_external_id(entry)
        add_external_id(session, item.id, source=source, external_id=external_id, url=url)
    for tag in tags:
        add_tag(session, item.id, tag)
    session.flush()
    return item


def get_item(session: Session, item_id: str, *, include_deleted: bool = False) -> Item | None:
    item = session.get(Item, item_id)
    if item is None:
        return None
    if item.deleted_at is not None and not include_deleted:
        return None
    return item


def require_item(session: Session, item_id: str, *, include_deleted: bool = False) -> Item:
    item = get_item(session, item_id, include_deleted=include_deleted)
    if item is None:
        raise ItemNotFoundError(f"找不到 item: {item_id}")
    return item


#: 创作者字符串里的分隔符：中英文逗号，以及历史上 importer 用来拼接多个创作者的 `` / ``。
#:
#: 两条保护规则（都是从真实数据里踩出来的，口径见 docs/development.md 的「字段口径」一节）：
#:   * **斜杠两侧必须都有空白** —— ``horn/wood``、``ミニチュアキマイラ1/24`` 是一个名字；
#:   * **括号里的分隔符不算** —— ``Current Storage (momi, ぴょん吉)``、
#:     ``STANKY (楓子、yozo)`` 里括号内是团体成员名单，属于名字本身。
CREATOR_SPLIT_RE = re.compile(r"\s*[,，]\s*|\s+/\s+")

#: 括号（成对出现时，里面的分隔符不拆）。中日文全角括号与方括号都认。
_BRACKETS = {"(": ")", "（": "）", "[": "]", "【": "】", "{": "}"}


def split_creators(text: str, pattern: re.Pattern[str] = CREATOR_SPLIT_RE) -> list[str]:
    """按 ``pattern`` 切开一个创作者字符串，**括号里的分隔符不动**。

    ``"Current Storage (momi, ぴょん吉)"`` -> 一个名字；
    ``"K8on,mh2en"`` -> 两个名字；``"A (x, y) / B"`` -> ``["A (x, y)", "B"]``。

    ``pattern`` 让别的模块能借用同一套「括号内不拆」的扫描（音乐标签的 ``A & B``、
    ``やなぎなぎ/麻枝准`` 就靠它，见 ``audiotags.split_artists``），而不用改全局默认规则。
    """
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    index = 0
    text = str(text)
    while index < len(text):
        char = text[index]
        if char in _BRACKETS:
            depth += 1
        elif char in _BRACKETS.values():
            depth = max(0, depth - 1)
        if depth == 0:
            match = pattern.match(text, index)
            if match and match.end() > index:
                parts.append("".join(buffer))
                buffer = []
                index = match.end()
                continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return [part.strip() for part in parts if part.strip()]


def clean_creators(value: Iterable[str] | str | None) -> list[str]:
    """创作者归一化：去空白、去重、保序；字符串按逗号 / `` / `` 切开（括号里不拆）。

    返回的永远是**列表**（空列表 = 没有创作者），落库时存成 JSON 数组。
    """
    if value is None:
        return []
    raw: list[str] = [value] if isinstance(value, str) else list(value)
    seen: dict[str, None] = {}
    for item in raw:
        for text in split_creators(str(item)):
            seen.setdefault(text, None)
    return list(seen)


def clean_creators_from_form(value: Any) -> list[str]:
    """表单/API 里的 creators（逗号分隔字符串或数组）→ 列表。"""
    if isinstance(value, (list, tuple)):
        return clean_creators(value)
    return clean_creators(str(value or ""))


def _item_filters(
    stmt: Any,
    *,
    kind: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    title_prefix: str | None = None,
    search: str | None = None,
    creator: str | None = None,
    rating_min: float | None = None,
    rating_max: float | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    tags_include: Sequence[str] = (),
    tags_exclude: Sequence[str] = (),
    include_deleted: bool = False,
) -> Any:
    """给 items 查询加过滤条件（列表与计数共用，保证两者口径一致）。

    这里只做 SQL，不做输入解析/校验（那是 ``services.filters`` 的事）：

    * ``search`` —— 标题**包含**（大小写不敏感，LIKE 通配符已转义）；
    * ``title_prefix`` —— 标题前缀（基础筛选里的那个）；
    * ``creator`` —— 创作者**包含**（点击列表里的创作者名就能筛）；
    * ``rating_min``/``rating_max`` —— 评分区间，未评分（NULL）天然不匹配任何区间；
    * ``created_from``/``created_to`` —— 入库时间区间（UTC ISO8601 字符串，可直接比大小）；
    * ``tags_include`` —— 必须**同时**带这些 tag（AND，每个一个 EXISTS）；
    * ``tags_exclude`` —— 必须不带这些 tag 中的任何一个（AND，每个一个 NOT EXISTS）。
    """
    if not include_deleted:
        stmt = stmt.where(col(Item.deleted_at).is_(None))
    if kind:
        stmt = stmt.where(Item.kind == kind)
    if status:
        stmt = stmt.where(Item.status == status)
    if tag:
        stmt = stmt.join(Tag, col(Tag.item_id) == col(Item.id)).where(Tag.tag == tag)
    if title_prefix:
        stmt = stmt.where(
            func.lower(col(Item.title)).like(f"{_escape_like(title_prefix.lower())}%", escape="\\")
        )
    if search:
        stmt = stmt.where(
            func.lower(col(Item.title)).like(
                f"%{_escape_like(search.lower())}%", escape="\\"
            )
        )
    if creator:
        # creators 是 JSON 数组：**任意一个**创作者匹配就算命中
        each = func.json_each(col(Item.creators)).table_valued("value").alias("creator_each")
        stmt = stmt.where(
            select(each.c.value)
            .where(func.lower(each.c.value).like(f"%{_escape_like(creator.lower())}%", escape="\\"))
            .exists()
        )
    if rating_min is not None:
        stmt = stmt.where(col(Item.rating) >= rating_min)
    if rating_max is not None:
        stmt = stmt.where(col(Item.rating) <= rating_max)
    if created_from:
        stmt = stmt.where(col(Item.created_at) >= created_from)
    if created_to:
        stmt = stmt.where(col(Item.created_at) <= created_to)
    for included in tags_include:
        if included:
            stmt = stmt.where(
                select(Tag.item_id)
                .where(col(Tag.item_id) == col(Item.id), Tag.tag == included)
                .exists()
            )
    for excluded in tags_exclude:
        if excluded:
            stmt = stmt.where(
                ~select(Tag.item_id)
                .where(col(Tag.item_id) == col(Item.id), Tag.tag == excluded)
                .exists()
            )
    return stmt


#: 列表可排序的字段 -> 排序表达式（title 用 lower() 让大小写不敏感）
SORT_EXPRESSIONS: dict[str, Any] = {
    "created_at": lambda: col(Item.created_at),
    "updated_at": lambda: col(Item.updated_at),
    "title": lambda: func.lower(col(Item.title)),
    "rating": lambda: col(Item.rating),
    "year": lambda: col(Item.year),
}


def order_by_clause(sort: str = "created_at", *, descending: bool = True) -> list[Any]:
    """排序表达式：NULL 永远排最后，再按 id 兜底保证顺序稳定。"""
    expression = SORT_EXPRESSIONS.get(sort, SORT_EXPRESSIONS["created_at"])()
    ordered = expression.desc() if descending else expression.asc()
    return [ordered.nulls_last(), col(Item.id)]


def list_items(
    session: Session,
    *,
    kind: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    title_prefix: str | None = None,
    search: str | None = None,
    creator: str | None = None,
    rating_min: float | None = None,
    rating_max: float | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    tags_include: Sequence[str] = (),
    tags_exclude: Sequence[str] = (),
    include_deleted: bool = False,
    sort: str = "created_at",
    descending: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> list[Item]:
    """列表查询（分页用 ``limit`` + ``offset``）。

    过滤条件见 ``_item_filters``；排序用 ``sort``（``SORT_EXPRESSIONS`` 里的键）+ ``descending``。
    """
    stmt = _item_filters(
        select(Item),
        kind=kind,
        status=status,
        tag=tag,
        title_prefix=title_prefix,
        search=search,
        creator=creator,
        rating_min=rating_min,
        rating_max=rating_max,
        created_from=created_from,
        created_to=created_to,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        include_deleted=include_deleted,
    )
    stmt = stmt.order_by(*order_by_clause(sort, descending=descending))
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


def count_items(
    session: Session,
    *,
    kind: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    title_prefix: str | None = None,
    search: str | None = None,
    creator: str | None = None,
    rating_min: float | None = None,
    rating_max: float | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    tags_include: Sequence[str] = (),
    tags_exclude: Sequence[str] = (),
    include_deleted: bool = False,
) -> int:
    """按同样的过滤条件数总数（给分页用）。"""
    stmt = _item_filters(
        select(func.count()).select_from(Item),
        kind=kind,
        status=status,
        tag=tag,
        title_prefix=title_prefix,
        search=search,
        creator=creator,
        rating_min=rating_min,
        rating_max=rating_max,
        created_from=created_from,
        created_to=created_to,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        include_deleted=include_deleted,
    )
    return int(session.exec(stmt).one())


UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "title",
        "subtitle",
        "creators",
        "year",
        "status",
        "rating",
        "notes",
        "cover_path",
        "extra",
    }
)


def update_item(
    session: Session,
    item_id: str,
    *,
    now: str | None = None,
    **changes: Any,
) -> Item:
    """更新给定字段并刷新 ``updated_at``。未知字段直接报错。

    **值都没变时直接返回，不写库也不推进 updated_at**：界面上「原样再保存一次」
    不该产生新版本（data 仓库里也就不会多出无意义的改动）。
    """
    unknown = set(changes) - UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"不可更新的字段: {', '.join(sorted(unknown))}")
    item = require_item(session, item_id)
    if "kind" in changes:
        get_kind(changes["kind"])
    if "status" in changes:
        changes["status"] = validate_status(changes["status"])
    if "rating" in changes:
        changes["rating"] = validate_rating(changes["rating"])
    if "creators" in changes:
        changes["creators"] = clean_creators(changes["creators"])
    if "extra" in changes:
        changes["extra"] = dict(changes["extra"] or {})
    effective = {key: value for key, value in changes.items() if getattr(item, key, None) != value}
    if not effective:
        return item
    for key, value in effective.items():
        setattr(item, key, value)
    item.updated_at = now or utc_now_iso()
    session.add(item)
    session.flush()
    return item


def soft_delete_item(session: Session, item_id: str, *, now: str | None = None) -> Item:
    """软删除：只写 ``deleted_at``，行仍在库里。"""
    item = require_item(session, item_id)
    timestamp = now or utc_now_iso()
    item.deleted_at = timestamp
    item.updated_at = timestamp
    session.add(item)
    session.flush()
    return item


def restore_item(session: Session, item_id: str, *, now: str | None = None) -> Item:
    item = require_item(session, item_id, include_deleted=True)
    item.deleted_at = None
    item.updated_at = now or utc_now_iso()
    session.add(item)
    session.flush()
    return item


# ------------------------------------------------------------------- external ids


def add_external_id(
    session: Session,
    item_id: str,
    *,
    source: str,
    external_id: str,
    url: str | None = None,
) -> bool:
    """写入 ``(source, external_id)``。返回 True 表示新建，False 表示已存在。"""
    existing = session.get(ExternalId, (source, external_id))
    if existing is None:
        session.add(ExternalId(source=source, external_id=external_id, item_id=item_id, url=url))
        session.flush()
        return True
    if existing.item_id != item_id or (url and existing.url != url):
        existing.item_id = item_id
        if url:
            existing.url = url
        session.add(existing)
        session.flush()
    return False


def remove_external_id(session: Session, source: str, external_id: str) -> None:
    existing = session.get(ExternalId, (source, external_id))
    if existing is not None:
        session.delete(existing)
        session.flush()


def list_external_ids(session: Session, item_id: str) -> list[ExternalId]:
    stmt = (
        select(ExternalId)
        .where(ExternalId.item_id == item_id)
        .order_by(col(ExternalId.source), col(ExternalId.external_id))
    )
    return list(session.exec(stmt).all())


def get_item_by_external_id(session: Session, source: str, external_id: str) -> Item | None:
    link = session.get(ExternalId, (source, external_id))
    if link is None:
        return None
    return get_item(session, link.item_id)


# ------------------------------------------------------------------------- 匹配


def sources_by_item(
    session: Session,
    item_ids: Iterable[str] | None = None,
) -> dict[str, set[str]]:
    """``item_id -> 该条目出现过的来源集合``（来自 ``external_ids``）。

    用于「先看是不是本源的，再看是不是别的源的」这套唯一性检查。
    没有 external_ids 的条目不会出现在结果里（= 来源未知）。
    """
    stmt = select(ExternalId.item_id, ExternalId.source)
    if item_ids is not None:
        ids = list(item_ids)
        if not ids:
            return {}
        stmt = stmt.where(col(ExternalId.item_id).in_(ids))
    result: dict[str, set[str]] = {}
    for item_id, source in session.exec(stmt).all():
        result.setdefault(str(item_id), set()).add(str(source))
    return result


def _title_match_stmt(
    *,
    kind: str,
    record_sources: Collection[str],
    cross_source: bool,
) -> Any:
    """「可以用标题比对的候选条目」查询。

    这是唯一性检查的核心口径（用户明确的规则）：

    * **同源**（候选条目已经挂了本记录来源的标识）——**标题不参与判重**：
      来源既然给了唯一标识，标识就是身份；同源两条记录即使标题相似，
      只要唯一标识不同就是不同作品。所以同源候选一律排除。
    * **非同源**（别的来源、或来源未知）——才用标题比对。
    * ``cross_source=False``（漫画这类不跨源查重的 kind）——连别的来源也不比，
      只留「来源未知」（没挂任何 external_id，例如手工录入）的条目。

    排除条件用 SQL 子查询下发，避免每条记录都把全表来源拉进 Python（平方级开销）。
    """
    stmt = select(Item).where(Item.kind == kind, col(Item.deleted_at).is_(None))
    sources = [s for s in record_sources if s]
    if sources:
        same_source = select(ExternalId.item_id).where(col(ExternalId.source).in_(sources))
        stmt = stmt.where(col(Item.id).not_in(same_source))
        if not cross_source:
            any_source = select(ExternalId.item_id)
            stmt = stmt.where(col(Item.id).not_in(any_source))
    return stmt.order_by(col(Item.created_at))


def find_by_title_year(
    session: Session,
    *,
    kind: str,
    title: str,
    year: int | None,
    record_sources: Collection[str] = (),
    cross_source: bool = True,
) -> Item | None:
    """唯一性检查（精确）：``kind + title + year`` 匹配（year 为 NULL 时同样匹配 NULL）。

    候选范围见 ``_title_match_stmt``：同源条目不会参与（同源只认唯一标识）。
    """
    stmt = _title_match_stmt(
        kind=kind, record_sources=record_sources, cross_source=cross_source
    ).where(Item.title == title)
    stmt = stmt.where(col(Item.year).is_(None) if year is None else Item.year == year)
    return session.exec(stmt).first()


def find_fuzzy_title_matches(
    session: Session,
    *,
    kind: str,
    title: str,
    exclude_item_id: str | None = None,
    limit: int = 20,
    record_sources: Collection[str] = (),
    cross_source: bool = True,
) -> list[Item]:
    """唯一性检查（模糊）：``kind + 标题`` 匹配（可能多个）——结果只进待复核。

    匹配用 ``titles_match``：段级全等，或（都够长时）互相包含——目的是让
    **不同来源**的同一作品能落到待复核里（见 ``title_keys`` 的说明）。
    这里只负责找候选，合并永远由人工在 review 里决定。

    实现为「取同 kind 的未删除 item 后在 Python 侧比较」。单用户规模下足够快，
    且保证与 ``normalize_title`` 完全一致（SQL 侧做同样的替换链容易与 Python
    的归一分词规则漂移）。
    """
    if not title_keys(title):
        return []
    stmt = _title_match_stmt(
        kind=kind, record_sources=record_sources, cross_source=cross_source
    )
    matches: list[Item] = []
    for item in session.exec(stmt).all():
        if exclude_item_id is not None and item.id == exclude_item_id:
            continue
        if titles_match(item.title, title):
            matches.append(item)
            if len(matches) >= limit:
                break
    return matches


# -------------------------------------------------------------------------- 内容判重


#: 比较键里连接列表元素的分隔符（不会出现在正常文本里，所以拼接不会产生新的相等关系）。
_IDENTITY_JOIN = "\x1f"


def identity_part(value: Any) -> str:
    """一个字段值 -> 比较用的归一串。

    列表（``list[str]`` 那类 extra 字段，例如音乐的 artists）**先归一每个元素、再去重、
    再排序**：``["A", "B"]`` 和 ``["B", "A"]`` 说的是同一组人，重复的名字也不算多一个人。
    空值（``None`` / 空串 / 空列表）统一归成空串，「都没写」= 一样。
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = {normalize_title(str(entry)) for entry in value if str(entry).strip()}
        return _IDENTITY_JOIN.join(sorted(parts))
    if value is None:
        return ""
    return normalize_title(str(value))


def identity_key(kind: str, title: str, extra: Mapping[str, Any] | None) -> tuple[str, ...]:
    """内容判重的比较键：归一化标题打头，后面依次是 ``KindDef.identity_fields`` 的那些字段。

    标题总是判重条件之一（不用声明），所以键的第一段永远是 ``normalize_title(title)``。
    两个键相等就是「同一个作品」。
    """
    kind_def = get_kind(kind)
    values = extra or {}
    return tuple(
        [identity_part(title)] + [identity_part(values.get(name)) for name in kind_def.identity_fields]
    )


def identity_candidates(session: Session, *, kind: str) -> list[Item]:
    """内容判重可用的全部候选：该 kind 下所有未删除条目（按入库顺序）。

    和 ``_title_match_stmt`` 不同，这里**不按来源排除**：内容判重是给「同一首歌被拷了
    好几份、每份都没有 ID」用的，而这些副本本来就来自同一个来源——按来源排除就等于把
    它要解决的问题原样排除掉了。（ ``(source, external_id)`` 那条规则仍在它之前跑，
    来源给了标识的条目永远先按标识认，两者不冲突。）
    """
    stmt = (
        select(Item)
        .where(Item.kind == kind, col(Item.deleted_at).is_(None))
        .order_by(col(Item.created_at))
    )
    return list(session.exec(stmt).all())


# -------------------------------------------------------------------------- tags


def list_tags(session: Session, item_id: str) -> list[str]:
    stmt = select(Tag).where(Tag.item_id == item_id).order_by(col(Tag.tag))
    return [row.tag for row in session.exec(stmt).all()]


def add_tag(session: Session, item_id: str, tag: str) -> bool:
    clean = tag.strip()
    if not clean:
        return False
    if session.get(Tag, (item_id, clean)) is not None:
        return False
    session.add(Tag(item_id=item_id, tag=clean))
    session.flush()
    return True


def remove_tag(session: Session, item_id: str, tag: str) -> bool:
    row = session.get(Tag, (item_id, tag.strip()))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def set_tags(session: Session, item_id: str, tags: Iterable[str]) -> list[str]:
    """整体覆盖 tags；调用方负责先做归一化（去重、去空）。

    标签集合没变时什么都不做（不推进 ``updated_at``）。
    """
    wanted = {t.strip() for t in tags if t and t.strip()}
    current = set(list_tags(session, item_id))
    if wanted == current:
        return sorted(wanted)
    for extra in sorted(current - wanted):
        remove_tag(session, item_id, extra)
    for missing in sorted(wanted - current):
        add_tag(session, item_id, missing)
    return sorted(wanted)


def all_tags(session: Session, *, include_deleted: bool = False) -> list[str]:
    stmt = select(Tag.tag).distinct().order_by(col(Tag.tag))
    if include_deleted:
        return list(session.exec(stmt).all())
    stmt = stmt.join(Item, col(Tag.item_id) == col(Item.id)).where(col(Item.deleted_at).is_(None))
    return list(session.exec(stmt).all())


# ------------------------------------------------------------------ import_logs


def create_import_log(session: Session, *, source: str, started_at: str | None = None) -> ImportLog:
    log = ImportLog(
        source=source,
        started_at=started_at or utc_now_iso(),
        errors=[],
    )
    session.add(log)
    session.flush()
    return log


def finish_import_log(
    session: Session,
    log_id: int,
    *,
    total: int,
    inserted: int,
    updated: int,
    skipped: int,
    errors: Sequence[dict[str, Any]],
    finished_at: str | None = None,
) -> ImportLog:
    log = session.get(ImportLog, log_id)
    if log is None:
        raise LookupError(f"找不到 import_log: {log_id}")
    log.finished_at = finished_at or utc_now_iso()
    log.total = total
    log.inserted = inserted
    log.updated = updated
    log.skipped = skipped
    log.errors = list(errors)
    session.add(log)
    session.flush()
    return log


def get_import_log(session: Session, log_id: int) -> ImportLog | None:
    return session.get(ImportLog, log_id)


def list_import_logs(session: Session, *, limit: int = 20) -> list[ImportLog]:
    stmt = select(ImportLog).order_by(col(ImportLog.id).desc()).limit(limit)
    return list(session.exec(stmt).all())


# --------------------------------------------------------------- pending_merges


def add_pending_merge(
    session: Session,
    *,
    item_id: str,
    candidate_id: str,
    reason: str,
    now: str | None = None,
) -> PendingMerge:
    """写入一条待处理合并；同一 (item, candidate) 不重复插入。"""
    stmt = select(PendingMerge).where(
        PendingMerge.item_id == item_id, PendingMerge.candidate_id == candidate_id
    )
    existing = session.exec(stmt).first()
    if existing is not None:
        return existing
    row = PendingMerge(
        item_id=item_id,
        candidate_id=candidate_id,
        reason=reason,
        created_at=now or utc_now_iso(),
    )
    session.add(row)
    session.flush()
    return row


def list_pending_merges(session: Session, *, limit: int | None = None) -> list[PendingMerge]:
    stmt = select(PendingMerge).order_by(col(PendingMerge.id))
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


def get_pending_merge(session: Session, pending_id: int) -> PendingMerge | None:
    return session.get(PendingMerge, pending_id)


def require_pending_merge(session: Session, pending_id: int) -> PendingMerge:
    row = get_pending_merge(session, pending_id)
    if row is None:
        raise PendingMergeNotFoundError(f"找不到 pending merge: {pending_id}")
    return row


def delete_pending_merge(session: Session, pending_id: int) -> None:
    row = session.get(PendingMerge, pending_id)
    if row is not None:
        session.delete(row)
        session.flush()


def count_pending_merges(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(PendingMerge)).one())


# ------------------------------------------------------------------------- 统计


def stats(session: Session) -> dict[str, Any]:
    """列表页/CLI/demo 共用的汇总统计。"""
    by_kind_stmt = (
        select(Item.kind, func.count())
        .where(col(Item.deleted_at).is_(None))
        .group_by(Item.kind)
        .order_by(Item.kind)
    )
    by_status_stmt = (
        select(Item.status, func.count())
        .where(col(Item.deleted_at).is_(None))
        .group_by(Item.status)
        .order_by(Item.status)
    )
    has_cover_stmt = (
        select(func.count())
        .select_from(Item)
        .where(col(Item.deleted_at).is_(None), col(Item.cover_path).is_not(None))
    )
    return {
        "total": count_items(session),
        "deleted": count_items(session, include_deleted=True) - count_items(session),
        "by_kind": {str(k): int(v) for k, v in session.exec(by_kind_stmt).all()},
        "by_status": {
            (str(k) if k is not None else UNSET_STATUS): int(v)
            for k, v in session.exec(by_status_stmt).all()
        },
        "with_cover": int(session.exec(has_cover_stmt).one()),
        "tags": len(all_tags(session)),
        "pending_merges": count_pending_merges(session),
    }
