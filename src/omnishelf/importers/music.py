"""音乐导入的两个 importer：本地文件清单 ``music_files`` 与 iTunes 资料库 ``itunes_xml``。

两者产出的都是 ``kind=music``，**字段口径一致——只收这 11 项**：

字段                落库位置
==================  ==========================================
标题                ``title``
副标题              ``subtitle``
评分                ``rating``（0-10）
备注                ``notes``
年份                ``year``
艺术家              ``creators`` + ``extra.artists``（原文）
专辑艺术家          ``creators`` + ``extra.album_artists``（原文）
专辑                ``extra.album``
编号                ``extra.track_number``
时长                ``extra.duration_seconds``
原始文件名          ``extra.filename``
==================  ==========================================

三条来自需求本身的规则：

* **文件名存进 ``extra.filename`` 备用**：标题一律取标签里的歌曲名，没有标签才退回文件名
  （``music_scan.title_from_filename``）。退回的那条到底是从文件名来的，随时能查。
* **艺术家和专辑艺术家都算 creators**：听歌时「谁」是两个不同的问法（演唱者 vs 唱片署名），
  但书架上都是这一条的创作者，所以合并进 ``creators``；同时两个字段各留一份原文，
  否则合完就分不出谁是谁了。
* **判重看内容不看 ID**：同一首歌可以被拷成任意个文件，本地文件没有稳定 ID 可依，
  所以 ``music_files`` 不写 ``external_ids``，交给 ``kinds.py`` 里声明的内容判重
  （标题 + 专辑 + 艺术家）。iTunes 的记录有 ``Persistent ID``，写进
  ``external_ids.itunes``——同一份 XML 重复导入走 ID 认亲；拿 XML 去补本地文件导入的
  同一条，走内容判重合并。

``itunes_xml`` 的取舍：跳过 ``Track Type``
不是 ``File`` 的（网络链接、被移除的文件）和播客；``Rating`` 是 0/20/40/60/80/100 的百分制
星级，除以 10 落成我们的 0-10。
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import unquote

from ..audiotags import split_artists
from .base import DictRawRecord, DraftExternalId, ItemDraft, NormalizeError, RawRecord
from .json_file import JsonFileImporter
from .registry import register

__all__ = ["build_music_draft", "MusicFilesImporter", "ItunesXmlImporter"]


def artist_names(value: Any) -> list[str]:
    """艺术家字段 -> 名字列表：接受列表或字符串，逐个按音乐口径切分并去重。

    扫描器已经切过一遍了（``audiotags.split_artists``），这里再切一次是为了让手写的
    清单、以及来源直接给 ``"A & B"`` 这种串的情况也走同一条规则。
    """
    if value is None:
        return []
    pieces: Sequence[Any] = value if isinstance(value, (list, tuple)) else [value]
    found: list[str] = []
    for piece in pieces:
        for name in split_artists(str(piece)):
            if name not in found:
                found.append(name)
    return found


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> int | None:
    """整数收整数，字符串只认纯数字；其它一律留空（不猜）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    return None


def build_music_draft(
    *,
    title: Any,
    subtitle: Any = None,
    rating: Any = None,
    notes: Any = None,
    year: Any = None,
    artists: Any = None,
    album_artists: Any = None,
    album: Any = None,
    filename: Any = None,
    track_number: Any = None,
    duration_seconds: Any = None,
    external_ids: Iterable[DraftExternalId] = (),
) -> ItemDraft:
    """按上面那套口径组装一条 music 草稿（两个 importer 共用）。"""
    singers = artist_names(artists)
    album_singers = artist_names(album_artists)
    creators: list[str] = []
    for name in singers + album_singers:
        if name not in creators:
            creators.append(name)

    parsed_rating = None
    if rating is not None and not (isinstance(rating, str) and not rating.strip()):
        try:
            parsed_rating = float(rating)
        except (TypeError, ValueError):
            parsed_rating = None
        if parsed_rating is not None and not 0 <= parsed_rating <= 10:
            parsed_rating = None  # 超出量程：来源用的是别的分级，不猜

    try:
        return ItemDraft(
            kind="music",
            title=str(title).strip(),
            subtitle=_text(subtitle),
            rating=parsed_rating,
            notes=_text(notes),
            year=_number(year),
            creators=creators,
            extra={
                "album": _text(album),
                "artists": singers,
                "album_artists": album_singers,
                "filename": _text(filename),
                "track_number": _number(track_number),
                "duration_seconds": _number(duration_seconds),
            },
            external_ids=list(external_ids),
        )
    except ValueError as exc:  # pydantic ValidationError 也是 ValueError
        raise NormalizeError(f"记录校验失败: {exc}") from exc


@register("music_files")
class MusicFilesImporter(JsonFileImporter):
    """导入 ``omnishelf.music_scan`` 生成的本地音乐清单。

    清单本身也认：``{"items": [...]}`` 或 ``[...]`` 都行，所以手写一份 JSON
    照样能导。字段名就是 ``music_scan._item()`` 产出的那 11 项 + ``path``。
    """

    source = "music_files"
    kind = "music"

    def normalize(self, raw: RawRecord) -> ItemDraft:
        payload = dict(raw.payload)
        title = _text(payload.get("title"))
        if not title:
            raise NormalizeError("记录缺少 title（清单里的标题应来自标签，无标签时是文件名）")
        return build_music_draft(
            title=title,
            subtitle=payload.get("subtitle"),
            rating=payload.get("rating"),
            notes=payload.get("notes"),
            year=payload.get("year"),
            artists=payload.get("artists"),
            album_artists=payload.get("album_artists"),
            album=payload.get("album"),
            filename=payload.get("filename"),
            track_number=payload.get("track_number"),
            duration_seconds=payload.get("duration_seconds"),
        )


