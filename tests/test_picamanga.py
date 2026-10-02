"""picamanga importer（离线；用合成 fixture，字段与真实导出严格同构）。

真实导出格式是 19 个字段的 items 数组；``test_fixture_matches_real_schema``
会守住这一点——将来来源改版，这里先红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.importers.picamanga import join_cover_url
from omnishelf.importers.registry import load_builtin_importers, names
from omnishelf.services.import_ import import_file

#: 真实 picamanga 导出里每条记录的字段集合（2026-10 实测样本）。
REAL_SCHEMA_KEYS = {
    "comicId",
    "title",
    "author",
    "thumbFileServer",
    "thumbPath",
    "thumbOriginalName",
    "categories",
    "groupId",
    "knownEpsCount",
    "knownLatestOrder",
    "remoteUpdatedAt",
    "finished",
    "checkEnabled",
    "hasNewChapter",
    "latestChapterTitle",
    "latestChapterOrder",
    "pinnedAt",
    "addedAt",
    "updatedAt",
}


@pytest.fixture()
def picamanga_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "picamanga.json"


def test_importer_is_registered() -> None:
    load_builtin_importers()
    assert "picamanga" in names()


def test_fixture_matches_real_schema(picamanga_path: Path) -> None:
    payload = json.loads(picamanga_path.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 1
    assert isinstance(payload["exportedAt"], int)
    assert payload["groups"][0]["id"] == "default"
    for row in payload["items"]:
        assert set(row) == REAL_SCHEMA_KEYS, "fixture 必须与真实导出的字段集合一致"
        assert isinstance(row["categories"], list)
        assert isinstance(row["finished"], bool)
        assert isinstance(row["addedAt"], int)
        assert len(row["comicId"]) == 24
        assert row["comicId"].startswith("aaaa")  # 合成 ID，不是真实数据的 ID


def test_import_fixture(session: Session, picamanga_path: Path) -> None:
    result = import_file(session, importer_name="picamanga", path=picamanga_path)
    assert result.total == 6
    assert result.inserted == 6
    assert result.error_count == 0
    assert result.warning_count == 0  # kinds.py 已声明所有用到的 extra 字段
    assert repo.stats(session)["by_kind"] == {"manga": 6}


def test_field_mapping(session: Session, picamanga_path: Path) -> None:
    import_file(session, importer_name="picamanga", path=picamanga_path)
    item = next(i for i in repo.list_items(session, kind="manga") if i.title.startswith("星屑"))

    # 标题原样保留（日文 | 中文 + 方括号后缀都不动）
    assert item.title == "星屑のカルテ | 星尘病历 [中国翻訳] [DL版]"
    assert item.kind == "manga"
    assert item.creators == ["サンプル作者 (sample_a)"]
    assert item.extra["finished"] is True
    assert item.extra["chapter_count"] == 1
    assert item.extra["latest_chapter"] == "第1話"
    assert item.extra["remote_updated_at"] == "2026-09-20T05:27:03.394Z"
    assert item.extra["has_new_chapter"] is False
    assert repo.list_tags(session, item.id) == ["全彩", "同人"]
    links = repo.list_external_ids(session, item.id)
    assert [(link.source, link.external_id) for link in links] == [
        ("picamanga", "aaaa00000000000000000001")
    ]


def test_cover_url_is_joined_from_two_fields(session: Session, picamanga_path: Path) -> None:
    import_file(session, importer_name="picamanga", path=picamanga_path)
    item = next(i for i in repo.list_items(session, kind="manga") if i.title.startswith("星屑"))
    assert item.extra["cover_url"] == (
        "https://storage-b.picacomic.com/tobeimg/EXAMPLEtoken/"
        "rs:fill:300:400:0/g:sm/ZXhhbXBsZS5qcGc=.jpg"
    )


def test_nullable_source_fields_are_omitted(session: Session, picamanga_path: Path) -> None:
    import_file(session, importer_name="picamanga", path=picamanga_path)
    items = {i.title: i for i in repo.list_items(session, kind="manga")}

    no_remote = items["銀河鉄道の夜想曲"]  # remoteUpdatedAt: null
    assert "remote_updated_at" not in no_remote.extra
    assert no_remote.extra["latest_chapter"] == "第4話"

    no_chapter = items["きみと歩く季節 | 与你同行的季节"]  # latestChapterTitle: null
    assert "latest_chapter" not in no_chapter.extra
    assert no_chapter.extra["finished"] is False
    assert no_chapter.extra["remote_updated_at"] == "2024-06-10T01:50:48.319Z"


def test_this_source_has_no_year_rating_or_status(session: Session, picamanga_path: Path) -> None:
    """picamanga 导出里没有年份/评分/阅读状态，这三项必须是空的，不能瞎猜。"""
    import_file(session, importer_name="picamanga", path=picamanga_path)
    for item in repo.list_items(session, kind="manga"):
        assert item.year is None
        assert item.rating is None
        assert item.status is None


def test_many_categories_become_tags(session: Session, picamanga_path: Path) -> None:
    import_file(session, importer_name="picamanga", path=picamanga_path)
    item = next(i for i in repo.list_items(session, kind="manga") if i.title == "雨上がりの図書館")
    assert repo.list_tags(session, item.id) == [
        "全彩",
        "單行本",
        "姐姐系",
        "扶他樂園",
        "短篇",
        "純愛",
    ]
    assert len(repo.list_items(session, tag="短篇")) >= 1


def test_reimport_is_idempotent(session: Session, picamanga_path: Path) -> None:
    import_file(session, importer_name="picamanga", path=picamanga_path)
    again = import_file(session, importer_name="picamanga", path=picamanga_path)
    assert again.inserted == 0
    assert again.updated == 0
    assert again.unchanged == 6
    assert len(repo.list_items(session, kind="manga")) == 6


def test_truncated_export_is_reported_not_crashed(session: Session, tmp_path: Path) -> None:
    """真实场景：导出文件被截断（样本就是这样），应当整批记一条 error 而不是崩溃。"""
    path = tmp_path / "truncated.json"
    path.write_text('{"schemaVersion":1,"items":[{"comicId":"a","title":"A"},{"comicId":"b"', encoding="utf-8")
    result = import_file(session, importer_name="picamanga", path=path)
    assert result.total == 0
    assert result.error_count == 1
    assert "fetch 失败" in result.errors[0]["message"]
    assert "JSON 解析失败" in result.errors[0]["message"]


def test_join_cover_url_helper() -> None:
    assert join_cover_url("https://a.com/", "/x/y.jpg") == "https://a.com/x/y.jpg"
    assert join_cover_url("https://a.com", "x.jpg") == "https://a.com/x.jpg"
    assert join_cover_url(None, "x.jpg") is None
    assert join_cover_url("https://a.com", None) is None
    assert join_cover_url("", "") is None
