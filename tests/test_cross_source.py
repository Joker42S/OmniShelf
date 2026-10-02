"""源（source）维度的唯一性规则。

三层关系：

1. **本源命中**：``(source, external_id)`` 见过 → 更新（所有类型都一样）；
2. **跨源检查**：默认行为——先用标题等信息看「是不是别的来源的同一条目」，命中进待复核；
3. **漫画特例**：``KindDef.cross_source_duplicates=False`` → 只查同源内部，不跨源比较。

注意：这里的第二个来源是**合成的**（字段名、来源名、标题都是我编的），真实数据只有
picamanga 一家。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.importers.json_file import JsonFileImporter
from omnishelf.importers.registry import IMPORTERS, register
from omnishelf.kinds import KINDS, KindDef, register as register_kind
from omnishelf.services import review as review_service
from omnishelf.services.import_ import import_file

# 合成 fixture 的标题形态：日文 | 中文 + 翻译组后缀
PICA_TITLE = "星屑のカルテ | 星尘病历 [中国翻訳] [DL版]"
# 第二个来源常见的形态：只有中文、不带后缀
OTHER_TITLE = "星尘病历"
PICA_ID = "aaaa00000000000000000001"

#: 一个「可能出现跨源重复」的测试 kind（默认策略 True）
MULTI_KIND = "unit_multi_source"


@pytest.fixture(autouse=True)
def _multi_source_kind() -> Iterator[None]:
    register_kind(KindDef(MULTI_KIND, "测试多来源"))
    try:
        yield
    finally:
        KINDS.pop(MULTI_KIND, None)


def _other_source_file(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    path = tmp_path / "other_source.json"
    path.write_text(json.dumps({"items": records}, ensure_ascii=False), encoding="utf-8")
    return path


def _other_record(kind: str, title: str = OTHER_TITLE, uid: str = "os-0001") -> dict[str, object]:
    return {"name": title, "uuid": uid, "writer": "サンプル作者", "tags": ["漢化"]}


def _make_other_source_importer(kind: str) -> type[JsonFileImporter]:
    @register("unit_other_source")
    class FakeOtherSource(JsonFileImporter):
        """合成来源：字段名与 picamanga 完全不同，验证「换来源只换 mapping」。"""

        source = "unit_other_source"
        mapping = {
            "title": ["name"],
            "extra.author": ["writer"],
            "external_ids.other": ["uuid"],
            "tags": ["tags"],
        }

    FakeOtherSource.kind = kind
    return FakeOtherSource


# ------------------------------------------------------- kind 策略与来源集合


def test_manga_disables_cross_source_duplicates() -> None:
    assert KINDS["manga"].cross_source_duplicates is False
    # 其它 kind 默认允许跨源检查
    assert KINDS["movie"].cross_source_duplicates is True
    assert KINDS["book"].cross_source_duplicates is True
    assert KindDef("x", "X").cross_source_duplicates is True


def test_sources_by_item(session: Session, make_item) -> None:
    one = make_item(title="A", external_ids=[("s1", "1")])
    two = make_item(title="B", external_ids=[("s1", "2"), ("s2", "3")])
    none = make_item(title="C")

    mapping = repo.sources_by_item(session)
    assert mapping[one.id] == {"s1"}
    assert mapping[two.id] == {"s1", "s2"}
    assert none.id not in mapping  # 来源未知
    assert repo.sources_by_item(session, [one.id]) == {one.id: {"s1"}}
    assert repo.sources_by_item(session, []) == {}


def test_find_fuzzy_title_matches_scoped_to_source(session: Session, make_item) -> None:
    """同源条目绝不参与标题比对；非同源（别的来源 / 来源未知）才用标题。"""
    from_this_source = make_item(
        kind="manga", title=PICA_TITLE, external_ids=[("picamanga", "1")]
    )
    unknown = make_item(kind="manga", title=OTHER_TITLE)  # 来源未知（手工录入）

    # 同源：本条记录来自 picamanga，picamanga 的条目被排除，只剩来源未知的
    same = repo.find_fuzzy_title_matches(
        session, kind="manga", title=OTHER_TITLE, record_sources={"picamanga"}, cross_source=True
    )
    assert [item.id for item in same] == [unknown.id]

    # 换个来源：picamanga 的条目成了「非同源」-> 参与标题比对
    other = repo.find_fuzzy_title_matches(
        session, kind="manga", title=OTHER_TITLE, record_sources={"other"}, cross_source=True
    )
    assert {item.id for item in other} == {from_this_source.id, unknown.id}

    # 不跨源查重（漫画）：别的来源的条目也排除，只留来源未知的
    strict = repo.find_fuzzy_title_matches(
        session, kind="manga", title=OTHER_TITLE, record_sources={"other"}, cross_source=False
    )
    assert [item.id for item in strict] == [unknown.id]


def test_find_by_title_year_scoped_to_source(session: Session, make_item) -> None:
    item = make_item(kind="manga", title="同名作品", year=2020, external_ids=[("picamanga", "9")])
    assert repo.find_by_title_year(session, kind="manga", title="同名作品", year=2020) is not None

    # 同源：只认唯一标识 -> 规则 2 不命中（这条记录带着 picamanga 标识）
    assert (
        repo.find_by_title_year(
            session,
            kind="manga",
            title="同名作品",
            year=2020,
            record_sources={"picamanga"},
            cross_source=True,
        )
        is None
    )
    # 非同源 + 允许跨源：可以合并（跨来源是同一作品）
    assert (
        repo.find_by_title_year(
            session,
            kind="manga",
            title="同名作品",
            year=2020,
            record_sources={"other"},
            cross_source=True,
        )
        is not None
    )
    # 非同源但不跨源查重（漫画）：别的来源的条目也要排除
    assert (
        repo.find_by_title_year(
            session,
            kind="manga",
            title="同名作品",
            year=2020,
            record_sources={"other"},
            cross_source=False,
        )
        is None
    )
    # 来源未知的条目（手工录入）任何情况下都可合并，避免重复录入
    manual = make_item(kind="manga", title="同名作品", year=2020)
    found = repo.find_by_title_year(
        session,
        kind="manga",
        title="同名作品",
        year=2020,
        record_sources={"picamanga"},
        cross_source=False,
    )
    assert found is not None and found.id in {manual.id, item.id}


# ------------------------------------------------------------- 端到端：两个来源


def test_two_sources_review_then_merge_then_both_are_idempotent(
    session: Session, tmp_path: Path, import_records
) -> None:
    """允许跨源查重的 type：进待复核 → 人工确认 → 合并 → 双向幂等。"""
    _make_other_source_importer(MULTI_KIND)
    try:
        # 1) 先接来源 A
        first = import_records(
            [
                {
                    "kind": MULTI_KIND,
                    "title": PICA_TITLE,
                    "extra": {"author": "サンプル作者 (sample_a)", "finished": True},
                    "tags": ["同人"],
                    "external_ids": [("picamanga", PICA_ID)],
                }
            ]
        )
        assert (first.inserted, first.pending) == (1, 0)
        kept = repo.list_items(session, kind=MULTI_KIND)[0]

        # 2) 来源 B 标题不同 -> 进待复核，绝不自动合并
        second = import_file(
            session,
            importer_name="unit_other_source",
            path=_other_source_file(tmp_path, [_other_record(MULTI_KIND)]),
        )
        assert (second.inserted, second.pending, second.candidates) == (0, 1, 1)
        assert len(repo.list_items(session, kind=MULTI_KIND)) == 2  # 候选已落库

        views = review_service.list_pending(session)
        assert len(views) == 1
        assert views[0].item is not None and views[0].item.id == kept.id

        # 3) 人工确认是同一条 -> 合并
        outcome = review_service.merge(session, views[0].id, kept.id)
        assert outcome.moved_tags == 1
        merged = repo.require_item(session, kept.id)
        assert {link.source for link in repo.list_external_ids(session, merged.id)} == {
            "picamanga",
            "other",
        }
        assert repo.count_items(session) == 1

        # 4) 两边再各导一次：都命中本源，不再产生候选
        again_a = import_records(
            [
                {
                    "kind": MULTI_KIND,
                    "title": PICA_TITLE,
                    "extra": {"author": "サンプル作者 (sample_a)", "finished": True},
                    "tags": ["同人"],
                    "external_ids": [("picamanga", PICA_ID)],
                }
            ]
        )
        again_b = import_file(
            session,
            importer_name="unit_other_source",
            path=_other_source_file(tmp_path, [_other_record(MULTI_KIND)]),
        )
        assert again_a.pending == 0 and again_b.pending == 0
        assert again_a.inserted == 0 and again_b.inserted == 0
        assert repo.count_items(session) == 1
        assert repo.list_tags(session, merged.id) == ["同人", "漢化"]
    finally:
        IMPORTERS.pop("unit_other_source", None)


def test_manga_only_checks_same_source(session: Session, tmp_path: Path, import_records) -> None:
    """漫画：同源只认唯一标识（不同标识=不同作品），别的来源更是完全不比。"""
    _make_other_source_importer("manga")
    try:
        first = import_records(
            [{"kind": "manga", "title": PICA_TITLE, "external_ids": [("picamanga", PICA_ID)]}]
        )
        assert first.inserted == 1

        # 别的来源、标题相似 -> 漫画不跨源查重 -> 直接新增，不进待复核
        second = import_file(
            session,
            importer_name="unit_other_source",
            path=_other_source_file(tmp_path, [_other_record("manga")]),
        )
        assert (second.inserted, second.pending, second.candidates) == (1, 0, 0)

        # 同一个来源、标题相似但 comicId 不同 -> 也是不同作品，直接新增
        renamed = tmp_path / "pica_renamed.json"
        renamed.write_text(
            json.dumps({"items": [{"title": OTHER_TITLE, "id": "pica-0002"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        same_source = import_file(
            session,
            importer_name="json_movies",
            path=renamed,
            kind="manga",
            source="picamanga",
            mapping={"title": ["title"], "external_ids.picamanga": ["id"]},
        )
        assert (same_source.inserted, same_source.pending) == (1, 0)
        assert repo.count_pending_merges(session) == 0
        assert len(repo.list_items(session, kind="manga")) == 3
    finally:
        IMPORTERS.pop("unit_other_source", None)


def test_same_source_external_id_still_updates(session: Session, import_records) -> None:
    """所有类型的第一层都是「本源命中就更新」。"""
    record = {"kind": "manga", "title": PICA_TITLE, "external_ids": [("picamanga", PICA_ID)]}
    assert import_records([record]).inserted == 1
    again = import_records([record])
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 1)
    changed = import_records([{**record, "rating": 8.0}])
    assert (changed.updated, changed.unchanged) == (1, 0)
    assert repo.list_items(session, kind="manga")[0].rating == 8.0


def test_different_work_is_not_flagged(session: Session, import_records) -> None:
    """不同作品不能因为放宽匹配而互相命中。"""
    import_records(
        [{"kind": "manga", "title": "雨上がりの図書館", "external_ids": [("picamanga", "p1")]}]
    )
    result = import_records(
        [{"kind": "manga", "title": "陽だまり食堂", "external_ids": [("other", "c1")]}]
    )
    assert result.inserted == 1
    assert result.pending == 0
    assert repo.count_pending_merges(session) == 0


# ------------------------------------------------------------- 标题匹配本身


def test_title_keys_strips_brackets_and_splits() -> None:
    keys = repo.title_keys(PICA_TITLE)
    assert "星屑のカルテ" in keys
    assert "星尘病历" in keys
    assert "中国翻訳" not in keys  # 括号里的内容被去掉


def test_titles_match_across_sources() -> None:
    assert repo.titles_match(PICA_TITLE, OTHER_TITLE) is True
    assert repo.titles_match("作品X [DL版]", "作品X [中国翻訳]") is True
    assert repo.titles_match("Spirited Away", "spirited-away ") is True
    assert repo.titles_match("Spirited Away", "Perfect Blue") is False


def test_titles_match_containment_guard() -> None:
    assert repo.titles_match("The Left Hand of Darkness", "Left Hand of Darkness") is True
    assert repo.titles_match("Dune", "Dune Messiah") is False
    assert repo.MIN_CONTAINMENT_LEN == 6


def test_find_fuzzy_matches_is_scoped_to_kind(session: Session, make_item) -> None:
    make_item(kind="manga", title=PICA_TITLE)
    assert len(repo.find_fuzzy_title_matches(session, kind="manga", title=OTHER_TITLE)) == 1
    assert repo.find_fuzzy_title_matches(session, kind="book", title=OTHER_TITLE) == []


def test_same_source_same_title_different_ids_are_different_items(
    session: Session, import_records
) -> None:
    """同源两条记录各有 comicId（= 来源说这是两条）：标题完全相同也各存各的。"""
    first = import_records(
        [
            {
                "kind": "manga",
                "title": PICA_TITLE,
                "extra": {"author": "作者A", "chapter_count": 1},
                "external_ids": [("picamanga", "same-1")],
            }
        ]
    )
    assert (first.inserted, first.updated, first.pending) == (1, 0, 0)

    second = import_records(
        [
            {
                "kind": "manga",
                "title": PICA_TITLE,  # 标题完全相同
                "extra": {"author": "作者B", "chapter_count": 2},
                "external_ids": [("picamanga", "same-2")],  # 但 comicId 不同
            }
        ]
    )
    assert (second.inserted, second.updated, second.pending, second.candidates) == (1, 0, 0, 0)
    assert len(repo.list_items(session, kind="manga")) == 2
    assert repo.count_pending_merges(session) == 0

    # 两条各自的字段都没被对方覆盖
    items = {
        repo.list_external_ids(session, i.id)[0].external_id: i
        for i in repo.list_items(session, kind="manga")
    }
    assert items["same-1"].extra["author"] == "作者A"
    assert items["same-2"].extra["author"] == "作者B"

    # 再导一次：两条都命中本源，全无变化（不会互相覆盖）
    again = import_records(
        [
            {
                "kind": "manga",
                "title": PICA_TITLE,
                "extra": {"author": "作者A", "chapter_count": 1},
                "external_ids": [("picamanga", "same-1")],
            },
            {
                "kind": "manga",
                "title": PICA_TITLE,
                "extra": {"author": "作者B", "chapter_count": 2},
                "external_ids": [("picamanga", "same-2")],
            },
        ]
    )
    assert (again.updated, again.unchanged, again.pending) == (0, 2, 0)
    items2 = {
        repo.list_external_ids(session, i.id)[0].external_id: i
        for i in repo.list_items(session, kind="manga")
    }
    assert items2["same-1"].extra["author"] == "作者A"
    assert items2["same-2"].extra["author"] == "作者B"
