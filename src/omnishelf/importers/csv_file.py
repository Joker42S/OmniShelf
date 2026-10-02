"""CSV importer 基类（标准库 csv，无额外依赖）。

各家的表头写法都不一样，所以这里**只给基类**，不预置具体的 CSV importer：
在自己的模块（或直接写在 ``csv_file.py`` 末尾）里加一个子类并注册即可——

    @register("csv_games")
    class GameCsvImporter(CsvFileImporter):
        source = "csv_games"
        kind = "game"
        mapping = {...}
        transforms = {...}

写在**本模块**里的具体 importer 会被 ``load_builtin_importers()`` 自动加载；
写在别的模块里，要把模块名加进 ``registry.load_builtin_importers()``。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .base import DictRawRecord, NormalizeError, RawRecord
from .mapping import MappingImporter


class CsvFileImporter(MappingImporter):
    """读取本地 CSV；表头即源字段名。

    * ``encoding`` 默认 ``utf-8-sig``：Excel「另存为 CSV」会带 BOM，用 utf-8 读会在
      第一个列名上留下 ``\\ufeff``，导致映射永远取不到那一列。
    * ``delimiter`` 默认 ``,``；分号/制表符导出的文件在子类里覆盖即可。
    * 空单元格是 ``""``、缺失的列是 ``None``，两者都被映射引擎当成「没有值」，
      会继续尝试下一个候选字段。
    """

    encoding: str = "utf-8-sig"
    delimiter: str = ","

    def __init__(
        self,
        path: str | Path,
        *,
        encoding: str | None = None,
        delimiter: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(path, **kwargs)
        if encoding is not None:
            self.encoding = encoding
        if delimiter is not None:
            self.delimiter = delimiter

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            raise NormalizeError(f"CSV 文件不存在: {self.path}")
        try:
            with self.path.open("r", encoding=self.encoding, newline="") as handle:
                reader = csv.DictReader(handle, delimiter=self.delimiter)
                return [dict(row) for row in reader]
        except UnicodeDecodeError as exc:
            raise NormalizeError(f"文件编码不是 {self.encoding}: {self.path}: {exc}") from exc
        except csv.Error as exc:
            raise NormalizeError(f"CSV 解析失败: {self.path}: {exc}") from exc

    def fetch(self) -> Iterable[RawRecord]:
        for payload in self.rows():
            yield DictRawRecord(source=self.source, payload=payload)


