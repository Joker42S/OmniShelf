"""宿主暴露给插件的接口：**数据查询（只读）** + **导入（写）** + **本地音频扫描**。

插件拿到的是**普通 dict / 基本类型**，不接触 ORM 与会话：这样插件的依赖面最小，
宿主内部改造也不会把插件弄坏。每个方法自己开一个会话，用完就关。

三组接口：

* ``query``：只读查询（stats/kinds/statuses/all_tags/count_items/list_items/get_item）；
* ``import``：把文件交给宿主的**内置 importer** 入库（``import_file``），
  也就是插件抓完数据后可以直接落库，不必让用户手动去上传页——
  走的还是同一条导入链路（来源级判重、import_logs 审计、原始文件存档），
  不会绕过任何规则；返给插件的是导入结果摘要（新增/更新/跳过/错误/日志 id）。
* ``audio``：读本地音乐文件的标签并生成清单（``scan_audio``）。标签解析、认哪些后缀算
  音乐、垃圾目录、无标签时的兜底标题都是宿主的事（``omnishelf.music_scan``），插件只做
  「选文件 → 调这里 → 把清单交给 import_file」，所以扫出来的口径和 CLI 完全一致。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from sqlalchemy import Engine

from .. import repository as repo
from ..db import session_scope
from ..kinds import KINDS, kind_labels
from ..models import STATUSES, STATUS_LABELS, UNSET_STATUS
from ..services import export_ as export_service

#: 当前宿主提供的接口集合（插件 metadata 里的 requires 只能写这里的名字）
AVAILABLE_INTERFACES: tuple[str, ...] = ("query", "import", "audio")


#: 每读一个文件回调一次：``(已读文件数, 当前文件)``（插件的进度条用）。
AudioProgressFn = Callable[[int, Path], None]


@runtime_checkable
class HostAPI(Protocol):
    """插件能调用的宿主的全部能力。"""

    def stats(self) -> dict[str, Any]: ...

    def kinds(self) -> list[dict[str, Any]]: ...

    def statuses(self) -> list[dict[str, Any]]: ...

    def all_tags(self) -> list[str]: ...

    def count_items(self, **filters: Any) -> int: ...

    def list_items(self, **filters: Any) -> list[dict[str, Any]]: ...

    def get_item(self, item_id: str) -> dict[str, Any] | None: ...

    # ---- import 接口（写） ----
    def importer_names(self) -> list[str]: ...

    def import_file(self, importer: str, path: str | Path, *, archive: bool = True) -> dict[str, Any]: ...

    # ---- audio 接口（只读本地文件，写盘只在插件自己的数据目录里） ----
    def audio_suffixes(self) -> list[str]: ...

    def scan_audio(
        self,
        targets: Sequence[str | Path],
        *,
        out_path: str | Path,
        progress: AudioProgressFn | None = None,
    ) -> dict[str, Any]: ...


class RepositoryHostAPI:
    """默认实现：``query`` 走 repository，``import`` 走内置 importer 的导入链路。"""

    def __init__(
        self,
        engine: Engine,
        *,
        imports_dir: Path | None = None,
        max_limit: int = 500,
    ) -> None:
        self._engine = engine
        #: 原始文件存档目录（``data/imports``）；给了才会在导入时存档
        self.imports_dir = Path(imports_dir) if imports_dir is not None else None
        #: 插件是第三方代码：给列表查询加个上限，别让它一次把库拖出来
        self.max_limit = max_limit

    # ------------------------------------------------------------- import 接口
    def importer_names(self) -> list[str]:
        """宿主当前注册的 importer 名字（插件可以先看看有没有自己要的那个）。"""
        from ..importers.registry import load_builtin_importers, names

        load_builtin_importers()
        return list(names())

    def import_file(self, importer: str, path: str | Path, *, archive: bool = True) -> dict[str, Any]:
        """用内置 importer 导入一个文件（和 CLI/上传页同一条链路）。

        ``archive=True`` 时先把原始文件按来源存档到 ``data/imports/<来源>/``
        （和上传页一致，便于回溯）。返回导入结果摘要（普通 dict）。
        """
        from ..importers.registry import UnknownImporterError, load_builtin_importers
        from ..services.import_ import import_file as run_import_file

        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"要导入的文件不存在: {target}")
        load_builtin_importers()
        with session_scope(self._engine) as session:
            try:
                result = run_import_file(
                    session,
                    importer_name=importer,
                    path=target,
                    archive_dir=self.imports_dir if archive else None,
                )
            except UnknownImporterError as exc:
                raise ValueError(
                    f"没有这个 importer: {importer!r}"
                    f"（可选: {', '.join(self.importer_names())}）"
                ) from exc
            return result.as_dict() | {
                # 页面/CLI 常要直接显示这两个数（as_dict 里只有明细列表）
                "error_count": result.error_count,
                "warning_count": result.warning_count,
                "summary": result.summary(),
            }

    # ------------------------------------------------------------- audio 接口
    def audio_suffixes(self) -> list[str]:
        """宿主认作音乐文件的后缀（插件的文件选择框要用同一份名单）。"""
        from ..music_scan import AUDIO_SUFFIXES

        return sorted(AUDIO_SUFFIXES)

    def scan_audio(
        self,
        targets: Sequence[str | Path],
        *,
        out_path: str | Path,
        progress: AudioProgressFn | None = None,
    ) -> dict[str, Any]:
        """读一批本地音乐文件（目录会深度遍历）的标签，写成清单 JSON，返回摘要。

        ``out_path`` 由插件指定（应当在自己的数据目录里），返回的 ``manifest_path``
        直接交给 ``import_file("music_files", ...)`` 入库。扫描失败的文件只进摘要的
        ``errors``，不会抛——几千个文件里坏几个是常态。
        """
        from ..music_scan import scan, write_manifest

        result = scan(list(targets), progress=progress)
        manifest_path = write_manifest(result, out_path)
        return {
            "manifest_path": str(manifest_path),
            "roots": list(result.roots),
            "scanned": result.scanned,
            "item_count": len(result.items),
            "tagless": result.tagless,
            "failed": result.failed,
            "errors": list(result.errors),
            "errors_hidden": result.errors_hidden,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "summary": result.summary(),
        }

    # ------------------------------------------------------------- 汇总
    def stats(self) -> dict[str, Any]:
        with session_scope(self._engine) as session:
            return repo.stats(session)

    def kinds(self) -> list[dict[str, Any]]:
        labels = kind_labels()
        return [
            {
                "name": name,
                "label": labels.get(name, name),
                "fields": [
                    {"name": f.name, "label": f.label, "type": f.type, "required": f.required}
                    for f in KINDS[name].extra_fields
                ],
            }
            for name in KINDS
        ]

    def statuses(self) -> list[dict[str, Any]]:
        """可选状态 + 一个 ``unset``（和 ``stats()["by_status"]`` 的键一致）。"""
        items = [
            {"name": name, "label": STATUS_LABELS.get(name, name)} for name in STATUSES
        ]
        items.append(
            {"name": UNSET_STATUS, "label": STATUS_LABELS.get(UNSET_STATUS, UNSET_STATUS)}
        )
        return items

    def all_tags(self) -> list[str]:
        with session_scope(self._engine) as session:
            return repo.all_tags(session)

    # ------------------------------------------------------------- 条目
    def count_items(self, **filters: Any) -> int:
        with session_scope(self._engine) as session:
            return repo.count_items(session, **self._clean(filters))

    def list_items(self, **filters: Any) -> list[dict[str, Any]]:
        raw_limit = filters.pop("limit", None)
        limit = 100 if raw_limit is None else int(raw_limit)
        offset = int(filters.pop("offset", 0) or 0)
        limit = max(1, min(limit, self.max_limit))
        with session_scope(self._engine) as session:
            items = repo.list_items(
                session, **self._clean(filters), limit=limit, offset=offset
            )
            return [export_service.item_to_dict(session, item) for item in items]

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with session_scope(self._engine) as session:
            item = repo.get_item(session, item_id)
            if item is None:
                return None
            return export_service.item_to_dict(session, item)

    @staticmethod
    def _clean(filters: dict[str, Any]) -> dict[str, Any]:
        """只放行 repository 认识的过滤键，插件传错键不会 500。"""
        allowed = {
            "kind", "status", "tag", "title_prefix", "search", "creator",
            "rating_min", "rating_max", "created_from", "created_to",
            "tags_include", "tags_exclude", "include_deleted", "sort", "descending",
        }
        clean = {key: value for key, value in filters.items() if key in allowed and value is not None}
        for key in ("tags_include", "tags_exclude"):
            value = clean.get(key)
            if isinstance(value, str):
                clean[key] = (value,)
            elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
                clean[key] = tuple(value)
        return clean


#: 旧名字（1.0 时只有查询接口）保留为别名，避免 import 老名字的代码直接炸。
QueryHostAPI = RepositoryHostAPI
