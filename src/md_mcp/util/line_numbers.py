"""Byte offset → line number translation.

Canonical implementation for ``Token.start`` (byte offset) to 1-based line
number. Extracted from three copies in ``tools/parser_tools.py``,
``paradox/schema.py``, and ``analysis/ref_audit.py`` (issue #5).
"""

from __future__ import annotations

import bisect


def line_starts(text: str) -> list[int]:
    """Cumulative offset of each line start. ``line_starts(text)[0] == 0``."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def pos_to_line(pos: int, line_starts_list: list[int]) -> int:
    """Translate a byte offset to a 1-based line number."""
    return bisect.bisect_right(line_starts_list, pos)
