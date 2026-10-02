"""``omnishelf demo``：伪造数据端到端。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnishelf.demo import FIXTURE_PLAN, run_demo


def test_fixture_plan_covers_all_fixtures() -> None:
    names = {filename for _, filename in FIXTURE_PLAN}
    assert names == {"movies.json", "books.json", "malformed.json"}


def test_run_demo_keeps_dir_when_asked(tmp_path: Path) -> None:
    lines: list[str] = []
    result = run_demo(data_dir=tmp_path / "demo", keep=True, echo=lines.append)

    assert result.data_dir.is_dir()
    assert result.db_path.is_file()
    assert result.schema_version == 3
    assert len(result.imports) == 3
    assert result.total_imported == 17  # 8 + 5 + 4

    stats = result.stats
    assert stats["total"] == 12
    assert stats["by_kind"] == {"book": 5, "movie": 7}
    assert stats["pending_merges"] == 1
    assert len(result.pending) == 1

    assert result.export_path.is_file()
    payload = json.loads(result.export_path.read_text(encoding="utf-8"))
    assert payload["count"] == 12
    titles = {row["title"] for row in payload["items"]}
    assert "Blade Runner" in titles and "Dune" in titles
    assert any(row["external_ids"] for row in payload["items"])

    output = "\n".join(lines)
    assert "omnishelf demo" in output
    assert "kind:" in output
    assert "待复核" in output
    assert "book=5" in output
    assert "movie=7" in output


def test_run_demo_cleans_up_temp_dir() -> None:
    result = run_demo(echo=lambda _line: None)
    assert result.kept is False
    assert not result.data_dir.exists()


def test_run_demo_honours_out_path(tmp_path: Path) -> None:
    out = tmp_path / "elsewhere" / "export.json"
    result = run_demo(data_dir=tmp_path / "demo2", out_path=out, keep=True, echo=lambda _l: None)
    assert result.export_path == out.resolve()
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8"))["count"] == 12


def test_run_demo_missing_fixtures(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_demo(
            data_dir=tmp_path / "demo3",
            fixtures_dir=tmp_path / "no-such-fixtures",
            echo=lambda _line: None,
        )


def test_demo_result_as_dict(tmp_path: Path) -> None:
    result = run_demo(data_dir=tmp_path / "demo4", keep=True, echo=lambda _l: None)
    data = result.as_dict()
    assert data["schema_version"] == 3
    assert len(data["imports"]) == 3
    assert data["stats"]["total"] == 12
    assert data["kept"] is True
