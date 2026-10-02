"""importers 注册表。

    @register("json_movies")
    class MovieJsonImporter(JsonFileImporter): ...

    cls = get("json_movies")
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:  # pragma: no cover
    from .base import Importer

IMPORTERS: dict[str, type["Importer"]] = {}

T = TypeVar("T", bound="Importer")


class UnknownImporterError(LookupError):
    """请求的 importer 未注册。"""


def register(name: str) -> Callable[[type[T]], type[T]]:
    """类装饰器：把 importer 注册到全局表。"""

    def deco(cls: type[T]) -> type[T]:
        if name in IMPORTERS and IMPORTERS[name] is not cls:
            raise ValueError(f"importer 名称重复: {name!r}")
        if not getattr(cls, "source", None):
            cls.source = name
        IMPORTERS[name] = cls
        return cls

    return deco


def get(name: str) -> type["Importer"]:
    """按名字取 importer 类；未注册时抛 ``UnknownImporterError``。"""
    try:
        return IMPORTERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(IMPORTERS)) or "(空)"
        raise UnknownImporterError(f"未注册的 importer: {name!r}（已注册: {known}）") from exc


def names() -> list[str]:
    return sorted(IMPORTERS)


def load_builtin_importers() -> None:
    """导入内置 importer 模块，触发 ``@register``。

    自己写的 importer 除了加 ``@register``，还要让模块被导入到这里
    （写进 ``json_file.py`` / ``csv_file.py``，或像 ``picamanga.py`` 那样在下面列出来）。
    """
    from . import (  # noqa: F401  （导入即注册）
        asmr_one,
        copymanga,
        csv_file,
        douban,
        json_file,
        music,
        picamanga,
    )
