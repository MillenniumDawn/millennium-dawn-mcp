"""Mod roots, settings, and git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

from md_mcp.config import Settings


def make_mod_root(path: Path) -> Path:
    """Bare mod root: `descriptor.mod` and an empty `tools/validation/`."""
    (path / "tools" / "validation").mkdir(parents=True)
    (path / "descriptor.mod").write_text('name = "x"\n', encoding="utf-8")
    return path


def mod_settings(
    mod_root: Path, cache_dir: Path | None = None, validator_mode: str = "isolated"
) -> Settings:
    """Settings with no vanilla path. The cache defaults to the mod's own cache dir."""
    if cache_dir is None:
        cache_dir = mod_root / ".md-mcp-cache"
    return Settings(
        mod_root=mod_root, vanilla_path=None, cache_dir=cache_dir, validator_mode=validator_mode
    )


def in_process_settings(mod_root: Path, cache_dir: Path) -> Settings:
    return mod_settings(mod_root, cache_dir, validator_mode="in_process")


def git(repo: Path, *args: str) -> str:
    """Run `git <args>` in `repo` and return stdout. A failing command does not raise."""
    out = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout


def init_repo(repo: Path) -> None:
    """Init a quiet test repo with a baseline commit so subsequent diffs are meaningful."""
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "baseline.txt").write_text("baseline\n")
    git(repo, "add", "baseline.txt")
    git(repo, "commit", "-qm", "baseline")
