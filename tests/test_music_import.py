"""音乐导入：本地扫描（``music_scan``）、清单 importer（``music_files``）、
iTunes 资料库（``itunes_xml``），以及 ``music`` 这类「按内容判重」的行为。

音频文件都是现场合成的字节（``tests.audio_synth``），iTunes 夹具在 ``tests/fixtures/itunes_library.xml``，
里面没有任何真实曲目或人名。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.importers.base import DictRawRecord, NormalizeError
from omnishelf.importers.music import (
    ItunesXmlImporter,
    MusicFilesImporter,
    build_music_draft,
    filename_from_location,
    is_podcast,
    itunes_rating,
)
from omnishelf.importers.registry import get as get_importer
from omnishelf.importers.registry import load_builtin_importers
from omnishelf.music_scan import (
    MAX_ERRORS,
    ScanResult,
    is_audio_file,
    scan,
    skip_name,
    title_from_filename,
    write_manifest,
)
from omnishelf.services.import_ import run_import
from tests.audio_synth import CASES, flac

# --------------------------------------------------------------------------- 工具


def record(payload: dict[str, Any]) -> DictRawRecord:
    return DictRawRecord(source="music_files", payload=payload)


def write_manifest_json(path: Path, items: Sequence[dict[str, Any]]) -> Path:
    path.write_text(
        json.dumps({"kind": "music_files", "items": list(items)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def track_payload(title: str, *, album: str = "Greatest", artists: str = "Ann") -> dict[str, Any]:
    """一条清单记录：标题 + 判重要用的专辑和艺术家，外加原始文件名。"""
    return {
        "title": title,
        "artists": artists,
        "album": album,
        "filename": f"{title}.mp3",
        "path": f"/music/{title}.mp3",
    }


def import_manifest(
    session: Session, tmp_path: Path, name: str, items: Sequence[dict[str, Any]]
) -> Any:
    return run_import(session, MusicFilesImporter(write_manifest_json(tmp_path / name, items)))


def by_filename(items: list[Any]) -> dict[str, Any]:
    return {item.extra["filename"]: item for item in items}


# ----------------------------------------------------------------------- music_scan


def audio_tree(root: Path) -> None:
    """一棵假的音乐目录：嵌套目录 + 回收站副本 + 隐藏目录 + 非音频文件。"""
    album = root / "Synthetic Album"
    (album / "Disc 1").mkdir(parents=True)
    (album / "Disc 1" / "01 - Tagged.mp3").write_bytes(CASES["a_id3v23.mp3"]())
    # 只有填充块的 FLAC：容器读得动、一条标签都没有 -> 标题退回文件名
    (album / "02 - Bare.flac").write_bytes(flac([(1, bytes(8), True)]))
    (album / "cover.txt").write_text("not audio", encoding="utf-8")

    recycle = root / "$RECYCLE.BIN" / "Synthetic Album"
    recycle.mkdir(parents=True)
    (recycle / "01 - Tagged.mp3").write_bytes(CASES["a_id3v23.mp3"]())
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "ghost.mp3").write_bytes(CASES["a_id3v23.mp3"]())


def test_scan_walks_directories_and_skips_junk(tmp_path: Path) -> None:
    """深度遍历子目录，回收站里的副本和隐藏目录整个跳过，非音频文件不看。"""
    audio_tree(tmp_path)
    result = scan([tmp_path])
    assert result.scanned == 2
    assert {Path(item["path"]).name for item in result.items} == {
        "01 - Tagged.mp3",
        "02 - Bare.flac",
    }
    assert result.roots == [str(tmp_path)]
    assert result.errors == []


def test_scan_tagless_item_falls_back_to_filename(tmp_path: Path) -> None:
    audio_tree(tmp_path)
    items = {item["filename"]: item for item in scan([tmp_path]).items}
    tagged = items["01 - Tagged.mp3"]
    assert tagged["title"] == "合成标题"
    assert tagged["artists"] == ["Alpha", "Beta", "Gamma"]
    assert "title_from" not in tagged

    bare = items["02 - Bare.flac"]
    assert bare["title"] == "Bare"  # 开头的曲目号「02 - 」被去掉
    assert bare["title_from"] == "filename"
    assert bare["album"] is None
    assert bare["path"].endswith("02 - Bare.flac")


def test_scan_counts_tagless(tmp_path: Path) -> None:
    audio_tree(tmp_path)
    assert scan([tmp_path]).tagless == 1


def test_scan_accepts_a_list_of_files(tmp_path: Path) -> None:
    """插件的「多选文件」走的就是这条路：内容是啥就读啥，不看后缀。"""
    first = tmp_path / "one.mp3"
    first.write_bytes(CASES["d_track.m4a"]())
    second = tmp_path / "two.mp3"
    second.write_bytes(CASES["e_lossless.flac"]())
    result = scan([first, second])
    assert result.scanned == 2
    assert sorted(item["title"] for item in result.items) == ["FLAC Title", "M4A Title"]


def test_scan_records_failures_and_keeps_going(tmp_path: Path) -> None:
    (tmp_path / "broken.mp3").write_bytes(b"")
    (tmp_path / "good.mp3").write_bytes(CASES["a_id3v23.mp3"]())
    result = scan([tmp_path])
    assert (result.scanned, result.failed, len(result.items)) == (2, 1, 1)
    assert "broken.mp3" in result.errors[0] and "空文件" in result.errors[0]
    assert "扫了 2 个音频文件" in result.summary()
    assert "1 个文件读失败" in result.summary()


def test_scan_progress_callback(tmp_path: Path) -> None:
    """每读一个文件回调一次：插件的进度条靠它，序号就是已读文件数。"""
    audio_tree(tmp_path)
    seen: list[tuple[int, str]] = []
    result = scan([tmp_path], progress=lambda index, path: seen.append((index, path.name)))
    assert [index for index, _ in seen] == [1, 2]
    assert {name for _, name in seen} == {"01 - Tagged.mp3", "02 - Bare.flac"}
    assert len(seen) == result.scanned


def test_write_manifest_roundtrips_into_an_import(tmp_path: Path, session: Session) -> None:
    audio_tree(tmp_path)
    manifest_path = write_manifest(scan([tmp_path]), tmp_path / "out" / "music_scan.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "music_files"
    assert payload["item_count"] == len(payload["items"]) == 2
    assert payload["roots"] == [str(tmp_path)]

    result = run_import(session, MusicFilesImporter(manifest_path))
    assert (result.total, result.inserted, result.error_count) == (2, 2, 0)
    # 文件名进 extra 备用；标签里的专辑艺术家留原文
    merged = by_filename(repo.list_items(session, kind="music"))["01 - Tagged.mp3"]
    assert merged.extra["album_artists"] == ["Various People", "Other Names"]


def test_error_list_is_capped() -> None:
    """几千个文件全坏时错误列表不能把页面撑爆：超出上限只记条数。"""
    result = ScanResult()
    for index in range(MAX_ERRORS + 5):
        result.add_error(f"错误 {index}")
    assert len(result.errors) == MAX_ERRORS
    assert result.errors_hidden == 5
    assert "另有 5 条错误未列出" in result.summary()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("07 - My Song.mp3", "My Song"),
        ("01.Track.flac", "Track"),
        ("03、中文歌名.m4a", "中文歌名"),
        # 「Artist - Title」这种文件名不拆：标题里带破折号的歌不少，猜错就把标题切成半句
        ("Artist - Title.mp3", "Artist - Title"),
        ("no-suffix", "no-suffix"),
    ],
)
def test_title_from_filename(name: str, expected: str) -> None:
    assert title_from_filename(Path(name)) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.MP3", True), ("track.flac", True), ("clip.mp4", True), ("notes.txt", False)],
)
def test_is_audio_file(name: str, expected: bool) -> None:
    assert is_audio_file(Path(name)) is expected


@pytest.mark.parametrize(
    "name",
    [
        # Ogg/Vorbis 容器要先验页 CRC 再重组分包，标签读不了，所以明确不扫
        "song.ogg",
        "voice.opus",
        "song.mid",
        "set.mka",
    ],
)
def test_unsupported_containers_are_not_scanned(name: str) -> None:
    assert not is_audio_file(Path(name))


@pytest.mark.parametrize(
    "name",
    ["$RECYCLE.BIN", ".hidden", ".DS_Store", "__MACOSX", "System Volume Information", "node_modules"],
)
def test_skip_name_covers_junk(name: str) -> None:
    assert skip_name(name)


# ------------------------------------------------------------------ music_files 映射


def test_normalize_maps_the_eleven_fields() -> None:
    draft = MusicFilesImporter(Path("unused.json")).normalize(
        record(
            {
                "title": "合成标题",
                "subtitle": "Synthetic Subtitle",
                "rating": 8.0,
                "notes": "备注：合成数据 example.invalid",
                "artists": ["Alpha", "Beta"],
                "album": "Synthetic Album",
                "album_artists": "Various People",
                "year": 2005,
                "track_number": "3",
                "duration_seconds": 26,
                "filename": "01 - Tagged.mp3",
                "path": "/music/01 - Tagged.mp3",  # 清单里的本地记账，不入库
            }
        )
    )
    assert draft.kind == "music"
    assert (draft.title, draft.subtitle, draft.rating, draft.notes, draft.year) == (
        "合成标题",
        "Synthetic Subtitle",
        8.0,
        "备注：合成数据 example.invalid",
        2005,
    )
    # 艺术家和专辑艺术家都算 creators（去重、保序）
    assert draft.creators == ["Alpha", "Beta", "Various People"]
    assert draft.extra == {
        "album": "Synthetic Album",
        "artists": ["Alpha", "Beta"],
        "album_artists": ["Various People"],
        "filename": "01 - Tagged.mp3",
        "track_number": 3,
        "duration_seconds": 26,
    }
    assert draft.external_ids == []


def test_normalize_requires_a_title() -> None:
    with pytest.raises(NormalizeError, match="title"):
        MusicFilesImporter(Path("unused.json")).normalize(record({"filename": "x.mp3"}))


def test_music_importers_are_registered() -> None:
    load_builtin_importers()
    assert get_importer("music_files") is MusicFilesImporter
    assert get_importer("itunes_xml") is ItunesXmlImporter


def test_build_music_draft_drops_out_of_range_rating() -> None:
    """超出 0-10 的量程说明来源用的是别的分级：留空，不猜。"""
    assert build_music_draft(title="T", rating=11).rating is None
    assert build_music_draft(title="T", rating="4.5").rating == 4.5
    assert build_music_draft(title="T", rating="好评").rating is None
    assert build_music_draft(title="T", rating=0).rating == 0.0


def test_build_music_draft_keeps_the_original_artist_strings() -> None:
    draft = build_music_draft(
        title="T", artists=["A & B", "A"], album_artists=["C、D"], album=None
    )
    assert draft.creators == ["A", "B", "C", "D"]
    assert draft.extra["artists"] == ["A", "B"]
    assert draft.extra["album_artists"] == ["C", "D"]
    assert draft.extra["album"] is None


def test_build_music_draft_does_not_guess_numeric_fields() -> None:
    draft = build_music_draft(
        title="T", year="Demo", track_number="12 of 16", duration_seconds=True
    )
    assert draft.year is None
    assert draft.extra["track_number"] is None
    assert draft.extra["duration_seconds"] is None  # bool 不是数字


def test_build_music_draft_rejects_blank_title() -> None:
    with pytest.raises(NormalizeError):
        build_music_draft(title="   ")


# ----------------------------------------------------------------------- 内容判重


def test_duplicate_files_of_the_same_track_merge(session: Session, tmp_path: Path) -> None:
    """同一首歌被拷了两份：标题 + 专辑 + 艺术家都一样 -> 同一条，先到的文件名留着。"""
    copy = track_payload("Hello", artists="Ann & Ben")
    copy["filename"] = "01 - Hello (copy).mp3"
    copy["duration_seconds"] = 213
    result = import_manifest(
        session, tmp_path, "m.json", [track_payload("Hello", artists="Ann & Ben"), copy]
    )
    assert (result.total, result.inserted, result.updated, result.skipped) == (2, 1, 1, 0)

    items = repo.list_items(session, kind="music")
    assert len(items) == 1
    assert items[0].extra["filename"] == "Hello.mp3"
    assert items[0].extra["duration_seconds"] == 213  # 空字段被补上


def test_identity_ignores_order_case_and_punctuation(session: Session, tmp_path: Path) -> None:
    result = import_manifest(
        session,
        tmp_path,
        "m.json",
        [
            track_payload("Hello There", artists="Ann, Ben"),
            track_payload("hello, there!", artists="Ben / Ann"),
        ],
    )
    assert result.inserted == 1
    assert [item.title for item in repo.list_items(session, kind="music")] == ["Hello There"]


def test_different_album_or_artists_stay_separate(session: Session, tmp_path: Path) -> None:
    """同一首歌收在不同专辑里是正常数据；换人唱也是另一条：都不能自动合。"""
    import_manifest(
        session,
        tmp_path,
        "m.json",
        [
            track_payload("Hello", album="Greatest"),
            track_payload("Hello", album="Live"),
            track_payload("Hello", album="Greatest", artists="Zoe"),
        ],
    )
    items = repo.list_items(session, kind="music")
    assert len(items) == 3
    assert {(item.extra["album"], tuple(item.extra["artists"])) for item in items} == {
        ("Greatest", ("Ann",)),
        ("Live", ("Ann",)),
        ("Greatest", ("Zoe",)),
    }


def test_content_dedupe_does_not_queue_pending_merges(session: Session, tmp_path: Path) -> None:
    """内容判重取代了模糊标题匹配：同标题不同专辑直接落两条，不写 pending_merges。"""
    import_manifest(
        session,
        tmp_path,
        "m.json",
        [track_payload("Hello", album="Greatest"), track_payload("Hello", album="Live")],
    )
    assert repo.count_pending_merges(session) == 0


def test_reimporting_the_same_manifest_changes_nothing(session: Session, tmp_path: Path) -> None:
    items = [track_payload("Hello"), track_payload("World", album="Singles")]
    first = import_manifest(session, tmp_path, "m.json", items)
    assert (first.inserted, first.updated, first.unchanged) == (2, 0, 0)
    second = import_manifest(session, tmp_path, "m.json", items)
    assert (second.total, second.inserted, second.updated, second.unchanged) == (2, 0, 0, 2)


def test_hand_written_rating_survives_a_reimport(session: Session, tmp_path: Path) -> None:
    """内容判重走的是「只补空字段」：你手写的评分和状态不会被清单覆盖。"""
    import_manifest(session, tmp_path, "m.json", [track_payload("Hello")])
    item = repo.list_items(session, kind="music")[0]
    repo.update_item(session, item.id, rating=9.5, status="done")

    richer = track_payload("Hello")
    richer.update({"rating": 3.0, "status": "wishlist", "notes": "清单带来的备注"})
    result = import_manifest(session, tmp_path, "again.json", [richer])
    assert (result.inserted, result.updated) == (0, 1)
    kept = repo.get_item(session, item.id)
    assert kept is not None
    assert (kept.rating, kept.status) == (9.5, "done")
    assert kept.notes == "清单带来的备注"  # 原来空着，所以补得上


def test_same_title_without_album_or_artists_is_one_work(session: Session, tmp_path: Path) -> None:
    """两条都没写专辑和艺术家：判重字段都是「空」，空和空算一样。"""
    result = import_manifest(
        session,
        tmp_path,
        "m.json",
        [{"title": "Unnamed", "filename": "a.mp3"}, {"title": "Unnamed", "filename": "b.mp3"}],
    )
    assert result.inserted == 1


def test_music_items_do_not_carry_external_ids(session: Session, tmp_path: Path) -> None:
    """本地文件没有稳定 ID：判重全靠内容，所以一条 external_ids 都不写。"""
    import_manifest(session, tmp_path, "m.json", [track_payload("Hello")])
    item = repo.list_items(session, kind="music")[0]
    assert repo.list_external_ids(session, item.id) == []


def test_identity_dedupe_leaves_other_kinds_alone(session: Session, import_records) -> None:
    """判重只作用于声明了 identity_fields 的 kind：影视仍走原来的三条规则。"""
    result = import_records(
        [
            {"kind": "movie", "title": "Hello", "year": 2016},
            {"kind": "movie", "title": "Hello", "year": 2016},
        ]
    )
    assert (result.inserted, result.updated, result.unchanged) == (1, 0, 1)
    assert repo.count_items(session, kind="music") == 0


# ------------------------------------------------------------------------- iTunes


@pytest.fixture()
def itunes(fixtures_dir: Path) -> ItunesXmlImporter:
    return ItunesXmlImporter(fixtures_dir / "itunes_library.xml")


def test_itunes_skips_streams_and_podcasts(itunes: ItunesXmlImporter) -> None:
    """只收真的落在本机文件里的音轨，且按轨道号排序（字符串序会把 10 排在 2 前面）。"""
    assert [str(track.get("Name") or "") for track in itunes.load()] == [
        "合成曲 Hello",
        "Second Track",
        "Collab Track",
        "",
    ]


def test_itunes_normalize_maps_the_apple_fields(itunes: ItunesXmlImporter) -> None:
    draft = itunes.normalize(next(iter(itunes.fetch())))
    assert draft.kind == "music"
    assert draft.title == "合成曲 Hello"
    assert draft.subtitle is None  # XML 里最接近的是 Work（母作品名），那不是副标题
    assert draft.rating == 8.0  # Rating 80 是百分制星级
    assert draft.notes == "备注：合成数据 example.invalid"
    assert draft.creators == ["Ann", "Ben"]
    assert draft.extra == {
        "album": "Greatest",
        "artists": ["Ann", "Ben"],
        "album_artists": ["Ann"],
        "filename": "Track One.mp3",
        "track_number": 3,
        "duration_seconds": 213,  # Total Time 是毫秒
    }
    assert [(e.source, e.external_id) for e in draft.external_ids] == [
        ("itunes", "AAA0000000000001")
    ]


def test_itunes_imports_the_library(session: Session, itunes: ItunesXmlImporter) -> None:
    result = run_import(session, itunes)
    assert (result.total, result.inserted, result.skipped) == (4, 3, 1)
    # 缺 Name 的那条只让自己失败，不中断整批
    assert result.errors[0]["record"] == 4
    assert "Name" in result.errors[0]["message"]

    items = {item.title: item for item in repo.list_items(session, kind="music")}
    assert set(items) == {"合成曲 Hello", "Second Track", "Collab Track"}
    windows = items["Second Track"]
    assert windows.extra["filename"] == "02-Two.mp3"  # Windows 的 file://localhost/D:/…
    assert (windows.year, windows.rating) == (None, None)  # Rating 0 = 没评过
    assert windows.creators == ["Solo One"]
    assert items["Collab Track"].creators == ["Cid", "Dee", "Eee"]
    assert items["Collab Track"].rating == 2.0


def test_itunes_rating_zero_is_unrated() -> None:
    assert itunes_rating(0) is None
    assert itunes_rating(20) == 2.0
    assert itunes_rating(100) == 10.0
    assert itunes_rating("80") == 8.0
    assert itunes_rating(None) is None


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("file://localhost/Users/example/Music/Track%20One.mp3", "Track One.mp3"),
        ("file:///Users/example/Music/中文.mp3", "中文.mp3"),
        ("file://localhost/D:/Music/02-Two.mp3", "02-Two.mp3"),
        ("file://localhost/Users/example/Music/", None),
        (None, None),
        ("   ", None),
    ],
)
def test_filename_from_location(location: Any, expected: str | None) -> None:
    assert filename_from_location(location) == expected


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        ({"Kind": "播客"}, True),
        ({"File Category": "Podcast"}, True),
        ({"Podcast URL": "https://rss.example.invalid/feed.xml"}, True),
        ({"Kind": "MPEG audio file"}, False),
        ({}, False),
    ],
)
def test_is_podcast(track: dict[str, Any], expected: bool) -> None:
    assert is_podcast(track) is expected


def test_itunes_needs_a_tracks_dict(session: Session, tmp_path: Path) -> None:
    """整份文件不是资料库导出：fetch 阶段就失败，只记一条错误，不抛给调用方。"""
    broken = tmp_path / "broken.xml"
    broken.write_text(
        '<?xml version="1.0"?>\n<plist version="1.0"><dict>'
        "<key>Major Version</key><integer>1</integer>"
        "</dict></plist>",
        encoding="utf-8",
    )
    result = run_import(session, ItunesXmlImporter(broken))
    assert result.total == 0
    assert "Tracks" in result.errors[0]["message"]


def test_itunes_merges_into_a_file_derived_item(session: Session, tmp_path: Path, itunes) -> None:
    """拿 XML 去补本地文件导入的那条：内容判重认出来，只补空字段，并挂上 itunes 的 ID。"""
    import_manifest(
        session, tmp_path, "m.json", [track_payload("合成曲 Hello", artists="Ann & Ben")]
    )
    file_item = repo.list_items(session, kind="music")[0]
    assert file_item.extra["filename"] == "合成曲 Hello.mp3"

    result = run_import(session, itunes)
    assert (result.total, result.inserted, result.updated, result.skipped) == (4, 2, 1, 1)
    assert len(repo.list_items(session, kind="music")) == 3

    merged = repo.get_item(session, file_item.id)
    assert merged is not None
    assert merged.rating == 8.0  # 本地文件没评分，XML 的星级补得进来
    assert merged.extra["filename"] == "合成曲 Hello.mp3"  # 已有的文件名不被覆盖
    sources = {link.source for link in repo.list_external_ids(session, merged.id)}
    assert sources == {"itunes"}


def test_itunes_reimport_recognizes_its_own_ids(session: Session, itunes) -> None:
    """第二次导同一份 XML 走 ID 认亲（规则 1），不再按内容找一遍，也不会插第二条。"""
    run_import(session, itunes)
    result = run_import(session, itunes)
    assert (result.total, result.inserted, result.updated, result.unchanged) == (4, 0, 0, 3)
    assert len(repo.list_items(session, kind="music")) == 3


# ---------------------------------------------------------------------------- CLI


def test_cli_scan_music_imports(tmp_path: Path, cli: Any, session: Session) -> None:
    """``omnishelf scan-music --import``：扫目录 -> 写清单 -> 入库，一条命令走完。"""
    library = tmp_path / "library"
    library.mkdir()
    (library / "01 - Tagged.mp3").write_bytes(CASES["a_id3v23.mp3"]())
    out = tmp_path / "manifest.json"

    result = cli("scan-music", str(library), "--out", str(out), "--import")
    assert result.exit_code == 0, result.output
    assert "扫了 1 个音频文件" in result.output
    assert json.loads(out.read_text(encoding="utf-8"))["items"][0]["title"] == "合成标题"
    assert repo.count_items(session, kind="music") == 1

    again = cli("scan-music", str(library), "--out", str(out), "--import", "--json")
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["import"]["inserted"] == 0


def test_cli_scan_music_without_import_leaves_the_library_alone(
    tmp_path: Path, cli: Any, session: Session
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "01 - Tagged.mp3").write_bytes(CASES["a_id3v23.mp3"]())
    result = cli("scan-music", str(library), "--out", str(tmp_path / "m.json"))
    assert result.exit_code == 0, result.output
    assert "omnishelf import -i music_files" in result.output
    assert repo.count_items(session, kind="music") == 0
