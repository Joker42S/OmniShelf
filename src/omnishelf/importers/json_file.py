"""JSON importer：支持 ``[...]`` 与 ``{"items": [...]}``（也认 ``records`` / ``data`` / ``works``）。

映射/组装逻辑在 ``mapping.MappingImporter``；CSV 见 ``csv_file.CsvFileImporter``。

新增自己的 importer：在类体里声明 ``source`` / ``kind`` / ``mapping`` / 可选 ``transforms``，
再 ``@register("名字")``。写在 ``json_file.py`` 或 ``csv_file.py`` 里的类会被
``load_builtin_importers()`` 自动加载；写在别的模块里的话，把模块名加进
``registry.load_builtin_importers()``。

约定：
  * ``fetch`` 只读文件、不转换类型。
  * ``normalize`` 只做字段映射；类型/范围校验交给 ItemDraft 与 kinds.coerce_extra。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .base import DictRawRecord, NormalizeError, RawRecord
from .mapping import CORE_FIELDS, MappingImporter, Transforms
from .registry import register

__all__ = [
    "CORE_FIELDS",
    "MappingImporter",
    "Transforms",
    "JsonFileImporter",
    "MovieJsonImporter",
    "BookJsonImporter",
]


class JsonFileImporter(MappingImporter):
    """从本地 JSON 文件读取记录。"""

    encoding: str = "utf-8"

    def __init__(
        self,
        path: str | Path,
        *,
        encoding: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(path, **kwargs)
        if encoding is not None:
            self.encoding = encoding

    # ------------------------------------------------------------------ fetch
    def load(self) -> list[dict[str, Any]]:
        """读文件并返回记录列表（非 dict 的行直接忽略）。"""
        if not self.path.is_file():
            raise NormalizeError(f"JSON 文件不存在: {self.path}")
        try:
            text = self.path.read_text(encoding=self.encoding)
        except UnicodeDecodeError as exc:
            raise NormalizeError(f"文件编码不是 {self.encoding}: {self.path}: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise NormalizeError(f"JSON 解析失败: {self.path}: {exc}") from exc

        if isinstance(data, dict):
            for key in ("items", "records", "data", "works"):
                value = data.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
            raise NormalizeError(
                f"JSON 对象里找不到 items/records/data/works 数组: {self.path}"
            )
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        raise NormalizeError(f"JSON 顶层必须是数组或对象: {self.path}")

    def fetch(self) -> Iterable[RawRecord]:
        for payload in self.load():
            yield DictRawRecord(source=self.source, payload=payload)


# --------------------------------------------------------------------------- 内置 JSON importer
# 通用 JSON 入库走这两个。字段名对不上时复制一个类，换掉 source/kind/mapping 即可；
# 具体的 CSV importer 写在 csv_file.py 的 CsvFileImporter 后面，会被自动加载。


@register("json_movies")
class MovieJsonImporter(JsonFileImporter):
    """影视 JSON：``{"items": [...]}`` 或 ``[...]``。"""

    source = "json_movies"
    kind = "movie"
    mapping: Mapping[str, Sequence[str]] = {
        "title": ["title", "name"],
        "subtitle": ["tagline", "subtitle"],
        "year": ["year", "release_year"],
        "status": ["status"],
        "rating": ["rating", "my_rating"],
        "notes": ["overview", "notes"],
        "extra.director": ["director"],
        "extra.cast": ["cast", "actors"],
        "extra.runtime": ["runtime", "runtime_minutes"],
        "extra.country": ["country"],
        "extra.tmdb_rating": ["tmdb_rating", "vote_average"],
        # 故意映射一个 kinds.py 里未声明的字段：导入时会给出 warning 但保留该值。
        "extra.letterboxd_uri": ["letterboxd_uri"],
        "external_ids.tmdb": ["tmdb_id"],
        "tags": ["genres", "tags"],
    }


@register("json_books")
class BookJsonImporter(JsonFileImporter):
    """图书 JSON：与影视同构，证明「换 kind 只需换配置」。"""

    source = "json_books"
    kind = "book"
    mapping: Mapping[str, Sequence[str]] = {
        "title": ["title", "name"],
        "subtitle": ["subtitle"],
        "year": ["year", "published_year"],
        "status": ["status"],
        "rating": ["rating"],
        "notes": ["notes", "summary"],
        "creators": ["authors", "author", "creator"],
        "extra.publisher": ["publisher"],
        "extra.isbn": ["isbn"],
        "extra.pages": ["pages", "page_count"],
        "extra.language": ["language"],
        "external_ids.douban": ["douban_id"],
        "external_ids.isbn": ["isbn13"],
        "tags": ["tags", "genres"],
    }
