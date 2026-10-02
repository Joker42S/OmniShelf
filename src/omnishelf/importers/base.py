"""Importer 的抽象契约。

这里只放协议与数据载体，不放具体实现，也不碰数据库：
  * ``RawRecord``  —— importer.fetch() 的产出（原始记录）。
  * ``ItemDraft``  —— importer.normalize() 的产出（待落库的规范数据）。
  * ``Importer``   —— fetch / normalize 两段式，两段互不依赖 DB。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import RATING_MAX, RATING_MIN
from ..models import STATUSES


class NormalizeError(ValueError):
    """normalize 阶段无法把一条原始记录转成 ItemDraft。"""


class DraftExternalId(BaseModel):
    """一条外部 ID 引用，例如 ``("tmdb", "78", "https://...")``。"""

    model_config = ConfigDict(frozen=True)

    source: str
    external_id: str
    url: str | None = None

    @field_validator("source", "external_id", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> str:
        if value is None:
            raise ValueError("external id 不能为 None")
        text = str(value).strip()
        if not text:
            raise ValueError("external id 不能为空")
        return text


class ItemDraft(BaseModel):
    """importer 的输出、service 的输入。字段与 items 表一一对应。"""

    model_config = ConfigDict(validate_assignment=True)

    kind: str
    title: str
    subtitle: str | None = None
    #: 创作者：多值（列表）。来源给单个字符串时用 `as_creator_list()` 包一下。
    creators: list[str] = []
    year: int | None = None
    status: str | None = None
    rating: float | None = None
    notes: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    external_ids: list[DraftExternalId] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _kind_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("kind 不能为空")
        return text

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"title 必须是字符串，得到 {type(value).__name__}")
        text = value.strip()
        if not text:
            raise ValueError("title 不能为空")
        return text

    @field_validator("status")
    @classmethod
    def _status_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text not in STATUSES:
            raise ValueError(f"非法 status: {value!r}（可选: {', '.join(STATUSES)}）")
        return text

    @field_validator("creators", mode="before")
    @classmethod
    def _creators(cls, value: object) -> list[str]:
        from ..repository import clean_creators

        return clean_creators(value)  # type: ignore[arg-type]

    @field_validator("subtitle", "notes")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("rating")
    @classmethod
    def _rating_range(cls, value: float | None) -> float | None:
        if value is None:
            return None
        number = float(value)
        if not (RATING_MIN <= number <= RATING_MAX):
            raise ValueError(f"rating 必须在 {RATING_MIN}-{RATING_MAX} 之间，得到 {number}")
        return number

    @field_validator("external_ids", mode="before")
    @classmethod
    def _coerce_external_ids(cls, value: Any) -> Any:
        """接受 ``(source, id)`` / ``(source, id, url)`` 元组或 dict。"""
        if value is None:
            return []
        if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
            raise ValueError("external_ids 必须是序列")
        items: list[Any] = []
        for entry in value:
            if isinstance(entry, DraftExternalId):
                items.append(entry)
            elif isinstance(entry, dict):
                items.append(entry)
            elif isinstance(entry, (tuple, list)):
                if len(entry) == 2:
                    items.append({"source": entry[0], "external_id": entry[1]})
                elif len(entry) == 3:
                    items.append({"source": entry[0], "external_id": entry[1], "url": entry[2]})
                else:
                    raise ValueError(f"external_ids 元组长度必须是 2 或 3: {entry!r}")
            else:
                raise ValueError(f"无法识别的 external_ids 元素: {entry!r}")
        return items

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",")]
        if isinstance(value, Iterable):
            return [str(part) for part in value]
        raise ValueError(f"tags 必须是字符串或序列，得到 {type(value).__name__}")


@runtime_checkable
class RawRecord(Protocol):
    """一条尚未规范化的原始记录。"""

    source: str
    payload: dict[str, Any]


@runtime_checkable
class Importer(Protocol):
    """两段式导入器：``fetch`` 只负责取数据，``normalize`` 只负责映射。"""

    source: str

    def fetch(self) -> Iterable[RawRecord]: ...

    def normalize(self, raw: RawRecord) -> ItemDraft: ...


@dataclass(slots=True)
class DictRawRecord:
    """``RawRecord`` 的通用实现。"""

    source: str
    payload: dict[str, Any]
