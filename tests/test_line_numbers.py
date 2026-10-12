"""Contract tests for util/line_numbers: offset to line translation.

Every site delegates here, so an off-by-one breaks all line numbers.
"""

from __future__ import annotations

import pytest

from md_mcp.util.line_numbers import line_and_column, line_starts, pos_to_line


@pytest.mark.parametrize(
    ("text", "starts"),
    [
        ("", [0]),
        ("abc", [0]),
        ("abc\n", [0, 4]),
        ("a\nb\nc", [0, 2, 4]),
        ("a\nb\n", [0, 2, 4]),
        ("ab\ncd\nef", [0, 3, 6]),
        ("a\n\nb", [0, 2, 3]),
        ("\n", [0, 1]),
        ("\n\n", [0, 1, 2]),
        ("\n\n\na\n\n", [0, 1, 2, 3, 5, 6]),
        # Only \n ends a line. A \r stays on the line it is in.
        ("a\r\nb", [0, 3]),
        ("a\r\nb\r\nc\r\n", [0, 3, 6, 9]),
        ("a\rb\r\n\r\n", [0, 5, 7]),
        # Offsets are str indices, not UTF-8 bytes.
        ("é\nx", [0, 2]),
        ("é\n日本語\n😀\nx", [0, 2, 6, 8]),
    ],
)
def test_line_starts(text, starts):
    assert line_starts(text) == starts


def test_line_starts_matches_a_char_by_char_scan_on_large_text():
    text = "".join(f"line {i} = {{ x = {i} }}\n" if i % 7 else "\n" for i in range(2000))

    assert line_starts(text) == [0, *(i + 1 for i, ch in enumerate(text) if ch == "\n")]


@pytest.mark.parametrize(
    ("text", "pos", "line"),
    [
        ("a\nb\nc", 0, 1),
        ("ab\ncd", 1, 1),
        # The newline character belongs to the line it ends.
        ("a\nb", 1, 1),
        ("a\nb", 2, 2),
        ("ab\ncd\nef", 0, 1),
        ("ab\ncd\nef", 3, 2),
        ("ab\ncd\nef", 6, 3),
        # One past the end, and far past it, are the last line.
        ("a\nb", 3, 2),
        ("a\nb", 99, 2),
        ("a\nb", -1, 1),
        ("a\nb", -99, 1),
        ("é\nx", 0, 1),
        ("é\nx", 1, 1),
        ("é\nx", 2, 2),
    ],
)
def test_pos_to_line(text, pos, line):
    assert pos_to_line(pos, line_starts(text)) == line


@pytest.mark.parametrize(
    ("text", "pos", "expected"),
    [
        ("ab\ncd\nef", 0, (1, 1)),
        ("ab\ncd\nef", 2, (1, 3)),
        ("ab\ncd\nef", 3, (2, 1)),
        ("ab\ncd\nef", 4, (2, 2)),
        ("ab\ncd\nef", 7, (3, 2)),
        ("a\nb", 3, (2, 2)),
        ("a\nb", -1, (1, 1)),
    ],
)
def test_line_and_column(text, pos, expected):
    assert line_and_column(pos, line_starts(text)) == expected


def test_pos_to_line_monotonic():
    text = "one\ntwo\nthree\nfour"
    starts = line_starts(text)
    lines = [pos_to_line(i, starts) for i in range(len(text))]
    assert lines == sorted(lines)
    assert lines[0] == 1
    assert lines[-1] == 4


def test_round_trip_starts_give_their_line():
    text = "x\ny\nz\n"
    starts = line_starts(text)
    for idx, off in enumerate(starts):
        assert pos_to_line(off, starts) == idx + 1
