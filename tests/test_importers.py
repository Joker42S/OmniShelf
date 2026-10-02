"""importer 抽象层：JSON importer 的结构支持、映射、错误处理与注册表。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnishelf.importers.base import (
    DictRawRecord,
    DraftExternalId,
    Importer,
    ItemDraft,
    NormalizeError,
    RawRecord,
)
from omnishelf.importers.json_file import BookJsonImporter, JsonFileImporter, MovieJsonImporter
from omnishelf.importers.registry import (
    IMPORTERS,
    UnknownImporterError,
    get,
    load_builtin_importers,
    names,
    register,
)


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_json_array_structure(tmp_path: Path) -> None:
    path = _write(tmp_path / "a.json", [{"title": "A"}, {"title": "B"}])
    importer = JsonFileImporter(path, kind="movie")
    records = list(importer.fetch())
    assert [r.payload["title"] for r in records] == ["A", "B"]
    assert all(r.source == importer.source for r in records)


def test_json_object_items_structure(tmp_path: Path) -> None:
    path = _write(tmp_path / "b.json", {"items": [{"title": "A"}]})
    assert [r.payload["title"] for r in JsonFileImporter(path, kind="movie").fetch()] == ["A"]


def test_json_object_records_alias(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.json", {"records": [{"title": "A"}]})
    assert len(list(JsonFileImporter(path, kind="movie").fetch())) == 1


def test_json_bad_structure(tmp_path: Path) -> None:
    path = _write(tmp_path / "d.json", {"nope": []})
    with pytest.raises(NormalizeError):
        list(JsonFileImporter(path, kind="movie").fetch())
    path2 = _write(tmp_path / "e.json", "just a string")
    with pytest.raises(NormalizeError):
        list(JsonFileImporter(path2, kind="movie").fetch())


def test_json_missing_file(tmp_path: Path) -> None:
    with pytest.raises(NormalizeError):
        list(JsonFileImporter(tmp_path / "missing.json", kind="movie").fetch())


def test_json_broken_syntax(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(NormalizeError):
        list(JsonFileImporter(path, kind="movie").fetch())


def test_normalize_maps_dotted_targets(tmp_path: Path) -> None:
    path = _write(tmp_path / "m.json", {"items": []})
    importer = MovieJsonImporter(path)
    draft = importer.normalize(
        DictRawRecord(
            source="json_movies",
            payload={
                "name": "Blade Runner",
                "release_year": 1982,
                "director": "Ridley Scott",
                "runtime": 117,
                "tmdb_id": 78,
                "genres": ["sci-fi", "noir"],
                "tagline": "tag",
            },
        )
    )
    assert draft.kind == "movie"
    assert draft.title == "Blade Runner"
    assert draft.year == 1982
    assert draft.extra["director"] == "Ridley Scott"
    assert draft.extra["runtime"] == 117
    assert draft.external_ids == [DraftExternalId(source="tmdb", external_id="78")]
    assert draft.tags == ["sci-fi", "noir"]
    assert draft.subtitle == "tag"


def test_normalize_picks_first_candidate_in_order(tmp_path: Path) -> None:
    importer = MovieJsonImporter(_write(tmp_path / "m.json", []))
    draft = importer.normalize(
        DictRawRecord(source="s", payload={"title": "Primary", "name": "Secondary"})
    )
    assert draft.title == "Primary"


def test_normalize_skips_none_candidates(tmp_path: Path) -> None:
    importer = MovieJsonImporter(_write(tmp_path / "m.json", []))
    draft = importer.normalize(
        DictRawRecord(source="s", payload={"title": "T", "year": None, "release_year": 1999})
    )
    assert draft.year == 1999


def test_normalize_missing_title_raises(tmp_path: Path) -> None:
    importer = MovieJsonImporter(_write(tmp_path / "m.json", []))
    with pytest.raises(NormalizeError) as excinfo:
        importer.normalize(DictRawRecord(source="s", payload={"year": 2001}))
    assert "title" in str(excinfo.value)


def test_normalize_blank_title_raises(tmp_path: Path) -> None:
    importer = MovieJsonImporter(_write(tmp_path / "m.json", []))
    with pytest.raises(NormalizeError):
        importer.normalize(DictRawRecord(source="s", payload={"title": "   "}))


def test_normalize_bad_year_raises(tmp_path: Path) -> None:
    importer = MovieJsonImporter(_write(tmp_path / "m.json", []))
    with pytest.raises(NormalizeError):
        importer.normalize(DictRawRecord(source="s", payload={"title": "T", "year": "MMXX"}))


def test_normalize_bad_status_raises(tmp_path: Path) -> None:
    importer = MovieJsonImporter(_write(tmp_path / "m.json", []))
    with pytest.raises(NormalizeError):
        importer.normalize(DictRawRecord(source="s", payload={"title": "T", "status": "finished"}))


def test_normalize_string_tags_split_on_comma(tmp_path: Path) -> None:
    path = _write(tmp_path / "m.json", [])
    importer = JsonFileImporter(path, kind="movie", mapping={"title": ["title"], "tags": ["tags"]})
    draft = importer.normalize(DictRawRecord(source="s", payload={"title": "T", "tags": "a, b"}))
    assert draft.tags == ["a", "b"]


def test_normalize_requires_kind(tmp_path: Path) -> None:
    importer = JsonFileImporter(_write(tmp_path / "m.json", []), mapping={"title": ["title"]})
    with pytest.raises(NormalizeError) as excinfo:
        importer.normalize(DictRawRecord(source="s", payload={"title": "T"}))
    assert "kind" in str(excinfo.value)


def test_normalize_unknown_mapping_target(tmp_path: Path) -> None:
    importer = JsonFileImporter(
        _write(tmp_path / "m.json", []), kind="movie", mapping={"nope": ["title"]}
    )
    with pytest.raises(NormalizeError):
        importer.normalize(DictRawRecord(source="s", payload={"title": "T"}))


def test_normalize_non_dict_rows_are_skipped(tmp_path: Path) -> None:
    path = _write(tmp_path / "m.json", [{"title": "A"}, "junk", 42, {"title": "B"}])
    records = list(JsonFileImporter(path, kind="movie").fetch())
    assert [r.payload["title"] for r in records] == ["A", "B"]


def test_unicode_roundtrip(tmp_path: Path) -> None:
    path = _write(tmp_path / "zh.json", {"items": [{"title": "千与千寻", "year": 2001}]})
    importer = JsonFileImporter(path, kind="movie", mapping={"title": ["title"], "year": ["year"]})
    draft = importer.normalize(next(iter(importer.fetch())))
    assert draft.title == "千与千寻"


def test_default_tags(tmp_path: Path) -> None:
    path = _write(tmp_path / "m.json", [{"title": "A"}])
    importer = JsonFileImporter(
        path, kind="movie", mapping={"title": ["title"]}, default_tags=("imported",)
    )
    assert importer.normalize(next(iter(importer.fetch()))).tags == ["imported"]


def test_builtin_importers_registered() -> None:
    load_builtin_importers()
    assert {"json_movies", "json_books"} <= set(names())
    assert get("json_movies") is MovieJsonImporter
    assert get("json_books") is BookJsonImporter


def test_registry_unknown() -> None:
    with pytest.raises(UnknownImporterError):
        get("nope_not_registered")


def test_register_duplicate_name_rejected() -> None:
    @register("unit_dup")
    class First(JsonFileImporter):
        pass

    try:
        with pytest.raises(ValueError):

            @register("unit_dup")
            class Second(JsonFileImporter):
                pass

    finally:
        IMPORTERS.pop("unit_dup", None)
    assert First.source == "unit_dup"


def test_register_sets_source_when_absent() -> None:
    @register("unit_auto_source")
    class Auto(JsonFileImporter):
        pass

    try:
        assert Auto.source == "unit_auto_source"
    finally:
        IMPORTERS.pop("unit_auto_source", None)


# ----------------------------------------------------------------- ItemDraft 语义


def test_item_draft_accepts_tuples() -> None:
    draft = ItemDraft.model_validate(
        {
            "kind": "movie",
            "title": " T ",
            "external_ids": [("tmdb", 78), ("douban", "1", "https://x")],
            "tags": "a,b",
        }
    )
    assert draft.title == "T"
    assert draft.external_ids[0] == DraftExternalId(source="tmdb", external_id="78")
    assert draft.external_ids[1].url == "https://x"
    assert draft.tags == ["a", "b"]


def test_item_draft_rejects_bad_rating_and_status() -> None:
    with pytest.raises(ValueError):
        ItemDraft(kind="movie", title="T", rating=11)
    with pytest.raises(ValueError):
        ItemDraft(kind="movie", title="T", status="nope")


def test_protocols_are_runtime_checkable(tmp_path: Path) -> None:
    path = _write(tmp_path / "m.json", [{"title": "A"}])
    importer = MovieJsonImporter(path)
    assert isinstance(importer, Importer)
    record = next(iter(importer.fetch()))
    assert isinstance(record, RawRecord)
