"""JSON 接口（``/api/*``），与页面路由分开挂载。

未来加 token 中间件时只需要作用在这个 router 上，页面结构不用动。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ... import repository as repo
from ...services import export_ as export_service
from ...services import review as review_service
from ..deps import session_dependency

router = APIRouter()

MAX_LIMIT = 500


class ItemPatch(BaseModel):
    kind: str | None = None
    title: str | None = None
    subtitle: str | None = None
    year: int | None = None
    status: str | None = None
    rating: float | None = None
    notes: str | None = None
    extra: dict[str, Any] | None = None
    tags: list[str] | None = None


@router.get("/health")
def health(session: Session = Depends(session_dependency)) -> dict[str, Any]:
    return {"status": "ok", "items": repo.count_items(session)}


@router.get("/stats")
def stats(session: Session = Depends(session_dependency)) -> dict[str, Any]:
    return repo.stats(session)


@router.get("/items")
def list_items(
    kind: Optional[str] = None,
    status: Optional[str] = None,
    tag: Optional[str] = None,
    title_prefix: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    session: Session = Depends(session_dependency),
) -> dict[str, Any]:
    limit = max(1, min(limit, MAX_LIMIT))
    items = repo.list_items(
        session,
        kind=kind,
        status=status,
        tag=tag,
        title_prefix=title_prefix,
        limit=limit,
        offset=offset,
    )
    return {
        "count": len(items),
        "items": [export_service.item_to_dict(session, item) for item in items],
    }


@router.get("/items/{item_id}")
def get_item(item_id: str, session: Session = Depends(session_dependency)) -> dict[str, Any]:
    item = repo.get_item(session, item_id, include_deleted=True)
    if item is None:
        raise HTTPException(status_code=404, detail=f"找不到 item: {item_id}")
    return export_service.item_to_dict(session, item)


@router.patch("/items/{item_id}")
def patch_item(
    item_id: str,
    patch: ItemPatch,
    session: Session = Depends(session_dependency),
) -> dict[str, Any]:
    item = repo.get_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"找不到 item: {item_id}")
    changes = patch.model_dump(exclude_unset=True)
    tags = changes.pop("tags", None)
    try:
        if changes:
            item = repo.update_item(session, item.id, **changes)
        if tags is not None:
            repo.set_tags(session, item.id, tags)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return export_service.item_to_dict(session, item)


@router.delete("/items/{item_id}")
def delete_item(item_id: str, session: Session = Depends(session_dependency)) -> dict[str, Any]:
    item = repo.get_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"找不到 item: {item_id}")
    repo.soft_delete_item(session, item.id)
    return {"deleted": item.id}


@router.get("/review")
def list_review(session: Session = Depends(session_dependency)) -> dict[str, Any]:
    views = review_service.list_pending(session)
    return {
        "count": len(views),
        "pending": [
            {
                "id": view.id,
                "reason": view.reason,
                "created_at": view.created_at,
                "item": export_service.item_to_dict(session, view.item) if view.item else None,
                "candidate": (
                    export_service.item_to_dict(session, view.candidate) if view.candidate else None
                ),
            }
            for view in views
        ],
    }


@router.post("/review/{pending_id}/merge")
def merge_review(
    pending_id: int,
    keep: str,
    session: Session = Depends(session_dependency),
) -> dict[str, Any]:
    try:
        outcome = review_service.merge(session, pending_id, keep)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "pending_id": outcome.pending_id,
        "kept": outcome.kept.id,
        "dropped": outcome.dropped.id,
        "moved_external_ids": outcome.moved_external_ids,
        "moved_tags": outcome.moved_tags,
        "moved_fields": outcome.moved_fields,
    }


@router.post("/review/{pending_id}/skip")
def skip_review(pending_id: int, session: Session = Depends(session_dependency)) -> dict[str, Any]:
    try:
        review_service.skip(session, pending_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"skipped": pending_id}
