"""待复核合并的页面路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from ...services import review as review_service
from ..deps import session_dependency, templates_of

router = APIRouter()


@router.get("/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    ok: str | None = None,
    error: str | None = None,
    session: Session = Depends(session_dependency),
) -> HTMLResponse:
    views = review_service.list_pending(session)
    templates = templates_of(request)
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={"views": views, "ok": ok, "error": error},
    )


@router.post("/review/{pending_id}/merge")
def review_merge(
    pending_id: int,
    keep: str = Form(...),
    session: Session = Depends(session_dependency),
) -> RedirectResponse:
    try:
        outcome = review_service.merge(session, pending_id, keep)
    except (LookupError, ValueError) as exc:
        from urllib.parse import quote

        return RedirectResponse(url=f"/review?error={quote(str(exc))}", status_code=303)
    message = f"已合并 #{outcome.pending_id}，保留 {outcome.kept.title}"
    from urllib.parse import quote

    return RedirectResponse(url=f"/review?ok={quote(message)}", status_code=303)


@router.post("/review/{pending_id}/skip")
def review_skip(
    pending_id: int,
    session: Session = Depends(session_dependency),
) -> RedirectResponse:
    from urllib.parse import quote

    try:
        review_service.skip(session, pending_id)
    except LookupError as exc:
        return RedirectResponse(url=f"/review?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(url=f"/review?ok=已跳过%20{pending_id}", status_code=303)
