"""通用「字段映射 -> ItemDraft」引擎。

文件型 importer（JSON / CSV / Excel / …）只需要实现 ``fetch()``，
把一组原始 dict 交给这里；映射、类型组装、空值处理都在这一处，
避免每个 importer 复制一遍 normalize。

映射表的写法::

    mapping = {
        "title": ["title", "name"],          # 候选源字段，按顺序取第一个「有值」的
        "year": ["year", "release_year"],
        "extra.director": ["director"],      # 点号 = 嵌套进 extra
        "external_ids.tmdb": ["tmdb_id"],    # 点号 = 写进 external_ids
        "tags": ["genres"],
    }

需要换算/清洗的字段用 ``transforms``（key 是 mapping 里的**目标**）::

    transforms = {
        "rating": lambda v: float(v) * 2,                       # 5 星制 -> 0-10
        "status": {"看过": "done", "在看": "doing"},             # 字典查表，未命中则原样透传
        "extra.platform": lambda v: [p.strip() for p in v.split("|")],
    }
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .base import ItemDraft, NormalizeError, RawRecord

#: 映射里允许直接落库的顶层字段（``cover_path`` 只能由封面服务维护）。
CORE_FIELDS: frozenset[str] = frozenset(
    {"kind", "title", "subtitle", "creators", "year", "status", "rating", "notes"}
)

#: 目标字段 -> 清洗函数（或查表 dict）。
Transforms = Mapping[str, "Callable[[Any], Any] | Mapping[Any, Any]"]


def is_blank(value: Any) -> bool:
    """判断「这个字段没有值」。

    ``None`` 与全空白字符串算没有值；``0`` / ``False`` / ``[]`` **不算**
    （它们是有效数据）。CSV 里到处是空字符串，这条规则决定了能不能正确回退到候选字段。
    """
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


class MappingImporter:
    """提供 ``pick()`` 与 ``normalize()``。子类必须自己实现 ``fetch()``。

    子类在类体里覆盖 ``source`` / ``kind`` / ``mapping`` / ``transforms`` 即可，
    也可以全部通过构造函数传入（``omnishelf import`` 的额外参数会透传到这里）。
    """

    source: str = ""
    kind: str | None = None
    mapping: Mapping[str, Sequence[str]] = {}
    transforms: Transforms = {}
    default_tags: Sequence[str] = ()
    #: True（默认）时，空字符串等同于「字段不存在」，会继续尝试下一个候选字段。
    skip_blank: bool = True
    #: True（默认）时，字符串值会去掉首尾空白（" Mobius " -> "Mobius"）。
    #: CSV 里到处都是多余空格，而首尾空格正是「同一条目被当成两条」的常见原因。
    strip_values: bool = True

    def __init__(
        self,
        path: str | Path,
        *,
        kind: str | None = None,
        mapping: Mapping[str, Sequence[str]] | None = None,
        source: str | None = None,
        transforms: Transforms | None = None,
        default_tags: Sequence[str] | None = None,
        skip_blank: bool | None = None,
        strip_values: bool | None = None,
    ) -> None:
        self.path = Path(path)
        if source is not None:
            self.source = source
        if kind is not None:
            self.kind = kind
        if mapping is not None:
            self.mapping = mapping
        if transforms is not None:
            self.transforms = transforms
        if default_tags is not None:
            self.default_tags = tuple(default_tags)
        if skip_blank is not None:
            self.skip_blank = skip_blank
        if strip_values is not None:
            self.strip_values = strip_values

    # ------------------------------------------------------------------ 取值
    @staticmethod
    def lookup(payload: Mapping[str, Any], key: str) -> tuple[bool, Any]:
        """取值：先按字面量键，再按 ``a.b.c`` 点路径下钻。返回 (是否找到, 值)。"""
        if key in payload:
            return True, payload[key]
        if "." not in key:
            return False, None
        node: Any = payload
        for part in key.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                return False, None
        return True, node

    def pick(self, payload: Mapping[str, Any], candidates: Sequence[str]) -> Any:
        """按候选顺序取第一个「有值」的字段；都没有则返回 ``None``。

        候选键可以是字面量（``"name"``），也可以是点路径（``"extra.platforms"``，
        用于豆瓣这类把若干字段塞在 ``extra`` 子对象里的导出）。字面量优先，
        所以既有的扁平 payload 行为不变。
        """
        for key in candidates:
            found, value = self.lookup(payload, key)
            if not found:
                continue
            if self.skip_blank and is_blank(value):
                continue
            if self.strip_values and isinstance(value, str):
                value = value.strip()
            return value
        return None

    def apply_transform(self, target: str, value: Any) -> Any:
        transform = self.transforms.get(target)
        if transform is None:
            return value
        if isinstance(transform, Mapping):
            return transform.get(value, value)
        return transform(value)

    # -------------------------------------------------------------- normalize
    def normalize(self, raw: RawRecord) -> ItemDraft:
        payload = dict(raw.payload)
        draft: dict[str, Any] = {"kind": self.kind, "extra": {}, "external_ids": [], "tags": []}
        extra: dict[str, Any] = draft["extra"]
        external_ids: list[dict[str, Any]] = draft["external_ids"]
        tags: list[str] = list(self.default_tags)

        for target, candidates in self.mapping.items():
            value = self.pick(payload, candidates)
            if value is None:
                continue
            value = self.apply_transform(target, value)
            if value is None:
                continue  # transform 说「这个值没内容」（例如来源给了空字符串）-> 不写这个字段

            if target in CORE_FIELDS:
                draft[target] = value
            elif target == "tags":
                if isinstance(value, str):
                    tags.extend(part.strip() for part in value.split(",") if part.strip())
                elif isinstance(value, (list, tuple, set)):
                    tags.extend(str(part).strip() for part in value if str(part).strip())
                else:
                    raise NormalizeError(f"tags 字段类型不支持: {value!r}")
            elif target.startswith("extra."):
                extra[target.split(".", 1)[1]] = value
            elif target.startswith("external_ids."):
                ext_source = target.split(".", 1)[1]
                external_ids.append({"source": ext_source, "external_id": value})
            elif target == "external_id":  # 简写：用 importer 的 source 当来源
                external_ids.append({"source": self.source, "external_id": value})
            else:
                raise NormalizeError(f"映射目标无法识别: {target!r}")

        if not draft.get("kind"):
            raise NormalizeError(
                f"记录没有 kind：importer {self.source!r} 未声明 kind，映射里也没有 kind 字段"
            )
        if not draft.get("title"):
            usable = ", ".join(sorted(payload)) or "无"
            raise NormalizeError(f"记录缺少 title（可用字段: {usable}）")
        draft["tags"] = tags
        try:
            return ItemDraft(**draft)
        except ValueError as exc:  # pydantic ValidationError 也是 ValueError
            raise NormalizeError(f"记录校验失败: {exc}") from exc
