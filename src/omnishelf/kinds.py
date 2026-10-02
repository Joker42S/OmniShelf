"""kind 注册表。

新增一个 kind 或一个新的 extra 字段，只改这个文件即可；
models / repository / CLI / Web 都不硬编码 kind 清单。

``extra`` 字段在 DB 层是自由 JSON，校验规则（类型、必填）全部来自这里。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: 允许的 extra 字段类型。
EXTRA_TYPES: tuple[str, ...] = ("str", "int", "float", "bool", "list[str]")


@dataclass(frozen=True, slots=True)
class FieldDef:
    """``extra`` JSON 中的一个字段。"""

    name: str
    label: str
    type: str
    required: bool = False

    def __post_init__(self) -> None:
        if self.type not in EXTRA_TYPES:
            raise ValueError(f"未知 extra 类型 {self.type!r}（字段 {self.name}），可选: {EXTRA_TYPES}")


@dataclass(frozen=True, slots=True)
class KindDef:
    """一个 kind 的定义。

    ``extra_fields`` 只放**该类型特有**的字段；通用字段（标题、创作者、年份、评分…）
    在 ``items`` 表里有独立的列，不在这里声明。
    

    ``cross_source_duplicates`` 决定导入时**要不要跨来源查重**：

    * ``True``（默认）——同一个条目可能同时存在于多个来源。导入时先按
      ``(source, external_id)`` 认本源，再用标题等信息检查「是不是别的来源的同一条目」，
      命中就进 ``pending_merges`` 等人工确认。
    * ``False``——**只检查同一来源内部的重复**（漫画就是这种：不同来源之间不考虑重复）。
      这样既不会有跨源误报，也仍然能发现「本源内部标题被改过」这类情况。

    ``identity_fields`` 声明这个 kind 的**内容判重**字段（都是 ``extra`` 里的字段名）：
    标题 + 这几个字段整体相同，就算同一个作品。音乐是这种情况——本地文件没有稳定 ID
    （同一首歌可以被拷成任意个文件），而「模糊标题匹配」对它是负担：同一首歌收在
    不同专辑里本来就是两条正常数据，走模糊匹配会把待复核刷屏。
    声明了它的 kind 在导入时用内容判重**取代** ``kind+title+year`` 精确匹配和标题模糊匹配
    （ ``(source, external_id)`` 那条规则照旧优先）。空元组 = 沿用通用规则。
    """

    name: str
    label: str
    extra_fields: tuple[FieldDef, ...] = field(default_factory=tuple)
    cross_source_duplicates: bool = True
    identity_fields: tuple[str, ...] = ()

    def field(self, name: str) -> FieldDef | None:
        for candidate in self.extra_fields:
            if candidate.name == name:
                return candidate
        return None


class UnknownKindError(KeyError):
    """kind 未在注册表中。"""


class ExtraFieldError(ValueError):
    """extra 字段类型不符、必填缺失（未知字段只警告，不报错）。"""


KINDS: dict[str, KindDef] = {
    "book": KindDef(
        "book",
        "图书",
        (
            FieldDef("publisher", "出版社", "str"),
            FieldDef("pub", "豆瓣出版信息串", "str"),
            FieldDef("isbn", "ISBN", "str"),
            FieldDef("pages", "页数", "int"),
            FieldDef("language", "语言", "str"),
            FieldDef("marked_date", "豆瓣标记日期", "str"),
            FieldDef("cover_url", "封面地址", "str"),
        ),
    ),
    "manga": KindDef(
        "manga",
        "漫画",
        (
            FieldDef("publisher", "出版社", "str"),
            FieldDef("volumes", "卷数", "int"),
            FieldDef("magazine", "连载杂志", "str"),
            # 下面这些来自 picamanga / copymanga 这类在线来源的导出
            # （同一个 kind 挂多个来源，见 docs/development.md 的「判重与来源」一节）。
            # 不同来源只会填其中一部分。
            FieldDef("finished", "已完结", "bool"),
            FieldDef("status_label", "连载状态（来源原文）", "str"),
            FieldDef("chapter_count", "已知话数", "int"),
            FieldDef("latest_chapter", "最新话", "str"),
            FieldDef("reading_progress", "读到", "str"),
            FieldDef("remote_updated_at", "源站更新于", "str"),
            FieldDef("has_new_chapter", "有更新", "bool"),
            # 约定：所有来源的「远程封面地址」都叫 cover_url，跨来源不用各写一个字段。
            FieldDef("cover_url", "封面远程地址", "str"),
        ),
        # 漫画特例：不同来源之间不考虑重复，只查同源内部
        # （口径见 docs/development.md 的「判重与来源」一节）。
        cross_source_duplicates=False,
    ),
    "movie": KindDef(
        "movie",
        "影视",
        (
            FieldDef("director", "导演", "str"),
            FieldDef("cast", "主演", "list[str]"),
            FieldDef("runtime", "片长（分钟）", "int"),
            FieldDef("country", "国家/地区", "str"),
            FieldDef("language", "语言", "str"),
            FieldDef("playable", "豆瓣可播放", "bool"),
            FieldDef("intro", "豆瓣简介串", "str"),
            FieldDef("marked_date", "豆瓣标记日期", "str"),
            FieldDef("cover_url", "封面地址", "str"),
            FieldDef("tmdb_rating", "TMDB 评分", "float"),
        ),
    ),
    "asmr": KindDef(
        "asmr",
        "ASMR",
        (
            # 创作者（社团 + 声优）在通用列 creators 里：多值，第一项是社团。
            FieldDef("release_date", "发售日", "str"),
            FieldDef("duration_minutes", "时长（分钟）", "int"),
            FieldDef("languages", "语言版本", "list[str]"),
            FieldDef("has_subtitle", "有字幕", "bool"),
            FieldDef("nsfw", "成人内容", "bool"),
            FieldDef("age_category", "年龄分级", "str"),
            FieldDef("price_jpy", "价格（日元）", "int"),
            FieldDef("source_type", "来源站", "str"),
            FieldDef("playlist_added_at", "加入播放列表", "str"),
            FieldDef("cover_url", "封面地址", "str"),
        ),
    ),
    "music": KindDef(
        "music",
        "音乐",
        (
            FieldDef("album", "专辑", "str"),
            FieldDef("tracks", "曲目数", "int"),
            FieldDef("label", "厂牌", "str"),
            # 本地文件标签 / iTunes 资料库带来的（字段口径见 docs/development.md）。
            # creators 通用列把「艺术家」和「专辑艺术家」合并了，回头分不出哪条是哪条，
            # 所以标签里这两个字段各留一份原文。
            FieldDef("artists", "艺术家（标签原文）", "list[str]"),
            FieldDef("album_artists", "专辑艺术家（标签原文）", "list[str]"),
            FieldDef("filename", "原始文件名", "str"),
            FieldDef("track_number", "编号", "int"),
            FieldDef("duration_seconds", "时长（秒）", "int"),
        ),
        # 同一首歌 = 标题 + 专辑 + 艺术家都相同（见 KindDef.identity_fields）。
        identity_fields=("album", "artists"),
    ),
    "game": KindDef(
        "game",
        "游戏",
        (
            FieldDef("platform", "平台", "list[str]"),
            FieldDef("developer", "开发商", "str"),
            FieldDef("publisher", "发行商", "str"),
            FieldDef("playtime_hours", "游戏时长（小时）", "float"),
            FieldDef("info", "豆瓣信息串", "str"),
            FieldDef("release_date", "发行日期", "str"),
            FieldDef("marked_date", "豆瓣标记日期", "str"),
            FieldDef("cover_url", "封面地址", "str"),
        ),
    ),
}


def get_kind(name: str) -> KindDef:
    """按名字取 kind 定义；未注册时抛 ``UnknownKindError``。"""
    try:
        return KINDS[name]
    except KeyError as exc:
        known = ", ".join(sorted(KINDS))
        raise UnknownKindError(f"未注册的 kind: {name!r}（已注册: {known}）") from exc


def register(kind: KindDef) -> None:
    """注册/覆盖一个 kind，供未来扩展（例如插件式 importer）使用。"""
    KINDS[kind.name] = kind


def kind_names() -> list[str]:
    return sorted(KINDS)


def kind_labels() -> dict[str, str]:
    return {name: kd.label for name, kd in KINDS.items()}


def coerce_value(field_def: FieldDef, value: Any) -> Any:
    """按声明类型转换单个 extra 值；无法安全转换时抛 ``ExtraFieldError``。"""
    kind = field_def.type
    where = f"extra.{field_def.name}"
    if kind == "str":
        if isinstance(value, bool):
            raise ExtraFieldError(f"{where} 期望 str，得到 bool: {value!r}")
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return value
        raise ExtraFieldError(f"{where} 期望 str，得到 {type(value).__name__}: {value!r}")

    if kind == "int":
        if isinstance(value, bool):
            raise ExtraFieldError(f"{where} 期望 int，得到 bool: {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and float(value).is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            return int(value.strip())
        raise ExtraFieldError(f"{where} 期望 int，得到 {value!r}")

    if kind == "float":
        if isinstance(value, bool):
            raise ExtraFieldError(f"{where} 期望 float，得到 bool: {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError as exc:
                raise ExtraFieldError(f"{where} 期望 float，得到 {value!r}") from exc
        raise ExtraFieldError(f"{where} 期望 float，得到 {type(value).__name__}: {value!r}")

    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
            return value.strip().lower() in {"true", "1"}
        raise ExtraFieldError(f"{where} 期望 bool，得到 {value!r}")

    if kind == "list[str]":
        if isinstance(value, (list, tuple)):
            items: list[str] = []
            for entry in value:
                if isinstance(entry, bool) or not isinstance(entry, (str, int, float)):
                    raise ExtraFieldError(f"{where} 期望 list[str]，元素非法: {entry!r}")
                items.append(str(entry))
            return items
        raise ExtraFieldError(f"{where} 期望 list[str]，得到 {type(value).__name__}: {value!r}")

    raise ExtraFieldError(f"{where} 未支持的类型 {kind!r}")


def coerce_extra(kind: str, extra: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """校验并转换 ``extra``。

    返回 ``(转换后的 extra, warnings)``：
      * 未知字段 -> 记一条 warning，值原样保留（不拒绝）。
      * 类型不符 / 必填缺失 -> 抛 ``ExtraFieldError``（拒绝整条记录）。
      * ``None`` 视为未提供，直接丢弃。
    """
    kind_def = get_kind(kind)
    warnings: list[str] = []
    coerced: dict[str, Any] = {}
    for key, value in extra.items():
        if value is None:
            continue
        field_def = kind_def.field(key)
        if field_def is None:
            warnings.append(
                f"kind={kind} 未声明 extra 字段 {key!r}，已原样保留（请在 kinds.py 中声明）"
            )
            coerced[key] = value
            continue
        coerced[key] = coerce_value(field_def, value)

    missing = [f.name for f in kind_def.extra_fields if f.required and f.name not in coerced]
    if missing:
        raise ExtraFieldError(f"kind={kind} 缺少必填 extra 字段: {', '.join(missing)}")
    return coerced, warnings
