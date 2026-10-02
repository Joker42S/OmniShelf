"""本地音乐文件扫描：把「一个文件夹」或「一堆文件」读成一份可导入的 manifest。

产出的是 **JSON 清单**，不是直接入库：清单交给内置 importer ``music_files``，
走的是和其它 importer 一样的链路（上传页存档、写 ``import_logs``、逐条判重）。
好处是「抓」和「放」分开——扫几千个文件很慢，中途断了清单还在；导入反复试也不会重扫。

扫描口径：

* **深度遍历**目录（``os.walk``，不跟随目录软链，免得在环里绕不出来）；
* 只认 ``AUDIO_SUFFIXES`` 里的后缀，其它文件看都不看；
* 跳过垃圾目录（回收站那类）——外置盘上 ``$RECYCLE.BIN`` 里躺着的往往是几万份
  音乐文件的副本，扫进来就是成倍的重复；
* 每个文件只读标签头部那几百字节（见 ``audiotags``），不整读文件。

``progress`` 回调（每读一个文件一次）是给插件的进度条用的：CLI 不传，插件传。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .audiotags import AudioTags, read_tags

#: 认作音乐文件的后缀（小写比较）。
#:
#: * 不含 ``.ogg`` / ``.opus``：``audiotags`` 读不了 Ogg 容器的标签（要先验页 CRC
#:   再重组分包），与其收一堆「只有文件名」的条目，不如明确不扫；
#: * 不含 ``.mid``：MIDI 不是录音，没有时长/艺术家这类标签；
#: * 不含 ``.mka``：Matroska 音频要解析 EBML 树，同上。
AUDIO_SUFFIXES: frozenset[str] = frozenset(
    {
        ".mp3",
        ".mp2",
        ".mpga",
        ".m4a",
        ".m4b",
        ".m4p",
        ".mp4",
        ".aac",
        ".flac",
        ".wav",
        ".wave",
        ".aif",
        ".aiff",
        ".aifc",
    }
)

#: 这些名字（小写比较）的目录整个不进场，文件级的同名垃圾也跳过。
SKIP_NAMES: frozenset[str] = frozenset(
    {
        "$recycle.bin",
        "system volume information",
        ".trash",
        ".ds_store",
        ".localized",
        "desktop.ini",
        "__macosx",
        "@eadir",
        ".fseventsd",
        ".spotlight-v100",
        ".temporaryitems",
        "lost+found",
        ".git",
        "node_modules",
    }
)

#: manifest 报告里最多列几条逐文件错误（几千条会淹没页面）。
MAX_ERRORS = 50

#: 没标题标签时，兜底标题要去掉的后缀与开头的曲目号。
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_TRACK_PREFIX = re.compile(r"^\s*0*\d{1,3}\s*[._\-、]\s*")

#: 每读一个文件回调一次：``(已读文件数, 当前文件)``。
ProgressFn = Callable[[int, Path], None]


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_SUFFIXES


def skip_name(name: str) -> bool:
    """该不该整个跳过：垃圾名单，或以 ``.`` 开头的隐藏目录。"""
    lowered = name.lower()
    return lowered in SKIP_NAMES or lowered.startswith(".")


@dataclass(slots=True)
class ScanResult:
    """一次扫描的结果。``items`` 就是 manifest 的正文。"""

    roots: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    #: 扫过多少音频文件（含读失败的）。
    scanned: int = 0
    #: 一条标签都没读到的文件数（只有文件名，页面要单独说明）。
    tagless: int = 0
    #: 读失败（文件损坏 / 没权限 / 容器认不出）的文件数。
    failed: int = 0
    #: 逐文件的错误说明，最多 ``MAX_ERRORS`` 条。
    errors: list[str] = field(default_factory=list)
    #: 错误列表被截断时，没列出来的还剩多少条。
    errors_hidden: int = 0
    started_at: str = ""
    finished_at: str = ""

    def add_error(self, message: str) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(message)
        else:
            self.errors_hidden += 1

    def manifest(self) -> dict[str, Any]:
        """落成 JSON 的那份字典（内置 importer ``music_files`` 认这个形状）。"""
        return {
            "kind": "music_files",
            "generated_at": self.finished_at,
            "started_at": self.started_at,
            "roots": self.roots,
            "scanned": self.scanned,
            "tagless": self.tagless,
            "failed": self.failed,
            "item_count": len(self.items),
            "errors": list(self.errors),
            "errors_hidden": self.errors_hidden,
            "items": list(self.items),
        }

    def summary(self) -> str:
        """一行给人看的摘要（插件报告和日志都用它）。"""
        parts = [f"扫了 {self.scanned} 个音频文件", f"读到 {len(self.items)} 条"]
        if self.tagless:
            parts.append(f"其中 {self.tagless} 条没有任何标签（用文件名当标题）")
        if self.failed:
            parts.append(f"{self.failed} 个文件读失败")
        if self.errors_hidden:
            parts.append(f"另有 {self.errors_hidden} 条错误未列出")
        return "，".join(parts)


def iter_audio_files(targets: Iterable[str | Path]) -> Iterable[Path]:
    """把「目录 + 文件」混合的目标列表展开成音频文件序列（目录深度遍历）。

    目录软链不跟随；文件软链照常读——指向音乐文件的软链是真数据。
    """
    for raw in targets:
        path = Path(raw)
        if path.is_dir():
            for dir_path, dir_names, file_names in os.walk(path):
                # 就地改 dir_names = 剪掉整棵子树（回收站里那几万份副本靠这个挡）
                dir_names[:] = sorted(name for name in dir_names if not skip_name(name))
                for name in sorted(file_names):
                    if skip_name(name):
                        continue
                    candidate = Path(dir_path) / name
                    if is_audio_file(candidate) and candidate.is_file():
                        yield candidate
        elif path.is_file():
            yield path


def title_from_filename(path: Path) -> str:
    """没有标题标签时的兜底标题：去掉后缀和开头的曲目号。

    只做到这一步——``"Artist - Title.mp3"`` 这种文件名**不按 `` - `` 拆**：标题里带
    破折号的歌不少，猜错会把标题切成半句；而文件名原样存在 ``extra.filename`` 里，
    真要回查随时查得到。
    """
    stem = _EXTENSION.sub("", path.name).strip()
    stem = _TRACK_PREFIX.sub("", stem).strip()
    return stem or path.name


def _item(tags: AudioTags) -> dict[str, Any]:
    """一条 manifest 记录：**只有那 11 项**，外加 ``path``（重扫/排查用的本地记账，不入库）。"""
    item: dict[str, Any] = {
        "path": str(tags.path),
        "filename": tags.filename,
        "title": tags.title or title_from_filename(tags.path),
        "subtitle": tags.subtitle,
        "artists": list(tags.artists),
        "album": tags.album,
        "album_artists": list(tags.album_artists),
        "year": tags.year,
        "track_number": tags.track_number,
        "duration_seconds": tags.duration_seconds,
        "rating": tags.rating,
        "notes": tags.notes,
    }
    if not tags.title:
        item["title_from"] = "filename"
    return item


def scan(
    targets: Sequence[str | Path] | str | Path,
    *,
    progress: ProgressFn | None = None,
) -> ScanResult:
    """逐个读取音频文件的标签，攒成一份 manifest。

    ``progress(已读文件数, 当前文件)`` 每读一个文件回调一次。扫几千个文件时插件页面
    靠它画进度条；这里**不预告总数**——目录里到底有多少音频得先数一遍，慢盘上数两遍
    比报个「已读 N 个」更亏。
    """
    if isinstance(targets, (str, Path)):
        targets = [targets]
    result = ScanResult(roots=[str(item) for item in targets])
    result.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for index, path in enumerate(iter_audio_files(targets), start=1):
        tags = read_tags(path)
        result.scanned += 1
        if tags.errors and not tags.has_tags:
            result.failed += 1
            result.add_error(f"{path}: {'；'.join(tags.errors)}")
        elif not tags.has_tags:
            result.tagless += 1
            result.items.append(_item(tags))
        else:
            if tags.errors:  # 读到了部分标签：条目照常收，问题记一笔
                result.add_error(f"{path}: {'；'.join(tags.errors)}")
            result.items.append(_item(tags))
        if progress is not None:
            progress(index, path)
    result.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return result


def write_manifest(result: ScanResult, path: str | Path) -> Path:
    """把清单写成 JSON（UTF-8、不转义非 ASCII，人可以直接在上传页选它）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target
