"""services：封面落盘、编辑、导出。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.config import Settings
from omnishelf.kinds import KindDef, register
from omnishelf.services import covers
from omnishelf.services import editing
from omnishelf.services import export_ as export_service

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32


# --------------------------------------------------------------------------- covers


def test_sniff_suffix() -> None:
    assert covers.sniff_suffix(PNG) == ".png"
    assert covers.sniff_suffix(JPEG) == ".jpg"
    assert covers.sniff_suffix(GIF) == ".gif"
    assert covers.sniff_suffix(WEBP) == ".webp"
    assert covers.sniff_suffix(b"not an image") is None


def test_normalize_suffix_prefers_content(settings: Settings) -> None:
    assert covers.normalize_suffix("wrong.jpg", "image/jpeg", PNG) == ".png"
    assert covers.normalize_suffix("x.jpeg", None, b"") == ".jpg"
    assert covers.normalize_suffix(None, "image/webp", b"") == ".webp"
    with pytest.raises(covers.CoverError):
        covers.normalize_suffix("x.txt", "text/plain", b"hello")


def test_save_cover_buckets_by_ulid(settings: Settings, make_item) -> None:
    item = make_item(title="A")
    rel = covers.save_cover(settings, item.id, PNG, filename="a.png", content_type="image/png")
    assert rel == f"{item.id[0:2]}/{item.id[2:4]}/{item.id}.png"
    assert (settings.attachments_dir / rel).is_file()
    assert covers.cover_exists(settings, rel) is True
    assert covers.cover_abs(settings, rel).read_bytes() == PNG


def test_save_cover_rejects_bad_input(settings: Settings, make_item) -> None:
    item = make_item(title="A")
    with pytest.raises(covers.CoverError):
        covers.save_cover(settings, item.id, b"")
    with pytest.raises(covers.CoverError):
        covers.save_cover(settings, item.id, b"plain text", filename="a.txt")
    with pytest.raises(covers.CoverError):
        covers.save_cover(settings, item.id, b"\x00" * (covers.MAX_COVER_BYTES + 1))


def test_cover_rel_path_validation(settings: Settings) -> None:
    with pytest.raises(ValueError):
        settings.cover_rel_path("abc")
    assert settings.cover_rel_path("01ABC", "png").endswith(".png")


def test_delete_cover(settings: Settings, make_item) -> None:
    item = make_item(title="A")
    rel = covers.save_cover(settings, item.id, JPEG)
    assert covers.delete_cover(settings, rel) is True
    assert covers.delete_cover(settings, rel) is False
    assert covers.delete_cover(settings, None) is False
    assert covers.cover_exists(settings, rel) is False


# --------------------------------------------------------------------------- export


def test_export_payload_shape(session: Session, make_item) -> None:
    make_item(
        kind="movie",
        title="Blade Runner",
        year=1982,
        rating=9.0,
        tags=["sci-fi"],
        external_ids=[("tmdb", "78")],
    )
    payload = export_service.export_payload(session)
    assert payload["count"] == 1
    row = payload["items"][0]
    assert row["title"] == "Blade Runner"
    assert row["tags"] == ["sci-fi"]
    assert row["external_ids"][0]["source"] == "tmdb"
    assert payload["stats"]["total"] == 1
    assert export_service.parse_export(payload)[0]["title"] == "Blade Runner"


def test_export_json_writes_utf8_file(session: Session, make_item, tmp_path: Path) -> None:
    make_item(kind="book", title="千与千寻", extra={"author": "宫崎骏"})
    out = tmp_path / "nested" / "export.json"
    count = export_service.export_json(session, out)
    assert count == 1
    raw = out.read_text(encoding="utf-8")
    assert "千与千寻" in raw
    data = json.loads(raw)
    assert data["items"][0]["extra"]["author"] == "宫崎骏"


def test_export_excludes_deleted_by_default(session: Session, make_item) -> None:
    item = make_item(title="Gone")
    make_item(title="Keep")
    repo.soft_delete_item(session, item.id)
    assert export_service.export_payload(session)["count"] == 1
    assert export_service.export_payload(session, include_deleted=True)["count"] == 2


def test_export_format_validation(session: Session, tmp_path: Path) -> None:
    with pytest.raises(export_service.ExportFormatError):
        export_service.export(session, fmt="csv", out_path=tmp_path / "x.csv")
    assert export_service.export(session, fmt="json", out_path=tmp_path / "x.json") == 0


def test_parse_export_rejects_bad_payload() -> None:
    with pytest.raises(ValueError):
        export_service.parse_export({"items": "nope"})


# -------------------------------------------------------------------------- editing




def test_update_item_from_form_applies_all_fields(session: Session, make_item) -> None:
    item = make_item(
        kind="movie",
        title="Arrival",
        year=2016,
        extra={"director": "Denis Villeneuve", "runtime": 116},
        tags=["sci-fi"],
    )
    updated = editing.update_item_from_form(
        session,
        item,
        {
            "title": "  Arrival  ",
            "subtitle": "  First Contact  ",
            "year": "2016",
            "status": "done",
            "rating": "8.5",
            "notes": " 好片 ",
            "tags": "sci-fi, first-contact",
            "extra.director": "Denis Villeneuve",
            "extra.runtime": "116",
        },
    )
    assert updated.title == "Arrival"
    assert updated.subtitle == "First Contact"
    assert updated.status == "done"
    assert updated.rating == 8.5
    assert updated.notes == "好片"
    assert updated.extra["runtime"] == 116
    assert repo.list_tags(session, item.id) == ["first-contact", "sci-fi"]


def test_update_item_from_form_clears_optional_fields(session: Session, make_item) -> None:
    item = make_item(kind="movie", title="A", year=2001, rating=7.0)
    editing.update_item_from_form(session, item, {"title": "A", "year": "", "rating": ""})
    session.refresh(item)
    assert item.year is None and item.rating is None


def test_update_item_from_form_keeps_title_when_blank(session: Session, make_item) -> None:
    item = make_item(kind="movie", title="Keep Me")
    editing.update_item_from_form(session, item, {"title": "   "})
    session.refresh(item)
    assert item.title == "Keep Me"


def test_update_item_from_form_preserves_undeclared_extra(session: Session, make_item) -> None:
    item = make_item(kind="movie", title="A", extra={"letterboxd_uri": "https://boxd.it/x"})
    updated = editing.update_item_from_form(
        session, item, {"title": "A", "extra.director": "Someone"}
    )
    # 声明的 bool 字段（movie 的 playable）在表单里未勾选 = False，会被写进去；未声明的原样保留
    assert updated.extra == {
        "director": "Someone",
        "letterboxd_uri": "https://boxd.it/x",
        "playable": False,
    }


def test_update_item_from_form_rejects_bad_values(session: Session, make_item) -> None:
    item = make_item(kind="movie", title="A", extra={"runtime": 100})
    for form in (
        {"title": "A", "year": "MMXX"},
        {"title": "A", "rating": "high"},
        {"title": "A", "status": "finished"},
        {"title": "A", "extra.runtime": "很久"},
    ):
        with pytest.raises(editing.ItemFormError):
            editing.update_item_from_form(session, item, form)
    session.refresh(item)
    assert item.extra["runtime"] == 100  # 失败时没有写坏数据


def test_form_list_extra_is_comma_separated(session: Session, make_item) -> None:
    """表单里的 list[str] 用逗号分隔（与 JSON 导入的严格校验不同）。"""
    item = make_item(kind="movie", title="A")
    updated = editing.update_item_from_form(
        session, item, {"title": "A", "extra.cast": "Amy Adams, Jeremy Renner"}
    )
    assert updated.extra["cast"] == ["Amy Adams", "Jeremy Renner"]


def test_parse_extra_bool_checkbox(session: Session, make_item) -> None:
    from omnishelf.kinds import KINDS, FieldDef

    register(KindDef("boardgame", "桌游", (FieldDef("owned", "已拥有", "bool"),)))
    try:
        item = make_item(kind="boardgame", title="Brass")
        assert editing.parse_extra({}, item) == {"owned": False}
        assert editing.parse_extra({"extra.owned": "on"}, item) == {"owned": True}
    finally:
        KINDS.pop("boardgame", None)


def test_status_normalization_is_case_insensitive(session: Session, make_item) -> None:
    item = make_item(kind="movie", title="A")
    updated = editing.update_item_from_form(session, item, {"title": "A", "status": " DONE "})
    assert updated.status == "done"
    assert repo.update_item(session, item.id, status="WISHLIST").status == "wishlist"


# ------------------------------------------------------------------- cover 编排


def test_attach_and_remove_cover(settings: Settings, session: Session, make_item) -> None:
    item = make_item(kind="movie", title="A")
    rel = covers.attach_cover(settings, session, item, PNG, filename="a.png")
    session.refresh(item)
    assert item.cover_path == rel
    assert (settings.attachments_dir / rel).is_file()

    # 换成 jpg：旧文件应被清掉
    new_rel = covers.attach_cover(settings, session, item, JPEG, filename="a.jpg")
    session.refresh(item)
    assert item.cover_path == new_rel
    assert not (settings.attachments_dir / rel).exists()
    assert (settings.attachments_dir / new_rel).is_file()

    assert covers.remove_cover(settings, session, item) is True
    session.refresh(item)
    assert item.cover_path is None
    assert not (settings.attachments_dir / new_rel).exists()
    assert covers.remove_cover(settings, session, item) is False


def test_env_var_fallback_to_old_collection_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正式名换成 omnishelf，但老的 COLLECTION_* 环境变量仍然有效。"""
    from omnishelf.config import load_settings

    monkeypatch.delenv("OMNISHELF_DATA_DIR", raising=False)
    monkeypatch.setenv("COLLECTION_DATA_DIR", str(tmp_path / "old"))
    assert load_settings().data_dir == (tmp_path / "old").resolve()

    monkeypatch.setenv("OMNISHELF_DATA_DIR", str(tmp_path / "new"))
    assert load_settings().data_dir == (tmp_path / "new").resolve()  # 新名字优先
