"""映射引擎（空值/换算）与 CSV importer。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.importers.base import DictRawRecord, NormalizeError
from omnishelf.importers.csv_file import CsvFileImporter
from omnishelf.importers.json_file import JsonFileImporter
from omnishelf.importers.mapping import MappingImporter, is_blank
from omnishelf.importers.registry import IMPORTERS, register
from omnishelf.services.import_ import import_file


def _blank_record(**payload: object) -> DictRawRecord:
    return DictRawRecord(source="unit", payload=dict(payload))


def _importer(tmp_path: Path, **kwargs: object) -> JsonFileImporter:
    path = tmp_path / "unused.json"
    path.write_text("[]", encoding="utf-8")
    return JsonFileImporter(path, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 空值语义


def test_is_blank_rules() -> None:
    assert is_blank(None) is True
    assert is_blank("") is True
    assert is_blank("   ") is True
    # 0 / False / 空列表都是有效数据，不能当空
    assert is_blank(0) is False
    assert is_blank(False) is False
    assert is_blank([]) is False


def test_blank_value_falls_through_to_next_candidate(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path,
        kind="movie",
        mapping={"title": ["title", "name"], "year": ["year", "release_year"]},
    )
    draft = importer.normalize(_blank_record(title="", name="Real Title", year="  ", release_year=1999))
    assert draft.title == "Real Title"
    assert draft.year == 1999


def test_zero_is_not_treated_as_missing(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path, kind="movie", mapping={"title": ["t"], "year": ["y"], "rating": ["r"]}
    )
    draft = importer.normalize(_blank_record(t="X", y=0, r=0))
    assert draft.year == 0
    assert draft.rating == 0.0


def test_all_blank_optional_fields_become_none(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path,
        kind="movie",
        mapping={"title": ["title"], "year": ["year"], "rating": ["rating"], "notes": ["notes"]},
    )
    draft = importer.normalize(_blank_record(title="X", year="", rating="", notes="   "))
    assert draft.year is None and draft.rating is None and draft.notes is None


def test_skip_blank_can_be_disabled(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path,
        kind="movie",
        mapping={"title": ["title", "name"]},
        skip_blank=False,
    )
    with pytest.raises(NormalizeError):
        importer.normalize(_blank_record(title="", name="Ignored"))


# ---------------------------------------------------------------------- 换算


def test_transform_callable(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path,
        kind="movie",
        mapping={"title": ["t"], "rating": ["stars"]},
        transforms={"rating": lambda v: float(v) * 2},  # 5 星 -> 0-10
    )
    draft = importer.normalize(_blank_record(t="X", stars="4.5"))
    assert draft.rating == 9.0


def test_transform_table_and_passthrough(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path,
        kind="movie",
        mapping={"title": ["t"], "status": ["state"]},
        transforms={"status": {"看过": "done", "在看": "doing"}},
    )
    assert importer.normalize(_blank_record(t="A", state="看过")).status == "done"
    # 表里没有的值原样透传，最后由 ItemDraft 校验并报错
    with pytest.raises(NormalizeError) as excinfo:
        importer.normalize(_blank_record(t="B", state="不知道"))
    assert "status" in str(excinfo.value)


def test_transform_on_nested_target(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path,
        kind="movie",
        mapping={"title": ["t"], "extra.cast": ["cast"]},
        transforms={"extra.cast": lambda v: [p.strip() for p in v.split("|")]},
    )
    draft = importer.normalize(_blank_record(t="X", cast="A|B | C"))
    assert draft.extra["cast"] == ["A", "B", "C"]


def test_transform_rating_out_of_range_still_rejected(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path,
        kind="movie",
        mapping={"title": ["t"], "rating": ["stars"]},
        transforms={"rating": lambda v: float(v) * 2},  # 6 星 -> 12，应当报错
    )
    with pytest.raises(NormalizeError):
        importer.normalize(_blank_record(t="X", stars="6"))


# ------------------------------------------------------------------------ CSV


def test_csv_reads_bom_quotes_and_crlf(tmp_path: Path) -> None:
    path = tmp_path / "books.csv"
    path.write_bytes('\ufefftitle,rating\r\n"Dune, Book One",4.5\r\n'.encode("utf-8"))
    rows = CsvFileImporter(path).rows()
    assert rows == [{"title": "Dune, Book One", "rating": "4.5"}]


def test_csv_custom_delimiter(tmp_path: Path) -> None:
    path = tmp_path / "semi.csv"
    path.write_text("title;year\nHades;2020\n", encoding="utf-8")

    class SemiCsv(CsvFileImporter):
        delimiter = ";"
        kind = "game"
        mapping = {"title": ["title"], "year": ["year"]}

    assert SemiCsv(path).normalize(next(iter(SemiCsv(path).fetch()))).year == 2020


def test_csv_missing_file(tmp_path: Path) -> None:
    with pytest.raises(NormalizeError):
        list(CsvFileImporter(tmp_path / "nope.csv", kind="movie").fetch())


def test_csv_end_to_end_through_service(session: Session, tmp_path: Path) -> None:
    """脏数据实战：BOM、空单元格、中文状态、5 星制、管道分隔的列表、外部 ID。"""

    @register("unit_csv_games")
    class GameCsvImporter(CsvFileImporter):
        source = "unit_csv_games"
        kind = "game"
        mapping = {
            "title": ["title", "name"],
            "year": ["year", "release_year"],
            "status": ["status"],
            "rating": ["stars"],
            "extra.platform": ["platforms"],
            "extra.developer": ["developer"],
            "external_ids.igdb": ["igdb_id"],
            "tags": ["tags"],
        }
        transforms = {
            "rating": lambda v: float(v) * 2,
            "status": {"玩过": "done", "在玩": "doing", "想玩": "wishlist"},
            "extra.platform": lambda v: [p.strip() for p in v.split("|")],
        }

    try:
        path = tmp_path / "games.csv"
        path.write_bytes(
            (
                "\ufefftitle,year,status,stars,platforms,developer,igdb_id,tags\r\n"
                "Hades,2020,玩过,4.5,PC|Switch,Supergiant,113112,roguelike\r\n"
                "Outer Wilds,2019,在玩,5,PC, Mobius ,,\r\n"
                "Empty Year,,想玩,,PS5,,,\r\n"
            ).encode("utf-8")
        )
        result = import_file(session, importer_name="unit_csv_games", path=path)
        assert result.total == 3
        assert result.inserted == 3
        assert result.error_count == 0

        items = {item.title: item for item in repo.list_items(session, kind="game")}
        hades = items["Hades"]
        assert hades.year == 2020
        assert hades.status == "done"
        assert hades.rating == 9.0  # 4.5 星 -> 9.0
        assert hades.extra["platform"] == ["PC", "Switch"]
        assert repo.list_tags(session, hades.id) == ["roguelike"]
        assert [link.external_id for link in repo.list_external_ids(session, hades.id)] == ["113112"]

        outer = items["Outer Wilds"]
        assert outer.rating == 10.0
        assert outer.extra["developer"] == "Mobius"  # 前后空格被清掉
        assert repo.list_external_ids(session, outer.id) == []

        empty = items["Empty Year"]
        assert empty.year is None and empty.rating is None  # 空单元格 -> NULL

        # 重复导入同一份文件：不新增、不重复
        again = import_file(session, importer_name="unit_csv_games", path=path)
        assert again.inserted == 0
        assert len(repo.list_items(session, kind="game")) == 3
    finally:
        IMPORTERS.pop("unit_csv_games", None)


def test_csv_bad_encoding_is_a_record_error(session: Session, tmp_path: Path) -> None:
    @register("unit_csv_gbk")
    class GbkCsv(CsvFileImporter):
        source = "unit_csv_gbk"
        kind = "movie"
        mapping = {"title": ["title"]}

    try:
        path = tmp_path / "gbk.csv"
        path.write_bytes("title\n千与千寻\n".encode("gbk"))
        result = import_file(session, importer_name="unit_csv_gbk", path=path)
        assert result.total == 0
        assert result.error_count == 1
        assert "编码" in result.errors[0]["message"]
    finally:
        IMPORTERS.pop("unit_csv_gbk", None)


def test_string_values_are_stripped_by_default(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path,
        kind="movie",
        mapping={"title": ["title"], "notes": ["notes"], "extra.country": ["country"]},
    )
    draft = importer.normalize(
        _blank_record(title="  Hades  ", notes="  多行\n文本  ", country=" Japan ")
    )
    assert draft.title == "Hades"
    assert draft.extra["country"] == "Japan"


def test_strip_values_can_be_disabled(tmp_path: Path) -> None:
    importer = _importer(
        tmp_path, kind="movie", mapping={"title": ["title"]}, strip_values=False
    )
    # title 仍会被 ItemDraft 自己的校验 strip
    assert importer.normalize(_blank_record(title="  X  ")).title == "X"


# ------------------------------------------------- 嵌套字段（点路径候选键）


def test_mapping_supports_dotted_source_paths() -> None:
    """候选键可以是 a.b.c 点路径：豆瓣那种把字段放在 extra 子对象里的导出要用。"""
    importer = MappingImporter(
        Path("unused.json"),
        kind="movie",
        mapping={"title": ["title"], "extra.platform": ["extra.platforms"]},
    )
    record = DictRawRecord(
        source="s", payload={"title": "T", "extra": {"platforms": ["PC", "NS"]}}
    )
    draft = importer.normalize(record)
    assert draft.extra["platform"] == ["PC", "NS"]


def test_mapping_literal_key_wins_over_path() -> None:
    """字面量键优先，所以既有扁平 payload 的行为不变。"""
    importer = MappingImporter(
        Path("unused.json"), kind="movie", mapping={"title": ["extra.name", "name"]}
    )
    flat = DictRawRecord(source="s", payload={"extra.name": "字面量", "name": "x"})
    assert importer.normalize(flat).title == "字面量"
    nested = DictRawRecord(source="s", payload={"extra": {"name": "嵌套"}})
    assert importer.normalize(nested).title == "嵌套"


def test_mapping_dotted_path_missing_is_skipped() -> None:
    importer = MappingImporter(
        Path("unused.json"),
        kind="movie",
        mapping={"title": ["title"], "subtitle": ["extra.nope.deep"]},
    )
    draft = importer.normalize(DictRawRecord(source="s", payload={"title": "T", "extra": {}}))
    assert draft.subtitle is None
