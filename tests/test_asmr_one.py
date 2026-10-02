"""asmr_one importer：asmr.one 的播放列表导出（``{"works": [...]}``）。

夹具是**合成数据**（`tests/fixtures/asmr_works.json`）：结构与真实导出一致，
标题/社团/声优/编号都是编的（仓库公开，不能用真实作品信息）。
"""

from __future__ import annotations

import json
from typing import Any
from pathlib import Path

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.importers.asmr_one import (
    AsmrOneImporter,
    edition_labels,
    optional_editions,
    ja_tag_names,
    minutes_from_seconds,
    rating_from_user,
    year_from_release,
)
from omnishelf.importers.base import ItemDraft, NormalizeError
from omnishelf.importers.registry import get, load_builtin_importers
from omnishelf.kinds import get_kind
from omnishelf.services.import_ import import_file

FIXTURE = Path(__file__).parent / "fixtures" / "asmr_works.json"


# --------------------------------------------------------------------- 注册/类型


def test_importer_and_kind_are_registered() -> None:
    load_builtin_importers()
    importer: Any = get("asmr_one")
    assert importer.source == "asmr_one" and importer.kind == "asmr"
    kind = get_kind("asmr")
    assert kind.label == "ASMR"
    assert {field.name for field in kind.extra_fields} >= {
        "release_date", "duration_minutes", "languages", "has_subtitle",
        "nsfw", "age_category", "price_jpy", "source_type", "playlist_added_at",
        "cover_url",
    }


def test_accepts_a_works_container() -> None:
    load_builtin_importers()
    importer_cls: Any = get("asmr_one")
    importer = importer_cls(FIXTURE)
    assert len(list(importer.fetch())) == 2  # {"works": [...]} 也能读


# ----------------------------------------------------------------- 换算函数


def test_ja_tag_names_prefers_japanese_and_falls_back() -> None:
    assert ja_tag_names([
        {"i18n": {"ja-jp": {"name": "潮吹き"}, "zh-cn": {"name": "潮吹"}}, "name": "潮吹"},
        {"name": "i18n 缺失"},  # 没 i18n -> 用 name
        {"i18n": {"zh-cn": {"name": "没有日文"}}, "name": "兜底"},
        "不是 dict",
    ]) == ["潮吹き", "i18n 缺失", "兜底"]
    assert ja_tag_names(None) == []


def test_year_and_rating_conversions() -> None:
    assert year_from_release("2024-03-05") == 2024
    assert year_from_release("") is None and year_from_release(None) is None
    assert year_from_release("不是日期") is None

    assert rating_from_user(5) == 10.0  # 1~5 星 ×2
    assert rating_from_user(4.5) == 9.0
    assert rating_from_user(None) is None
    assert rating_from_user(0) is None
    assert rating_from_user("") is None
    assert rating_from_user(9) == 10.0  # 超出范围夹到 5 星


def test_duration_and_editions() -> None:
    assert minutes_from_seconds(5400) == 90
    assert minutes_from_seconds(0) is None and minutes_from_seconds(None) is None
    assert edition_labels([
        {"label": "簡体中文", "display_order": 5},
        {"label": "日本語", "display_order": 1},
        {"label": "英語", "display_order": 3},
        {"label": "日本語", "display_order": 1},  # 重复的丢掉
    ]) == ["日本語", "英語", "簡体中文"]
    assert edition_labels([]) == []
    assert optional_editions([]) is None  # 空的不写进 extra
    assert optional_editions([{"label": "日本語"}]) == ["日本語"]


# ------------------------------------------------------------------- 字段映射


@pytest.fixture()
def drafts() -> list[ItemDraft]:
    load_builtin_importers()
    importer_cls: Any = get("asmr_one")
    importer = importer_cls(FIXTURE)
    return [importer.normalize(raw) for raw in importer.fetch()]


def test_first_record_maps_every_field(drafts: list[ItemDraft]) -> None:
    draft = drafts[0]
    assert draft.kind == "asmr"
    assert draft.title.startswith("【サンプル】")
    # 社团 + 两个声优都是创作者，社团在前
    assert draft.creators == ["サンプルサークル", "サンプル声優A", "サンプル声優B"]
    assert draft.year == 2024
    assert draft.rating == 10.0  # userRating = 5 星
    assert draft.notes == "示例评论：声音很有辨识度。"
    # 标签用日文原名（第三个标签的 name 是旧的中文名，i18n 里有日文）
    assert draft.tags == ["学校/学園", "潮吹き", "オホ声", "i18n 缺失的标签"]

    assert {link.source: link.external_id for link in draft.external_ids} == {
        "asmr_one": "900001",
        "dlsite": "RJ09000001",
    }
    assert all(link.url for link in draft.external_ids)

    extra = draft.extra
    assert extra["release_date"] == "2024-03-05"
    assert extra["duration_minutes"] == 90
    assert extra["languages"] == ["日本語", "英語", "簡体中文"]
    assert extra["has_subtitle"] is False
    assert extra["nsfw"] is True
    assert extra["age_category"] == "adult"
    assert extra["price_jpy"] == 990
    assert extra["source_type"] == "DLSITE"
    assert extra["playlist_added_at"] == "2025-01-02 03:04:05"
    assert extra["cover_url"].endswith("900001.jpg?type=main")
    # 大众评分/排行/公开统计一概不进来
    for noisy in ("rate_average_2dp", "rate_count", "rank", "dl_count", "review_count",
                  "work_attributes", "translation_info", "other_language_editions_in_db"):
        assert noisy not in extra


def test_second_record_handles_missing_bits(drafts: list[ItemDraft]) -> None:
    draft = drafts[1]
    assert draft.creators == ["サンプルサークル乙", "サンプル声優C"]
    assert draft.rating is None  # userRating = null -> 不打分
    assert draft.notes is None
    assert draft.tags == []
    assert "duration_minutes" not in draft.extra  # duration=0 -> 不写这个字段
    assert "languages" not in draft.extra  # 空列表不写
    assert draft.extra["has_subtitle"] is True
    assert draft.extra["nsfw"] is False


def test_dedupe_by_either_external_id(session: Session) -> None:
    """同一个作品再导一次是按编号更新，不会新增；DLsite 编号也能认出来。"""
    load_builtin_importers()
    first = import_file(session, importer_name="asmr_one", path=FIXTURE)
    session.commit()
    assert first.inserted == 2 and first.total == 2

    again = import_file(session, importer_name="asmr_one", path=FIXTURE)
    session.commit()
    assert again.inserted == 0 and again.unchanged == 2

    items = {item.title: item for item in repo.list_items(session, kind="asmr")}
    assert len(items) == 2
    one = next(item for item in items.values() if item.title.endswith("其の一"))
    assert one.creators[0] == "サンプルサークル"
    assert one.rating == 10.0
    assert repo.list_tags(session, one.id) == ["i18n 缺失的标签", "オホ声", "学校/学園", "潮吹き"]
    assert repo.get_item_by_external_id(session, "dlsite", "RJ09000001") is not None
    assert repo.get_item_by_external_id(session, "asmr_one", "900002") is not None


def test_record_without_title_fails_loudly(tmp_path: Path) -> None:
    load_builtin_importers()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"works": [{"id": 1, "name": "只有社团"}]}), encoding="utf-8")
    importer_cls: Any = get("asmr_one")
    importer = importer_cls(bad)
    with pytest.raises(NormalizeError) as excinfo:
        for raw in importer.fetch():
            importer.normalize(raw)
    assert "title" in str(excinfo.value)
