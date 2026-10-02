"""上传页面：选一个 importer + 选一个数据文件，直接导入。

* importer 下拉**从注册表读**（`importers.registry.names()`），不硬编码；
* 上传的文件先按来源存档到 `data/imports/<来源>/`，再走正常的导入流程；
* 导入完成后 303 到 ``/upload?log_id=N``，用 ``import_logs`` 那行显示统计与错误明细
  （POST-Redirect-GET，刷新页面不会重复导入；就算重复导入也是幂等的）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from ... import repository as repo
from ...importers.base import NormalizeError
from ...importers.registry import IMPORTERS, names
from ...kinds import kind_labels
from ...services.import_ import import_uploaded
from ..deps import session_dependency, settings_of, templates_of

router = APIRouter()

#: 上传文件大小上限（数据备份通常几 MB；给足余量但拦住误传的大文件）
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
#: 结果面板里最多显示多少条错误/警告明细
MAX_SHOWN_ENTRIES = 30


@dataclass(slots=True)
class ImporterOption:
    """下拉框里的一项（kind 只用于显示，不影响导入）。"""

    name: str
    source: str
    kind: str
    kind_label: str
    label: str


def importer_options() -> list[ImporterOption]:
    """从注册表生成下拉选项（新增 importer 后这里自动出现）。"""
    options: list[ImporterOption] = []
    for name in names():
        cls = IMPORTERS[name]
        kind = str(getattr(cls, "kind", "") or "")
        kind_text = kind_labels().get(kind, kind or "未声明类型")
        options.append(
            ImporterOption(
                name=name,
                source=str(getattr(cls, "source", "") or name),
                kind=kind,
                kind_label=kind_text,
                label=f"{name}（{kind_text}）",
            )
        )
    return options


def _result_context(session: Session, log_id: int, archived: Optional[str]) -> dict[str, Any]:
    """把 import_logs 的一行转成结果面板需要的结构。"""
    log = repo.get_import_log(session, log_id)
    if log is None:
        return {"result": None, "result_error": f"找不到导入记录 #{log_id}"}

    entries = log.errors or []
    errors = [e for e in entries if e.get("level") == "error"]
    warnings = [e for e in entries if e.get("level") != "error"]
    return {
        "result": {
            "id": log.id,
            "source": log.source,
            "started_at": log.started_at,
            "finished_at": log.finished_at,
            "total": log.total,
            "inserted": log.inserted,
            "updated": log.updated,
            "skipped": log.skipped,
            "errors": errors[:MAX_SHOWN_ENTRIES],
            "warnings": warnings[:MAX_SHOWN_ENTRIES],
            "error_count": len(errors),
            "warning_count": len(warnings),
            "hidden_count": max(0, len(errors) - MAX_SHOWN_ENTRIES)
            + max(0, len(warnings) - MAX_SHOWN_ENTRIES),
            "archived": archived,
        },
        "result_error": None,
    }


@router.get("/upload", response_class=HTMLResponse)
def upload_page(
    request: Request,
    log_id: Optional[int] = None,
    archived: Optional[str] = None,
    session: Session = Depends(session_dependency),
) -> HTMLResponse:
    """上传页；带 ``log_id`` 时显示那次导入的结果。"""
    context: dict[str, Any] = {
        "options": importer_options(),
        "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "result": None,
        "result_error": None,
        "error": None,
        "selected": None,
        "archived": archived,
    }
    if log_id is not None:
        context.update(_result_context(session, log_id, archived))
    templates = templates_of(request)
    return templates.TemplateResponse(request=request, name="upload.html", context=context)


@router.post("/upload", response_class=HTMLResponse)
async def upload_import(
    request: Request,
    importer: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(session_dependency),
) -> Response:
    """接收上传的数据文件并导入。"""
    templates = templates_of(request)
    settings = settings_of(request)

    def fail(message: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="upload.html",
            context={
                "options": importer_options(),
                "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
                "result": None,
                "result_error": None,
                "error": message,
                "selected": importer,
                "archived": None,
            },
            status_code=400,
        )

    if importer not in IMPORTERS:
        available = ", ".join(names()) or "(没有注册任何 importer)"
        return fail(f"未注册的 importer: {importer!r}（可选: {available}）")

    data = await file.read()
    if not data:
        return fail("没有选择文件，或者文件是空的。")
    if len(data) > MAX_UPLOAD_BYTES:
        return fail(
            f"文件太大（{len(data) / 1024 / 1024:.1f} MB），上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB。"
        )

    try:
        result = import_uploaded(
            session,
            importer_name=importer,
            filename=file.filename or "upload.json",
            data=data,
            imports_dir=settings.imports_dir,
        )
    except (NormalizeError, OSError, ValueError) as exc:  # 文件级问题不该 500
        # 解析失败的文件不算「导入过的数据」：把刚存下的那份删掉，别污染存档目录
        from ...services.import_ import safe_filename

        stale = settings.imports_dir / str(getattr(IMPORTERS[importer], "source", importer) or importer)
        stale = stale / safe_filename(file.filename or "upload.json")
        stale.unlink(missing_ok=True)
        return fail(
            f"这个文件没法用 {importer} 解析（{type(exc).__name__}: {exc}）。"
            "确认一下 importer 选对了、文件是完整的导出？"
        )

    target = f"/upload?log_id={result.log_id}"
    if result.archive_path:
        from urllib.parse import quote

        target += f"&archived={quote(result.archive_path, safe='')}"
    return RedirectResponse(url=target, status_code=303)
