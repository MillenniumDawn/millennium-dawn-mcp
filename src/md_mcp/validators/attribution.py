"""Resolve a validator issue back to a mod-relative file path.

`Issue.file` is not uniform across the suite. `validator_common.py` reverse-
engineers the field from the message text with four location regexes and falls
back to `""` when none match, so the shape depends on how a given check happened
to phrase itself: mod-relative for most, `os.path.basename(...)` for some, empty
for others, the literal `"unknown"` for a few. Nine upstream modules emit more
than one shape, so a per-validator lookup table can't express it.

This resolves by shape instead, against an index of the mod's real files:

  1. an exact mod-relative path wins outright
  2. a partial path (bare basename, or trailing segments) resolves if it matches
     exactly one file in the validator's scan directories
  3. an empty or placeholder field falls back to filename-shaped tokens scraped
     out of the message, resolved the same way
  4. anything ambiguous or unrecognised stays unattributed

Constraining step 2 to the validator's own scan directories is what makes it
safe. Mod-wide, 14% of `.txt` files share a basename (`USA.txt` lives in seven
directories). Within one validator's domain, collisions are close to zero, and
the ones that remain are detected here and refused rather than guessed.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Extensions the validator suite reports on. Bounds the message scrape so prose
# like "1.13.*" or "ALG.2001" can't be mistaken for a filename.
_SCRIPT_EXTS = ("txt", "yml", "gfx", "gui", "lua")

_FILENAME_RE = re.compile(
    r"[\w][\w.\-]*(?:[/\\][\w.\-]+)*\.(?:" + "|".join(_SCRIPT_EXTS) + r")\b",
    re.IGNORECASE,
)

# Values that mean "no location", not a real path.
_PLACEHOLDERS = frozenset({"", "unknown", "none", "n/a", "-"})

# Fallback scan roots when the mod isn't a git checkout. Keeps the walk off
# resources/ and docs/, which validators never report against.
_WALK_ROOTS = ("common", "events", "history", "interface", "localisation", "gfx", "map")


class IssueAttributor:
    """Maps validator issues onto mod-relative paths. Index built once, lazily."""

    def __init__(self, mod_root: Path):
        self.mod_root = Path(mod_root)
        self._index: Optional[Dict[str, List[str]]] = None
        self._path_tuple: Optional[Tuple[str, ...]] = None

    def resolve(self, issue: dict, scan_prefixes: Sequence[str] = ()) -> Optional[str]:
        """Return the mod-relative path this issue belongs to, or None."""
        raw = (issue.get("file") or "").strip()
        candidate = _normalise(raw)

        if candidate and candidate.lower() not in _PLACEHOLDERS:
            if candidate in self._paths():
                return candidate
            hit = self._match_suffix(candidate, scan_prefixes)
            if hit:
                return hit
            # Directory component means it's already a mod-relative path, even if
            # the file is gone. Hand it back and let the scope test drop it; only
            # values we genuinely can't place count as unattributed.
            return candidate if "/" in candidate else None

        for token in _FILENAME_RE.findall(issue.get("message") or ""):
            hit = self._match_suffix(_normalise(token), scan_prefixes)
            if hit:
                return hit
        return None

    # ------------------------------------------------------------------

    def _match_suffix(self, partial: str, scan_prefixes: Sequence[str]) -> Optional[str]:
        if not partial:
            return None
        candidates = self._index_by_basename().get(os.path.basename(partial), ())
        if scan_prefixes:
            prefixes = tuple(scan_prefixes)
            candidates = [c for c in candidates if c.startswith(prefixes)]
        matches = [c for c in candidates if c == partial or c.endswith("/" + partial)]
        return matches[0] if len(matches) == 1 else None

    def _index_by_basename(self) -> Dict[str, List[str]]:
        if self._index is None:
            self._index = {}
            for p in self._paths():
                self._index.setdefault(os.path.basename(p), []).append(p)
        return self._index

    def _paths(self) -> Tuple[str, ...]:
        if self._path_tuple is None:
            self._path_tuple = _list_mod_files(self.mod_root)
        return self._path_tuple


def _normalise(value: str) -> str:
    return value.replace("\\", "/").strip().lstrip("./")


def _list_mod_files(mod_root: Path) -> Tuple[str, ...]:
    """Every mod-relative file path. `git ls-files` when possible, else a walk."""
    try:
        proc = subprocess.run(
            # --others picks up files the user just created; lint's changed-file
            # scope includes them, so attribution has to see them too.
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(mod_root),
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        if proc.returncode == 0:
            listed = proc.stdout.decode("utf-8", "replace").split("\0")
            paths = tuple(p for p in listed if p)
            if paths:
                return paths
    except (OSError, subprocess.SubprocessError):
        pass

    out: List[str] = []
    for root_name in _WALK_ROOTS:
        root = mod_root / root_name
        if not root.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, mod_root).replace(os.sep, "/")
            out.extend(f"{rel_dir}/{f}" for f in filenames)
    return tuple(out)
