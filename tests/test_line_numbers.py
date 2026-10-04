"""Contract tests for util/line_numbers — offset→line translation.

Every site delegates here, so an off-by-one breaks all line numbers.
"""

from __future__ import annotations

import pytest

from md_mcp.paradox import parse_string
from md_mcp.paradox.schema import extract_focus_records, to_json_with_lines
from md_mcp.util.line_numbers import line_and_column, line_starts, pos_to_line

# ---------------------------------------------------------------------------
# line_starts — shape
# ---------------------------------------------------------------------------


def test_line_starts_empty():
    assert line_starts("") == [0]


def test_line_starts_no_newline():
    assert line_starts("abc") == [0]


def test_line_starts_single_newline():
    assert line_starts("abc\n") == [0, 4]


def test_line_starts_multiple_lines():
    assert line_starts("a\nb\nc") == [0, 2, 4]


def test_line_starts_consecutive_empty_lines():
    assert line_starts("a\n\nb") == [0, 2, 3]


def test_line_starts_trailing_newlines():
    assert line_starts("\n") == [0, 1]
    assert line_starts("\n\n") == [0, 1, 2]


def test_line_starts_trailing_newline_with_content():
    assert line_starts("a\nb\n") == [0, 2, 4]


def test_line_starts_crlf_keeps_cr_on_the_line():
    assert line_starts("a\r\nb") == [0, 3]


def test_line_starts_non_ascii_uses_str_indices():
    text = "é\nx"
    starts = line_starts(text)
    assert starts == [0, 2]
    assert pos_to_line(0, starts) == 1
    assert pos_to_line(1, starts) == 1
    assert pos_to_line(2, starts) == 2


def _reference_line_starts(text: str) -> list[int]:
    """The original char-by-char implementation, kept as the oracle."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


@pytest.mark.parametrize(
    "text",
    [
        "",
        "abc",
        "abc\n",
        "a\nb\nc",
        "a\r\nb\r\nc\r\n",
        "a\rb\r\n\r\n",
        "\n\n\na\n\n",
        "é\n日本語\n😀\nx",
    ],
    ids=[
        "empty",
        "no-trailing-newline",
        "trailing-newline",
        "multi-line",
        "crlf",
        "bare-cr-and-crlf",
        "consecutive-newlines",
        "multibyte",
    ],
)
def test_line_starts_matches_reference_implementation(text):
    assert line_starts(text) == _reference_line_starts(text)


def test_line_starts_matches_reference_on_large_generated_text():
    text = "".join(f"line {i} = {{ x = {i} }}\n" if i % 7 else "\n" for i in range(2000))
    assert line_starts(text) == _reference_line_starts(text)


# ---------------------------------------------------------------------------
# pos_to_line — boundaries (the bug magnet)
# ---------------------------------------------------------------------------


def test_pos_to_line_at_zero_is_line_one():
    starts = line_starts("a\nb\nc")
    assert pos_to_line(0, starts) == 1


def test_line_and_column_at_offsets():
    starts = line_starts("ab\ncd\nef")
    assert line_and_column(0, starts) == (1, 1)
    assert line_and_column(2, starts) == (1, 3)
    assert line_and_column(3, starts) == (2, 1)
    assert line_and_column(4, starts) == (2, 2)
    assert line_and_column(7, starts) == (3, 2)


def test_line_and_column_with_eof_position():
    starts = line_starts("a\nb")
    assert line_and_column(3, starts) == (2, 2)


def test_pos_to_line_inside_first_line():
    starts = line_starts("ab\ncd")
    assert pos_to_line(1, starts) == 1


def test_pos_to_line_at_newline_char_stays_on_its_line():
    text = "a\nb"
    starts = line_starts(text)
    assert text[1] == "\n"
    assert pos_to_line(1, starts) == 1
    assert pos_to_line(2, starts) == 2


def test_pos_to_line_at_exact_line_start():
    starts = line_starts("ab\ncd\nef")
    assert starts == [0, 3, 6]
    assert pos_to_line(0, starts) == 1
    assert pos_to_line(3, starts) == 2
    assert pos_to_line(6, starts) == 3


def test_pos_to_line_beyond_end_is_last_line():
    starts = line_starts("a\nb")
    assert pos_to_line(3, starts) == 2  # len("a\nb") == 3, one past end
    assert pos_to_line(99, starts) == 2


def test_pos_to_line_negative_clamps_to_first_line():
    starts = line_starts("a\nb")
    assert pos_to_line(-1, starts) == 1
    assert pos_to_line(-99, starts) == 1


def test_line_and_column_negative_clamps_to_one_one():
    starts = line_starts("a\nb")
    assert line_and_column(-1, starts) == (1, 1)


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


# ---------------------------------------------------------------------------
# Integration — the same helpers are now used by the three former sites.
# These prove the real wiring, not just the unit, and catch the off-by-one
# that the old weak ``> 0`` assertions missed.
# ---------------------------------------------------------------------------


def test_to_json_with_lines_exact_line():
    src = "a = 1\nb = 2\nc = { d = 3 }"
    root = parse_string(src)
    j = to_json_with_lines(root, src)
    children = j["value"]["children"]
    assert children[0]["line"] == 1  # a
    assert children[1]["line"] == 2  # b
    assert children[2]["line"] == 3  # c


def test_extract_focus_records_exact_lines():
    src = """
