"""本地缓存层：只服务于 app 详情（``{cache_path}/apps/{cc}/{appid}.json``）。

app 详情的价格/封面随商店区域（cc）变化，所以缓存**按区域分目录**：
``apps/US/730.json`` 和 ``apps/CN/730.json`` 是两份互不污染的条目。

规则（设计文档 §4.3）：

* 缓存文件损坏 → 视为未命中，静默重建；
* 只有**路径不可写**才抛 ``CacheError``（构造期即检查，不让抓取跑完一半才炸）；
* 删除只发生在 ``apps/`` 目录内，缓存根目录下的其他文件不碰。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .errors import CacheError

__all__ = ["FileCache"]

_CACHE_SOURCE = "appdetails"


def _region(cc: str) -> str:
    region = str(cc or "").strip().upper()
    return region if len(region) == 2 and region.isalpha() else "US"


class FileCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.apps_dir = self.root / "apps"
        try:
            self.apps_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CacheError(f"缓存目录无法创建: {self.apps_dir}（{exc}）") from exc
        probe = self.apps_dir / ".write-probe"
        try:
            probe.write_text("", encoding="utf-8")
        except OSError as exc:
            raise CacheError(f"缓存目录不可写: {self.apps_dir}（{exc}）") from exc
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ 读写
    def path(self, appid: int, cc: str = "US") -> Path:
        return self.apps_dir / _region(cc) / f"{int(appid)}.json"

    def get(self, appid: int, cc: str, ttl: int) -> dict[str, Any] | None:
        """命中返回 payload dict；文件缺失/损坏/过期一律返回 None（未命中）。"""
        path = self.path(appid, cc)
        if not path.is_file():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
            return None
        try:
            cached_at = float(envelope["_cached_at"])
        except (KeyError, TypeError, ValueError):
            return None
        # 以写入时记录的 _ttl 为准；缺失/坏值回退到调用方给的 ttl
        try:
            stored_ttl = float(envelope.get("_ttl"))
        except (TypeError, ValueError):
            stored_ttl = float(ttl)
        if time.time() - cached_at > (stored_ttl or float(ttl)):
            return None
        return envelope["payload"]

    def set(self, appid: int, cc: str, payload: dict[str, Any], ttl: int) -> Path:
        """覆盖写。先写 .part 再改名，避免读到写了一半的文件。"""
        path = self.path(appid, cc)
        envelope = {
            "_cached_at": time.time(),
            "_ttl": int(ttl),
            "_source": _CACHE_SOURCE,
            "_cc": _region(cc),
            "payload": payload,
        }
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise CacheError(f"缓存写入失败: {path}（{exc}）") from exc
        return path

    # ------------------------------------------------------------------ 清理
    def clear(self) -> int:
        """删除 ``apps/`` 下全部缓存文件（含各区域子目录），返回删除数量。"""
        removed = 0
        for path in sorted(self.apps_dir.rglob("*.json")):
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
        for region_dir in sorted(self.apps_dir.iterdir()):
            if region_dir.is_dir():
                try:
                    region_dir.rmdir()       # 只删已经空掉的区域目录
                except OSError:
                    pass
        return removed
