"""Suppress the validator findings that upstream documents as false positives.

Upstream `.claude/docs/known-false-positives.md` says a `GFX_*` sprite missing
from `interface/*.gfx` is fine when `tools/validation/vanilla_sprites.txt` lists
it. The focus and decision icon validators only read the live vanilla install,
so they flag those sprites on machines that have the manifest but no install.
Only those two validator categories are matched, and only when a sprite named in
the message is in the manifest. Everything else stays visible.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .vanilla_manifest import load_sprite_manifest

_FOCUS_ICON_RE = re.compile(r"^Missing icon sprite '([^']+)' for focus ")
_DECISION_CANDIDATES_RE = re.compile(r"-> no sprite (.+?) defined in interface/\*\.gfx")


def suppress_issues(issues: Iterable[dict], mod_root: Path) -> tuple[list[dict], int]:
    """Return unsuppressed issues and the number removed as manifest-backed."""
    sprites = load_sprite_manifest(Path(mod_root))
    if not sprites:
        return list(issues), 0

    kept: list[dict] = []
    suppressed = 0
    for issue in issues:
        if _sprite_in_manifest(issue, sprites):
            suppressed += 1
        else:
            kept.append(issue)
    return kept, suppressed


def suppressed_count(result: dict) -> int:
    """Read a non-negative suppression count from a validator result."""
    try:
        return max(0, int(result.get("suppressed", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _sprite_in_manifest(issue: dict, sprites: frozenset[str]) -> bool:
    message = str(issue.get("message") or "")
    category = issue.get("category")
    if category == "missing-focus-icon":
        match = _FOCUS_ICON_RE.match(message)
        return match is not None and match.group(1) in sprites
    if category == "missing-decision-icon":
        match = _DECISION_CANDIDATES_RE.search(message)
        return match is not None and any(name in sprites for name in match.group(1).split(" / "))
    return False
