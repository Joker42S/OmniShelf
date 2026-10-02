"""FastAPI 应用工厂。

* 页面路由（``/``、``/items/...``、``/review``）与 JSON 接口（``/api/*``）分开挂载，
  将来加 token 中间件只作用在 ``/api`` 上，不需要改结构。
* 无认证，默认只绑定 127.0.0.1（由 CLI ``serve`` 决定）。
* 每个请求一个 Session；路由里只调用 repository / services。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Settings, load_settings
from ..db import build_engine, init_db
from ..kinds import kind_labels, kind_names
from ..models import STATUS_LABELS, STATUSES
from ..importers.registry import load_builtin_importers
from ..plugins.manager import PluginManager
from ..services import covers
from .routes import api as api_routes
from .routes import logs as log_routes
from .routes import plugins as plugin_routes
from .routes import upload as upload_routes
from .routes import pages as page_routes
from .routes import review as review_routes

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


#: 没有封面 / 封面文件缺失时用的默认图
DEFAULT_COVER = "/static/img/cover-na.svg"


def asset_version(static_dir: Path = STATIC_DIR) -> str:
    """静态资源版本号 = 所有 css/js 的内容哈希。

    页面里引用成 ``/static/app.css?v=<hash>``，改了样式/脚本后浏览器必然拿新的，
    不会出现「HTML 是新的、CSS 还是旧的缓存」这种诡异现象。
    """
    digest = hashlib.sha256()
    for path in sorted(static_dir.rglob("*")):
        if path.is_file() and path.suffix in {".css", ".js"}:
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def build_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def cover_url(row: dict[str, Any]) -> str:
        """有封面文件就返回它的 URL，否则返回默认图。"""
        rel = row.get("cover_path")
        if rel and covers.cover_exists(settings, str(rel)):
            return f"/attachments/{rel}"
        return DEFAULT_COVER

    def local_time(value: Any) -> str:
        """把库里的 UTC ISO8601 显示成本地时间（用户看到的才是自己的钟点）。"""
        if not value:
            return "-"
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%Y-%m-%d %H:%M")

    templates.env.filters["local_time"] = local_time
    templates.env.globals.update(
        asset_version=asset_version(),
        cover_url=cover_url,
        default_cover=DEFAULT_COVER,
        kind_names=kind_names(),
        kind_labels=kind_labels(),
        statuses=STATUSES,
        status_labels=STATUS_LABELS,
    )
    templates.env.filters["status_label"] = lambda value: STATUS_LABELS.get(value or "", value or "-")
    templates.env.filters["kind_label"] = lambda value: kind_labels().get(value or "", value or "-")
    templates.env.filters["rating_text"] = lambda value: "-" if value is None else f"{value:g}"
    return templates


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    init_db(resolved)
    engine = build_engine(resolved)

    app = FastAPI(
        title="OmniShelf",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = resolved
    app.state.engine = engine
    app.state.templates = build_templates(resolved)
    app.state.plugin_manager = PluginManager(
        plugins_dir=resolved.plugins_dir,
        plugins_data_dir=resolved.plugins_data_dir,
        engine=engine,
        imports_dir=resolved.imports_dir,
        logs_dir=resolved.plugin_logs_dir,
    )
    app.state.plugins_loaded = False

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """进程退出时给所有插件一次 stop() 的机会。"""
        try:
            yield
        finally:
            app.state.plugin_manager.stop_all()

    app.router.lifespan_context = lifespan

    load_builtin_importers()  # 让「上传」页的下拉框能列出所有内置 importer

    app.include_router(page_routes.router)
    app.include_router(upload_routes.router)
    app.include_router(plugin_routes.router)
    app.include_router(log_routes.router)
    app.include_router(review_routes.router)
    app.include_router(api_routes.router, prefix="/api")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    resolved.attachments_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/attachments",
        StaticFiles(directory=str(resolved.attachments_dir)),
        name="attachments",
    )
    return app
