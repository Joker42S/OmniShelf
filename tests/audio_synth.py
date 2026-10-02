"""合成音频文件：字节全部现场构造，仓库里不存二进制。

为什么不用 ``tests/fixtures/``：这些「文件」的价值就在于**构造意图**——测试断言的是
「按 ID3v2.4 的同步安全帧长写出来的标签，读回来应该是什么」，所以构造器留在代码里，
改协议细节时改这里就行；几十 KB 的伪音频提交进仓库只是占地方。

所有文案都是合成数据（``example.invalid``），不含任何真实曲目/人名。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------- ID3 (MP3)


def synchsafe(value: int) -> bytes:
    """ID3v2 的「同步安全整数」：每字节只用低 7 位。"""
    return bytes([(value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F])


def id3_frame(name: str, text: str, *, v4: bool = False, encoding: int = 0) -> bytes:
    """一个文本帧：首字节是编码号。

    ``encoding=0`` 按规定是 ISO-8859-1，但现实里大量中文标签就是拿 0 当 UTF-8 用的
    （``audiotags._latin1_or_utf8`` 就是为接住它而写的），所以这里按**文本本身**决定字节：
    能用 latin-1 表示的（``Björk``）发 latin-1，不能的（中文）发 UTF-8。
    """
    if encoding == 1:
        body = text.encode("utf-16")  # Python 给的是 UTF-16LE + BOM
    elif encoding == 2:
        body = text.encode("utf-16-be")
    elif encoding == 3:
        body = b"\xef\xbb\xbf" + text.encode("utf-8")
    else:
        try:
            body = text.encode("latin-1")
        except UnicodeEncodeError:
            body = text.encode("utf-8")
    payload = bytes([encoding]) + body
    size = synchsafe(len(payload)) if v4 else struct.pack(">I", len(payload))
    return name.encode("latin-1") + size + b"\x00\x00" + payload


def id3v2(frames: list[bytes], *, v4: bool = False) -> bytes:
    body = b"".join(frames)
    return b"ID3" + bytes([4 if v4 else 3, 0, 0]) + synchsafe(len(body)) + body


def comment_frame(text: str, description: str = "") -> bytes:
    """COMM 帧：编码号 + 语言 + 说明 + ``\\x00`` + 正文。"""
    payload = b"\x00" + b"eng" + description.encode("latin-1") + b"\x00" + text.encode("utf-8")
    return b"COMM" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def popm_frame(rating: int) -> bytes:
    """POPM 星级帧（Windows Media / iTunes 都写这个）。"""
    payload = b"iTunes_Rating\x00" + bytes([rating]) + b"\x00\x00\x00\x00"
    return b"POPM" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


MPG1_L3 = bytes([0xFF, 0xFB, 0x90, 0xC0])  # MPEG1 Layer III, 128kbps, 44100Hz
FRAME_LEN = 144 * 128 * 1000 // 44100  # 417 字节/帧


def mp3_audio(frames: int, *, xing_frames: int | None = None) -> bytes:
    """若干个静音 MPEG 帧；给 ``xing_frames`` 就在首帧塞 Xing 头（VBR 的帧数来源）。"""
    first = bytearray(MPG1_L3)
    if xing_frames is not None:
        head = bytearray(FRAME_LEN - 4)
        head[17:21] = b"Xing"
        head[21:25] = struct.pack(">I", 0x03)  # 有帧数 + 有字节数
        head[25:29] = struct.pack(">I", xing_frames)
        head[29:33] = struct.pack(">I", 0)
        first += head
    else:
        first += bytes(FRAME_LEN - 4)
    out = bytes(first)
    if frames > 1:
        out += (MPG1_L3 + bytes(FRAME_LEN - 4)) * (frames - 1)
    return out


def id3v1(
    title: str, artist: str, album: str, year: str, comment: str, track: int | None = None
) -> bytes:
    """尾部 128 字节的 ID3v1；给了 ``track`` 就是 v1.1（注释第 29 字节为 0 + 曲目号）。"""
    def pad(text: str, width: int) -> bytes:
        raw = text.encode("latin-1")[:width]
        return raw + b" " * (width - len(raw))

    if track is None:
        tail = pad(comment, 30)
    else:
        tail = pad(comment, 28) + b"\x00" + bytes([track])
    return b"TAG" + pad(title, 30) + pad(artist, 30) + pad(album, 30) + pad(year, 4) + tail + b"\xff"


# -------------------------------------------------------------------------- MP4


def atom(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + name + payload


def data_atom(text: str) -> bytes:
    """``data`` atom：版本/标志（末字节 1 = UTF-8 文本）+ 保留 4 字节 + 正文。"""
    body = text.encode("utf-8")
    return atom(b"data", b"\x00\x00\x00\x01" + b"\x00\x00\x00\x00" + body)


def trkn_atom(track: int, total: int) -> bytes:
    """``trkn``：标志 0（二进制）+ 保留 + [曲目号 2 字节][总数 2 字节][保留 4 字节]。"""
    value = b"\x00\x00" + struct.pack(">HH", track, total) + b"\x00\x00\x00\x00"
    return atom(b"data", b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + value)


def mvhd(timescale: int, duration: int) -> bytes:
    """``mvhd``（版本 0）：时长 = duration / timescale 秒。"""
    return atom(
        b"mvhd",
        b"\x00\x00\x00\x00"
        + b"\x00\x00\x00\x00\x00\x00\x00\x00"
        + struct.pack(">I", timescale)
        + struct.pack(">I", duration)
        + b"\x00" * 8,
    )


def mp4(moov_children: bytes) -> bytes:
    return atom(b"ftyp", b"M4A " + bytes(8)) + atom(b"moov", moov_children)


def ilst(pairs: list[tuple[bytes, bytes]]) -> bytes:
    """``udta/meta/ilst``：``meta`` 是 fullbox，所以内容前多 4 字节版本号。"""
    body = b"".join(atom(kind, child) for kind, child in pairs)
    return atom(b"udta", atom(b"meta", b"\x00\x00\x00\x00" + atom(b"ilst", body)))


# -------------------------------------------------------------------------- FLAC


def vorbis_comment(entries: list[str], vendor: str = "synthetic") -> bytes:
    """Vorbis comment 块：小端长度前缀的 vendor + ``KEY=value`` 列表。"""
    body = struct.pack("<I", len(vendor)) + vendor.encode()
    items = [entry.encode("utf-8") for entry in entries]
    body += struct.pack("<I", len(items))
    for item in items:
        body += struct.pack("<I", len(item)) + item
    return body


def flac_block(block_type: int, payload: bytes, last: bool = False) -> bytes:
    head = bytes([(0x80 if last else 0) | block_type]) + struct.pack(">I", len(payload))[1:]
    return head + payload


def flac_streaminfo(sample_rate: int, channels: int, bit_depth: int, total_samples: int) -> bytes:
    """STREAMINFO：那三段挤在 8 字节里的字段按 ``sample_rate<<44 | … | total_samples`` 拼。"""
    packed = (sample_rate << 44) | (channels << 41) | ((bit_depth - 1) << 36) | total_samples
    return b"\x00\x00" + b"\x00\x00" + bytes(6) + struct.pack(">Q", packed) + bytes(16)


def flac(blocks: list[tuple[int, bytes, bool]]) -> bytes:
    body = b"".join(flac_block(kind, payload, last=last) for kind, payload, last in blocks)
    return b"fLaC" + body


# ---------------------------------------------------------------------- WAV/AIFF


def riff_chunk(kind: bytes, payload: bytes, *, big: bool = False) -> bytes:
    """RIFF/FORM 的子块：长度字段 + 内容，**奇数长度补一字节**（AIFF 的长度是大端）。"""
    pack = ">I" if big else "<I"
    pad = b"\x00" if len(payload) % 2 else b""
    return kind + struct.pack(pack, len(payload)) + payload + pad


def riff_container(children: bytes, form_type: bytes, *, big: bool = False) -> bytes:
    """RIFF（小端）或 FORM（大端）：长度 = 类型标记 + 各子块的长度。"""
    body = form_type + children
    head = b"FORM" if big else b"RIFF"
    return head + struct.pack(">I" if big else "<I", len(body)) + body


def wav(children: bytes) -> bytes:
    return riff_container(children, b"WAVE")


def aiff(children: bytes) -> bytes:
    return riff_container(children, b"AIFF", big=True)


def wav_fmt(sample_rate: int, channels: int, bits_per_sample: int) -> bytes:
    byte_rate = sample_rate * channels * bits_per_sample // 8
    payload = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, 4, bits_per_sample)
    return riff_chunk(b"fmt ", payload)


def wav_data(sample_rate: int, channels: int, bits_per_sample: int, seconds: int) -> bytes:
    payload = bytes(sample_rate * channels * bits_per_sample // 8 * seconds)
    return riff_chunk(b"data", payload)


def aiff_comm(sample_rate: int, channels: int, bit_depth: int) -> bytes:
    return riff_chunk(
        b"comm",
        struct.pack(">HIH", channels, sample_rate, bit_depth) + extended_80(float(sample_rate)),
        big=True,
    )


def extended_80(value: float) -> bytes:
    """80 位 IEEE 扩展精度（AIFF 的采样率用这个表示）。"""
    exponent = 16383
    while value >= 2.0:
        value /= 2.0
        exponent += 1
    while value < 1.0:
        value *= 2.0
        exponent -= 1
    mantissa = int(round(value * 2**63))
    return bytes([exponent >> 8, exponent & 0xFF]) + struct.pack(">Q", mantissa)


# ------------------------------------------------------------------------ 用例

#: 文件名 -> 构造器。每个用例都覆盖了某个容器的一条特定读法。
CASES: dict[str, Callable[[], bytes]] = {
    # 1. MP3：ID3v2.3 全套标签 + Xing 帧数 + 尾部 ID3v1（v1 必须被 v2 压住）
    "a_id3v23.mp3": lambda: id3v2(
        [
            id3_frame("TIT2", "合成标题"),
            id3_frame("TIT3", "Synthetic Subtitle"),
            id3_frame("TALB", "Synthetic Album"),
            id3_frame("TPE1", "Alpha & Beta feat. Gamma"),
            id3_frame("TPE2", "Various People / Other Names"),
            id3_frame("TYER", "2005"),
            id3_frame("TRCK", "03/12"),
            comment_frame("备注：合成数据 example.invalid"),
            popm_frame(204),
        ]
    )
    + mp3_audio(300, xing_frames=1000)
    + id3v1("IGNORED", "IGNORED", "IGNORED", "1999", "ignored", 9),
    # 2. MP3：ID3v2.4（同步安全帧长）+ TLEN，没有 Xing
    "b_id3v24.mp3": lambda: id3v2(
        [id3_frame("TIT2", "Vfour Title", v4=True), id3_frame("TLEN", "180000", v4=True)], v4=True
    )
    + mp3_audio(200),
    # 3. MP3：只有 ID3v1.1（v2 一个字节都没有）
    "c_id3v1.mp3": lambda: mp3_audio(50, xing_frames=50)
    + id3v1("V1 Only", "Solo Artist; Second One", "V1 Album", "1988", "old comment", 7),
    # 4. M4A：ilst 文本标签 + trkn 二进制 + covr（几百 KB 的封面不许整读）
    "d_track.m4a": lambda: mp4(
        mvhd(1000, 213456)
        + ilst(
            [
                (b"\xa9nam", data_atom("M4A Title")),
                (b"\xa9ART", data_atom("A; B")),
                (b"aART", data_atom("Album Person")),
                (b"\xa9alb", data_atom("M4A Album")),
                (b"\xa9day", data_atom("2019-11-03")),
                (b"\xa9cmt", data_atom("m4a note")),
                (b"trkn", trkn_atom(12, 5)),
                (b"covr", atom(b"data", b"\x00\x00\x00\x0d" + b"\x00\x00\x00\x00" + bytes(3000))),
            ]
        )
    ),
    # 5. FLAC：STREAMINFO 时长 + VORBIS_COMMENT
    "e_lossless.flac": lambda: flac(
        [
            (0, flac_streaminfo(44100, 2, 16, 441000), False),
            (
                4,
                vorbis_comment(
                    [
                        "TITLE=FLAC Title",
                        "ARTIST=X、Y",
                        "ALBUMARTIST=FLAC People",
                        "ALBUM=FLAC Album",
                        "DATE=2011",
                        "TRACKNUMBER=12/16",
                        "COMMENT=flac note",
                    ]
                ),
                True,
            ),
        ]
    )
    + bytes(1024),
    # 6. WAV：LIST/INFO 标签
    "f_wave.wav": lambda: wav(
        wav_fmt(44100, 2, 16)
        + wav_data(44100, 2, 16, 1)
        + riff_chunk(
            b"LIST",
            b"INFO"
            + riff_chunk(b"INAM", b"Wav Title\x00")
            + riff_chunk(b"IART", b"Wav Artist, Two\x00")
            + riff_chunk(b"IPRD", b"Wav Album")
            + riff_chunk(b"ICMT", b"wav note\x00")
            + riff_chunk(b"ICRD", b"2001\x00")
            + riff_chunk(b"ITRK", b"04/11"),
        )
    ),
    # 7. WAV：头部挂着 ID3v2（很多打标签软件这么写），标签优先、时长仍走 fmt/data
    "g_wave_id3.wav": lambda: id3v2(
        [id3_frame("TIT2", "Wav Id3 Title"), id3_frame("TPE1", "Id3 Artist")]
    )
    + wav(wav_fmt(44100, 2, 16) + wav_data(44100, 2, 16, 1)),
    # 8. AIFF：大端 FORM + comm 的 80 位采样率 + NAME/AUTH/ANNO
    "h_aiff.aiff": lambda: aiff(
        aiff_comm(44100, 2, 16)
        + riff_chunk(b"NAME", b"Aiff Title\x00\x00", big=True)
        + riff_chunk(b"AUTH", b"Aiff Artist\x00", big=True)
        + riff_chunk(b"ANNO", b"aiff note", big=True)
    ),
    # 9. MP3：文本帧的三种编码混在一起（UTF-16、带 BOM 的 UTF-8、真正的 latin-1）
    "i_encodings.mp3": lambda: id3v2(
        [
            id3_frame("TIT2", "UTF16 标题", encoding=1),
            id3_frame("TALB", "BOM 专辑", encoding=3),
            id3_frame("TPE1", "Björk", encoding=0),
        ]
    )
    + mp3_audio(40),
    # 10. MP3：只有 TLEN 给得出时长（正文不是合法 MPEG，帧头找不到）；v2.4 的 TLEN 是毫秒
    "j_tlen.mp3": lambda: id3v2(
        [id3_frame("TIT2", "Tlen Only", v4=True), id3_frame("TLEN", "180000", v4=True)], v4=True
    )
    + bytes(512),
    # 11. 根本不是音频
    "k_unknown.txt": lambda: b"not audio at all",
    # 12. 空文件（0 字节也要能读，不能抛）
    "l_empty.mp3": lambda: b"",
}


def synth_files(directory: str | Path) -> dict[str, Path]:
    """把全部用例写成文件，返回 ``{文件名: 路径}``。"""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, builder in CASES.items():
        path = target / name
        path.write_bytes(builder())
        written[name] = path
    return written
