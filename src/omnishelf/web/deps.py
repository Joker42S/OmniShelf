"""Web 层的共享依赖。

单独放一个模块是为了避免 ``app.py`` 与 ``routes/*`` 之间的循环导入：
routes 只依赖这里，``app.py`` 负责组装。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from ..config import Settings
from ..plugins.manager import PluginManager


def session_dependency(request: Request) -> Iterator[Session]:
    """请求级 Session：正常提交、异常回滚。"""
    engine = request.app.state.engine
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def templates_of(request: Request) -> Jinja2Templates:
    templates: Jinja2Templates = request.app.state.templates
    return templates


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def plugin_manager_of(request: Request) -> PluginManager:
    """插件管理器（首次访问时懒加载全部插件）。"""
    manager: PluginManager = request.app.state.plugin_manager
    if not getattr(request.app.state, "plugins_loaded", False):
        manager.load_all()
        request.app.state.plugins_loaded = True
    return manager
