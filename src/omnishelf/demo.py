"""``omnishelf demo``：伪造数据端到端演示。

流程：临时目录建库 → 导入 tests/fixtures 下的伪造数据 → 打印统计（各 kind 数量、
pending 数量）→ 导出 JSON → 清理。不依赖网络，也不依赖 data/ 里的真实数据。
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import repository as repo
from .config import load_settings
from .db import build_engine, current_version, init_db, session_scope
from .services import export_ as export_service
from .services import review as review_service
from .services.import_ import ImportResult, import_file

#: (importer 名, fixture 文件名)，顺序固定以保证输出可复现。
FIXTURE_PLAN: tuple[tuple[str, str], ...] = (
    ("json_movies", "movies.json"),
    ("json_books", "books.json"),
    ("json_movies", "malformed.json"),
)


@dataclass
class DemoResult:
    data_dir: Path
    db_path: Path
    export_path: Path
    schema_version: int
    imports: list[ImportResult] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)
    kept: bool = False

    @property
    def total_imported(self) -> int:
        return sum(r.total for r in self.imports)

    def as_dict(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
            "export_path": str(self.export_path),
            "schema_version": self.schema_version,
            "imports": [r.as_dict() for r in self.imports],
            "stats": self.stats,
            "pending": self.pending,
            "kept": self.kept,
        }


def run_demo(
    *,
    data_dir: Path | str | None = None,
    out_path: Path | str | None = None,
    keep: bool = False,
    fixtures_dir: Path | None = None,
    echo: Callable[[str], None] = print,
) -> DemoResult:
    """跑一遍完整流程。``data_dir=None`` 时使用临时目录。"""
    cleanup = data_dir is None
    base = Path(data_dir) if data_dir is not None else Path(tempfile.mkdtemp(prefix="omnishelf-demo-"))
    base = base.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)

    settings = load_settings(base)
    if fixtures_dir is not None:
        settings = replace(settings, fixtures_dir=Path(fixtures_dir).resolve())

    echo("== omnishelf demo ==")
    echo(f"[1/6] 数据目录: {settings.data_dir}")
    version = init_db(settings)
    echo(f"      建库完成: {settings.db_path}（schema user_version={version}）")

    fixtures = settings.fixtures_dir
    echo(f"[2/6] 导入伪造数据：{fixtures}")
    engine = build_engine(settings)
    imports: list[ImportResult] = []
    pending: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    export_path = Path(out_path).expanduser().resolve() if out_path else base / "export.json"

    try:
        with session_scope(engine) as session:
            for importer_name, filename in FIXTURE_PLAN:
                path = fixtures / filename
                if not path.is_file():
                    raise FileNotFoundError(
                        f"找不到 fixture: {path}（可用 OMNISHELF_FIXTURES_DIR 指定目录）"
                    )
                result = import_file(session, importer_name=importer_name, path=path)
                imports.append(result)
                echo(f"      {filename:<16} {result.summary()}")
                for entry in result.errors:
                    marker = "警告" if entry.get("level") == "warning" else "错误"
                    echo(f"        - [{marker}] 记录 {entry.get('record')}: {entry.get('message')}")

            stats = repo.stats(session)
            echo("[3/6] 统计")
            kinds = ", ".join(f"{k}={v}" for k, v in stats["by_kind"].items()) or "(空)"
            statuses = ", ".join(f"{k}={v}" for k, v in stats["by_status"].items()) or "(空)"
            echo(f"      kind:   {kinds}")
            echo(f"      status: {statuses}")
            echo(
                f"      items 总数 {stats['total']}，标签 {stats['tags']}，"
                f"有封面 {stats['with_cover']}，软删除 {stats['deleted']}，"
                f"待复核 {stats['pending_merges']}"
            )

            echo("[4/6] 待复核（模糊匹配，绝不自动合并）")
            views = review_service.list_pending(session)
            if not views:
                echo("      (无)")
            for view in views:
                item_title = _label(view.item)
                cand_title = _label(view.candidate)
                pending.append(
                    {
                        "id": view.id,
                        "item_id": view.pending.item_id,
                        "candidate_id": view.pending.candidate_id,
                        "reason": view.reason,
                    }
                )
                echo(f"      #{view.id}  {item_title}  <->  {cand_title}")
                echo(f"           {view.reason}")

            echo("[5/6] 导出 JSON")
            count = export_service.export_json(session, export_path)
            echo(f"      -> {export_path}（{count} 条）")
    finally:
        engine.dispose()

    if cleanup and not keep:
        echo(f"[6/6] 清理临时目录: {base}")
        shutil.rmtree(base, ignore_errors=True)
    else:
        echo(f"[6/6] 保留目录: {base}")

    return DemoResult(
        data_dir=base,
        db_path=settings.db_path,
        export_path=export_path,
        schema_version=current_version(settings.db_path) if settings.db_path.exists() else version,
        imports=imports,
        stats=stats,
        pending=pending,
        kept=(not cleanup) or keep,
    )


def _label(item: Any) -> str:
    if item is None:
        return "(已删除/缺失)"
    year = f"({item.year})" if item.year else ""
    return f"{item.id} {item.kind} {item.title!r}{year}"
