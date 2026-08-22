"""Byte offset → line number translation.

Canonical ``Token.start`` → 1-based line via ``line_starts`` + ``pos_to_line``.
"""

from __future__ import annotations

import bisect


def line_and_column(pos: int, starts: list[int]) -> tuple[int, int]:
    """Translate a byte offset to a 1-based ``(line, column)`` tuple.

    ``starts`` must be from :func:`line_starts`; out-of-range ``pos`` (``>=
    len(text)``) maps to the final line.
    """
    line = pos_to_line(pos, starts)
    line_start = starts[line - 1]
    column = (pos - line_start) + 1
    return line, column


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
