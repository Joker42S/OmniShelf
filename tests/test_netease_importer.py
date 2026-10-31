"""netease_music importer：网易云歌单快照 / ncm-cli 原始行 -> music 草稿。

数据全是合成的（假 song id、假歌手），不碰真实收藏。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnishelf.importers.base import DictRawRecord, NormalizeError
from omnishelf.importers.netease import (
    NeteaseMusicImporter,
    artist_names_of,
    duration_seconds_of,
    song_id_of,
)
from omnishelf.importers.registry import get, load_builtin_importers


def make_importer(tmp_path: Path) -> NeteaseMusicImporter:
    return NeteaseMusicImporter(tmp_path / "snapshot.json")


def test_importer_is_registered() -> None:
    load_builtin_importers()
    importer: Any = get("netease_music")
    assert importer.source == "netease_music"
    assert importer.kind == "music"


# ------------------------------------------------------------------ 原始行


def test_normalizes_raw_ncm_cli_row(tmp_path: Path) -> None:
    row = {
        "originalId": 3450019222,
        "id": "CB2F5E535C288C4799381D7051ABA88B",
        "name": "God Knows…（异环 live版）",
        "duration": 179999,
        "artists": [{"originalId": 1, "id": "A", "name": "Octans"}],
        "fullArtists": [{"id": None, "name": "不该当主艺术家"}],
        "album": {"originalId": 404536181, "id": "B", "name": "God Knows…(异环cover ver.）"},
        "liked": True,
    }
    draft = make_importer(tmp_path).normalize(
        DictRawRecord(source="netease_music", payload=row)
    )

    assert draft.kind == "music"
    assert draft.title == "God Knows…（异环 live版）"
    assert draft.creators == ["Octans"]
    assert draft.extra["artists"] == ["Octans"]
    assert draft.extra["album"] == "God Knows…(异环cover ver.）"
    assert draft.extra["duration_seconds"] == 180
    assert [(item.source, item.external_id) for item in draft.external_ids] == [
        ("netease_music", "3450019222")
    ]


def test_snapshot_row_uses_song_id_and_string_artists(tmp_path: Path) -> None:
    row = {
        "song_id": 123456,
        "name": "Synthetic Song",
        "artists": ["Artist A", "Artist B"],
        "album": "Synthetic Album",
        "duration_seconds": 245,
        "track_number": 3,
    }
    draft = make_importer(tmp_path).normalize(
        DictRawRecord(source="netease_music", payload=row)
    )

    assert draft.creators == ["Artist A", "Artist B"]
    assert draft.extra["album"] == "Synthetic Album"
    assert draft.extra["duration_seconds"] == 245
    assert draft.extra["track_number"] == 3
    assert draft.external_ids[0].external_id == "123456"


def test_missing_title_is_a_normalize_error(tmp_path: Path) -> None:
    with pytest.raises(NormalizeError):
        make_importer(tmp_path).normalize(
            DictRawRecord(source="netease_music", payload={"originalId": 1, "name": "  "})
        )


def test_rows_without_ids_are_still_accepted(tmp_path: Path) -> None:
    draft = make_importer(tmp_path).normalize(
        DictRawRecord(source="netease_music", payload={"name": "Only A Title"})
    )
    assert draft.title == "Only A Title"
    assert draft.external_ids == []


# ------------------------------------------------------------------ 小工具


def test_artist_names_accept_objects_strings_and_duplicates() -> None:
    assert artist_names_of([{"name": "A"}, "B", {"name": "A"}, {"name": ""}, None]) == ["A", "B"]
    assert artist_names_of(None) == []


def test_song_id_prefers_original_id_and_falls_back_to_encrypted() -> None:
    assert song_id_of({"originalId": 7, "id": "ENC"}) == "7"
    assert song_id_of({"id": "ENC"}) == "ENC"
    assert song_id_of({"originalId": None, "id": ""}) is None


def test_duration_seconds_handles_both_shapes() -> None:
    assert duration_seconds_of({"duration": 179999}) == 180
    assert duration_seconds_of({"duration_seconds": 245}) == 245
    assert duration_seconds_of({"duration": 0}) is None
    assert duration_seconds_of({}) is None
