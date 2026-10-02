"""data-commit 的 git 编排（在临时仓库里跑，不碰真实 repo）。"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from omnishelf.config import Settings
from omnishelf.services import vcs
from tests.conftest import needs_git


def _init_repo(git_bin: str, path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run([git_bin, *args], cwd=path, check=True, capture_output=True)


def test_gitignore_is_created_idempotently(settings: Settings) -> None:
    path = vcs.ensure_data_gitignore(settings)
    content = path.read_text(encoding="utf-8")
    assert "*.db-wal" in content and "*.db-shm" in content and "*.db-journal" in content
    assert ".DS_Store" in content  # macOS 垃圾文件，别提交进 data 仓库
    vcs.ensure_data_gitignore(settings)
    assert path.read_text(encoding="utf-8").count("*.db-wal") == 1


def test_data_commit_requires_git_repo(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(vcs.DataRepoError):
        vcs.data_commit(settings, "msg")


def test_data_commit_requires_message(settings: Settings) -> None:
    with pytest.raises(vcs.DataRepoError):
        vcs.data_commit(settings, "   ")


@needs_git
def test_data_commit_commits_data_repo(settings: Settings, git_bin: str) -> None:
    _init_repo(git_bin, settings.data_dir)
    (settings.data_dir / "collection.db").write_bytes(b"fake-db")
    (settings.data_dir / "attachments").mkdir()
    cfg = replace(settings, git_bin=git_bin)

    result = vcs.data_commit(cfg, "add db")
    assert result.data_committed is True
    assert result.data_sha
    # CLI 会自己加 "data/: " 前缀，这里不能再带一遍
    assert not result.data_message.startswith("data/")
    assert vcs.has_changes(cfg, settings.data_dir) is False

    again = vcs.data_commit(cfg, "noop")
    assert again.data_committed is False
    assert again.data_sha == result.data_sha
    assert again.data_message == "nothing to commit"


@needs_git
def test_git_helpers(settings: Settings, git_bin: str, tmp_path: Path) -> None:
    cfg = replace(settings, git_bin=git_bin)
    assert vcs.is_git_repo(cfg, tmp_path) is False
    _init_repo(git_bin, tmp_path)
    assert vcs.is_git_repo(cfg, tmp_path) is True
    assert vcs.current_sha(cfg, tmp_path) is None  # 还没有提交
    assert vcs.has_changes(cfg, tmp_path) is False

    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    assert vcs.has_changes(cfg, tmp_path) is True
    result = vcs.git(cfg, "status", "--porcelain", cwd=tmp_path)
    assert result.ok is True
    assert "f.txt" in result.stdout


@needs_git
def test_data_commit_does_not_touch_the_superproject(
    settings: Settings, git_bin: str, tmp_path: Path
) -> None:
    """data/ 是独立仓库：data-commit 只动 data/，主仓库一个字节都不该变。"""
    _init_repo(git_bin, settings.data_dir)
    (settings.data_dir / "collection.db").write_bytes(b"fake-db")

    superproject = tmp_path / "super"
    _init_repo(git_bin, superproject)
    (superproject / "code.py").write_text("print('hi')\n", encoding="utf-8")
    vcs.git(replace(settings, git_bin=git_bin), "add", "-A", cwd=superproject)
    vcs.git(replace(settings, git_bin=git_bin), "commit", "-m", "init", cwd=superproject)
    before = vcs.current_sha(replace(settings, git_bin=git_bin), superproject)

    cfg = replace(settings, git_bin=git_bin)
    result = vcs.data_commit(cfg, "add db")
    assert result.data_committed is True

    # 主仓库：没有新提交、没有未跟踪/未提交的变化（data/ 也不在它的索引里）
    assert vcs.current_sha(cfg, superproject) == before
    assert vcs.has_changes(cfg, superproject) is False
    assert "data" not in vcs.git(cfg, "ls-files", cwd=superproject).stdout.split("\n")
