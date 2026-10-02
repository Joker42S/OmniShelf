"""封面附件读写。

路径规则：``attachments/ab/cd/<ulid>.<ext>``（ULID 前 4 位分桶）。
所有路径都是相对于 ``data/`` 的 POSIX 字符串，存进 ``items.cover_path``。

``attach_cover`` / ``remove_cover`` 是给 Web/CLI 用的编排：文件 + ``cover_path`` 一起改。
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from .. import repository as repo
from ..config import Settings
from ..models import Item

ALLOWED_SUFFIXES: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".gif")

#: 单个封面的大小上限（8 MiB）。
MAX_COVER_BYTES = 8 * 1024 * 1024

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


class CoverError(ValueError):
    """封面文件后缀/大小/内容不合法。"""


def sniff_suffix(data: bytes) -> str | None:
    """按 magic bytes 猜后缀；认不出来返回 None。"""
    for magic, suffix in _MAGIC:
        if data.startswith(magic):
            return suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def normalize_suffix(filename: str | None, content_type: str | None, data: bytes) -> str:
    """决定落盘后缀：优先真实内容，其次文件名，最后 content-type。"""
    sniffed = sniff_suffix(data)
    if sniffed is not None:
        return ".jpg" if sniffed == ".jpeg" else sniffed
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in ALLOWED_SUFFIXES:
            return ".jpg" if suffix == ".jpeg" else suffix
    if content_type:
        guessed = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(content_type.split(";")[0].strip().lower())
        if guessed:
            return guessed
    raise CoverError(
        f"无法识别的图片格式（文件名={filename!r}, content-type={content_type!r}）；"
        f"支持: {', '.join(ALLOWED_SUFFIXES)}"
    )


def save_cover(
    settings: Settings,
    item_id: str,
    data: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> str:
    """写入封面，返回相对 ``attachments/`` 的路径（POSIX 字符串）。"""
    if not data:
        raise CoverError("封面上传内容为空")
    if len(data) > MAX_COVER_BYTES:
        raise CoverError(f"封面超过大小上限 {MAX_COVER_BYTES} 字节")
    suffix = normalize_suffix(filename, content_type, data)
    rel_path = settings.cover_rel_path(item_id, suffix)
    target = settings.cover_abs_path(rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return rel_path


def cover_abs(settings: Settings, rel_path: str) -> Path:
    return settings.cover_abs_path(rel_path)


def cover_exists(settings: Settings, rel_path: str | None) -> bool:
    if not rel_path:
        return False
    return cover_abs(settings, rel_path).is_file()


def delete_cover(settings: Settings, rel_path: str | None) -> bool:
    """删除封面文件；不存在返回 False。``cover_path`` 的清理由调用方负责。"""
    if not rel_path:
        return False
    target = cover_abs(settings, rel_path)
    if target.is_file():
        target.unlink()
        return True
    return False


def attach_cover(
    settings: Settings,
    session: Session,
    item: Item,
    data: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> str:
    """保存封面并更新 ``cover_path``（顺带清掉旧文件），返回新的相对路径。"""
    rel_path = save_cover(
        settings, item.id, data, filename=filename, content_type=content_type
    )
    if item.cover_path and item.cover_path != rel_path:
        delete_cover(settings, item.cover_path)
    repo.update_item(session, item.id, cover_path=rel_path)
    return rel_path


def remove_cover(settings: Settings, session: Session, item: Item) -> bool:
    """删除封面文件并清空 ``cover_path``；返回文件是否真的存在过。"""
    existed = delete_cover(settings, item.cover_path)
    if item.cover_path is not None:
        repo.update_item(session, item.id, cover_path=None)
    return existed
