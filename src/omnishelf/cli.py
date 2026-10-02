"""Typer CLI。

CLI 里不允许有业务逻辑：每个命令都只做「解析参数 → 调 repository/services → 打印」。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Optional

import typer
from sqlmodel import Session

from . import music_scan
from . import repository as repo
from .config import Settings, load_settings
from .db import build_engine, checkpoint, init_db, session_scope
from .kinds import kind_labels, kind_names
from . import __version__
from .models import STATUSES, STATUS_LABELS
from .plugins.manager import PluginError, PluginManager
from .services import export_ as export_service
from .services import review as review_service
from .services import filters as filters_service
from .services import vcs
from .services.import_ import import_many

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="OmniShelf：本地优先的图书/漫画/影视/音乐/游戏收藏库。",
)
review_app = typer.Typer(no_args_is_help=True, help="待复核的模糊匹配（永不自动合并）。")
app.add_typer(review_app, name="review")


@dataclass
class State:
    """CLI 全局状态（由 callback 填充到 ``ctx.obj``）。"""

    settings: Settings
    explicit_data_dir: bool = False

    def with_data_dir(self, data_dir: Path) -> State:
        return State(settings=self.settings.with_data_dir(data_dir), explicit_data_dir=True)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    data_dir: Optional[Path] = typer.Option(
        None,
        "--data-dir",
        help="数据目录（默认取 $OMNISHELF_DATA_DIR 或 <repo>/data）。",
        show_default=False,
    ),
    version: bool = typer.Option(
        False, "--version", "-V", is_eager=True, help="显示版本号并退出。"
    ),
) -> None:
    """OmniShelf：本地优先的收藏管理系统。"""
    if version:
        typer.echo(f"omnishelf {__version__}")
        raise typer.Exit()
    ctx.obj = State(settings=load_settings(data_dir), explicit_data_dir=data_dir is not None)


def _state(ctx: typer.Context) -> State:
    state = ctx.obj
    if not isinstance(state, State):  # pragma: no cover - 仅防御性
        raise typer.BadParameter("CLI 状态未初始化")
    return state


@contextmanager
def _session(ctx: typer.Context) -> Iterator[Session]:
    """打开会话；顺带保证 schema 已迁移（幂等）。"""
    state = _state(ctx)
    init_db(state.settings)
    engine = build_engine(state.settings)
    try:
        with session_scope(engine) as session:
            yield session
    finally:
        engine.dispose()


def _out(data: Any, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def _status_label(status: str | None) -> str:
    if not status:
        return "-"
    return STATUS_LABELS.get(status, status)


def _rating_text(rating: float | None) -> str:
    return "-" if rating is None else f"{rating:g}"


def _parse_rating(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().lower()
    if text in {"none", "null", "clear", "-"}:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise typer.BadParameter(f"--rating 需要数字或 none，得到 {value!r}") from exc


def item_to_dict(session: Session, item: Any) -> dict[str, Any]:
    return export_service.item_to_dict(session, item)


def _item_line(item: Any, label: str = "") -> str:
    """一行显示一个 item（供 review list / demo 使用）。"""
    if item is None:
        return "(已删除/缺失)"
    year = f" (year={item.year})" if item.year else ""
    prefix = f"{label} " if label else ""
    return f"{prefix}{item.id} {item.title}{year}"


# --------------------------------------------------------------------------- init


@app.command()
def init(ctx: typer.Context) -> None:
    """建目录 + 执行迁移（幂等）。"""
    state = _state(ctx)
    version = init_db(state.settings)
    gitignore = vcs.ensure_data_gitignore(state.settings)
    typer.echo(f"数据库: {state.settings.db_path}")
    typer.echo(f"schema user_version = {version}")
    typer.echo(f"附件目录: {state.settings.attachments_dir}")
    typer.echo(f".gitignore: {gitignore}")


# ------------------------------------------------------------------------- import


@app.command("import")
def import_cmd(
    ctx: typer.Context,
    files: list[Path] = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="要导入的文件（可给多个）"
    ),
    importer: str = typer.Option(..., "--importer", "-i", help="importer 注册名，如 json_movies"),
    strict: bool = typer.Option(False, "--strict", help="有记录级错误时以退出码 1 结束"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """按注册的 importer 导入文件（同一种 importer 可一次导入多个文件）。"""
    with _session(ctx) as session:
        try:
            results = import_many(
                session, importer, files, archive_dir=_state(ctx).settings.imports_dir
            )
        except LookupError as exc:
            _fail(str(exc))
            return
    if as_json:
        _out([result.as_dict() for result in results], True)
    else:
        for result in results:
            typer.echo(result.summary())
            for entry in result.errors:
                level = "警告" if entry.get("level") == "warning" else "错误"
                typer.echo(f"  [{level}] 记录 {entry.get('record')}: {entry.get('message')}")
            if result.archive_path:
                typer.echo(f"  原始文件已存档: data/{result.archive_path}")
    if strict and any(result.error_count for result in results):
        raise typer.Exit(code=1)


# ------------------------------------------------------------------------- music


@app.command("scan-music")
def scan_music_cmd(
    ctx: typer.Context,
    targets: list[Path] = typer.Argument(
        ..., exists=True, help="要扫描的目录或音频文件（可给多个，目录深度遍历）"
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="清单输出路径（默认 data/imports/music_scan.json）"
    ),
    do_import: bool = typer.Option(False, "--import", help="扫完直接用 music_files 导入库存"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """读本地音乐文件的标签，生成清单 JSON（可选顺手导入）。

    清单是留底用的中间产物：几千个文件扫一次很慢，导入反复试不该重扫。
    """
    state = _state(ctx)
    scan_result = music_scan.scan(targets)
    manifest_path = music_scan.write_manifest(
        scan_result, out or state.settings.imports_dir / "music_scan.json"
    )
    import_result = None
    if do_import:
        with _session(ctx) as session:
            import_result = import_many(
                session, "music_files", [manifest_path], archive_dir=state.settings.imports_dir
            )[0]

    if as_json:
        payload: dict[str, Any] = {"scan": scan_result.manifest()}
        if import_result is not None:
            payload["import"] = import_result.as_dict()
        _out(payload, True)
        return

    typer.echo(f"清单: {manifest_path}")
    typer.echo(scan_result.summary())
    for message in scan_result.errors:
        typer.echo(f"  [警告] {message}")
    if import_result is None:
        typer.echo(f"确认过后导入: omnishelf import -i music_files {manifest_path}")
        return
    typer.echo(import_result.summary())
    for entry in import_result.errors:
        level = "警告" if entry.get("level") == "warning" else "错误"
        typer.echo(f"  [{level}] 记录 {entry.get('record')}: {entry.get('message')}")


# --------------------------------------------------------------------------- list


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    kind: Optional[str] = typer.Option(None, "--kind", "-k", help=f"可选: {', '.join(kind_names())}"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help=f"可选: {', '.join(STATUSES)}"),
    tag: Optional[str] = typer.Option(None, "--tag", "-t"),
    title_prefix: Optional[str] = typer.Option(None, "--title-prefix", help="标题前缀 LIKE"),
    search: Optional[str] = typer.Option(None, "--search", "-q", help="搜索标题（包含匹配）"),
    creator: Optional[str] = typer.Option(None, "--creator", "-c", help="创作者包含匹配"),
    rating_min: Optional[float] = typer.Option(None, "--rating-min", help="评分下限 0-10"),
    rating_max: Optional[float] = typer.Option(None, "--rating-max", help="评分上限 0-10"),
    created_from: Optional[str] = typer.Option(None, "--created-from", help="入库日期起 YYYY-MM-DD（本地日）"),
    created_to: Optional[str] = typer.Option(None, "--created-to", help="入库日期止 YYYY-MM-DD（本地日）"),
    with_tag: list[str] = typer.Option([], "--with-tag", help="必须带的 tag（可重复，AND）"),
    without_tag: list[str] = typer.Option([], "--without-tag", help="必须不带的 tag（可重复，AND）"),
    sort: Optional[str] = typer.Option(
        None, "--sort", help=f"排序字段: {', '.join(filters_service.SORT_LABELS)}（默认 created_at）"
    ),
    reverse: bool = typer.Option(False, "--reverse", "-r", help="反向排序（默认由字段决定）"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, help="每页条数"),
    page: int = typer.Option(1, "--page", min=1, help="第几页（从 1 开始）"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列表（基础筛选 + 搜索 + 高级筛选，与网页版同一套逻辑）。"""
    sort_key = sort or filters_service.DEFAULT_SORT
    try:
        filters = filters_service.parse_filters(
            kind=kind,
            status=status,
            tag=tag,
            title_prefix=title_prefix,
            search=search,
            creator=creator,
            rating_min=rating_min,
            rating_max=rating_max,
            created_from=created_from,
            created_to=created_to,
            tags_include=with_tag,
            tags_exclude=without_tag,
            sort=sort_key,
            descending=(
                not filters_service.default_descending(sort_key) if reverse else None
            ),
        )
    except filters_service.FilterError as exc:
        typer.secho(f"筛选条件有问题: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    with _session(ctx) as session:
        total = repo.count_items(session, **filters.repo_kwargs())
        items = repo.list_items(
            session,
            **filters.repo_kwargs(),
            sort=filters.sort,
            descending=filters.descending,
            limit=limit,
            offset=(page - 1) * limit,
        )
        payload = [item_to_dict(session, item) for item in items]
    if as_json:
        _out(payload, True)
        return
    if not payload:
        typer.echo("(没有匹配的收藏)")
        return
    for row in payload:
        year = f" ({row['year']})" if row["year"] else ""
        tags = " ".join(f"#{t}" for t in row["tags"])
        creators = row.get("creators") or []
        creator = f"  {', '.join(creators)}" if creators else ""
        added = (row.get("created_at") or "")[:10]
        typer.echo(
            f"{row['id']}  {row['kind']:<6} {row['title']}{year}{creator}  "
            f"[{_status_label(row['status'])}]  {_rating_text(row['rating'])}  {added}"
            + (f"  {tags}" if tags else "")
        )
    pages = max(1, -(-total // limit))
    arrow = "↓" if filters.descending else "↑"
    typer.echo(f"-- 第 {page}/{pages} 页 · 共 {total} 条 · 本页 {len(payload)} 条 · 排序 {filters.sort} {arrow}")


# ------------------------------------------------------------------------ plugins

plugins_app = typer.Typer(
    help="插件管理：查看、重载（跑插件自己的命令用 omnishelf plugin <插件名> <命令>）"
)
app.add_typer(plugins_app, name="plugins")


def _plugin_manager(ctx: typer.Context) -> PluginManager:
    state = _state(ctx)
    # 插件可能要用宿主的查询/导入接口，所以先保证库结构就绪（幂等；和 Web 一样）
    init_db(state.settings)
    engine = build_engine(state.settings)
    return PluginManager(
        plugins_dir=state.settings.plugins_dir,
        plugins_data_dir=state.settings.plugins_data_dir,
        engine=engine,
        imports_dir=state.settings.imports_dir,
        logs_dir=state.settings.plugin_logs_dir,
        # 一次性进程不做轮转：把常驻 serve 正在写的那个文件 rename 走，
        # 它的句柄会跟着 inode 继续往 .1 里写，日志就分叉了。
        log_rotate=False,
    )


@plugins_app.command("list")
def plugins_list(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出已安装的插件与状态。"""
    manager = _plugin_manager(ctx)
    statuses = manager.load_all()
    manager.stop_all()
    payload = [status.as_dict() for status in statuses]
    if as_json:
        _out(payload, True)
        return
    if not payload:
        typer.echo("(没有插件)  在 plugins/ 下建目录放 metadata.toml 即可")
        return
    icons = {"loaded": "✓", "error": "✗", "missing": "·", "disabled": "-"}
    for status in statuses:
        mark = icons.get(status.state, "?")
        page = "有页面" if status.has_page else "无页面"
        typer.echo(
            f"{mark} {status.id:<16} {status.title} v{status.version}  [{status.state} · {page}]"
        )
        if status.description:
            typer.echo(f"    {status.description}")
        if status.error:
            typer.echo(f"    错误: {status.error.splitlines()[0]}")
    typer.echo(f"-- 共 {len(payload)} 个插件 · 代码目录 {manager.plugins_dir}")


PLUGIN_CLI_USAGE = """用法：
  omnishelf plugin                            看有哪些插件与命令
  omnishelf plugin <插件名>                    看这个插件的命令（含参数说明）
  omnishelf plugin <插件名> <命令> [--参数]     执行；加 -h 看该命令的详细帮助
"""


def _print_plugin_overview(manager: PluginManager) -> int:
    typer.echo("OmniShelf 插件命令\n")
    typer.echo(PLUGIN_CLI_USAGE)
    statuses = manager.list_plugins()
    if not statuses:
        typer.echo("(还没有插件)  在 plugins/ 下建目录放 metadata.toml 即可")
        return 0
    typer.echo("已装插件：")
    for status in statuses:
        mark = "✓" if status.ok else ("✗" if status.state == "error" else "·")
        typer.echo(f"  {mark} {status.id:<16} {status.title} v{status.version}")
        if status.error:
            typer.echo(f"      错误: {status.error.splitlines()[0]}")
            continue
        commands = manager.commands(status.id)
        if not commands:
            typer.echo("      (没有注册命令)")
        for item in commands:
            typer.echo(f"      {item.name:<14} {item.help}")
    return 0


def _print_plugin_commands(manager: PluginManager, plugin_id: str, status: Any) -> int:
    typer.echo(f"{status.title}（{plugin_id}）v{status.version}")
    if status.description:
        typer.echo(f"  {status.description}")
    commands = manager.commands(plugin_id)
    if not commands:
        typer.echo("\n这个插件没有注册命令行命令。")
        return 0
    typer.echo("\n命令：")
    for item in commands:
        typer.echo(f"\n  omnishelf plugin {plugin_id} {item.name} [--参数]")
        typer.echo(f"      {item.help}")
        for line in item.doc.splitlines():
            stripped = line.strip()
            if stripped:
                typer.echo(f"      {stripped}")
    typer.echo(f"\n{PLUGIN_CLI_USAGE}")
    return 0


def _dispatch_plugin_cli(manager: PluginManager, argv: list[str]) -> int:
    """``omnishelf plugin ...`` 的落地实现（返回退出码）。"""
    if not argv or argv[0] in ("-h", "--help", "list", "--list"):
        return _print_plugin_overview(manager)

    plugin_id, rest = argv[0], argv[1:]
    statuses = {status.id: status for status in manager.list_plugins()}
    status = statuses.get(plugin_id)
    if status is None:
        known = ", ".join(sorted(statuses)) or "(没有插件)"
        _fail(f"没有插件 {plugin_id!r}；已装的有: {known}")
    if status.error:
        _fail(f"插件 {plugin_id} 加载失败：\n{status.error}")

    commands = {item.name: item for item in manager.commands(plugin_id)}
    if not rest or (rest[0] in ("-h", "--help", "list") and rest[0] not in commands):
        return _print_plugin_commands(manager, plugin_id, status)

    name, args = rest[0], rest[1:]
    if name not in commands:
        available = ", ".join(sorted(commands)) or "(没有注册命令)"
        _fail(f"插件 {plugin_id} 没有命令 {name!r}；可用: {available}")

    try:
        result = manager.run_command(plugin_id, name, args)
    except PluginError as exc:
        _fail(str(exc))
    if result is None:
        return 0
    if isinstance(result, str):
        typer.echo(result)
    else:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


@app.command(
    "plugin",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        # 关掉 Click 自带的 --help，让 -h/--help 透传给插件自己的参数解析器
        "help_option_names": [],
    },
)
def plugin_cmd(ctx: typer.Context) -> None:
    """运行插件注册的命令行命令。

    用法：``omnishelf plugin <插件名> <命令> [--参数 ...]``。
    ``omnishelf plugin`` 看插件与命令总览，``omnishelf plugin <插件名>`` 看该插件的
    命令与参数说明（加 ``-h`` 看单个命令的详细帮助）。
    只看插件状态/重载用 ``omnishelf plugins list|reload``。
    """
    argv = [str(item) for item in ctx.args]
    manager = _plugin_manager(ctx)
    try:
        manager.load_all()
        code = _dispatch_plugin_cli(manager, argv)
    finally:
        manager.stop_all()
    if code:
        raise typer.Exit(code=code)


@plugins_app.command("reload")
def plugins_reload(
    ctx: typer.Context,
    plugin_id: Optional[str] = typer.Argument(None, help="只重载这个插件（省略=全部）"),
) -> None:
    """重载插件（开发时改完代码跑一下）。"""
    manager = _plugin_manager(ctx)
    try:
        reloaded = manager.reload(plugin_id)
    except PluginError as exc:
        manager.stop_all()
        typer.secho(f"重载失败: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        pass
    manager.stop_all()
    typer.echo(f"已重载: {', '.join(reloaded) if reloaded else '(没有匹配的插件)'}")


# --------------------------------------------------------------------------- show


@app.command()
def show(
    ctx: typer.Context,
    item_id: str = typer.Argument(..., help="item 的 ULID"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """展示单条收藏的完整信息。"""
    with _session(ctx) as session:
        item = repo.get_item(session, item_id, include_deleted=True)
        if item is None:
            _fail(f"找不到 item: {item_id}")
            return
        payload = item_to_dict(session, item)
    if as_json:
        _out(payload, True)
        return
    typer.echo(f"id:        {payload['id']}")
    typer.echo(f"kind:      {payload['kind']} ({kind_labels().get(payload['kind'], '?')})")
    typer.echo(f"title:     {payload['title']}")
    if payload["subtitle"]:
        typer.echo(f"subtitle:  {payload['subtitle']}")
    typer.echo(f"year:      {payload['year'] if payload['year'] is not None else '-'}")
    typer.echo(f"status:    {_status_label(payload['status'])}")
    typer.echo(f"rating:    {_rating_text(payload['rating'])}")
    typer.echo(f"tags:      {', '.join(payload['tags']) or '-'}")
    typer.echo(f"cover:     {payload['cover_path'] or '-'}")
    typer.echo(f"created:   {payload['created_at']}")
    typer.echo(f"updated:   {payload['updated_at']}")
    typer.echo(f"deleted:   {payload['deleted_at'] or '-'}")
    if payload["external_ids"]:
        typer.echo("external_ids:")
        for link in payload["external_ids"]:
            typer.echo(f"  - {link['source']}:{link['external_id']} {link['url'] or ''}".rstrip())
    if payload["extra"]:
        typer.echo("extra:")
        for key, value in payload["extra"].items():
            typer.echo(f"  - {key}: {value}")
    if payload["notes"]:
        typer.echo("notes:")
        typer.echo(f"  {payload['notes']}")


# --------------------------------------------------------------------------- edit


@app.command()
def edit(
    ctx: typer.Context,
    item_id: str = typer.Argument(...),
    status: Optional[str] = typer.Option(None, "--status", "-s", help=f"可选: {', '.join(STATUSES)}"),
    rating: Optional[str] = typer.Option(None, "--rating", "-r", help="0-10 的数字，或 none 清除"),
    title: Optional[str] = typer.Option(None, "--title"),
    subtitle: Optional[str] = typer.Option(None, "--subtitle"),
    year: Optional[int] = typer.Option(None, "--year"),
    notes: Optional[str] = typer.Option(None, "--notes"),
    tags: Optional[list[str]] = typer.Option(None, "--tag", help="整体覆盖标签，可重复"),
    add_tags: Optional[list[str]] = typer.Option(None, "--add-tag", help="追加标签，可重复"),
    remove_tags: Optional[list[str]] = typer.Option(None, "--remove-tag", help="删除标签，可重复"),
) -> None:
    """更新一条收藏的字段 / 标签。"""
    changes: dict[str, Any] = {}
    if status is not None:
        changes["status"] = status
    if rating is not None:
        changes["rating"] = _parse_rating(rating)
    if title is not None:
        changes["title"] = title
    if subtitle is not None:
        changes["subtitle"] = subtitle
    if year is not None:
        changes["year"] = year
    if notes is not None:
        changes["notes"] = notes

    with _session(ctx) as session:
        try:
            item = repo.require_item(session, item_id)
            if changes:
                item = repo.update_item(session, item.id, **changes)
            if tags is not None:
                repo.set_tags(session, item.id, tags)
            for tag in add_tags or []:
                repo.add_tag(session, item.id, tag)
            for tag in remove_tags or []:
                repo.remove_tag(session, item.id, tag)
            payload = item_to_dict(session, item)
        except (repo.ItemNotFoundError, ValueError) as exc:
            _fail(str(exc))
            return
    typer.echo(f"已更新 {payload['id']}: {payload['title']}")
    typer.echo(f"status={_status_label(payload['status'])} rating={_rating_text(payload['rating'])}")
    typer.echo(f"tags={', '.join(payload['tags']) or '-'}")


# ------------------------------------------------------------------------- review


@review_app.command("list")
def review_list(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出所有待复核的模糊匹配。"""
    with _session(ctx) as session:
        views = review_service.list_pending(session)
        payload = [
            {
                "id": v.id,
                "created_at": v.created_at,
                "reason": v.reason,
                "item": item_to_dict(session, v.item) if v.item is not None else None,
                "candidate": item_to_dict(session, v.candidate) if v.candidate is not None else None,
            }
            for v in views
        ]
        # 会话关闭后 ORM 实例会 detach，先把要打印的文本取出来。
        lines = [(v.id, v.reason, _item_line(v.item), _item_line(v.candidate)) for v in views]
    if as_json:
        _out(payload, True)
        return
    if not lines:
        typer.echo("(没有待复核项)")
        return
    for pending_id, reason, item_text, cand_text in lines:
        typer.echo(f"#{pending_id}  {reason}")
        typer.echo(f"    item:      {item_text}")
        typer.echo(f"    candidate: {cand_text}")
    typer.echo(f"-- 共 {len(lines)} 条待复核")


@review_app.command("merge")
def review_merge(
    ctx: typer.Context,
    pending_id: int = typer.Argument(..., help="待复核记录 id"),
    keep: str = typer.Option(..., "--keep", help="保留哪一侧的 item id"),
) -> None:
    """合并两个 item：保留 --keep，另一侧并入后软删除。"""
    with _session(ctx) as session:
        try:
            outcome = review_service.merge(session, pending_id, keep)
            # 会话关闭后 ORM 实例会 detach，所以先把要打印的内容取成字符串。
            lines = [
                f"已合并 #{outcome.pending_id}: 保留 {outcome.kept.id} ({outcome.kept.title})，"
                f"合并并软删除 {outcome.dropped.id} ({outcome.dropped.title})",
                f"迁移 external_ids {outcome.moved_external_ids} 条，tags {outcome.moved_tags} 个，"
                f"字段 {', '.join(outcome.moved_fields) or '无'}",
            ]
        except (LookupError, ValueError) as exc:
            _fail(str(exc))
            return
    for line in lines:
        typer.echo(line)


@review_app.command("skip")
def review_skip(
    ctx: typer.Context,
    pending_id: int = typer.Argument(...),
) -> None:
    """判定「不是同一条」：关闭待复核记录，两个 item 都保留。"""
    with _session(ctx) as session:
        try:
            view = review_service.skip(session, pending_id)
            message = (
                f"已跳过 #{pending_id}（item {view.pending.item_id} 与 candidate "
                f"{view.pending.candidate_id} 都保留为独立条目）"
            )
        except LookupError as exc:
            _fail(str(exc))
            return
    typer.echo(message)


# ------------------------------------------------------------------------- export


@app.command("export")
def export_cmd(
    ctx: typer.Context,
    out: Path = typer.Option(..., "--out", "-o", help="输出文件路径"),
    fmt: str = typer.Option("json", "--format", "-f", help="导出格式（目前只支持 json）"),
    kind: Optional[str] = typer.Option(None, "--kind", "-k"),
    include_deleted: bool = typer.Option(False, "--include-deleted"),
) -> None:
    """导出为 JSON。"""
    with _session(ctx) as session:
        try:
            count = export_service.export(
                session, fmt=fmt, out_path=out, kind=kind, include_deleted=include_deleted
            )
        except export_service.ExportFormatError as exc:
            _fail(str(exc))
            return
    typer.echo(f"已导出 {count} 条 -> {out}")


# --------------------------------------------------------------------------- demo


@app.command()
def demo(
    ctx: typer.Context,
    keep: bool = typer.Option(False, "--keep", help="保留临时目录（默认跑完删除）"),
    out: Optional[Path] = typer.Option(None, "--out", help="导出 JSON 的路径（默认在临时目录内）"),
    demo_dir: Optional[Path] = typer.Option(None, "--demo-dir", help="不用临时目录，指定一个目录"),
) -> None:
    """伪造数据端到端演示：建库 → 导入 → 统计 → 导出 → 清理。"""
    from .demo import run_demo

    state = _state(ctx)
    target = state.settings.data_dir if state.explicit_data_dir else demo_dir
    run_demo(data_dir=target, out_path=out, keep=keep, echo=typer.echo)


# --------------------------------------------------------------------- data-commit


@app.command("data-commit")
def data_commit_cmd(
    ctx: typer.Context,
    message: str = typer.Option(..., "-m", "--message", help="commit message"),
) -> None:
    """在 data/（独立仓库）里 commit，不 push、也不碰主仓库。

    提交前会先 ``PRAGMA wal_checkpoint(TRUNCATE)``，保证 collection.db 快照里
    含全部改动（WAL 文件被 .gitignore 忽略，不 checkpoint 会丢东西）。
    """
    state = _state(ctx)
    checkpoint(state.settings)  # 先落盘 WAL，避免提交出去的库缺改动
    try:
        result = vcs.data_commit(state.settings, message)
    except vcs.DataRepoError as exc:
        _fail(str(exc))
        return
    typer.echo(f"data/: {result.data_message} (sha={result.data_sha or '-'})")
    typer.echo("主仓库未改动（data/ 是独立仓库，不写指针）")


# -------------------------------------------------------------------------- serve


@app.command()
def serve(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(None, "--host", help="默认 127.0.0.1"),
    port: Optional[int] = typer.Option(None, "--port", help="默认 8765"),
    reload: bool = typer.Option(False, "--reload", help="开发模式自动重载"),
) -> None:
    """启动 Web 界面（无认证，默认只绑定 127.0.0.1）。"""
    import uvicorn

    from .web.app import create_app

    state = _state(ctx)
    bind_host = host or state.settings.host
    bind_port = port or state.settings.port
    init_db(state.settings)
    application = create_app(state.settings)
    typer.echo(f"omnishelf web -> http://{bind_host}:{bind_port}  (data: {state.settings.data_dir})")
    uvicorn.run(application, host=bind_host, port=bind_port, reload=reload, log_level="info")


def main() -> None:
    """``omnishelf`` 入口点。"""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
