"""data/ repo 的 git 操作：``omnishelf data-commit -m MSG``。

data/ 是**独立的 git 仓库**（不是 submodule）：这里只做
``git add -A`` + ``git commit``（提交前由 CLI 先 checkpoint WAL）。
主仓库完全不参与——它的提交历史里不会出现任何 data 的痕迹。**绝不自动 push**。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings


class DataRepoError(RuntimeError):
    """data/ 不是 git 仓库，或 git 命令失败。"""


@dataclass(slots=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(slots=True)
class DataCommitResult:
    data_committed: bool
    data_sha: str | None
    data_message: str


def git(settings: Settings, *args: str, cwd: Path) -> GitResult:
    """在 ``cwd`` 执行一条 git 命令（不经过 shell，Windows 同样可用）。"""
    proc = subprocess.run(  # noqa: S603 - 参数是常量 + 调用方给定消息
        [settings.git_bin, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return GitResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def is_git_repo(settings: Settings, path: Path) -> bool:
    if not path.is_dir():
        return False
    return git(settings, "rev-parse", "--git-dir", cwd=path).ok


def has_changes(settings: Settings, cwd: Path) -> bool:
    result = git(settings, "status", "--porcelain", cwd=cwd)
    if not result.ok:
        raise DataRepoError(f"git status 失败: {result.stderr}")
    return bool(result.stdout)


def current_sha(settings: Settings, cwd: Path) -> str | None:
    result = git(settings, "rev-parse", "HEAD", cwd=cwd)
    return result.stdout if result.ok else None


def data_commit(settings: Settings, message: str) -> DataCommitResult:
    """提交 data/ 仓库（只动 data/，不碰主仓库）。"""
    if not message.strip():
        raise DataRepoError("commit message 不能为空")
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    if not is_git_repo(settings, data_dir):
        raise DataRepoError(f"{data_dir} 不是 git 仓库。先执行：git init {data_dir}")

    add = git(settings, "add", "-A", cwd=data_dir)
    if not add.ok:
        raise DataRepoError(f"git add 失败: {add.stderr}")

    data_committed = False
    data_sha = current_sha(settings, data_dir)
    data_message = "nothing to commit"
    if has_changes(settings, data_dir):
        commit = git(settings, "commit", "-m", message, cwd=data_dir)
        if not commit.ok:
            raise DataRepoError(f"data/ commit 失败: {commit.stderr or commit.stdout}")
        data_committed = True
        data_sha = current_sha(settings, data_dir)
        data_message = commit.stdout.splitlines()[0] if commit.stdout else "committed"

    return DataCommitResult(
        data_committed=data_committed,
        data_sha=data_sha,
        data_message=data_message,
    )


def ensure_data_gitignore(settings: Settings) -> Path:
    """在 data/ 写入 .gitignore（忽略 WAL/SHM/journal）。幂等。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / ".gitignore"
    wanted = ["*.db-wal", "*.db-shm", "*.db-journal", ".DS_Store"]
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    missing = [line for line in wanted if line not in existing]
    if missing:
        lines = [*existing, *missing]
        path.write_text("\n".join(lines).lstrip("\n") + "\n", encoding="utf-8")
    return path
