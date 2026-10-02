"""本地音频文件的标签读取：只用标准库，只收音乐导入要的那 11 项。

为什么自己解析：项目依赖里没有任何标签库（``mutagen`` / ``tinytag`` 都不在），
而多一个依赖就多一条「装不上就跑不了」的路；音乐文件的标签块都排布在容器头部，
用标准库做字节切片足够读出来（而且只 seek/read 需要的那几百字节，不整读文件）。

**只收集这些**（多一项都不写，口径见 ``docs/development.md`` 的「字段口径」一节）：
标题、副标题、评分、备注、艺术家、专辑、专辑艺术家、年份、编号、时长、原始文件名。
所以这里**没有**流派、封面、编码器、碟号、总曲数。

支持的容器：

===========  ==========================================================
后缀          标签位置
===========  ==========================================================
``.mp3``     ID3v2.2/2.3/2.4（``TIT2`` 那一套帧）+ 文件末尾 ID3v1 兜底
``.m4a``     MP4 ``moov/udta/meta/ilst`` 里的 ``©nam`` / ``©ART`` / ``aART`` 原子
``.flac``    ``VORBIS_COMMENT`` 块，时长来自 ``STREAMINFO``
``.wav``     文件头的 ID3v2（多数打标签软件写这儿）与 ``LIST/INFO`` 子块
``.aiff``    ``ID3 `` 块优先，其次 ``NAME`` / ``AUTH`` / ``ANNO``，时长来自 ``comm``
===========  ==========================================================

读不了的（宁可在 ``errors`` 里说明，也不给一份空数据冒充有标签）：
``.ogg`` / ``.opus``（Ogg 页要先验 CRC 再重组分包，成本与收益不成比例）、
``.wv`` / ``.ape``，以及 M4A 的 ``rtng`` 原子——它在 iTunes 语义里是**内容分级**
（Explicit），和用户打的星级区分不开，所以不猜：M4A 的评分走 iTunes 资料库导入拿。

时长：MP3 用 ``Xing`` / ``Info`` / ``VBRI`` 里的总帧数（VBR 也准），
读不到才按第一帧码率做 CBR 估算；MP4 用 ``mvhd``；FLAC 用 ``STREAMINFO``；
WAV 用 ``data`` 块字节数除以 ``fmt`` 的 byte rate；AIFF 用 ``comm`` 的帧数/采样率。

评分：ID3v2 ``POPM`` 是一字节 0-255，但现实里有两套口径——按百分制写的
（20/40/60/80/100）和按满值 255 写的星级（51/102/153/204/255）。
两套的「五星」都落在 10 分，所以以 100 为界分别换算（见 ``popm_to_rating``）。

任何单个文件解析失败都只会进 ``AudioTags.errors``，**不往外抛**——
一次扫描要过几千个文件，不能因为某个文件的标签块写得歪就整批停下来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

#: 单个标签块最多读进内存的字节数（超大封面只会被跳过，不会进内存）。
MAX_TAG_BYTES = 8 * 1024 * 1024
#: 找第一帧 MPEG 帧头时往前扫的窗口。
FRAME_SCAN_WINDOW = 4096

_ID3_MAGIC = b"ID3"
_ID3_2_MAGIC = b"ID2"
_FRAME_ID = re.compile(rb"^[A-Z][A-Z0-9]{3}$")

#: 单值文本帧 -> ``AudioTags`` 字段。
_TEXT_FRAME = {"TIT2": "title", "TIT3": "subtitle", "TALB": "album"}
#: 人物帧（要切多个艺术家）。
_PERSON_FRAME = {"TPE1": "artists", "TPE2": "album_artists"}
#: 同义的年份帧，先到先得。
_YEAR_FRAME = ("TYER", "TDRC", "TDOR", "TRDA")
#: 音轨号常见写法：``12``、``12/16``、``03``、``12 of 16``。
_TRACK_RE = re.compile(
    r"^\s*0*(\d{1,4})(?:\s*(?:/|／|of)\s*0*\d{1,4})?\s*$",
    re.IGNORECASE,
)

#: ID3v2.2 的三字母帧名 -> 四字母（只映射用得上的）。
_ID3_2_FRAMES = {
    "TT2": "TIT2",
    "TT3": "TIT3",
    "TP1": "TPE1",
    "TP2": "TPE2",
    "TAL": "TALB",
    "TYE": "TYER",
    "TRK": "TRCK",
    "COM": "COMM",
    "POP": "POPM",
    "TLE": "TLEN",
}

# --------------------------------------------------------------------- 艺术家切分

#: 音乐特有的一批分隔符。走 ``repository.split_creators`` 的括号保护来切，
#: **不改全局默认规则**（``ItemDraft.creators`` 对所有 kind 仍按原口径）。
#:
#: * ``&`` 必须至少一侧有空白——``R&B`` 是一个词，``A & B`` 才是两个人；
#: * ``feat.`` / ``ft.`` / ``featuring`` 要空白包围；
#: * ``、`` 和 ``；``（中日文顿号/分号）两侧常不写空格，直接切；
#: * ASCII 分号只在后面跟空白时切（同理，别误伤名字里的符号）。
_MUSIC_SPLIT_RE = re.compile(
    r"\s+&\s*|&\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+|、|；|;\s+",
    re.IGNORECASE,
)
#: 中日文名字之间常用**不带空白的斜杠**表并列（``やなぎなぎ/麻枝准``）。
_CJK_SLASH_RE = re.compile(r"\s*/\s*")
_CJK_RE = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def split_artists(text: str) -> list[str]:
    """把一串艺术家切成列表：通用规则 + 音乐特有写法，**括号里的分隔符不拆**。

    ``"A & B"`` -> ``["A", "B"]``；``"Current Storage (momi、ぴょん吉)"`` -> 一个名字；
    ``"R&B"`` -> 一个名字。
    """
    from .repository import split_creators

    pieces: list[str] = []
    for piece in split_creators(text):  # 逗号、带空白的斜杠（通用规则）
        pieces.extend(split_creators(piece, _MUSIC_SPLIT_RE))
    found: list[str] = []
    for piece in pieces:
        for name in _slash_or_keep(piece):
            name = name.strip()
            if name and name not in found:
                found.append(name)
    return found


def _slash_or_keep(piece: str) -> list[str]:
    """``A/B`` 只在「至少一侧含中日文」且「没有一侧是纯数字」时才当两个人。

    ``horn/wood``（没有中日文）与 ``ミニチュアキマイラ1/24``（右侧纯数字）都保持一个名字，
    和 ``repository.split_creators()`` 的保护规则保持一致
    （见 ``docs/development.md`` 的「字段口径」一节）。
    """
    from .repository import split_creators

    parts = split_creators(piece, _CJK_SLASH_RE)
    if len(parts) < 2 or not any(_CJK_RE.search(part) for part in parts):
        return [piece]
    if any(part.isdigit() for part in parts):
        return [piece]
    return parts


# --------------------------------------------------------------------- 数值/文本换算


def parse_year(value: Any) -> int | None:
    """从 ``"2005"`` / ``"2005-11-03"`` / ``"(C) 2005"`` 里取年份。"""
    match = re.search(r"(1[0-9]{3}|2[0-9]{3})", str(value if value is not None else ""))
    return int(match.group(1)) if match else None


def parse_track_number(value: Any) -> int | None:
    """``"12/16"`` -> 12；``"03"`` -> 3；``"12 of 16"`` -> 12；``"A"`` -> None。"""
    match = _TRACK_RE.match(str(value if value is not None else ""))
    return int(match.group(1)) if match else None


def popm_to_rating(byte_value: int) -> float | None:
    """``POPM`` 的一字节 -> 我们的 0-10 评分。

    以 100 为界分两套口径换算：``<=100`` 当百分制（``/10``），``>100`` 当满值 255 的星级
    （``/25.5``）。两套的五星都落在 10、二星都落在 2，边界处自洽。
    """
    if byte_value <= 0:
        return None
    raw = byte_value / 10.0 if byte_value <= 100 else byte_value / 25.5
    return round(min(10.0, raw), 1)


# ------------------------------------------------------------------------- 数据载体


@dataclass(slots=True)
class AudioTags:
    """一个音频文件读出来的信息，就是音乐导入要收的那 11 项。

    ``errors`` 不是导入字段，而是「这份数据有多可信」的记账：解析意外、容器认不出来、
    标签块太大被跳过，都写在这里——调用方因此能区分「真的没标签」和「读挂了」。
    """

    path: Path
    title: str | None = None
    subtitle: str | None = None
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    album_artists: list[str] = field(default_factory=list)
    year: int | None = None
    track_number: int | None = None
    duration_seconds: int | None = None
    rating: float | None = None
    notes: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        """原始文件名（第 11 项）：标题往后被改过也还能回查是哪个文件。"""
        return self.path.name

    @property
    def has_tags(self) -> bool:
        """除文件名之外，有没有读到任何一项。"""
        return any(
            (
                self.title,
                self.subtitle,
                self.artists,
                self.album,
                self.album_artists,
                self.year,
                self.track_number,
                self.duration_seconds,
                self.rating,
                self.notes,
            )
        )

    def fill(self, **values: Any) -> None:
        """**只在字段还是空的时候**写入：后读的兜底来源不覆盖先读到的正经标签。"""
        for key, value in values.items():
            if value in (None, "", [], ()):
                continue
            current = getattr(self, key)
            if isinstance(current, list):
                if not current:
                    setattr(self, key, list(value))
            elif current in (None, ""):
                setattr(self, key, value)


# ------------------------------------------------------------------------- 字节工具


def _synchsafe(raw: bytes) -> int:
    """ID3v2 的「同步安全整数」：每字节只用低 7 位。"""
    value = 0
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
    return value


def _be(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


def _le(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _u32(raw: bytes, offset: int) -> int | None:
    """在 ``offset`` 处取大端 4 字节；越界返回 None（坏文件不能靠异常拦）。"""
    chunk = raw[offset : offset + 4]
    return _be(chunk) if len(chunk) == 4 else None


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\x00", " ")).strip()


def _decode_scheme_bytes(scheme: int, body: bytes) -> str:
    if scheme == 0:
        return _latin1_or_utf8(body)
    if scheme == 1:
        return body.decode("utf-16", "replace")
    if scheme == 2:
        return body.decode("utf-16-be", "replace")
    # 编码号 3 按规定是「不带 BOM 的 UTF-8」，可有些标签软件还是把 BOM 写进去了；
    # 留着它会在标题前面显出一个不可见的 \ufeff，所以进门先摘掉。
    return body.removeprefix(b"\xef\xbb\xbf").decode("utf-8", "replace")


def _decode_by_scheme(payload: bytes) -> str:
    """按 ID3v2 文本帧首字节的编码号解码；返回的串里多值仍以 ``\\x00`` 相连。"""
    if not payload:
        return ""
    return _decode_scheme_bytes(payload[0], payload[1:])


def _latin1_or_utf8(body: bytes) -> str:
    """编码号 0 按规定是 ISO-8859-1，但**大量中文标签是拿 0 当 UTF-8 用的**。

    所以先试 UTF-8：那串字节本身构成合法 UTF-8 就按 UTF-8 解。真正的 latin-1 标题
    （``Björk``、``café`` 那种单字节重音）不构成合法 UTF-8 序列，不会被误转。
    """
    if any(byte >= 0x80 for byte in body):
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return body.decode("latin-1", "replace")


def _text_values(payload: bytes) -> list[str]:
    return [part.strip() for part in _decode_by_scheme(payload).split("\x00") if part.strip()]


# ---------------------------------------------------------------------------- ID3v2


def read_id3v2(fh: BinaryIO, start: int, size: int, tags: AudioTags) -> int:
    """读 ID3v2 块填进 ``tags``，返回**标签之后的偏移**（没有标签就原样返回 ``start``）。

    覆盖 v2.2（``ID2``、三字母帧名、三字节长度）、v2.3（普通整数帧长）与
    v2.4（同步安全整数帧长 + 可选 10 字节 footer），外加整体去同步旗标和扩展头。
    """
    fh.seek(start)
    head = fh.read(10)
    if len(head) < 10:
        return start
    if head[:3] == _ID3_2_MAGIC:
        version, flags, tag_size, footer = 2, 0, _be(b"\x00" + head[4:7]), False
    elif head[:3] == _ID3_MAGIC:
        version = 4 if head[3] == 4 else 3
        flags = head[5]
        tag_size = _synchsafe(head[6:10])
        footer = version == 4 and bool(flags & 0x10)
    else:
        return start
    if tag_size <= 0 or start + 10 + tag_size > size:
        tags.errors.append(f"ID3v2 声明的长度越界（{tag_size}），跳过标签")
        return start

    body = fh.read(min(tag_size, MAX_TAG_BYTES))
    if len(body) < tag_size:
        tags.errors.append("ID3v2 标签块过大，只读了前面一部分，可能少字段")
    if version != 2 and flags & 0x80:  # 整体去同步：0xFF 0x00 是转义出来的 0xFF
        body = body.replace(b"\xFF\x00", b"\xFF")

    pos = 0
    if version != 2 and flags & 0x40:  # 扩展头
        if version == 3:
            size_field = _u32(body, 0)
            pos = 4 + size_field if size_field is not None else 0
        else:
            pos = _synchsafe(body[0:4])  # v2.4 的扩展头长度含自己
        if pos > len(body):
            pos = 0
    _apply_id3_frames(body, pos, version, tags)
    return min(start + 10 + tag_size + (10 if footer else 0), size)


def _apply_id3_frames(body: bytes, pos: int, version: int, tags: AudioTags) -> None:
    header = 6 if version == 2 else 10
    tlen_seconds: float | None = None
    while pos + header <= len(body):
        raw_id = body[pos : pos + (3 if version == 2 else 4)]
        if raw_id == b"\x00" * len(raw_id):
            break  # 填充区：后面没有帧了
        if version == 2:
            name = _ID3_2_FRAMES.get(raw_id.decode("latin-1", "replace"), "")
            frame_size = _be(b"\x00" + body[pos + 3 : pos + 6])
        else:
            if not _FRAME_ID.match(raw_id):
                break  # 不是帧名的东西 = 尾部垃圾
            name = raw_id.decode("latin-1", "replace")
            # v2.4 帧长是同步安全整数、v2.3 是普通整数，读错会整块串位
            frame_size = (
                _synchsafe(body[pos + 4 : pos + 8]) if version == 4 else _be(body[pos + 4 : pos + 8])
            )
        if frame_size <= 0:
            break
        payload = body[pos + header : pos + header + frame_size]
        if len(payload) < frame_size:
            break  # 标签被截断（已经 warning 过）
        pos += header + frame_size
        values = _text_values(payload)
        if name in _TEXT_FRAME:
            if values:
                tags.fill(**{_TEXT_FRAME[name]: _clean(values[0])})
        elif name in _PERSON_FRAME:
            joined = " / ".join(values)  # 同帧多值先拼起来，再走统一切分
            if joined:
                tags.fill(**{_PERSON_FRAME[name]: split_artists(joined)})
        elif name in _YEAR_FRAME:
            if values:
                tags.fill(year=parse_year(values[0]))
        elif name == "TRCK":
            if values:
                tags.fill(track_number=parse_track_number(values[0]))
        elif name == "COMM":
            tags.fill(notes=_comment_text(payload))
        elif name == "POPM":
            rating = _popm_rating(payload)
            if rating is not None and (tags.rating is None or rating > tags.rating):
                tags.rating = rating  # 多个软件各写一帧 POPM 时取最高，见模块 docstring
        elif name == "TLEN":
            # v2.3 这帧是秒、v2.4 是毫秒；时长以按帧算出来的为准，这里只当兜底
            if values and values[0].isdigit():
                tlen_seconds = int(values[0]) / (1000 if version == 4 else 1)
    if tlen_seconds and not tags.duration_seconds:
        tags.duration_seconds = int(round(tlen_seconds))


def _comment_text(payload: bytes) -> str:
    """``COMM``：编码字节 + 3 字节语言 + 简短描述（以 0 结尾）+ 正文。"""
    if len(payload) < 5:
        return ""
    scheme = payload[0]
    body = payload[4:]
    tail = body[_terminator_offset(body, 2 if scheme in (1, 2) else 1) :]
    return _clean(_decode_scheme_bytes(scheme, tail).split("\x00", 1)[-1])


def _terminator_offset(raw: bytes, unit: int) -> int:
    """第一个「``unit`` 个 0」之后的位置（没有就整段长度）。"""
    if unit == 1:
        index = raw.find(b"\x00")
        return 0 if index < 0 else index + 1
    for index in range(0, len(raw) - 1, 2):
        if raw[index] == 0 and raw[index + 1] == 0:
            return index + 2
    return len(raw)


def _popm_rating(payload: bytes) -> float | None:
    index = payload.find(b"\x00")  # 邮箱（以 0 结尾）之后的那一字节才是分数
    if index < 0 or index + 1 >= len(payload):
        return None
    return popm_to_rating(payload[index + 1])


def read_id3v1(fh: BinaryIO, size: int, tags: AudioTags) -> None:
    """文件末尾 128 字节的 ``TAG``：只用来补 ID3v2 没给到的字段。"""
    if size < 128:
        return
    fh.seek(size - 128)
    block = fh.read(128)
    if len(block) < 128 or block[:3] != b"TAG":
        return

    def chunk(raw: bytes) -> str:
        return _clean(raw.split(b"\x00", 1)[0].decode("latin-1", "replace"))

    track: int | None = None
    # 记录格式：TAG(3) title(30) artist(30) album(30) year(4) comment(30) genre(1)
    comment = block[97:127]
    if comment[28] == 0 and comment[29] != 0:  # v1.1 把最后两字节挪用成了音轨号
        track = comment[29]
        comment = comment[:28]
    tags.fill(
        title=chunk(block[3:33]),
        artists=split_artists(chunk(block[33:63])),
        album=chunk(block[63:93]),
        year=parse_year(chunk(block[93:97])),
        notes=chunk(comment),
        track_number=track,
    )


# -------------------------------------------------------------------------- MPEG 帧

#: ``index -> kbps``（索引 0 是「免费/保留」，0xF 非法），按 (是否 Layer I, 是否 MPEG1) 分四张。
_BITRATE_TABLES: dict[tuple[bool, bool], tuple[int, ...]] = {
    (False, True): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (True, True): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (False, False): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (True, False): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 224),
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def mpeg_frame_header(raw: bytes) -> dict[str, int] | None:
    """解析 4 字节 MPEG 帧头（码率/采样率/每帧样本数/帧长）；不是合法帧头返回 None。"""
    if len(raw) < 4 or raw[0] != 0xFF or (raw[1] & 0xE0) != 0xE0:
        return None
    version_id = (raw[1] >> 3) & 0x03  # 3=MPEG1, 2=MPEG2, 0=MPEG2.5（1 保留）
    layer_bits = (raw[1] >> 1) & 0x03  # 3=Layer I, 2=II, 1=III（0 保留）
    if version_id == 1 or layer_bits == 0:
        return None
    layer = 4 - layer_bits
    bitrate_index = (raw[2] >> 4) & 0x0F
    rate_index = (raw[2] >> 2) & 0x03
    padding = (raw[2] >> 1) & 0x01
    if bitrate_index in (0, 0xF) or rate_index == 3:
        return None
    table = _BITRATE_TABLES[(layer == 1, version_id == 3)]
    bitrate = table[bitrate_index]
    sample_rate = _SAMPLE_RATES[version_id][rate_index]
    if layer == 1:
        samples = 384
        frame_length = 12 * bitrate * 1000 // sample_rate + (4 if padding else 0)
    else:
        samples = 1152 if version_id == 3 else 576
        frame_length = 144 * bitrate * 1000 // sample_rate + padding
    if frame_length <= 0:
        return None
    return {
        "bitrate": bitrate,
        "sample_rate": sample_rate,
        "samples": samples,
        "frame_length": frame_length,
    }


def _find_first_frame(fh: BinaryIO, start: int, end: int) -> tuple[int, dict[str, int], bytes] | None:
    """在 ``start`` 之后的一小段里找第一帧，连帧头后的窗口一起返回（给 Xing 用）。"""
    window = min(end - start, FRAME_SCAN_WINDOW)
    if window <= 4:
        return None
    fh.seek(start)
    buf = fh.read(window)
    for index in range(len(buf) - 4):
        if buf[index] != 0xFF:
            continue
        frame = mpeg_frame_header(buf[index : index + 4])
        if frame is None or start + index + frame["frame_length"] > end:
            continue  # 越界的假同步字
        return start + index, frame, buf[index:]
    return None


def _xing_frames(buf: bytes) -> int | None:
    """第一帧之后的 ``Xing`` / ``Info`` / ``VBRI`` 头里的总帧数。"""
    head = buf[:80]
    offset = head.find(b"Xing")
    if offset < 0:
        offset = head.find(b"Info")
    if offset >= 0:
        flags = _u32(buf, offset + 4)
        if flags is not None and flags & 0x01:  # bit0 = 有帧数字段
            frames = _u32(buf, offset + 8)
            if frames is not None and 0 < frames < 10_000_000:
                return frames
    vbri = head.find(b"VBRI")
    if vbri >= 0:
        # VBRI: version(2) delay(2) quality(4) bytes(4) blocks(4) frames(4)
        frames = _u32(buf, vbri + 24)
        if frames is not None and 0 < frames < 10_000_000:
            return frames
    return None


def parse_mp3(fh: BinaryIO, size: int, tags: AudioTags) -> None:
    audio_start = read_id3v2(fh, 0, size, tags)
    has_v1 = False
    if size >= audio_start + 128:
        fh.seek(size - 128)
        has_v1 = fh.read(3) == b"TAG"
    audio_end = size - (128 if has_v1 else 0)

    found = _find_first_frame(fh, audio_start, audio_end)
    if found is None:
        if not tags.duration_seconds:
            tags.errors.append("没找到 MPEG 帧头，时长未知")
    else:
        frame_at, frame, buf = found
        total_frames = _xing_frames(buf)
        if total_frames:
            seconds = total_frames * frame["samples"] / frame["sample_rate"]
        else:
            # CBR 估算：整段音频字节按第一帧码率折算（VBR 却没写 Xing 时会偏）
            seconds = max(0, audio_end - frame_at) * 8 / (frame["bitrate"] * 1000)
        tags.duration_seconds = int(round(seconds))
    if has_v1:
        read_id3v1(fh, size, tags)


# -------------------------------------------------------------------------- MP4/M4A

_MP4_TEXT = {
    b"\xa9nam": "title",
    b"\xa9ART": "artists",
    b"aART": "album_artists",
    b"\xa9alb": "album",
    b"\xa9cmt": "notes",
}
_MP4_CONTAINERS = {b"moov", b"udta", b"meta", b"ilst", b"trak", b"mdia", b"minf", b"stbl"}


def _mp4_boxes(fh: BinaryIO, lo: int, hi: int, depth: int) -> list[tuple[bytes, int, int]]:
    """把原子树扫平，返回 ``(类型, 内容起点, 内容终点)``；``meta`` 会跳过它的版本位。"""
    boxes: list[tuple[bytes, int, int]] = []
    if depth > 8:
        return boxes
    pos = lo
    while pos + 8 <= hi:
        fh.seek(pos)
        head = fh.read(12)
        if len(head) < 8:
            break
        kind = head[4:8]
        box_size = _be(head[:4])
        header = 8
        if box_size == 1:  # 64 位长度（超大文件）
            if len(head) < 12:
                break
            box_size = _be(head[8:12])
            header = 16
        elif box_size == 0:  # 延伸到父范围末尾
            box_size = hi - pos
        if box_size < header:
            break  # 长度 0/1 之类的小值 = 已经不是原子了
        end = min(pos + box_size, hi)
        inner = pos + header
        if kind == b"meta":
            # ``meta`` 是 pseudo-box：多数实现插 4 字节 version/flags，个别直接跟 ``ilst``
            fh.seek(pos + header)
            if fh.read(4) != b"ilst":
                inner += 4
        boxes.append((kind, inner, end))
        if kind in _MP4_CONTAINERS:
            boxes.extend(_mp4_boxes(fh, inner, end, depth + 1))
        pos = end
    return boxes


def _mp4_atom_value(fh: BinaryIO, start: int, end: int) -> tuple[int, bytes]:
    """取 ``data`` 子原子的 ``(格式, 载荷)``：格式 1=UTF-8，2=UTF-16BE。"""
    for kind, lo, hi in _mp4_boxes(fh, start, end, 6):
        if kind == b"data":
            fh.seek(lo)
            payload = fh.read(hi - lo)
            if len(payload) < 8:
                return 1, b""
            return payload[3] & 0x0F, payload[8:]
    return 1, b""


def _mp4_text(fh: BinaryIO, start: int, end: int) -> str:
    fmt, raw = _mp4_atom_value(fh, start, end)
    if fmt == 2:
        return _clean(raw.decode("utf-16-be", "replace"))
    return _clean(raw.decode("utf-8", "replace"))


def parse_mp4(fh: BinaryIO, size: int, tags: AudioTags) -> None:
    for kind, lo, hi in _mp4_boxes(fh, 0, size, 0):
        if kind in _MP4_TEXT:
            value = _mp4_text(fh, lo, hi)
            if not value:
                continue
            name = _MP4_TEXT[kind]
            if name in ("artists", "album_artists"):
                tags.fill(**{name: split_artists(value)})
            else:
                tags.fill(**{name: value})
        elif kind == b"\xa9day":
            tags.fill(year=parse_year(_mp4_text(fh, lo, hi)))
        elif kind == b"trkn":
            _fmt, raw = _mp4_atom_value(fh, lo, hi)
            if len(raw) >= 4:
                tags.fill(track_number=_be(raw[2:4]) or None)
        elif kind == b"mvhd":
            fh.seek(lo)
            payload = fh.read(hi - lo)
            if len(payload) >= 32 and payload[0] == 1:  # 版本 1：时间戳各 8 字节
                timescale, duration = _be(payload[20:24]), _be(payload[24:32])
            elif len(payload) >= 20:
                timescale, duration = _be(payload[12:16]), _be(payload[16:20])
            else:
                continue
            if timescale > 0:
                tags.duration_seconds = int(round(duration / timescale))


# ----------------------------------------------------------------------------- FLAC


def _vorbis_comment_fields(blob: bytes) -> dict[str, list[str]]:
    """``VORBIS_COMMENT``：厂商串 + 条数 + 每条 ``KEY=value``（长度都是小端）。"""
    found: dict[str, list[str]] = {}
    pos = 0
    vendor_size = _u32_le(blob, 0)
    if vendor_size is None:
        return found
    pos = 4 + vendor_size
    count = _u32_le(blob, pos)
    if count is None:
        return found
    pos += 4
    for _ in range(count):
        entry_size = _u32_le(blob, pos)
        if entry_size is None:
            break
        pos += 4
        entry = blob[pos : pos + entry_size]
        if len(entry) < entry_size:
            break
        pos += entry_size
        key, sep, value = entry.decode("utf-8", "replace").partition("=")
        if sep:
            found.setdefault(key.strip().upper(), []).append(_clean(value))
    return found


def _u32_le(raw: bytes, offset: int) -> int | None:
    chunk = raw[offset : offset + 4]
    return _le(chunk) if len(chunk) == 4 else None


def parse_flac(fh: BinaryIO, size: int, tags: AudioTags) -> None:
    fh.seek(4)
    while True:
        block_head = fh.read(4)
        if len(block_head) < 4:
            return
        is_last = bool(block_head[0] & 0x80)
        block_type = block_head[0] & 0x7F
        length = _be(b"\x00" + block_head[1:])
        start = fh.tell()
        if block_type == 0 and length >= 18:  # STREAMINFO
            blob = fh.read(18)
            packed = _be(blob[10:18])
            sample_rate = packed >> 44
            total_samples = packed & ((1 << 36) - 1)
            if sample_rate and total_samples:
                tags.duration_seconds = int(round(total_samples / sample_rate))
        elif block_type == 4:  # VORBIS_COMMENT
            # 注释块正常只有几 KB，声明到几十 KB 以上就是把音频数据算进来了：
            # 整块跳过并记下，好过读回一堆乱码字段冒充标签。
            if length > MAX_TAG_BYTES:
                tags.errors.append(
                    f"VORBIS_COMMENT 块声明了 {length} 字节，超过 {MAX_TAG_BYTES}，跳过标签"
                )
            else:
                fields = _vorbis_comment_fields(fh.read(length))
                tags.fill(
                    title=(fields.get("TITLE") or [""])[0],
                    subtitle=(fields.get("SUBTITLE") or [""])[0],
                    album=(fields.get("ALBUM") or [""])[0],
                    notes=(fields.get("COMMENT") or [""])[0],
                )
                if fields.get("DATE"):
                    tags.fill(year=parse_year(fields["DATE"][0]))
                if fields.get("TRACKNUMBER"):
                    tags.fill(track_number=parse_track_number(fields["TRACKNUMBER"][0]))
                if fields.get("ARTIST"):
                    tags.fill(artists=split_artists(" / ".join(fields["ARTIST"])))
                if fields.get("ALBUMARTIST"):
                    tags.fill(album_artists=split_artists(" / ".join(fields["ALBUMARTIST"])))
        if is_last:
            return
        if fh.tell() < start:
            return
        fh.seek(start + length)


# -------------------------------------------------------------------------- WAV / AIFF


def _extended_float(raw: bytes) -> float:
    """80 位 IEEE 754 扩展精度（AIFF 的采样率存这个）。"""
    exponent = ((raw[0] & 0x7F) << 8) | raw[1]
    return _be(raw[2:10]) * 2.0 ** (exponent - 16383 - 63)


def _riff_info(blob: bytes) -> dict[str, str]:
    """``LIST/INFO`` 的子块：``INAM`` 标题、``IART`` 艺术家、``IPRD`` 专辑、``ICMT`` 备注……"""
    found: dict[str, str] = {}
    pos = 0
    while pos + 8 <= len(blob):
        kind = blob[pos : pos + 4]
        length = _le(blob[pos + 4 : pos + 8])
        payload = blob[pos + 8 : pos + 8 + length]
        found[kind.decode("latin-1", "replace")] = _clean(
            payload.split(b"\x00", 1)[0].decode("latin-1", "replace")
        )
        pos += 8 + length + (length % 2)
    return found


def _riff_chunks(
    fh: BinaryIO, lo: int, hi: int, names: set[bytes], *, big_endian: bool = False
) -> list[tuple[bytes, int, int]]:
    """扫 RIFF/FORM 的块链表，返回 ``names`` 里那些块的 ``(类型, 内容起点, 内容终点)``。

    WAV 的块长是小端，AIFF 是大端——两者结构一样、字节序不一样，所以用 ``big_endian`` 分派。
    块按 2 字节对齐（奇数长度补一字节），这是当初踩过的坑。
    """
    out: list[tuple[bytes, int, int]] = []
    read_length = _be if big_endian else _le
    pos = lo
    while pos + 8 <= hi:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            break
        kind = head[:4]
        length = read_length(head[4:8])
        # 声明长度超出文件是常见的（截断/流式写入的 WAV）：夹到文件尾，别让时长算歪
        end = min(pos + 8 + length, hi)
        if kind in names:
            out.append((kind, pos + 8, end))
        pos = pos + 8 + length + (length % 2)
    return out


def parse_wav(fh: BinaryIO, size: int, tags: AudioTags) -> None:
    """WAVE：标签优先看文件头的 ID3v2（多数打标签软件写那儿），再看 ``LIST/INFO``。"""
    pos = read_id3v2(fh, 0, size, tags)
    fh.seek(pos)
    if fh.read(4) != b"RIFF":
        if pos == 0:
            tags.errors.append("RIFF 头不对，按无标签处理")
            return
        pos = 0  # 有个假 ID3 头的东西：退回从头按 RIFF 扫
        fh.seek(0)
        if fh.read(4) != b"RIFF":
            tags.errors.append("RIFF 头不对，按无标签处理")
            return
    chunks = _riff_chunks(fh, pos + 12, size, {b"fmt ", b"data", b"LIST", b"ID3 ", b"id3 "})
    byte_rate: int | None = None
    bits_per_sample: int | None = None
    channels: int | None = None
    sample_rate: int | None = None
    data_size = 0
    info: dict[str, str] = {}
    for kind, lo, hi in chunks:
        fh.seek(lo)
        blob = fh.read(hi - lo)
        if kind == b"fmt " and len(blob) >= 16:
            channels = _le(blob[0:2])
            sample_rate = _le(blob[4:8])
            byte_rate = _le(blob[8:12]) or None
            bits_per_sample = _le(blob[12:14]) or None
        elif kind == b"data":
            data_size = max(data_size, hi - lo)
        elif kind in (b"ID3 ", b"id3 "):
            # 标签块自己声明的长度才是准的（很多实现会多补零字节填充），read_id3v2 用它读
            read_id3v2(fh, lo, size, tags)
        elif kind == b"LIST" and blob[:4] == b"INFO":
            info = _riff_info(blob[4:])
    if info:
        tags.fill(
            title=info.get("INAM", ""),
            album=info.get("IPRD", ""),
            notes=info.get("ICMT", ""),
            year=parse_year(info.get("ICRD", "")),
        )
        if info.get("IART"):
            tags.fill(artists=split_artists(info["IART"]))
        track = info.get("ITRK") or info.get("TRCK")
        if track:
            tags.fill(track_number=parse_track_number(track))
    if data_size:
        if byte_rate:
            tags.duration_seconds = int(round(data_size / byte_rate))
        elif sample_rate and channels and bits_per_sample:
            per_second = sample_rate * channels * bits_per_sample // 8
            if per_second:
                tags.duration_seconds = int(round(data_size / per_second))


def parse_aiff(fh: BinaryIO, size: int, tags: AudioTags) -> None:
    """AIFF/AIFC：``ID3 `` 块优先（信息更全），其次 ``NAME``/``AUTH``/``ANNO``，时长来自 ``comm``。"""
    fh.seek(0)
    if fh.read(4) != b"FORM":
        tags.errors.append("FORM 头不对，按无标签处理")
        return
    fh.seek(8)
    if fh.read(4) not in (b"AIFF", b"AIFC"):
        tags.errors.append("不是 AIFF/AIFC，按无标签处理")
        return
    chunks = _riff_chunks(fh, 12, size, {b"comm", b"NAME", b"AUTH", b"ANNO", b"ID3 ", b"id3 "}, big_endian=True)
    duration: int | None = None
    legacy: dict[str, Any] = {}
    id3_at: int | None = None
    for kind, lo, hi in chunks:
        fh.seek(lo)
        blob = fh.read(hi - lo)
        if kind == b"comm" and len(blob) >= 18:
            frames = _be(blob[2:6])
            try:
                rate = _extended_float(blob[8:18])
            except (IndexError, ValueError):
                rate = 0.0
            if frames and rate:
                duration = int(round(frames / rate))
        elif kind in (b"ID3 ", b"id3 "):
            id3_at = lo
        elif kind == b"NAME":
            legacy["title"] = _clean(blob.decode("latin-1", "replace"))
        elif kind == b"AUTH":
            legacy["artists"] = split_artists(_clean(blob.decode("latin-1", "replace")))
        elif kind == b"ANNO":
            legacy["notes"] = _clean(blob.decode("latin-1", "replace"))
    if id3_at is not None:
        read_id3v2(fh, id3_at, size, tags)
    tags.fill(**legacy)
    if duration:
        tags.duration_seconds = duration


# ----------------------------------------------------------------------------- 入口

_HANDLER_BY_MAGIC = (
    (b"RIFF", parse_wav),
    (b"FORM", parse_aiff),
    (b"fLaC", parse_flac),
)
_MP4_SUFFIXES = frozenset({".m4a", ".m4b", ".m4p", ".mp4", ".aac", ".m4v"})
_MP3_SUFFIXES = frozenset({".mp3", ".mp2", ".mpga"})
_AIFF_SUFFIXES = frozenset({".aiff", ".aif", ".aifc"})


def read_tags(path: str | Path) -> AudioTags:
    """读一个音频文件的标签。**任何失败都只记进 ``errors``，不抛异常。**"""
    file_path = Path(path)
    tags = AudioTags(path=file_path)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        tags.errors.append(f"读不到文件：{exc}")
        return tags
    if size == 0:
        tags.errors.append("空文件")
        return tags
    try:
        with file_path.open("rb") as fh:
            handler = _pick_handler(fh.read(12), file_path.suffix.lower(), tags)
            if handler is not None:
                handler(fh, size, tags)
    except Exception as exc:  # noqa: BLE001 - 单个坏文件不能让整批扫描停下来
        tags.errors.append(f"解析失败：{type(exc).__name__}: {exc}")
    return tags


def _pick_handler(head: bytes, suffix: str, tags: AudioTags) -> Any:
    """按文件头认容器，认不出来再按后缀兜。返回 None 表示「读不了」，原因已写进 ``errors``。

    ``RIFF``/``FORM``/``fLaC`` 这些**容器魔数排在 ID3 之前**：带 ID3v2 头的 WAV
    是常见写法，按魔数走才会交给 ``parse_wav``（它会先把那段 ID3 读了再扫 RIFF 块）。
    """
    for magic, handler in _HANDLER_BY_MAGIC:
        if head.startswith(magic):
            return handler
    if head[4:8] == b"ftyp":
        return parse_mp4
    suffix_handler = (
        (_MP4_SUFFIXES, parse_mp4),
        (frozenset({".wav", ".wave"}), parse_wav),
        (_AIFF_SUFFIXES, parse_aiff),
        (_MP3_SUFFIXES, parse_mp3),
    )
    for suffixes, handler in suffix_handler:
        if suffix in suffixes:
            return handler
    if head[:3] in (_ID3_MAGIC, _ID3_2_MAGIC):
        return parse_mp3  # 没有效后缀、但明显是 ID3 打头的 MPEG
    tags.errors.append(f"认不出容器格式（后缀 {suffix or '无'}），按无标签处理")
    return None