# --------------------------------------------------------------------------- iTunes

#: iTunes 的 ``Location`` 是 ``file://`` URL，常见三种写法都要能吃下：
#: ``file://localhost/Users/...``（老 Mac）、``file:///Users/...``、
#: Windows 导出的 ``file://localhost/D:/Music/...``（在本机读不到文件，但文件名照留）。
_LOCATION_RE = re.compile(r"^file:(//[^/]*)?/", re.IGNORECASE)


def filename_from_location(value: Any) -> str | None:
    """从 ``Location`` URL 里取文件名（只取最后一段，不碰路径分隔符）。"""
    text = _text(value)
    if not text:
        return None
    decoded = unquote(text)
    head = _LOCATION_RE.sub("", decoded) if _LOCATION_RE.match(decoded) else decoded
    name = re.split(r"[\\/]", head)[-1].strip()
    return name or None


#: 播客在 ``Kind`` / ``File Category`` 里的写法：这两个字段是跟着 iTunes 界面语言
#: 本地化的（英文版 ``Podcast``、中文版「播客」），所以按标记词包含判断。
_PODCAST_MARKERS = ("podcast", "播客")


def is_podcast(track: Mapping[str, Any]) -> bool:
    """这条是不是播客节目（不当代歌收）。

    iTunes 在不同版本里把播客标在几个不同字段上，任一处标到就跳过：
    ``Kind`` / ``File Category``，以及只有播客才有的那些 ``Podcast *`` 字段。
    """
    if any(key.startswith("Podcast") for key in track):
        return True
    for key in ("Kind", "File Category"):
        value = _text(track.get(key))
        if value and any(marker in value.lower() for marker in _PODCAST_MARKERS):
            return True
    return False


def itunes_rating(value: Any) -> float | None:
    """iTunes 星级是百分制（20/40/60/80/100 = 1~5 星），换成我们的 0-10。

    ``Rating Computed`` 为真时这个值是 iTunes 按播放次数算出来的，不是用户手点的——照收，
    因为用户在 iTunes 里看到的就是这几颗星；我们不额外区分，免得书架上的分和 App 里的分对不上。
    """
    number = _number(value)
    if not number or number < 0:
        return None
    return round(number / 10.0, 2)


@register("itunes_xml")
class ItunesXmlImporter(JsonFileImporter):
    """iTunes「资料库导出 XML」的音轨（``Music/Library.xml`` 那一类文件）。

    顶层是 ``<dict>``，音轨在 ``Tracks`` 里（``{轨道号: {...}}``）。文件有十几 MB、
    上万条，但 ``plistlib``（标准库）一次读得下——依赖里没有 lxml，而苹果的 DTD 也不需要。
    """

    source = "itunes_xml"
    kind = "music"

    def load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            raise NormalizeError(f"XML 文件不存在: {self.path}")
        try:
            with self.path.open("rb") as fh:
                data = plistlib.load(fh)
        except plistlib.InvalidFileException as exc:
            raise NormalizeError(f"不是合法的 plist/XML 资料库导出: {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise NormalizeError(f"顶层不是 dict，不像 iTunes 资料库导出: {self.path}")
        tracks = data.get("Tracks")
        if not isinstance(tracks, dict):
            raise NormalizeError(f"找不到 Tracks 字典（是资料库导出吗？）: {self.path}")
        rows: list[dict[str, Any]] = []
        # 按轨道号排序，导入顺序才是确定的（同名 key 的字符串序会把 10 排在 2 前面）。
        for key in sorted(tracks, key=lambda item: _number(item) or 0):
            track = tracks[key]
            if not isinstance(track, dict):
                continue
            # 只有真存在本机文件里的音轨才收：URL / Remote 是流媒体链接，
            # 播客是节目不是歌，两者都不是「我收藏的音乐」。
            if str(track.get("Track Type") or "File") != "File" or is_podcast(track):
                continue
            rows.append(track)
        return rows

    def fetch(self) -> Iterable[RawRecord]:
        for payload in self.load():
            yield DictRawRecord(source=self.source, payload=payload)

    def normalize(self, raw: RawRecord) -> ItemDraft:
        track = dict(raw.payload)
        title = _text(track.get("Name"))
        if not title:
            raise NormalizeError("音轨没有 Name 字段，无法作为标题")

        external_ids: list[DraftExternalId] = []
        # Persistent ID 在资料库重建前后都稳定；Track ID 会随重排变，只作兜底。
        stable = _text(track.get("Persistent ID")) or _number(track.get("Track ID"))
        if stable is not None:
            external_ids.append(
                DraftExternalId(source="itunes", external_id=str(stable))
            )

        duration = _number(track.get("Total Time"))
        return build_music_draft(
            title=title,
            # 副标题留空：XML 里最接近的是 Work（古典乐章所属的母作品名），那不是副标题。
            rating=itunes_rating(track.get("Rating")),
            notes=_text(track.get("Comments")),
            year=_number(track.get("Year")),
            artists=track.get("Artist"),
            album_artists=track.get("Album Artist"),
            album=track.get("Album"),
            filename=filename_from_location(track.get("Location")),
            track_number=_number(track.get("Track Number")),
            duration_seconds=round(duration / 1000) if duration else None,
            external_ids=external_ids,
        )
