"""Index cache helpers: stepped mtimes, state snapshots, and a parse spy."""

from __future__ import annotations

import os
from pathlib import Path


def write_stepped(path: Path, text: str, step: int) -> None:
    """Write `path` and give it a distinct mtime so the signature always moves."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    ns = 1_700_000_000_000_000_000 + step * 1_000_000_000
    os.utime(path, ns=(ns, ns))


def write_loc(root: Path, rel: str, lang: str, key: str, value: str) -> Path:
    """Write a one-key localisation file with a BOM, as the game expects."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'﻿{lang}:\n {key}:0 "{value}"\n', encoding="utf-8")
    return path


def bump_mtime(path: Path) -> None:
    """Move `path`'s mtime a second forward so its signature changes."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def force_refresh(idx) -> None:
    """Skip the debounce and refresh now."""
    idx._stale_check.force_next()
    idx.ensure_fresh()


def index_state(idx) -> tuple:
    idx.ensure_fresh()
    return (
        list(idx._by_file.items()),
        idx._by_key,
        idx._duplicates,
        idx._parse_errors,
    )


def scratch_state(cls, root: Path, tmp_path: Path, tag: str, **kwargs) -> tuple:
    """State of a brand-new instance with an empty cache: the ground truth."""
    return index_state(cls(root, tmp_path / f"scratch-{tag}", include_vanilla=False, **kwargs))


def spy_parse(idx, monkeypatch) -> list[list[str]]:
    """Record the relpaths each `_parse_parallel` call is asked to parse."""
    calls: list[list[str]] = []
    real = idx._parse_parallel

    def spy(relpaths: list[str]) -> list:
        calls.append(list(relpaths))
        return real(relpaths)

    monkeypatch.setattr(idx, "_parse_parallel", spy)
    return calls
