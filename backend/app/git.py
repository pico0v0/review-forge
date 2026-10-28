"""Git operations restricted to explicit paths and resolved commit IDs."""

import os
import subprocess
from pathlib import Path

from app.errors import HubError


def git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-c", "core.hooksPath=", "-C", str(root), *args],
            input=input_bytes,
            capture_output=True,
            timeout=60,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HubError("git_failed", "Git command failed or timed out") from exc
    if result.returncode:
        raise HubError("git_failed", result.stderr.decode("utf-8", errors="replace")[-3000:])
    return result.stdout


def head(root: Path) -> str:
    return git(root, "rev-parse", "--verify", "HEAD").decode().strip()


def require_clean(root: Path) -> None:
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise HubError(
            "project_dirty",
            "Commit or remove pending changes before starting a review",
            details={"status": status.decode("utf-8", errors="replace")[:2000]},
        )


def create_worktree(root: Path, target: Path, branch: str, commit: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-b", branch, str(target), commit)


def diff(root: Path, base_commit: str) -> str:
    return git(
        root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", base_commit, "--"
    ).decode("utf-8", errors="strict")


def commit_files(root: Path, paths: list[str], message: str) -> None:
    if not paths:
        return
    git(root, "add", "--", *paths)
    git(
        root,
        "-c",
        "user.name=ReviewForge",
        "-c",
        "user.email=reviewforge@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        message,
    )


def reset_to(root: Path, commit: str) -> None:
    """Discard generated changes inside a task-owned worktree only."""
    git(root, "reset", "--hard", commit)
    git(root, "clean", "-fd")
