"""页面路由：列表 / 详情 / 编辑 / 封面上传。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from ... import repository as repo
from ...kinds import get_kind
from ...kinds import kind_labels, kind_names
from ...models import STATUS_LABELS, STATUSES
from ...services import covers, editing
from ...services import export_ as export_service
from ...services import filters as filters_service
from ..deps import session_dependency, settings_of, templates_of

router = APIRouter()

#: 列表页默认每页条数 / 上限（可以用 ?per_page= 覆盖）。
DEFAULT_PER_PAGE = 60
MIN_PER_PAGE = 10
MAX_PER_PAGE = 200
#: 页码导航左右各显示几个页码。
PAGER_WINDOW = 2


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _pager(filters: Any, *, page: int, pages: int, per_page: int, total: int) -> dict[str, Any]:
    """分页导航（所有链接都保留筛选与排序条件）。"""

    def url(target: int) -> str:
        return filters.to_query(page=target, per_page=per_page)

    first = max(1, page - PAGER_WINDOW)
    last = min(pages, page + PAGER_WINDOW)
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": total,
        "prev_url": url(page - 1) if page > 1 else None,
        "next_url": url(page + 1) if page < pages else None,
        "first_url": url(1) if page > 1 else None,
        "last_url": url(pages) if page < pages else None,
        "links": [
            {"page": number, "url": url(number), "current": number == page}
            for number in range(first, last + 1)
        ],
    }


def _empty_pager(per_page: int = DEFAULT_PER_PAGE) -> dict[str, Any]:
    """没有任何结果（或筛选条件非法）时的空分页。"""
    return {
        "page": 1,
        "pages": 1,
        "per_page": per_page,
        "total": 0,
        "prev_url": None,
        "next_url": None,
        "first_url": None,
        "last_url": None,
        "links": [],
    }


@router.get("/", response_class=HTMLResponse)
def list_page(
    request: Request,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    tag: Optional[str] = None,
    title_prefix: Optional[str] = None,
    search: Optional[str] = None,
    creator: Optional[str] = None,
    rating_min: Optional[str] = None,
    rating_max: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    tags_include: list[str] = Query(default=[]),
    tags_exclude: list[str] = Query(default=[]),
    sort: Optional[str] = None,
    dir: Optional[str] = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    session: Session = Depends(session_dependency),
) -> HTMLResponse:
    """列表页。

    结果由**三部分共同决定**：基础筛选（kind/status/tag/标题前缀）+ 搜索（只搜标题）
    + 高级筛选（评分区间 / 入库日期区间 / 创作者 / tag 包含与排除）。

    HTMX 用 ``hx-select=#items-panel`` 做局部刷新，所以这里始终返回整页。
    """
    templates = templates_of(request)
    page_size = max(MIN_PER_PAGE, min(int(per_page), MAX_PER_PAGE))
    base_context: dict[str, Any] = {
        "kind_names": kind_names(),
        "kind_labels": kind_labels(),
        "statuses": STATUSES,
        "status_labels": STATUS_LABELS,
        "stats": repo.stats(session),
        "all_tags": repo.all_tags(session),
    }

    def render(filters: Any, *, status_code: int = 200, filter_error: Optional[str] = None) -> HTMLResponse:
        if filter_error or filters is None:
            context = {
                **base_context,
                "rows": [],
                "filters": filters,
                "filter_error": filter_error,
                "pager": _empty_pager(page_size),
                "sort_links": [],
                "active_filters": [],
                "advanced_open": False,
                "tags_include_text": "",
                "tags_exclude_text": "",
            }
            return templates.TemplateResponse(
                request=request, name="list.html", context=context, status_code=status_code
            )

        count_kwargs = filters.repo_kwargs()
        total = repo.count_items(session, **count_kwargs)
        pages = max(1, -(-total // page_size))  # 向上取整
        current = max(1, min(int(page), pages))  # 页码越界时夹到有效范围
        items = repo.list_items(
            session,
            **count_kwargs,
            sort=filters.sort,
            descending=filters.descending,
            limit=page_size,
            offset=(current - 1) * page_size,
        )
        rows = [export_service.item_to_dict(session, item) for item in items]
        context = {
            **base_context,
            "rows": rows,
            "filters": filters,
            "filter_error": None,
            "pager": _pager(filters, page=current, pages=pages, per_page=page_size, total=total),
            "sort_links": filters.sort_links(),
            "active_filters": filters.active_filters(),
            "advanced_open": filters.has_advanced,
            "tags_include_text": ", ".join(filters.tags_include),
            "tags_exclude_text": ", ".join(filters.tags_exclude),
        }
        return templates.TemplateResponse(request=request, name="list.html", context=context)

    try:
        filters = filters_service.parse_filters(
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
            sort=sort,
            dir=dir,
        )
    except filters_service.FilterError as exc:
        return render(None, status_code=400, filter_error=str(exc))
    return render(filters)


@router.get("/items/{item_id}", response_class=HTMLResponse)
def detail_page(
    request: Request,
    item_id: str,
    error: Optional[str] = None,
    ok: Optional[str] = None,
    session: Session = Depends(session_dependency),
) -> HTMLResponse:
    item = repo.get_item(session, item_id, include_deleted=True)
    templates = templates_of(request)
    if item is None:
        return templates.TemplateResponse(
            request=request,
            name="not_found.html",
            context={"item_id": item_id},
            status_code=404,
        )
    kind_def = get_kind(item.kind)
    declared = {field.name for field in kind_def.extra_fields}
    row = export_service.item_to_dict(session, item)
    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "row": row,
            "kind_def": kind_def,
            "unknown_extra": {k: v for k, v in row["extra"].items() if k not in declared},
            "cover_missing": bool(row["cover_path"])
            and not covers.cover_exists(settings_of(request), row["cover_path"]),
            "creators": row["creators"],
            "stats": repo.stats(session),
            "error": _clean(error),
            "ok": _clean(ok),
        },
    )


@router.post("/items/{item_id}/quick")
async def quick_update(
    request: Request,
    item_id: str,
    session: Session = Depends(session_dependency),
) -> Response:
    """列表页行内编辑：只改状态/评分。

    HTMX 请求返回该行的 HTML 片段（原地替换）；普通表单提交则 303 回列表页。
    """
    item = repo.get_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"找不到 item: {item_id}")
    form = await request.form()
    try:
        editing.update_status_and_rating(
            session, item, status=form.get("status"), rating=form.get("rating")
        )
    except editing.ItemFormError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.headers.get("HX-Request"):
        fresh = repo.require_item(session, item.id)
        templates = templates_of(request)
        return templates.TemplateResponse(
            request=request,
            name="_row.html",
            context={"row": export_service.item_to_dict(session, fresh)},
        )
    return RedirectResponse(url=request.headers.get("referer") or "/", status_code=303)


@router.post("/items/{item_id}")
async def update_item(
    request: Request,
    item_id: str,
    session: Session = Depends(session_dependency),
) -> RedirectResponse:
    """表单更新：核心字段 + tags + 按 kinds.py 声明的 extra 字段。"""
    item = repo.get_item(session, item_id)
    if item is None:
        return RedirectResponse(url=f"/items/{item_id}", status_code=303)

    form = await request.form()
    try:
        editing.update_item_from_form(session, item, form)
    except editing.ItemFormError as exc:
        return _error_redirect(item_id, str(exc))
    return RedirectResponse(url=f"/items/{item.id}?ok=已保存", status_code=303)


@router.post("/items/{item_id}/cover")
async def upload_cover(
    request: Request,
    item_id: str,
    cover: UploadFile = File(...),
    session: Session = Depends(session_dependency),
) -> RedirectResponse:
    """上传封面：写入 attachments/ab/cd/<ulid>.<ext> 并更新 cover_path。"""
    item = repo.get_item(session, item_id)
    if item is None:
        return RedirectResponse(url=f"/items/{item_id}", status_code=303)
    data = await cover.read()
    try:
        covers.attach_cover(
            settings_of(request),
            session,
            item,
            data,
            filename=cover.filename,
            content_type=cover.content_type,
        )
    except covers.CoverError as exc:
        return _error_redirect(item_id, str(exc))
    return RedirectResponse(url=f"/items/{item.id}?ok=封面已更新", status_code=303)


@router.post("/items/{item_id}/cover/delete")
def delete_cover(
    request: Request,
    item_id: str,
    session: Session = Depends(session_dependency),
) -> RedirectResponse:
    item = repo.get_item(session, item_id)
    if item is None:
        return RedirectResponse(url=f"/items/{item_id}", status_code=303)
    covers.remove_cover(settings_of(request), session, item)
    return RedirectResponse(url=f"/items/{item.id}?ok=封面已删除", status_code=303)


def _error_redirect(item_id: str, message: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(url=f"/items/{item_id}?error={quote(message)}", status_code=303)
