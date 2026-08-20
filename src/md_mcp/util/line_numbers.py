"""Byte offset → line number translation.

Canonical implementation for ``Token.start`` (byte offset) to 1-based line
number. Extracted from three copies in ``tools/parser_tools.py``,
``paradox/schema.py``, and ``analysis/ref_audit.py`` (issue #5).
"""

from __future__ import annotations

import bisect


def line_starts(text: str) -> list[int]:
    """Cumulative offset of each line start. ``line_starts(text)[0] == 0``.

    Only ``\\n`` (``\\x0a``) is treated as a line delimiter; ``\\r`` is
    kept as part of the line. Offsets are Python ``str`` indices (``ord``
    units), which matches ``Token.start`` for the ASCII-only sources the
    mod uses.
    """
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def pos_to_line(pos: int, starts: list[int]) -> int:
    """Translate a byte offset to a 1-based line number via ``bisect_right``.

    ``starts`` must be from :func:`line_starts`. Out-of-bounds ``pos``
    (``>= len(text)``) returns the last line, matching the previous
    linear-scan behaviour.
    """
    return bisect.bisect_right(starts, pos)
