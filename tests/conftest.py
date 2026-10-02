"""共享测试夹具。

约定：
  * 所有测试都用 ``tmp_path`` 下的独立数据目录，不碰真实的 ``data/``。
  * 不访问网络；伪造数据来自 ``tests/fixtures/``。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlmodel import Session
from typer.testing import CliRunner

from omnishelf import repository as repo
from omnishelf.config import Settings, load_settings, repo_root
from omnishelf.db import build_engine, init_db
from omnishelf.importers.base import DictRawRecord, ItemDraft, RawRecord
from omnishelf.services.import_ import run_import

FIXTURES_DIR = repo_root() / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def no_outbound_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底红线：任何测试都不许真的出网。

    抓取类插件的测试都会自己把 ``urlopen`` 换成假站点；漏换时这个替身会直接报出来，
    而不是悄悄把（合成）凭据发到真的站点上去。
    """
    def refuse(*args: Any, **kwargs: Any) -> Any:
        target = args[0] if args else ""
        raise AssertionError(
            f"测试没有装假站点，却想发请求：{getattr(target, 'full_url', target)}"
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request, "build_opener", refuse)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture()
def movies_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "movies.json"


@pytest.fixture()
def books_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "books.json"


@pytest.fixture()
def malformed_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "malformed.json"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """全部落在 tmp_path 里：数据、插件代码目录、插件数据目录都隔离。"""
    base = load_settings(tmp_path / "data")
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    return replace(
        base,
        plugins_dir=plugins_dir,
        plugins_data_dir=tmp_path / "plugins_data",
    )


@pytest.fixture()
def engine(settings: Settings) -> Iterator[Engine]:
    init_db(settings)
    eng = build_engine(settings)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        yield sess
        sess.commit()


# --------------------------------------------------------------------- 测试用 importer


class ListImporter:
    """内存 importer：payload 直接就是 ItemDraft 的 kwargs。"""

    source = "unit"

    def __init__(self, records: Sequence[dict[str, Any]], *, source: str | None = None) -> None:
        self.records = list(records)
        if source:
            self.source = source

    def fetch(self) -> Iterable[RawRecord]:
        for payload in self.records:
            yield DictRawRecord(source=self.source, payload=payload)

    def normalize(self, raw: RawRecord) -> ItemDraft:
        return ItemDraft(**raw.payload)


@pytest.fixture()
def make_importer() -> Callable[..., ListImporter]:
    return ListImporter


@pytest.fixture()
def import_records(session: Session) -> Callable[..., Any]:
    def _import(records: Sequence[dict[str, Any]], **kwargs: Any) -> Any:
        return run_import(session, ListImporter(records, **kwargs))

    return _import


# --------------------------------------------------------------------------- 数据


@pytest.fixture()
def make_item(session: Session) -> Callable[..., Any]:
    def _make(kind: str = "movie", title: str = "Blade Runner", **kwargs: Any) -> Any:
        return repo.create_item(session, kind=kind, title=title, **kwargs)

    return _make


def write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------- CLI


@pytest.fixture()
def cli(settings: Settings) -> Callable[..., Any]:
    """调用 CLI，自动带上 ``--data-dir``。"""
    from omnishelf.cli import app

    runner = CliRunner()

    def _invoke(*args: str, **kwargs: Any) -> Any:
        return runner.invoke(app, ["--data-dir", str(settings.data_dir), *args], **kwargs)

    return _invoke


# ---------------------------------------------------------------------------- git


def _find_git() -> str | None:
    candidates = [
        os.environ.get("OMNISHELF_GIT_BIN"),
        "git",
        "/Applications/Xcode.app/Contents/Developer/usr/bin/git",
        "/usr/bin/git",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            proc = subprocess.run(
                [candidate, "--version"], capture_output=True, text=True, check=False
            )
        except OSError:
            continue
        if proc.returncode == 0:
            return candidate
    return None


GIT_BIN = _find_git()
needs_git = pytest.mark.skipif(GIT_BIN is None, reason="本机没有可用的 git")


@pytest.fixture()
def git_bin() -> str:
    assert GIT_BIN is not None
    return GIT_BIN
