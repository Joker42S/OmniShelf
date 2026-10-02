"""导入的原始文件按来源存档（data/imports/<source>/<文件名>）。"""

from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.config import Settings
from omnishelf.services.import_ import archive_source_file, import_file


def _write(path: Path, count: int = 2) -> Path:
    path.write_text(
        json.dumps({"items": [{"title": f"T{n}", "comicId": f"c{n}"} for n in range(count)]}),
        encoding="utf-8",
    )
    return path


def test_archive_source_file_copies_by_source(tmp_path: Path) -> None:
    src = _write(tmp_path / "backup.json")
    imports = tmp_path / "data" / "imports"
    target = archive_source_file(imports, "picamanga", src)

    assert target == imports / "picamanga" / "backup.json"
    assert target is not None and target.read_bytes() == src.read_bytes()


def test_archive_source_file_is_noop_for_same_path(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    (imports / "picamanga").mkdir(parents=True)
    src = _write(imports / "picamanga" / "backup.json")
    before = src.stat().st_mtime_ns
    assert archive_source_file(imports, "picamanga", src) == src
    assert src.stat().st_mtime_ns == before  # 没有自己覆盖自己


def test_archive_source_file_missing_file(tmp_path: Path) -> None:
    assert archive_source_file(tmp_path / "imports", "picamanga", tmp_path / "nope.json") is None


def test_archive_overwrites_same_name(tmp_path: Path) -> None:
    """同一来源同一文件名：新的覆盖旧的（旧版本留在 git 历史里）。"""
    imports = tmp_path / "imports"
    first = archive_source_file(imports, "picamanga", _write(tmp_path / "a.json", 1))
    second = archive_source_file(imports, "picamanga", _write(tmp_path / "a.json", 3))
    assert first == second == imports / "picamanga" / "a.json"
    assert second is not None
    assert len(json.loads(second.read_text(encoding="utf-8"))["items"]) == 3


def test_import_file_archives_and_reports_path(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    src = _write(tmp_path / "movies_like.json", 2)
    result = import_file(
        session,
        importer_name="json_movies",
        path=src,
        archive_dir=settings.imports_dir,
    )
    assert (result.total, result.inserted) == (2, 2)
    assert result.archive_path == "imports/json_movies/movies_like.json"
    assert (settings.data_dir / "imports" / "json_movies" / "movies_like.json").is_file()
    # 记录里也能看到存档信息（--json 输出用）
    assert result.as_dict()["archive_path"] == result.archive_path


def test_import_without_archive_dir_does_not_archive(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    result = import_file(session, importer_name="json_movies", path=_write(tmp_path / "x.json", 1))
    assert result.archive_path is None
    assert not settings.imports_dir.exists()


def test_reimport_archived_file_is_fine(session: Session, settings: Settings, tmp_path: Path) -> None:
    """导入存档过的文件本身：不报错、不会再复制一份，数据幂等。"""
    src = _write(tmp_path / "b.json", 2)
    import_file(session, importer_name="json_movies", path=src, archive_dir=settings.imports_dir)
    archived = settings.imports_dir / "json_movies" / "b.json"

    again = import_file(
        session, importer_name="json_movies", path=archived, archive_dir=settings.imports_dir
    )
    assert again.inserted == 0
    assert again.archive_path == "imports/json_movies/b.json"
    assert len(repo.list_items(session, kind="movie")) == 2
