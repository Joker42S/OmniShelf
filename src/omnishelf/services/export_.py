"""导出编排：把库里数据写成 JSON，便于备份/迁移。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlmodel import Session

from .. import repository as repo
from ..models import Item

SUPPORTED_FORMATS: tuple[str, ...] = ("json",)


class ExportFormatError(ValueError):
    """不支持的导出格式。"""


def item_to_dict(session: Session, item: Item) -> dict[str, Any]:
    """单个 item 的完整可导出表示（含 tags / external_ids）。"""
    return {
        "id": item.id,
        "kind": item.kind,
        "title": item.title,
        "subtitle": item.subtitle,
        "creators": list(item.creators or []),
        "year": item.year,
        "status": item.status,
        "rating": item.rating,
        "notes": item.notes,
        "cover_path": item.cover_path,
        "extra": dict(item.extra or {}),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "deleted_at": item.deleted_at,
        "tags": repo.list_tags(session, item.id),
        "external_ids": [
            {"source": link.source, "external_id": link.external_id, "url": link.url}
            for link in repo.list_external_ids(session, item.id)
        ],
    }


def export_payload(
    session: Session,
    *,
    kind: str | None = None,
    include_deleted: bool = False,
) -> dict[str, Any]:
    items = repo.list_items(session, kind=kind, include_deleted=include_deleted)
    return {
        "exported_at": repo.utc_now_iso(),
        "count": len(items),
        "stats": repo.stats(session),
        "items": [item_to_dict(session, item) for item in items],
    }


def export_json(
    session: Session,
    out_path: str | Path,
    *,
    kind: str | None = None,
    include_deleted: bool = False,
) -> int:
    """写 JSON 文件，返回写出的 item 数量。"""
    payload = export_payload(session, kind=kind, include_deleted=include_deleted)
    path = Path(out_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return int(payload["count"])


def export(
    session: Session,
    *,
    fmt: str,
    out_path: str | Path,
    kind: str | None = None,
    include_deleted: bool = False,
) -> int:
    """按格式导出（目前只有 json）。CLI 与 demo 都走这里。"""
    if fmt not in SUPPORTED_FORMATS:
        raise ExportFormatError(f"不支持的导出格式 {fmt!r}，可选: {', '.join(SUPPORTED_FORMATS)}")
    return export_json(session, out_path, kind=kind, include_deleted=include_deleted)


def parse_export(payload: dict[str, Any]) -> Sequence[dict[str, Any]]:
    """导出内容 -> 记录列表（供未来 import 复用）。"""
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("导出 JSON 缺少 items 数组")
    return [row for row in items if isinstance(row, dict)]
