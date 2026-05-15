"""Structured branch diff — what changed, classified by HOI4 content kind.

Runs `git diff --name-status <base>..HEAD` from the mod root, classifies each path
into a kind (focus / event / decision / idea / loc / gfx / mio / other), and for
text files where we can cheaply parse, computes added/removed/modified IDs by
diffing the AST against the base revision.

Returns a flat structured summary. Designed to be consumed mid-turn so the agent
knows where to focus its review.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..paradox import parse_string
from ..paradox.schema import (
    extract_decision_records,
    extract_event_records,
    extract_focus_records,
    extract_idea_records,
)
from ..util.response import enforce_budget


def diff_summary(
    mod_root: Path,
    base: str = "main",
    *,
    kinds: Optional[List[str]] = None,
    with_ids: bool = True,
    limit: int = 200,
) -> dict:
    """Structured branch diff vs `base`.

    Args:
      kinds    — restrict to these kinds (focus/event/decision/idea/loc/gfx/mio/history/...).
      with_ids — diff added/removed IDs per file (slow on big branches; parses both revs).
                 Set False for a fast file-only listing.
      limit    — cap returned `files`. `total_files` + `counts_by_kind` stay accurate.
    """
    git_files = _git_diff_files(mod_root, base)
    if git_files is None:
        return {"ok": False, "error": "git diff failed — is this a git repo?"}

    kinds_set = set(kinds) if kinds else None
    by_kind: Dict[str, List[dict]] = {}
    file_records: List[dict] = []

    for status, path in git_files:
        kind = _classify(path)
        if kinds_set is not None and kind not in kinds_set:
            continue
        record = {"path": path, "status": status, "kind": kind}

        if with_ids and kind in ("focus", "event", "decision", "idea") and status != "D":
            try:
                head_text = _read_at(mod_root, "HEAD", path)
                base_text = _read_at(mod_root, base, path) if status != "A" else ""
                added, removed = _diff_ids(base_text, head_text, kind)
                if added:
                    record["added_ids"] = added
                if removed:
                    record["removed_ids"] = removed
            except Exception:
                pass

        file_records.append(record)
        by_kind.setdefault(kind, []).append(record)

    counts = {kind: len(records) for kind, records in by_kind.items()}
    truncated = len(file_records) > limit
    return enforce_budget(
        {
            "ok": True,
            "base": base,
            "total_files": len(file_records),
            "files_returned": min(limit, len(file_records)),
            "counts_by_kind": counts,
            "truncated": truncated,
            "with_ids": with_ids,
            "files": file_records[:limit],
        },
        heavy_keys=("files",),
    )


def _classify(path: str) -> str:
    p = path.replace("\\", "/")
    if p.startswith("common/national_focus/"):
        return "focus"
    if p.startswith("events/"):
        return "event"
    if p.startswith("common/decisions/"):
        return "decision"
    if p.startswith("common/ideas/"):
        return "idea"
    if p.startswith("localisation/"):
        return "loc"
    if p.startswith("interface/") and p.endswith(".gfx"):
        return "gfx"
    if p.startswith("common/military_industrial_organization/"):
        return "mio"
    if p.startswith("history/"):
        return "history"
    if p.startswith("tools/"):
        return "tools"
    if p.startswith(".claude/"):
        return "claude"
    return "other"


def _git_diff_files(mod_root: Path, base: str) -> Optional[List[Tuple[str, str]]]:
    """Returns [(status_letter, path), ...] from `git diff --name-status base..HEAD`."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-status", f"{base}..HEAD"],
            cwd=str(mod_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None

    out: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        # Format: `M\tpath`, `A\tpath`, `D\tpath`, `R100\told\tnew` for renames
        parts = line.split("\t")
        if parts[0].startswith("R") and len(parts) >= 3:
            out.append(("R", parts[2]))
        elif len(parts) >= 2:
            out.append((parts[0][0], parts[1]))
    return out


def _read_at(mod_root: Path, rev: str, path: str) -> str:
    """Read `path` at git revision `rev`. Empty string if it doesn't exist there."""
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=str(mod_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    text = proc.stdout
    # Strip BOM if any.
    if text.startswith("﻿"):
        text = text[1:]
    return text


def _diff_ids(base_text: str, head_text: str, kind: str) -> Tuple[List[str], List[str]]:
    """Return (added_ids, removed_ids) for the given content kind."""
    base_ids = set(_extract_ids(base_text, kind)) if base_text else set()
    head_ids = set(_extract_ids(head_text, kind)) if head_text else set()
    added = sorted(head_ids - base_ids)
    removed = sorted(base_ids - head_ids)
    return added, removed


def _extract_ids(text: str, kind: str) -> List[str]:
    try:
        root = parse_string(text)
    except Exception:
        return []
    if kind == "focus":
        return [r["id"] for r in extract_focus_records(root)]
    if kind == "event":
        return [r["id"] for r in extract_event_records(root)]
    if kind == "decision":
        return [r["id"] for r in extract_decision_records(root)]
    if kind == "idea":
        return [r["id"] for r in extract_idea_records(root)]
    return []