focus_tree = {
    id = T
    focus = {
        id = FIRST
        x = 0
        y = 0
    }
    focus = {
        id = SECOND
        x = 1
        y = 0
    }
}
""".lstrip()
    root = parse_string(src)
    records = extract_focus_records(root, source=src)
    by_id = {r["id"]: r for r in records}
    # line is the `focus = {` line, not the `id =` line
    assert by_id["FIRST"]["line"] == 3
    assert by_id["SECOND"]["line"] == 8


def test_parser_tools_top_level_only_exact_lines(tmp_path):
    from md_mcp.tools.parser_tools import parse_file_tool

    f = tmp_path / "x.txt"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    out = parse_file_tool(str(f), tmp_path, top_level_only=True)
    assert out["ok"] is True
    lines = {e["name"]: e["line"] for e in out["top_level"]}
    assert lines["a"] == 1
    assert lines["b"] == 2
    assert lines["c"] == 3


def test_line_numbers_agree_with_lexer_for_offsets():
    """Parity check: util helpers must match the lexer's own line math on same src."""
    from md_mcp.paradox.lexer import Tokenizer

    src = "focus = {\n    id = X\n    x = 0\n}\n"
    starts = line_starts(src)
    # Tokenize and compare a few known offsets
    tok = Tokenizer(src)
    # First token 'focus' at 0 -> line 1
    assert pos_to_line(tok.peek().start, starts) == 1
    tok.next()  # focus
    tok.next()  # =
    tok.next()  # {
    # 'id' token should be line 2 (after first \n)
    id_tok = tok.next()
    assert id_tok.value == "id"
    assert pos_to_line(id_tok.start, starts) == 2


def test_tokenizer_line_table_is_lazy_and_error_position_is_correct():
    """The line table is built on first error, not in __init__."""
    from md_mcp.paradox.lexer import LexError, Tokenizer

    src = "a = 1\r\nb = 2\r\nc = $"
    tok = Tokenizer(src)
    assert tok._line_starts is None  # not computed eagerly
    tok.next()  # a
    tok.next()  # =
    assert tok._line_starts is None  # still not built by normal tokenizing
    with pytest.raises(LexError) as excinfo:
        while True:
            tok.next()
    assert (excinfo.value.line, excinfo.value.column) == (
        3,
        4,
    )  # failed match begins right after `c =`
    assert tok._line_starts == _reference_line_starts(src)  # built on first error


def test_tokenizer_error_position_with_multibyte_text_before_error():
    from md_mcp.paradox.lexer import LexError, Tokenizer

    src = "# é 日本\n\n  x = $"
    tok = Tokenizer(src)
    with pytest.raises(LexError) as excinfo:
        while True:
            tok.next()
    # Error offset is where the failed match began (right after `x =`).
    err_pos = src.index("$") - 1
    assert (excinfo.value.line, excinfo.value.column) == line_and_column(
        err_pos, _reference_line_starts(src)
    )
    assert (excinfo.value.line, excinfo.value.column) == (3, 6)
