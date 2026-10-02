"""CLI 冒烟：init → import → list → show → edit → review → export 全链路。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _json(result: Any) -> Any:
    return json.loads(result.output)


def test_cli_help(cli: Callable[..., object]) -> None:
    result = cli("--help")
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "demo" in result.output  # type: ignore[attr-defined]


def test_cli_full_chain(cli: Callable[..., object], fixtures_dir: Path, tmp_path: Path) -> None:
    result = cli("init")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "user_version = 3" in result.output  # type: ignore[attr-defined]

    result = cli("import", str(fixtures_dir / "movies.json"), "--importer", "json_movies")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "新增 5" in result.output  # type: ignore[attr-defined]
    assert "跳过 1" in result.output  # type: ignore[attr-defined]

    result = cli("import", str(fixtures_dir / "books.json"), "--importer", "json_books")
    assert result.exit_code == 0  # type: ignore[attr-defined]

    result = cli("list", "--json")
    assert result.exit_code == 0  # type: ignore[attr-defined]
    rows = _json(result)
    assert len(rows) == 11
    movie = next(r for r in rows if r["title"] == "Blade Runner")

    result = cli("list", "--kind", "book", "--json")
    assert len(_json(result)) == 5

    result = cli("list", "--status", "done")
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "Blade Runner" in result.output  # type: ignore[attr-defined]

    result = cli("list", "--tag", "sci-fi", "--json")
    assert len(_json(result)) == 3  # Blade Runner / Arrival / Dune

    result = cli("list", "--title-prefix", "bla")
    assert "Blade Runner" in result.output  # type: ignore[attr-defined]

    result = cli("show", movie["id"])
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "Ridley Scott" in result.output  # type: ignore[attr-defined]
    assert "tmdb:78" in result.output  # type: ignore[attr-defined]

    result = cli("show", movie["id"], "--json")
    detail = _json(result)
    assert detail["external_ids"][0]["source"] == "tmdb"

    result = cli(
        "edit",
        movie["id"],
        "--status",
        "doing",
        "--rating",
        "9.9",
        "--add-tag",
        "favorite",
        "--subtitle",
        "Final Cut",
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "9.9" in result.output  # type: ignore[attr-defined]

    result = cli("show", movie["id"], "--json")
    edited = _json(result)
    assert edited["status"] == "doing"
    assert edited["rating"] == 9.9
    assert "favorite" in edited["tags"]
    assert edited["subtitle"] == "Final Cut"

    result = cli("edit", movie["id"], "--tag", "only", "--rating", "none")
    assert result.exit_code == 0  # type: ignore[attr-defined]
    result = cli("show", movie["id"], "--json")
    assert _json(result)["tags"] == ["only"]
    assert _json(result)["rating"] is None

    result = cli("review", "list")
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "Spirited Away" in result.output  # type: ignore[attr-defined]

    result = cli("review", "list", "--json")
    pending = _json(result)
    assert len(pending) == 1
    candidate_id = pending[0]["candidate"]["id"]
    pending_id = pending[0]["id"]

    result = cli("review", "merge", str(pending_id), "--keep", candidate_id)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "已合并" in result.output  # type: ignore[attr-defined]

    result = cli("review", "list")
    assert "没有待复核项" in result.output  # type: ignore[attr-defined]

    out = tmp_path / "export.json"
    result = cli("export", "--format", "json", "--out", str(out))
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["count"] == 10  # 11 - 1 条被合并后软删除
    assert payload["stats"]["by_kind"] == {"book": 5, "movie": 5}


def test_cli_import_multiple_files(cli: Callable[..., object], fixtures_dir: Path) -> None:
    """同一个 importer 一次导入多个文件。"""
    assert cli("init").exit_code == 0  # type: ignore[attr-defined]
    result = cli(
        "import",
        str(fixtures_dir / "movies.json"),
        str(fixtures_dir / "malformed.json"),
        "--importer",
        "json_movies",
        "--json",
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    payload = _json(result)
    assert len(payload) == 2
    assert payload[0]["inserted"] == 5
    assert payload[1]["inserted"] == 1
    assert len(_json(cli("list", "--json"))) == 7


def test_cli_import_strict_fails_on_record_errors(
    cli: Callable[..., object], fixtures_dir: Path
) -> None:
    assert cli("init").exit_code == 0  # type: ignore[attr-defined]
    ok = cli("import", str(fixtures_dir / "malformed.json"), "--importer", "json_movies")
    assert ok.exit_code == 0  # type: ignore[attr-defined]
    assert "错误 3" in ok.output  # type: ignore[attr-defined]
    strict = cli(
        "import", str(fixtures_dir / "malformed.json"), "--importer", "json_movies", "--strict"
    )
    assert strict.exit_code == 1  # type: ignore[attr-defined]


def test_cli_errors(cli: Callable[..., object], fixtures_dir: Path, tmp_path: Path) -> None:
    assert cli("init").exit_code == 0  # type: ignore[attr-defined]
    assert cli("show", "01HZZZZZZZZZZZZZZZZZZZZZZZ").exit_code == 1  # type: ignore[attr-defined]
    assert cli("import", str(fixtures_dir / "movies.json"), "--importer", "nope").exit_code == 1  # type: ignore[attr-defined]
    assert cli("edit", "01HZZZZZZZZZZZZZZZZZZZZZZZ", "--status", "done").exit_code == 1  # type: ignore[attr-defined]
    assert cli("edit", "01HZZZZZZZZZZZZZZZZZZZZZZZ", "--status", "bogus").exit_code == 1  # type: ignore[attr-defined]
    bad_out = tmp_path / "x.csv"
    assert cli("export", "--format", "csv", "--out", str(bad_out)).exit_code == 1  # type: ignore[attr-defined]
    assert cli("review", "skip", "999").exit_code == 1  # type: ignore[attr-defined]


def test_cli_review_skip(cli: Callable[..., object], fixtures_dir: Path) -> None:
    assert cli("init").exit_code == 0  # type: ignore[attr-defined]
    cli("import", str(fixtures_dir / "movies.json"), "--importer", "json_movies")
    result = cli("review", "list", "--json")
    pending_id = _json(result)[0]["id"]
    skip = cli("review", "skip", str(pending_id))
    assert skip.exit_code == 0  # type: ignore[attr-defined]
    assert "已跳过" in skip.output  # type: ignore[attr-defined]
    assert _json(cli("review", "list", "--json")) == []


def test_cli_demo_uses_explicit_data_dir(cli: Callable[..., object]) -> None:
    result = cli("demo")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "统计" in result.output  # type: ignore[attr-defined]
    assert "待复核" in result.output  # type: ignore[attr-defined]


def test_cli_data_commit_reports_error_without_git(
    cli: Callable[..., object], tmp_path: Path
) -> None:
    assert cli("init").exit_code == 0  # type: ignore[attr-defined]
    result = cli("data-commit", "-m", "test")
    # data/ 由 init 建出来但不是 git 仓库 -> 友好报错
    assert result.exit_code == 1  # type: ignore[attr-defined]


def test_cli_version_flag(cli: Any) -> None:
    """--version 与包版本一致（发布时别只改 pyproject 忘了 __version__）。"""
    from omnishelf import __version__

    result = cli("--version")
    assert result.exit_code == 0
    assert f"omnishelf {__version__}" in result.output
    assert __version__ == "0.1.0-alpha"
