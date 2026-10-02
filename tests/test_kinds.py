"""kind 注册表与 extra 校验。"""

from __future__ import annotations

import pytest

from omnishelf.kinds import (
    EXTRA_TYPES,
    KINDS,
    ExtraFieldError,
    FieldDef,
    KindDef,
    coerce_extra,
    coerce_value,
    get_kind,
    kind_names,
    register,
)


def test_builtin_kinds_exist() -> None:
    assert {"book", "manga", "movie", "music", "game"} <= set(KINDS)
    assert kind_names() == sorted(KINDS)
    for kind_def in KINDS.values():
        assert kind_def.label
        for field_def in kind_def.extra_fields:
            assert field_def.type in EXTRA_TYPES


def test_get_kind_unknown() -> None:
    with pytest.raises(KeyError):
        get_kind("nope")


def test_field_def_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        FieldDef("x", "X", "dict")


def test_register_new_kind() -> None:
    register(KindDef("boardgame", "桌游", (FieldDef("players", "人数", "int"),)))
    try:
        assert get_kind("boardgame").label == "桌游"
        assert "boardgame" in kind_names()
    finally:
        KINDS.pop("boardgame", None)


# ------------------------------------------------------------------ coerce_value


def test_coerce_int_from_string_and_float() -> None:
    field = FieldDef("pages", "页数", "int")
    assert coerce_value(field, 412) == 412
    assert coerce_value(field, "412") == 412
    assert coerce_value(field, 412.0) == 412
    with pytest.raises(ExtraFieldError):
        coerce_value(field, "abc")
    with pytest.raises(ExtraFieldError):
        coerce_value(field, True)


def test_coerce_float() -> None:
    field = FieldDef("score", "评分", "float")
    assert coerce_value(field, 8) == 8.0
    assert coerce_value(field, "8.5") == 8.5
    with pytest.raises(ExtraFieldError):
        coerce_value(field, "high")


def test_coerce_bool() -> None:
    field = FieldDef("owned", "已拥有", "bool")
    assert coerce_value(field, True) is True
    assert coerce_value(field, "false") is False
    assert coerce_value(field, "1") is True
    with pytest.raises(ExtraFieldError):
        coerce_value(field, "yes")


def test_coerce_str_is_lenient_for_numbers() -> None:
    field = FieldDef("isbn", "ISBN", "str")
    assert coerce_value(field, "978") == "978"
    assert coerce_value(field, 978) == "978"
    with pytest.raises(ExtraFieldError):
        coerce_value(field, True)


def test_coerce_list_of_str_is_strict() -> None:
    field = FieldDef("cast", "主演", "list[str]")
    assert coerce_value(field, ["a", "b"]) == ["a", "b"]
    assert coerce_value(field, ("a",)) == ["a"]
    with pytest.raises(ExtraFieldError):
        coerce_value(field, "Tom Hanks")


# ------------------------------------------------------------------ coerce_extra


def test_coerce_extra_ok() -> None:
    extra, warnings = coerce_extra(
        "movie", {"director": "Ridley Scott", "runtime": "117", "cast": ["A", "B"]}
    )
    assert extra == {"director": "Ridley Scott", "runtime": 117, "cast": ["A", "B"]}
    assert warnings == []


def test_unknown_extra_field_warns_but_is_kept() -> None:
    extra, warnings = coerce_extra("movie", {"letterboxd_uri": "https://boxd.it/x"})
    assert extra == {"letterboxd_uri": "https://boxd.it/x"}
    assert len(warnings) == 1
    assert "letterboxd_uri" in warnings[0]


def test_extra_type_mismatch_raises() -> None:
    with pytest.raises(ExtraFieldError):
        coerce_extra("movie", {"runtime": "很長"})
    with pytest.raises(ExtraFieldError):
        coerce_extra("movie", {"cast": "Tom Hanks"})


def test_extra_required_missing_raises() -> None:
    register(KindDef("unit_required", "测试必填", (FieldDef("isbn", "ISBN", "str", required=True),)))
    try:
        with pytest.raises(ExtraFieldError):
            coerce_extra("unit_required", {"pages": 100})
        extra, _ = coerce_extra("unit_required", {"isbn": "978"})
        assert extra == {"isbn": "978"}
    finally:
        KINDS.pop("unit_required", None)


def test_book_author_is_now_a_core_field() -> None:
    """作者已提升为通用列 creators（多值），不再是 book 的 extra 字段。"""
    assert KINDS["book"].field("author") is None
    assert KINDS["manga"].field("author") is None
    assert KINDS["music"].field("artist") is None
    extra, warnings = coerce_extra("book", {"publisher": "Chilton"})
    assert extra == {"publisher": "Chilton"} and warnings == []


def test_extra_none_values_are_dropped() -> None:
    extra, warnings = coerce_extra("movie", {"director": None, "cast": None})
    assert extra == {}
    assert warnings == []


def test_extra_unknown_kind() -> None:
    with pytest.raises(KeyError):
        coerce_extra("nope", {})
