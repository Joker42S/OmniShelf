"""插件元数据：``plugins/<插件目录>/metadata.toml``。

约定见 ``docs/plugin-development.md``。最小可用的元数据::

    [plugin]
    title = "我的插件"

其余字段都有默认值：``id`` 默认取目录名、``entry`` 默认 ``plugin.py``。
``entry`` 可以写成 ``plugin.py:MyPlugin`` 指定类名；只写文件名时，
宿主会在文件里自动找唯一的 ``Plugin`` 子类（多于一个就必须写明）。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

METADATA_FILENAME = "metadata.toml"


class PluginMetadataError(ValueError):
    """metadata.toml 缺失/写错/字段不合法。"""


@dataclass(frozen=True)
class PluginMetadata:
    """一个插件的声明信息。"""

    id: str
    title: str
    directory: Path
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    entry: str = "plugin.py"
    #: 入口文件里要实例化的 ``Plugin`` 子类名；``None`` = 自动找唯一的那个
    class_name: str | None = None
    #: 声明用到宿主哪些接口（现在只有 "query"）；只用于展示与校验，不影响加载
    requires: tuple[str, ...] = ()
    #: 元数据文件里没认识的其他键（原样保留，方便将来扩展）
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def entry_path(self) -> Path:
        return self.directory / self.entry

    @property
    def entry_spec(self) -> str:
        """``plugin.py`` 或 ``plugin.py:MyPlugin``（展示用）。"""
        return f"{self.entry}:{self.class_name}" if self.class_name else self.entry

    @property
    def templates_dir(self) -> Path:
        return self.directory / "templates"

    @property
    def static_dir(self) -> Path:
        return self.directory / "static"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "entry": self.entry_spec,
            "requires": list(self.requires),
            "directory": str(self.directory),
        }


def _text(data: Mapping[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if isinstance(value, (list, tuple, dict)):
        raise PluginMetadataError(f"{key} 要是字符串，不能是 {type(value).__name__}")
    text = str(value).strip()
    return text or default


def parse_metadata_text(text: str, *, directory: Path) -> PluginMetadata:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PluginMetadataError(f"metadata.toml 不是合法 TOML: {exc}") from exc

    section = raw.get("plugin", raw)
    if not isinstance(section, Mapping):
        raise PluginMetadataError("metadata.toml 里 [plugin] 必须是表（table）")

    plugin_id = _text(section, "id", directory.name)
    if not plugin_id:
        raise PluginMetadataError("插件 id 不能为空")
    title = _text(section, "title", plugin_id)
    raw_entry = _text(section, "entry", "plugin.py")
    entry_text, _, class_text = raw_entry.partition(":")
    entry = entry_text.strip() or "plugin.py"
    class_name: str | None = class_text.strip() or None
    requires = section.get("requires", ())
    if isinstance(requires, str):
        requires = (requires,)
    if not isinstance(requires, (list, tuple)):
        raise PluginMetadataError("requires 要是字符串数组")

    known = {
        "id", "title", "version", "description", "author", "homepage",
        "entry", "requires",
    }
    extra = {k: v for k, v in section.items() if k not in known}
    if not isinstance(section, Mapping):  # pragma: no cover - 上面已判
        raise PluginMetadataError("metadata.toml 格式不对")

    return PluginMetadata(
        id=plugin_id,
        title=title,
        directory=directory,
        version=_text(section, "version", "0.0.0"),
        description=_text(section, "description"),
        author=_text(section, "author"),
        homepage=_text(section, "homepage"),
        entry=entry,
        class_name=class_name,
        requires=tuple(str(item) for item in requires),
        extra=extra,
    )


def load_metadata(directory: Path) -> PluginMetadata:
    """读一个插件目录的 metadata.toml。"""
    path = directory / METADATA_FILENAME
    if not path.is_file():
        raise PluginMetadataError(f"缺少 {METADATA_FILENAME}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginMetadataError(f"读不了 {METADATA_FILENAME}: {exc}") from exc
    return parse_metadata_text(text, directory=directory)
