"""GFX (sprite) index — sprite name ↔ .gfx file lookup.

Mirrors `MD-VSCode-Utility-Tool/src/util/gfxindex.ts`. Walks `<mod_root>/interface/`
(plus vanilla, when configured) for any `.gfx` file and indexes the
`spriteTypes = { spriteType = { name = "..." } }` entries.

We bypass the full AST parser here and use a specialised structural scanner.
Reason: `interface/goals_shine.gfx` alone is 300 000 lines, and the full parser
spends 28 s on it. The GFX format is restricted enough that a brace-tracking
regex extracts name + texturefile in milliseconds without losing accuracy on the
sprites the agent ever needs to resolve. The full parser remains the fallback
for anything ambiguous.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from ..paradox import parse_string
from ..paradox.schema import extract_sprite_records
from ..util.encoding import read_text
from .base import GenericTxtIndex

logger = logging.getLogger(__name__)

_SPRITE_KINDS = (
    "spriteType",
    "corneredTileSpriteType",
    "frameAnimatedSpriteType",
    "maskedShieldType",
    "progressbartype",
    "barChartType",
    "PieChartType",
    "LineChartType",
    "scrollingSprite",
)

# Match `<kind> = {`. Case-sensitive because HOI4 itself is case-sensitive on identifiers
# (per general-rules.md). Field-name regexes stay case-insensitive — `name` and
# `texturefile` are *property* keys inside a sprite block and the engine is lenient there.
_SPRITE_OPEN_RE = re.compile(
    r"\b(" + "|".join(_SPRITE_KINDS) + r")\s*=\s*\{"
)
_NAME_RE = re.compile(r'\bname\s*=\s*"([^"\\]*(?:\\.[^"\\]*)*)"', re.IGNORECASE)
_NAME_BARE_RE = re.compile(r"\bname\s*=\s*([A-Za-z_][\w.]*)", re.IGNORECASE)
_TEXTUREFILE_RE = re.compile(r'\btexturefile\s*=\s*"([^"]+)"', re.IGNORECASE)
_TEXTUREFILE_BARE_RE = re.compile(r"\btexturefile\s*=\s*([^\s{}]+)", re.IGNORECASE)
_LINE_COUNT_PER_FILE = 50_000  # cap on lines we compute per match (perf safety net)


_BRACE_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|#[^\n]*|[{}]')


def _build_line_offsets(text: str) -> List[int]:
    """Precompute cumulative byte offset of each line start. O(n) once, O(log n) lookups."""
    offsets = [0]
    for i, c in enumerate(text):
        if c == "\n":
            offsets.append(i + 1)
    return offsets


def _line_at(line_offsets: List[int], pos: int) -> int:
    """Binary search line index for the given position. 1-based line number."""
    # bisect_right gives the insertion point; line index is that - 1, 1-based becomes that.
    import bisect

    return bisect.bisect_right(line_offsets, pos)


def _scan_sprite_blocks(text: str) -> List[dict]:
    """Brace-balanced scan: for each `<kind> = { ... }` block, extract name + texturefile.

    Performance approach:
      * One sweep of `_BRACE_TOKEN_RE` collects every `{` / `}` / string / comment
        token position. The regex skips quoted strings and `#` comments so brace
        counting isn't fooled by `"foo {"`.
      * `_SPRITE_OPEN_RE` independently finds every `<kind> = {` opening.
      * Line numbers come from one precomputed offset table (`O(log n)` per lookup).

    Roughly O(n) in characters, no Python-level char-by-char loop.
    """
    line_offsets = _build_line_offsets(text)

    # Find brace positions (skipping strings and comments).
    open_positions: List[int] = []
    close_positions: List[int] = []
    for m in _BRACE_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok == "{":
            open_positions.append(m.start())
        elif tok == "}":
            close_positions.append(m.start())

    # Build a sorted list of (pos, kind) — kind is +1 for open, -1 for close.
    # Then for each sprite-open position, find the matching close by walking forward.
    brace_events: List[tuple] = []
    for p in open_positions:
        brace_events.append((p, 1))
    for p in close_positions:
        brace_events.append((p, -1))
    brace_events.sort()

    # Map open-brace position → matching close-brace position via single linear pass.
    match_close: Dict[int, int] = {}
    stack: List[int] = []
    for pos, delta in brace_events:
        if delta == 1:
            stack.append(pos)
        else:
            if not stack:
                raise ValueError("unbalanced braces (extra `}`)")
            open_pos = stack.pop()
            match_close[open_pos] = pos
    if stack:
        raise ValueError("unbalanced braces (unclosed `{`)")

    records: List[dict] = []
    for m in _SPRITE_OPEN_RE.finditer(text):
        kind = m.group(1)
        open_brace = m.end() - 1
        close_brace = match_close.get(open_brace)
        if close_brace is None:
            # The sprite opener's `{` isn't in our brace map — implies it was inside
            # a string/comment. Skip.
            continue
        body = text[open_brace + 1 : close_brace]
        name_m = _NAME_RE.search(body) or _NAME_BARE_RE.search(body)
        if not name_m:
            continue
        name = name_m.group(1)
        tex_m = _TEXTUREFILE_RE.search(body) or _TEXTUREFILE_BARE_RE.search(body)
        texturefile = tex_m.group(1) if tex_m else None
        records.append(
            {
                "name": name,
                "kind": kind,
                "texturefile": texturefile,
                "line": _line_at(line_offsets, m.start()),
            }
        )

    return records


import bisect  # noqa: E402 — used by _line_at; imported at module load for hot-path perf


def _parse_gfx_file(abs_path: str, relpath: str) -> Optional[List[dict]]:
    try:
        text = read_text(abs_path)
    except OSError as e:
        logger.warning("gfx index: cannot read %s: %s", abs_path, e)
        return None
    if "spriteType" not in text and "spritetype" not in text:
        return []

    # Fast path: structural scanner. Falls back to the AST parser if anything looks
    # off — guarantees we never silently lose a sprite to scanner brittleness.
    try:
        return _scan_sprite_blocks(text)
    except Exception as e:
        logger.info("gfx index: fast scan failed on %s, falling back to AST parser: %s", relpath, e)

    try:
        root = parse_string(text, error_prefix=f"In file {relpath}:\n")
    except Exception as e:
        logger.warning("gfx index: parse failed for %s: %s", relpath, e)
        return None
    return extract_sprite_records(root, source=text)


class GfxIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "gfx"
    subdir = "interface"
    pattern = "*.gfx"
    primary_key = "name"
    parser_fn = staticmethod(_parse_gfx_file)
