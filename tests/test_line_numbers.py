"""Direct contract tests for util/line_numbers — the canonical offset→line translation.

These guard the deduplication in issue #5. Every other site (parser_tools,
paradox/schema, analysis/ref_audit) now delegates here, so an off-by-one here
breaks every line number the agent sees. Prior coverage was indirect and weak
(``line is not None and > 0``). These tests pin exact values and boundaries.
"""

from __future__ import annotations

from md_mcp.paradox import parse_string
from md_mcp.paradox.schema import extract_focus_records, to_json_with_lines
from md_mcp.util.line_numbers import line_starts, pos_to_line

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


def test_line_starts_only_newlines():
    assert line_starts("a\nb\n") == [0, 2, 4]


# ---------------------------------------------------------------------------
# pos_to_line — boundaries (the bug magnet)
# ---------------------------------------------------------------------------


def test_pos_to_line_at_zero_is_line_one():
    starts = line_starts("a\nb\nc")
    assert pos_to_line(0, starts) == 1


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
        if off < len(text) or text.endswith("\n"):
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
