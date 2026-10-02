"""``audiotags``：手写音频标签解析器的测试（合成字节见 ``tests/audio_synth.py``）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnishelf.audiotags import (
    MAX_TAG_BYTES,
    AudioTags,
    mpeg_frame_header,
    parse_track_number,
    parse_year,
    popm_to_rating,
    read_tags,
    split_artists,
)
from tests.audio_synth import (
    CASES,
    aiff_comm,
    atom,
    comment_frame,
    flac,
    flac_block,
    flac_streaminfo,
    id3_frame,
    id3v1,
    id3v2,
    ilst,
    mp3_audio,
    mvhd,
    popm_frame,
    riff_chunk,
    riff_container,
    synth_files,
    vorbis_comment,
    wav,
    wav_data,
    wav_fmt,
)


@pytest.fixture(scope="module")
def audio_dir(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return synth_files(tmp_path_factory.mktemp("audio"))


def tags_of(audio_dir: dict[str, Path], name: str) -> AudioTags:
    return read_tags(audio_dir[name])


# ------------------------------------------------------------------ 各容器的读取结果

#: 每个用例期望读到的字段（没列出来的字段必须是 None / 空）。
EXPECTED: dict[str, dict[str, Any]] = {
    # ID3v2.3：文本帧 + COMM + POPM + Xing 帧数；尾部那个 ID3v1 必须被压住
    "a_id3v23.mp3": {
        "title": "合成标题",
        "subtitle": "Synthetic Subtitle",
        "artists": ["Alpha", "Beta", "Gamma"],
        "album": "Synthetic Album",
        "album_artists": ["Various People", "Other Names"],
        "year": 2005,
        "track_number": 3,
        "duration_seconds": 26,  # Xing 说 1000 帧 @44100 = 26 秒
        "rating": 8.0,  # POPM 204 -> 204/25.5 = 8
        "notes": "备注：合成数据 example.invalid",
    },
    # ID3v2.4：同步安全帧长；没 Xing 时按 CBR 折算（200 帧 ≈ 5 秒），TLEN 不抢帧算的时长
    "b_id3v24.mp3": {
        "title": "Vfour Title",
        "duration_seconds": 5,
    },
    # 只有 ID3v1.1：曲目号藏在注释的最后两个字节里
    "c_id3v1.mp3": {
        "title": "V1 Only",
        "artists": ["Solo Artist", "Second One"],
        "album": "V1 Album",
        "year": 1988,
        "track_number": 7,
        "duration_seconds": 1,
        "notes": "old comment",
    },
    # M4A：ilst 文本 + trkn 二进制 + mvhd 时长；3KB 的 covr 直接跳过
    "d_track.m4a": {
        "title": "M4A Title",
        "artists": ["A", "B"],
        "album_artists": ["Album Person"],
        "album": "M4A Album",
        "year": 2019,
        "track_number": 12,
        "duration_seconds": 213,
        "notes": "m4a note",
    },
    # FLAC：VORBIS_COMMENT + STREAMINFO（441000 样本 / 44100 = 10 秒）
    "e_lossless.flac": {
        "title": "FLAC Title",
        "artists": ["X", "Y"],
        "album_artists": ["FLAC People"],
        "album": "FLAC Album",
        "year": 2011,
        "track_number": 12,
        "duration_seconds": 10,
        "notes": "flac note",
    },
    # WAV：LIST/INFO 那一套四个字母的 ID
    "f_wave.wav": {
        "title": "Wav Title",
        "artists": ["Wav Artist", "Two"],
        "album": "Wav Album",
        "year": 2001,
        "track_number": 4,
        "duration_seconds": 1,
        "notes": "wav note",
    },
    # WAV 头上挂 ID3v2：标签从 ID3 来，时长还是按 WAV 自己算
    "g_wave_id3.wav": {
        "title": "Wav Id3 Title",
        "artists": ["Id3 Artist"],
        "duration_seconds": 1,
    },
    # AIFF：大端 FORM + comm 里的 80 位扩展精度采样率
    "h_aiff.aiff": {
        "title": "Aiff Title",
        "artists": ["Aiff Artist"],
        "duration_seconds": 1,
        "notes": "aiff note",
    },
    # 文本帧的三种编码混用：UTF-16、带 BOM 的 UTF-8、真正的 latin-1
    "i_encodings.mp3": {
        "title": "UTF16 标题",
        "album": "BOM 专辑",
        "artists": ["Björk"],
        "duration_seconds": 1,
    },
    # 正文不是合法 MPEG：只有 TLEN 给得出时长（v2.4 是毫秒）
    "j_tlen.mp3": {
        "title": "Tlen Only",
        "duration_seconds": 180,
    },
}

ALL_FIELDS = (
    "title",
    "subtitle",
    "artists",
    "album_artists",
    "album",
    "year",
    "track_number",
    "duration_seconds",
    "rating",
    "notes",
)

#: 人物字段没标签时读回来是空列表，其它字段是 None：断言「没列出来」的字段按类型缺省。
LIST_FIELDS = frozenset({"artists", "album_artists"})


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_container_tags(name: str, audio_dir: dict[str, Path]) -> None:
    tags = tags_of(audio_dir, name)
    assert tags.errors == [], tags.errors
    assert tags.filename == name
    for field_name in ALL_FIELDS:
        default: Any = [] if field_name in LIST_FIELDS else None
        want = EXPECTED[name].get(field_name, default)
        assert getattr(tags, field_name) == want, f"{name}.{field_name}"


def test_id3v2_beats_trailing_id3v1(audio_dir: dict[str, Path]) -> None:
    """v2.3 和 v1 同时存在时取 v2（那个 ``IGNORED`` 绝不能进来）。"""
    tags = tags_of(audio_dir, "a_id3v23.mp3")
    assert "IGNORED" not in repr(tags)


def test_unknown_container_is_reported_not_raised(audio_dir: dict[str, Path]) -> None:
    tags = tags_of(audio_dir, "k_unknown.txt")
    assert not tags.has_tags
    assert tags.errors and "认不出容器格式" in tags.errors[0]


def test_empty_file_is_reported_not_raised(audio_dir: dict[str, Path]) -> None:
    tags = tags_of(audio_dir, "l_empty.mp3")
    assert tags.title is None
    assert tags.errors == ["空文件"]


def test_missing_file_never_raises(tmp_path: Path) -> None:
    """读不到文件也只记错误：扫描几千个文件时不能让一个坏文件中断整批。"""
    tags = read_tags(tmp_path / "根本不存在.mp3")
    assert tags.errors
    assert not tags.has_tags


def test_all_cases_are_covered() -> None:
    """合成用例加了新文件却忘了写期望，这条会挡住。"""
    assert set(EXPECTED) | {"k_unknown.txt", "l_empty.mp3"} == set(CASES)


# ---------------------------------------------------------------------- 逐条构造的用例


def test_id3v1_only_track_and_latin1(tmp_path: Path) -> None:
    """ID3v1 是定长 latin-1 块：曲目号藏在注释末尾，中文根本写不进去。"""
    path = tmp_path / "v1.mp3"
    path.write_bytes(mp3_audio(50, xing_frames=50) + id3v1("T", "A, B", "Al", "1999", "note", 12))
    tags = read_tags(path)
    assert (tags.title, tags.album, tags.year, tags.track_number) == ("T", "Al", 1999, 12)
    assert tags.artists == ["A", "B"]


def test_multiple_popm_frames_take_the_highest(tmp_path: Path) -> None:
    """不同软件各写一帧 POPM（一帧百分制、一帧 255 制）：取高的那个，不取最后读的。"""
    path = tmp_path / "popm.mp3"
    path.write_bytes(
        id3v2([id3_frame("TIT2", "T"), popm_frame(60), popm_frame(204)]) + mp3_audio(8)
    )
    assert read_tags(path).rating == 8.0


def test_comm_frame_with_description(tmp_path: Path) -> None:
    """COMM 的「简短描述」不能混进备注里。"""
    path = tmp_path / "comm.mp3"
    path.write_bytes(
        id3v2([id3_frame("TIT2", "T"), comment_frame("正文", description="Rating")])
        + mp3_audio(8)
    )
    assert read_tags(path).notes == "正文"


def test_tlen_is_only_a_fallback(tmp_path: Path) -> None:
    """能按帧算时长时，TLEN（v2.3 是秒）不抢——它只在帧头找不到的时候兜底。"""
    path = tmp_path / "tlen.mp3"
    path.write_bytes(id3v2([id3_frame("TIT2", "T"), id3_frame("TLEN", "999")]) + mp3_audio(200))
    assert read_tags(path).duration_seconds == 5


def test_flac_without_vorbis_block(tmp_path: Path) -> None:
    """只有 STREAMINFO 的 FLAC：时长读得到，标签为空但不算失败。"""
    path = tmp_path / "bare.flac"
    path.write_bytes(flac([(0, flac_streaminfo(48000, 2, 24, 480000), True)]) + bytes(64))
    tags = read_tags(path)
    assert tags.duration_seconds == 10
    assert tags.title is None
    assert tags.errors == []


def test_flac_oversized_comment_is_skipped_with_error(tmp_path: Path) -> None:
    """注释块声明得比整个标签区还大（把音频数据算进去了）：整块跳过并记下原因，
    而不是读回一堆乱码字段冒充标签。"""
    path = tmp_path / "huge.flac"
    fake_head = bytes([0x84]) + (MAX_TAG_BYTES + 1).to_bytes(3, "big")
    path.write_bytes(
        b"fLaC"
        + flac_block(0, flac_streaminfo(44100, 2, 16, 44100))
        + fake_head
        + vorbis_comment(["TITLE=不该被读到"])
    )
    tags = read_tags(path)
    assert tags.title is None
    assert tags.duration_seconds == 1
    assert any("超过" in error for error in tags.errors)


def test_mp4_persistent_metadata_only_still_gives_duration(tmp_path: Path) -> None:
    """只有 ``mvhd`` 没有 ilst 的 m4a：时长照读，标签为空、无错误。"""
    path = tmp_path / "no_tags.m4a"
    path.write_bytes(atom(b"ftyp", b"M4A " + bytes(8)) + mvhd(1000, 123456))
    tags = read_tags(path)
    assert tags.duration_seconds == 123
    assert tags.title is None
    assert tags.errors == []


def test_aiff_annos_only(tmp_path: Path) -> None:
    path = tmp_path / "anno.aiff"
    path.write_bytes(
        riff_container(
            aiff_comm(44100, 1, 16) + riff_chunk(b"ANNO", b"only note", big=True),
            b"AIFF",
            big=True,
        )
    )
    tags = read_tags(path)
    assert (tags.notes, tags.title, tags.duration_seconds) == ("only note", None, 1)


def test_wav_without_list_info(tmp_path: Path) -> None:
    """没有 LIST/INFO 的 WAV（采集出来的原始文件）：只有时长。"""
    path = tmp_path / "raw.wav"
    path.write_bytes(wav(wav_fmt(44100, 2, 16) + wav_data(44100, 2, 16, 3)))
    tags = read_tags(path)
    assert (tags.duration_seconds, tags.title, tags.errors) == (3, None, [])


def test_truncated_riff_declared_length(tmp_path: Path) -> None:
    """声明长度超过文件实际长度（截断/复制粘贴的头）：夹到文件末尾，不抛。"""
    path = tmp_path / "broken.wav"
    children = wav_fmt(44100, 2, 16) + b"data" + (44100 * 2 * 2).to_bytes(4, "little")
    path.write_bytes(wav(children + bytes(16)))
    tags = read_tags(path)
    assert tags.duration_seconds is not None


def test_mpeg_frame_header_rejects_non_frames() -> None:
    assert mpeg_frame_header(b"ID3\x03\x00\x00") is None
    assert mpeg_frame_header(b"\xff\xfb\x90\xc0") is not None


# --------------------------------------------------------------------------- 切分/换算

SPLIT_CASES: list[tuple[str, list[str]]] = [
    ("Alpha & Beta feat. Gamma", ["Alpha", "Beta", "Gamma"]),
    ("A、B；C; D", ["A", "B", "C", "D"]),
    ("Solo One", ["Solo One"]),
    ("", []),
    ("やなぎなぎ/麻枝准", ["やなぎなぎ", "麻枝准"]),
    ("Current Storage (momi、ぴょん吉)", ["Current Storage (momi、ぴょん吉)"]),
    ("STANKY (A/B)", ["STANKY (A/B)"]),
    ("R&B", ["R&B"]),
    (" horn / wood ", ["horn", "wood"]),
]


@pytest.mark.parametrize(("text", "expected"), SPLIT_CASES)
def test_split_artists(text: str, expected: list[str]) -> None:
    assert split_artists(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("horn/wood", ["horn/wood"]),
        ("ミニチュアキマイラ1/24", ["ミニチュアキマイラ1/24"]),
        ("1/2", ["1/2"]),
    ],
)
def test_cjk_slash_guards(text: str, expected: list[str]) -> None:
    """斜杠协作只在「有中日文且不是数字」时拆（与 ``split_creators`` 的保护一致）。"""
    assert split_artists(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2005", 2005), ("2019-11-03", 2019), ("(C) 1999 Foo", 1999), ("", None), ("Demo", None), (2005, 2005)],
)
def test_parse_year(value: Any, expected: int | None) -> None:
    assert parse_year(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("03/12", 3), ("12 of 16", 12), ("5", 5), ("5／3", 5), ("A", None), (None, None), ("", None)],
)
def test_parse_track_number(value: Any, expected: int | None) -> None:
    assert parse_track_number(value) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, None), (1, 0.1), (50, 5.0), (100, 10.0), (128, 5.0), (204, 8.0), (255, 10.0), (300, 10.0)],
)
def test_popm_to_rating(raw: int, expected: float | None) -> None:
    """两套口径都得落进 0-10：五星恒为 10，二星恒为 2。"""
    assert popm_to_rating(raw) == expected


def test_popm_two_star_is_two() -> None:
    assert popm_to_rating(51) == 5.1 and popm_to_rating(128) == 5.0


# ----------------------------------------------------------------------- AudioTags


def test_fill_only_sets_empty_fields() -> None:
    tags = AudioTags(path=Path("x.mp3"), title="已有标题")
    tags.fill(title="兜底标题", album="Album")
    assert (tags.title, tags.album) == ("已有标题", "Album")


def test_has_tags_ignores_filename() -> None:
    assert not AudioTags(path=Path("x.mp3")).has_tags
    assert AudioTags(path=Path("x.mp3"), duration_seconds=10).has_tags
