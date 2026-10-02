"""copymanga importer（离线；合成 fixture，字段与真实导出严格同构）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.importers.base import DictRawRecord
from omnishelf.importers.copymanga import CopymangaImporter, as_creator_list, chapter_name
from omnishelf.importers.registry import load_builtin_importers, names
from omnishelf.services.import_ import import_file

#: 真实 copymanga 导出里每条记录的字段集合（2026-10 实测 392 条）。
REAL_ITEM_KEYS = {
    "collect_id", "name", "comic_uuid", "path_word", "url", "cover", "authors", "themes",
    "status", "status_label", "popular", "last_chapter", "last_browse", "comic_updated_at",
    "folder", "free_type", "kind", "shelf_position", "first_seen", "last_seen",
    "removed", "removed_at", "raw",
}
REAL_TOP_KEYS = {
    "source", "host", "user", "updated_at", "last_full_sync", "schema_version", "created_at",
    "ordering", "shelf_watermark", "shelf_top_position", "total", "active_total", "items",
}


@pytest.fixture()
def copymanga_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "copymanga.json"


def _record(**overrides: object) -> DictRawRecord:
    payload: dict[str, object] = {
        "name": "T",
        "authors": ["A"],
        "themes": [],
        "comic_uuid": "00000000-0000-4000-8000-000000000000",
        "url": "https://copy4000.com/comics/x",
        "cover": "https://c/x.jpg",
        "status_label": "連載中",
        "last_chapter": {"id": "1", "name": "第1話", "url": "u"},
        "last_browse": {"id": "", "name": ""},
        "comic_updated_at": "2026-01-01",
    }
    payload.update(overrides)
    return DictRawRecord(source="copymanga", payload=payload)


def test_importer_is_registered() -> None:
    load_builtin_importers()
    assert "copymanga" in names()


def test_fixture_matches_real_schema(copymanga_path: Path) -> None:
    payload = json.loads(copymanga_path.read_text(encoding="utf-8"))
    assert set(payload) == REAL_TOP_KEYS
    assert payload["schema_version"] == 3
    assert payload["total"] == payload["active_total"] == len(payload["items"])
    for row in payload["items"]:
        assert set(row) == REAL_ITEM_KEYS, "fixture 必须与真实导出的字段集合一致"
        assert isinstance(row["authors"], list) and isinstance(row["themes"], list)
        assert isinstance(row["last_chapter"], dict) and isinstance(row["last_browse"], dict)
        assert isinstance(row["removed"], bool)


def test_import_fixture(session: Session, copymanga_path: Path) -> None:
    result = import_file(session, importer_name="copymanga", path=copymanga_path)
    assert (result.total, result.inserted, result.error_count, result.warning_count) == (6, 6, 0, 0)
    assert repo.stats(session)["by_kind"] == {"manga": 6}


def test_field_mapping(session: Session, copymanga_path: Path) -> None:
    import_file(session, importer_name="copymanga", path=copymanga_path)
    item = next(i for i in repo.list_items(session, kind="manga") if i.title == "銀河食堂の夜")

    assert item.creators == ["サンプル作者B", "サンプル作者C"]  # 多值：直接是列表
    assert item.extra["status_label"] == "連載中"
    assert item.extra["finished"] is False
    assert item.extra["latest_chapter"] == "第12話"
    assert item.extra["remote_updated_at"] == "2026-07-30"
    assert item.extra["cover_url"].startswith("https://cover.copy4000.com/")
    assert "reading_progress" not in item.extra  # 这条没有阅读进度（空字符串 -> 不写）

    links = repo.list_external_ids(session, item.id)
    assert [(link.source, link.external_id) for link in links] == [
        ("copymanga", "00000002-0000-4000-8000-000000000002")
    ]
    assert links[0].url == "https://copy4000.com/comics/sample-path-2"  # 漫画页地址


def test_tags_from_themes(session: Session, copymanga_path: Path) -> None:
    import_file(session, importer_name="copymanga", path=copymanga_path)
    item = next(i for i in repo.list_items(session, kind="manga") if i.title == "雨の日の図書館")
    assert repo.list_tags(session, item.id) == ["戀愛", "日常"]  # list_tags 按 tag 排序
    assert item.extra["reading_progress"] == "第2話"
    assert len(repo.list_items(session, tag="戀愛")) == 1


def test_finished_is_derived_from_status_label(session: Session) -> None:
    importer = CopymangaImporter(Path("unused.json"))
    for label, expected in (("已完結", True), ("短篇", True), ("連載中", False)):
        draft = importer.normalize(_record(status_label=label))
        assert draft.extra["finished"] is expected, label
    # 未知状态不猜：字段直接不写，记录仍然导入
    draft = importer.normalize(_record(status_label="休載中"))
    assert "finished" not in draft.extra
    assert draft.extra["status_label"] == "休載中"


def test_non_date_updated_at_is_kept_as_text(session: Session) -> None:
    draft = CopymangaImporter(Path("unused.json")).normalize(_record(comic_updated_at="2026-01-01"))
    assert draft.extra["remote_updated_at"] == "2026-01-01"


def test_this_source_has_no_year_rating_or_status(session: Session, copymanga_path: Path) -> None:
    import_file(session, importer_name="copymanga", path=copymanga_path)
    for item in repo.list_items(session, kind="manga"):
        assert item.year is None and item.rating is None and item.status is None


def test_blank_dict_fields_are_skipped(session: Session) -> None:
    """空字符串的 name / 空 dict 都不写进 extra。"""
    draft = CopymangaImporter(Path("unused.json")).normalize(
        _record(last_chapter={}, last_browse={"id": "", "name": ""})
    )
    assert "latest_chapter" not in draft.extra
    assert "reading_progress" not in draft.extra
    # 真实数据里第 6 条的 last_chapter.name 就是空字符串
    assert chapter_name({"name": ""}) is None
    assert chapter_name(None) is None


def test_reimport_is_idempotent(session: Session, copymanga_path: Path) -> None:
    import_file(session, importer_name="copymanga", path=copymanga_path)
    again = import_file(session, importer_name="copymanga", path=copymanga_path)
    assert again.inserted == 0 and again.updated == 0 and again.unchanged == 6
    assert len(repo.list_items(session, kind="manga")) == 6


def test_helpers() -> None:
    assert as_creator_list(["A", "B"]) == ["A", "B"]
    assert as_creator_list(["A", " ", "B"]) == ["A", "B"]
    assert as_creator_list([]) == []
    assert as_creator_list(None) == []
    assert as_creator_list("单人") == ["单人"]
    assert as_creator_list("A / B") == ["A", "B"]  # 来源自己拼过的情况
    assert chapter_name({"name": " 第3話 "}) == "第3話"
    assert chapter_name("not-a-dict") is None
