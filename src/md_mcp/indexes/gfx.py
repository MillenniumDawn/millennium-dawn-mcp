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
from typing import Optional

from ..paradox import parse_string
from ..paradox.parser import _unescape_string
from ..paradox.schema import SPRITE_KINDS, extract_sprite_records
from ..util.encoding import read_text
from ..util.line_numbers import line_starts, pos_to_line
from .base import GenericTxtIndex

logger = logging.getLogger(__name__)

# Match `<kind> = {`. Case-sensitive because HOI4 itself is case-sensitive on identifiers
# (per general-rules.md).
_SPRITE_OPEN_RE = re.compile(r"\b(" + "|".join(SPRITE_KINDS) + r")\s*=\s*\{")
_SPRITE_TYPES_OPEN_RE = re.compile(r"\bspriteTypes\w*\s*=\s*\{", re.IGNORECASE)

# Value shapes the AST reads as a name or path: a quoted string, or a bare symbol
# (the lexer's symbol token, which `_SYMBOL_CHAR` mirrors). Anything else (`{`, a number)
# is not a scalar.
_STRING = r'"(?:\\.|[^"\\])*"'
_SYMBOL_CHAR = r"[\w:.@\[\]\-?^/|\xa0-ɏ]"
_BARE_VALUE = r"(?:\d+\.)?[a-zA-Z_@\[\]]" + _SYMBOL_CHAR + "*"
_VALUE = rf"\s*=\s*({_STRING}|{_BARE_VALUE}|)"


def _key_alternatives(key: str) -> str:
    """Whole-symbol, case-insensitive `key = value` alternatives, one per case of the first letter.

    Each alternative starts with a plain literal so the regex engine can skip to candidate
    characters. A set or lookbehind start measured ~1.6x slower on `goals_shine.gfx`.
    """
    head, rest = key[0], key[1:]
    return "|".join(rf"{c}(?<!{_SYMBOL_CHAR}{c})(?i:{rest}){_VALUE}" for c in (head, head.upper()))


# One sweep tokenizes strings, `#` comments, braces, and the `name` / `texturefile` keys. A
# string or comment is consumed whole, so key-like text inside one never matches. The value
# groups are 1-2 (`name`) and 3-4 (`texturefile`); `m.lastindex` is None for the rest.
_SWEEP_RE = re.compile(
    rf"{_STRING}|#[^\n]*|\{{|\}}|{_key_alternatives('name')}|{_key_alternatives('texturefile')}"
)


def _scalar_text(raw: str) -> str | None:
    """Unquote a captured value like the parser does; an empty capture is a non-scalar."""
    if raw.startswith('"'):
        return _unescape_string(raw) if "\\" in raw else raw[1:-1]
    return raw or None


def _scan_sprite_blocks(text: str) -> list[dict]:
    """Brace-balanced scan: for each `<kind> = { ... }` block, extract name + texturefile.

    Performance approach:
      * One sweep of `_SWEEP_RE` walks every `{` / `}` / string / comment / `name` /
        `texturefile` token. Strings and `#` comments are consumed whole, so brace
        counting and key matching aren't fooled by `"foo {"` or `# name = "x"`.
      * The sweep keeps the open-brace stack, so each key is attributed to the block it
        is directly inside. The first occurrence per block wins, as in `Node.get`, and
        nested blocks never leak their keys to the sprite around them.
      * `_SPRITE_OPEN_RE` independently finds every `<kind> = {` opening.
      * Line numbers come from one precomputed offset table (`O(log n)` per lookup).

    Roughly O(n) in characters, no Python-level char-by-char loop.
    """
    line_offsets = line_starts(text)

    # Each open brace's immediate parent drives the hierarchy filter below. `first_name` /
    # `first_texture` map a block's open brace to the value of its first direct key
    # (None when that value is not a scalar).
    parent_open: dict[int, int] = {}
    first_name: dict[int, str | None] = {}
    first_texture: dict[int, str | None] = {}
    stack: list[int] = []
    for m in _SWEEP_RE.finditer(text):
        key = m.lastindex
        if key is not None:
            if stack:
                first = first_name if key <= 2 else first_texture
                first.setdefault(stack[-1], _scalar_text(m[key]))
            continue
        tok = m[0]
        if tok == "{":
            pos = m.start()
            parent_open[pos] = stack[-1] if stack else -1
            stack.append(pos)
        elif tok == "}":
            if not stack:
                raise ValueError("unbalanced braces (extra `}`)")
            stack.pop()
    if stack:
        raise ValueError("unbalanced braces (unclosed `{`)")

    # Collect the `{` positions of real spriteTypes* container blocks (a position missing
    # from `parent_open` was inside a string/comment).
    sprite_container_opens: set[int] = {
        m.end() - 1
        for m in _SPRITE_TYPES_OPEN_RE.finditer(text)
        if parent_open.get(m.end() - 1) == -1
    }

    records: list[dict] = []
    for m in _SPRITE_OPEN_RE.finditer(text):
        open_brace = m.end() - 1
        # Only index sprites that are direct children of a spriteTypes* block,
        # matching the hierarchy enforced by find_sprite_nodes / extract_sprite_records.
        # An opener whose `{` sat inside a string/comment has no parent entry; skip it.
        if parent_open.get(open_brace, -1) not in sprite_container_opens:
            continue
        name = first_name.get(open_brace)
        if name is None:
            continue
        records.append(
            {
                "name": name,
                "kind": m.group(1),
                "texturefile": first_texture.get(open_brace),
                "line": pos_to_line(m.start(), line_offsets),
            }
        )

    return records


def _parse_gfx_file(abs_path: str, relpath: str) -> Optional[list[dict]]:
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
    cache_version = 2
    cache_name = "gfx"
    subdir = "interface"
    pattern = "*.gfx"
    primary_key = "name"
    parser_fn = staticmethod(_parse_gfx_file)
