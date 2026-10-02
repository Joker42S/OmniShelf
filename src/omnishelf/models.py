"""SQLModel 表定义。

表结构与 ``migrations/001_init.sql`` 一一对应；这里只用于类型化的查询与
序列化，绝不调用 ``create_all()``（建表只能通过迁移）。

时间戳一律是 ISO8601 UTC 字符串，由 repository 层维护。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

#: ``items.status`` 的合法取值。
STATUSES: tuple[str, ...] = ("wishlist", "doing", "done", "dropped")

#: status 的中文展示名（UI 用）。
#: 「没有状态」（``items.status IS NULL``）那一档的键与展示名。
#: ``repo.stats()["by_status"]`` 与插件接口都用这个键，避免各处硬写字符串。
UNSET_STATUS = "unset"
UNSET_STATUS_LABEL = "未设置"

STATUS_LABELS: dict[str, str] = {
    "wishlist": "想",
    "doing": "进行中",
    "done": "已完成",
    "dropped": "已放弃",
    UNSET_STATUS: UNSET_STATUS_LABEL,
}


class Item(SQLModel, table=True):
    __tablename__ = "items"

    id: str = Field(primary_key=True)
    kind: str = Field(index=True)
    title: str
    subtitle: str | None = None
    #: 创作者（作者 / 画师 / 声优 / 社团…），**多值**：一个作品可以有多个创作者。
    #: 通用字段；具体来源把它的字段映射到这里。空数组 = 没有创作者信息。
    creators: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    year: int | None = Field(default=None, index=True)
    status: str | None = Field(default=None, index=True)
    rating: float | None = None
    notes: str | None = None
    cover_path: str | None = None
    extra: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("extra", JSON, nullable=False, default=dict),
    )
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class ExternalId(SQLModel, table=True):
    __tablename__ = "external_ids"

    source: str = Field(primary_key=True)
    external_id: str = Field(primary_key=True)
    item_id: str = Field(index=True)
    url: str | None = None


class Tag(SQLModel, table=True):
    __tablename__ = "tags"

    item_id: str = Field(primary_key=True)
    tag: str = Field(primary_key=True, index=True)


class ImportLog(SQLModel, table=True):
    __tablename__ = "import_logs"

    id: int | None = Field(default=None, primary_key=True)
    source: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    total: int | None = None
    inserted: int | None = None
    updated: int | None = None
    skipped: int | None = None
    errors: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column("errors", JSON, nullable=True),
    )


class PendingMerge(SQLModel, table=True):
    __tablename__ = "pending_merges"

    id: int | None = Field(default=None, primary_key=True)
    item_id: str | None = None
    candidate_id: str | None = None
    reason: str | None = None
    created_at: str | None = None
