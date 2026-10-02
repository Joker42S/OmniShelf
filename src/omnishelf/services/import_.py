"""导入编排：fetch → normalize → 校验 → 唯一性检查 → upsert，并写 import_logs。

**源（source）**：一次导入来自哪个站点/工具，由 importer 的 ``source`` 决定
（如 ``picamanga``）。每条记录的唯一标识写进 ``external_ids(source, external_id)``，
既用于下次导入同一来源时认出它，也用于判断一个条目出现在哪些来源里。

**唯一性检查（按顺序，命中即停）**：

  1. **本源命中**：``(source, external_id)`` 在 ``external_ids`` 里 → 更新该 item
     （外部数据权威，覆盖非空字段）。这是「这条我导过没有」。
  2. **其它信息命中**（精确）：``kind + title + year`` → 视为同一 item，
     合并 external_ids/tags（只补空字段，不覆盖你手写的评分/状态）。
     注意：**本源已经给过 ID 的条目不会被它合并**——同源里两条记录各有自己的
     ``external_id``，就是来源在说「这是两条」，标题相同也只能进待复核（规则 3）。
  3. **其它信息命中**（模糊）：``kind + 标题`` → **绝不自动合并**：把这条记录存为新 item，
     对每个命中项写 ``pending_merges``，本次计为 skipped。

  **内容判重（音乐这类）**：``KindDef.identity_fields`` 声明了判重字段的 kind，走的是
  另一套——``标题 + 声明的那几个 extra 字段``（如专辑、艺术家）归一后完全相同即同一个作品，
  命中按规则 2 的方式合并（只补空字段）。它**取代**规则 2 与 3：这些来源的记录没有 ID、
  同一首歌本就该合并，而模糊标题匹配会把待复核刷屏（同一首歌收在不同专辑里是正常数据）。
  规则 1 仍在它之前：来源给了 ID 就永远先按 ID 认。

  2/3 是否**跨来源**比较由 kind 策略决定（``kinds.py`` 的 ``KindDef.cross_source_duplicates``）：

  * ``True``（默认，给「同一作品可能出现在多个来源」的类型）——2/3 会去看别的来源的条目，
    命中就进待复核，由人工确认后才合并；
  * ``False``（漫画）——**只检查同一来源内部的重复**：只跟本源（或来源未知）的条目比，
    不同来源之间不考虑重复。「来源未知」= 该条目没有任何 external_ids，例如手工录入的。

注意规则 3 的取舍：pending_merges 的两个字段都是 item 引用，所以「候选」必须落库才能被
review 合并且不丢数据。它在导入统计里算 skipped（没有被自动接受进既有 item）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError
from sqlmodel import Session

from .. import repository as repo
from ..importers.base import Importer, ItemDraft, NormalizeError
from ..importers.registry import get as get_importer
from ..importers.registry import load_builtin_importers
from ..kinds import ExtraFieldError, UnknownKindError, coerce_extra, get_kind

#: 会被写入 items 的草稿字段（extra 单独处理）。
_SCALAR_FIELDS: tuple[str, ...] = (
    "kind",
    "title",
    "subtitle",
    "creators",
    "year",
    "status",
    "rating",
    "notes",
)

#: normalize 阶段可能抛出的、可以按「单条记录失败」处理的异常。
_RECORD_ERRORS = (
    NormalizeError,
    ValidationError,
    ExtraFieldError,
    UnknownKindError,
    ValueError,
    TypeError,
    KeyError,
)


def archive_source_file(imports_dir: Path, source: str, path: Path) -> Path | None:
    """把导入用的原始文件按来源存档到 ``data/imports/<source>/<文件名>``。

    * 同一个来源的同一个文件名会被覆盖（旧版本留在 data 仓库的 git 历史里）；
    * 源文件已经在目标位置时不做任何事（重复导入存档过的文件不会报错）；
    * 文件不存在（例如 importer 是程序生成的）返回 ``None``。
    """
    if not path.is_file():
        return None
    target_dir = imports_dir / source
    target = target_dir / path.name
    if target.resolve() == path.resolve():
        return target
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(path.read_bytes())  # 原样按字节复制，不做转码
    return target


_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str, *, fallback: str = "upload.json") -> str:
    """把上传的文件名变成安全的文件名（去掉路径、非法字符、隐藏名前缀）。"""
    base = Path(str(name or "").replace("\\", "/")).name.strip()
    base = _UNSAFE_FILENAME_CHARS.sub("_", base).lstrip(".")
    if not base:
        return fallback
    return base[:120]


def save_uploaded_source(imports_dir: Path, source: str, filename: str, data: bytes) -> Path:
    """把上传的数据写成 ``data/imports/<source>/<文件名>``，返回落盘路径。"""
    target_dir = imports_dir / (source or "unknown")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_filename(filename)
    target.write_bytes(data)
    return target


def import_uploaded(
    session: Session,
    *,
    importer_name: str,
    filename: str,
    data: bytes,
    imports_dir: Path,
) -> ImportResult:
    """Web 上传入口：先按来源存档，再走正常的导入流程。"""
    load_builtin_importers()
    importer_cls = get_importer(importer_name)  # 未注册 -> UnknownImporterError
    source = getattr(importer_cls, "source", "") or importer_name
    path = save_uploaded_source(imports_dir, source, filename, data)
    result = import_file(session, importer_name=importer_name, path=path)
    try:
        result.archive_path = str(path.relative_to(imports_dir.parent))
    except ValueError:  # 存档目录不在 data/ 下
        result.archive_path = str(path)
    return result


@dataclass
class ImportResult:
    """一次导入的统计，与 import_logs 行一一对应。"""

    source: str
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    unchanged: int = 0
    pending: int = 0
    candidates: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log_id: int | None = None
    #: 原始文件存档位置（相对 data/ 的路径），没存档时为 None
    archive_path: str | None = None

    @property
    def error_count(self) -> int:
        return sum(1 for entry in self.errors if entry.get("level") == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for entry in self.errors if entry.get("level") == "warning")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "unchanged": self.unchanged,
            "pending": self.pending,
            "candidates": self.candidates,
            "errors": self.errors,
            "warnings": self.warnings,
            "log_id": self.log_id,
            "archive_path": self.archive_path,
        }

    def summary(self) -> str:
        return (
            f"{self.source}: 共 {self.total} 条，新增 {self.inserted}，更新 {self.updated}，"
            f"跳过 {self.skipped}（其中待复核 {self.pending}，候选已落库 {self.candidates}），"
            f"未变化 {self.unchanged}，错误 {self.error_count}，警告 {self.warning_count}"
        )


def _record(
    index: int, source: str, message: str, *, level: str = "error"
) -> dict[str, Any]:
    return {"level": level, "record": index, "source": source, "message": message}


def _prepare_draft(
    draft: ItemDraft, result: ImportResult, index: int
) -> ItemDraft:
    """按 kinds.py 声明校验/转换 extra，把 warning 记进结果。"""
    extra, warnings = coerce_extra(draft.kind, draft.extra)
    for text in warnings:
        result.warnings.append(f"记录 {index}: {text}")
        result.errors.append(_record(index, result.source, text, level="warning"))
    if extra == draft.extra:
        return draft
    return draft.model_copy(update={"extra": extra})


def _write_scalars(
    session: Session, item: Any, draft: ItemDraft, *, overwrite: bool
) -> bool:
    """写标量字段。``overwrite=False`` 时只补空字段（合并语义）。

    返回是否真的改动了数据（用于区分「更新」与「未变化」）。
    """
    changes: dict[str, Any] = {}
    for name in _SCALAR_FIELDS:
        if name == "kind":
            continue
        value = getattr(draft, name)
        if value is None:
            continue
        current = getattr(item, name)
        should_write = (
            overwrite or current is None or (isinstance(current, str) and not current.strip())
        )
        # 值一样就不写：否则重复导入会白白刷新 updated_at，还会被计成「更新」。
        if should_write and current != value:
            changes[name] = value

    merged = dict(item.extra or {})
    extra_changed = False
    for key, value in draft.extra.items():
        if overwrite or merged.get(key) is None:
            if merged.get(key) != value:
                merged[key] = value
                extra_changed = True
    if extra_changed:
        changes["extra"] = merged
    if changes:
        repo.update_item(session, item.id, **changes)
        return True
    return False


def _attach(session: Session, item: Any, draft: ItemDraft) -> bool:
    """写入 external_ids / tags；返回是否有新增。"""
    changed = False
    for ext in draft.external_ids:
        if repo.add_external_id(
            session, item.id, source=ext.source, external_id=ext.external_id, url=ext.url
        ):
            changed = True
    for tag in draft.tags:
        if repo.add_tag(session, item.id, tag):
            changed = True
    return changed


def _create_from_draft(session: Session, draft: ItemDraft) -> Any:
    return repo.create_item(
        session,
        kind=draft.kind,
        title=draft.title,
        subtitle=draft.subtitle,
        creators=draft.creators,
        year=draft.year,
        status=draft.status,
        rating=draft.rating,
        notes=draft.notes,
        extra=draft.extra,
        tags=draft.tags,
        external_ids=[(e.source, e.external_id, e.url) for e in draft.external_ids],
    )


def _count_touch(result: ImportResult, changed: bool, path: str) -> str:
    """命中既有 item：真的改了算 updated，否则算 unchanged。"""
    if changed:
        result.updated += 1
        return path
    result.unchanged += 1
    return "unchanged"


class _IdentityIndex:
    """一次导入里「按内容认同一作品」的索引（见 ``KindDef.identity_fields``）。

    为什么不是逐条查库：判重要在 Python 侧归一（大小写、标点、列表排序），SQL 里做不出
    同一个口径；而每条记录都把同 kind 的条目全捞出来比，音乐这种量级（上万条）就是平方级。
    所以开跑时一次读进来建成 dict，之后每条 O(1)。

    导入过程中新建的条目会当场 ``add()`` 进索引——同一份清单里有重复文件是最常见的情况，
    批内不认就等于没判重。合并之后也 ``add()`` 一次：把空字段补上（比如原来没专辑，
    现在有了）会让这条 item 的比较键变化，不重登记的话下一条就找不着它了。
    """

    def __init__(self, session: Session, kind: str) -> None:
        self.kind = kind
        self._keys: dict[tuple[str, ...], str] = {}
        self._by_item: dict[str, tuple[str, ...]] = {}
        for item in repo.identity_candidates(session, kind=kind):
            self.add(item)

    def add(self, item: Any) -> None:
        key = repo.identity_key(self.kind, item.title, item.extra)
        previous = self._by_item.get(item.id)
        if previous is not None and previous != key:
            # 只在确实还指向这条 item 时撤掉旧键，别把别人的登记抢走。
            if self._keys.get(previous) == item.id:
                self._keys.pop(previous, None)
        self._keys.setdefault(key, item.id)
        self._by_item[item.id] = key

    def find(self, session: Session, draft: ItemDraft) -> Any:
        item_id = self._keys.get(repo.identity_key(self.kind, draft.title, draft.extra))
        return repo.get_item(session, item_id) if item_id else None


def _apply_draft(
    session: Session,
    draft: ItemDraft,
    result: ImportResult,
    index: int,
    batch_source: str,
    identities: dict[str, _IdentityIndex],
) -> str:
    # 「这条记录的来源」= 它携带的 external_ids 的来源集合（一个影视记录可能同时带
    # tmdb 和 douban）；没带任何 ID 时退回 importer 的 source。
    record_sources = {ext.source for ext in draft.external_ids} or (
        {batch_source} if batch_source else set()
    )
    # 规则 1：本源命中（(source, external_id) 见过）-> 更新
    for ext in draft.external_ids:
        matched = repo.get_item_by_external_id(session, ext.source, ext.external_id)
        if matched is not None:
            changed = _write_scalars(session, matched, draft, overwrite=True)
            changed = _attach(session, matched, draft) or changed
            # 覆盖写可能改掉标题/专辑这些判重字段，索引里那条得按新值重新登记。
            identity = identities.get(matched.kind)
            if identity is not None:
                identity.add(matched)
            return _count_touch(result, changed, "updated")

    # 内容判重的 kind（音乐）：标题 + 声明的那几个字段全都归一相同，就算同一个作品。
    # 命中就按「补空字段」合并（和规则 2 一样的温和写法，不覆盖你手填的评分/状态）。
    # 这一条**取代**规则 2 和 3：标题模糊匹配对音乐没意义——同一首歌收在不同专辑里
    # 本来就是两条正常数据，走模糊匹配只会把待复核刷屏。
    kind_def = get_kind(draft.kind)
    if kind_def.identity_fields:
        identity = identities.get(draft.kind)
        if identity is None:
            identity = identities[draft.kind] = _IdentityIndex(session, draft.kind)
        existing = identity.find(session, draft)
        if existing is not None:
            changed = _write_scalars(session, existing, draft, overwrite=False)
            changed = _attach(session, existing, draft) or changed
            identity.add(existing)
            return _count_touch(result, changed, "updated")
        created = _create_from_draft(session, draft)
        identity.add(created)
        result.inserted += 1
        return "inserted"

    # 规则 2/3 是否跨来源比较，由 kind 策略决定（漫画只查同源）。
    cross_source = kind_def.cross_source_duplicates

    # 规则 2：kind + title + year 精确匹配 -> 合并到既有 item
    # （只会合并「本源还没有给它 ID」的条目；同源两条记录各有 ID 时不许自动合并）
    exact = repo.find_by_title_year(
        session,
        kind=draft.kind,
        title=draft.title,
        year=draft.year,
        record_sources=record_sources,
        cross_source=cross_source,
    )
    if exact is not None:
        changed = _write_scalars(session, exact, draft, overwrite=False)
        changed = _attach(session, exact, draft) or changed
        return _count_touch(result, changed, "updated")

    # 规则 3：标题模糊匹配 -> 绝不自动合并
    fuzzy = repo.find_fuzzy_title_matches(
        session,
        kind=draft.kind,
        title=draft.title,
        record_sources=record_sources,
        cross_source=cross_source,
    )
    if fuzzy:
        candidate = _create_from_draft(session, draft)
        result.candidates += 1
        for existing in fuzzy:
            reason = (
                f"模糊标题匹配: 新记录 {draft.title!r}(year={draft.year}) "
                f"≈ 既有 {existing.title!r}(year={existing.year})"
            )
            repo.add_pending_merge(
                session, item_id=existing.id, candidate_id=candidate.id, reason=reason
            )
            result.pending += 1
        message = (
            f"标题模糊匹配 {len(fuzzy)} 条既有 item，已写入待复核，未自动合并"
            f"（新记录 id={candidate.id}）"
        )
        result.warnings.append(message)
        result.errors.append(_record(index, result.source, message, level="warning"))
        result.skipped += 1
        return "pending"

    _create_from_draft(session, draft)
    result.inserted += 1
    return "inserted"


def run_import(session: Session, importer: Importer) -> ImportResult:
    """执行一次完整导入并落 import_logs。单条记录失败不会中断整批。"""
    source = getattr(importer, "source", "") or type(importer).__name__
    result = ImportResult(source=source)
    log = repo.create_import_log(session, source=source)
    result.log_id = log.id
    # 内容判重的索引按 kind 懒建（多数 kind 用不到，一上来就建等于白读一遍表）。
    identities: dict[str, _IdentityIndex] = {}

    try:
        records = list(importer.fetch())
    except Exception as exc:  # fetch 是文件/网络边界，失败只记日志
        result.errors.append(_record(0, source, f"fetch 失败: {exc}"))
        repo.finish_import_log(
            session,
            log.id,  # type: ignore[arg-type]
            total=0,
            inserted=0,
            updated=0,
            skipped=0,
            errors=result.errors,
        )
        return result

    for index, raw in enumerate(records, start=1):
        result.total += 1
        try:
            draft = importer.normalize(raw)
            draft = _prepare_draft(draft, result, index)
        except _RECORD_ERRORS as exc:
            result.errors.append(_record(index, source, str(exc)))
            result.skipped += 1
            continue
        _apply_draft(session, draft, result, index, source, identities)

    repo.finish_import_log(
        session,
        log.id,  # type: ignore[arg-type]
        total=result.total,
        inserted=result.inserted,
        updated=result.updated,
        skipped=result.skipped,
        errors=result.errors,
    )
    session.flush()
    return result


def import_file(
    session: Session,
    *,
    importer_name: str,
    path: str | Path,
    archive_dir: str | Path | None = None,
    **importer_kwargs: Any,
) -> ImportResult:
    """按注册名构造 importer 并导入一个文件。

    ``archive_dir`` 给定时（CLI 传 ``data/imports``），会先把原始文件按来源存档一份。
    """
    load_builtin_importers()
    source_path = Path(path)
    importer_cls = get_importer(importer_name)
    # 具体 importer 的 __init__ 签名各不相同（协议里不约束构造函数）。
    factory = cast("Callable[..., Importer]", importer_cls)
    importer = factory(source_path, **importer_kwargs)
    result = run_import(session, importer)
    if archive_dir is not None:
        archived = archive_source_file(
            Path(archive_dir), getattr(importer, "source", "") or importer_name, source_path
        )
        if archived is not None:
            base = Path(archive_dir).parent
            try:
                result.archive_path = str(archived.relative_to(base))
            except ValueError:  # 存档目录不在 data/ 下（测试里常见）
                result.archive_path = str(archived)
    return result


def import_many(
    session: Session,
    importer_name: str,
    paths: Sequence[str | Path],
    archive_dir: str | Path | None = None,
    **importer_kwargs: Any,
) -> list[ImportResult]:
    return [
        import_file(
            session,
            importer_name=importer_name,
            path=p,
            archive_dir=archive_dir,
            **importer_kwargs,
        )
        for p in paths
    ]
