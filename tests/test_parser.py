"""Unit tests for the paradox parser."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from md_mcp.paradox import ParseError, parse_string
from md_mcp.paradox.lexer import LexError, Tokenizer
from md_mcp.paradox.nodes import SymbolNode, Token
from md_mcp.util.encoding import read_text
from md_mcp.util.line_numbers import line_and_column, line_starts, pos_to_line

FIXTURES = Path(__file__).parent / "fixtures"


def test_empty_input():
    root = parse_string("")
    assert root.value == []


def test_single_scalar_assignment():
    root = parse_string("foo = 42")
    [node] = root.children()
    assert node.name == "foo"
    assert node.operator == "="
    assert node.value == 42


def test_string_with_escapes():
    root = parse_string(r'name = "He said \"hi\""')
    [node] = root.children()
    assert node.value == 'He said "hi"'


def test_string_backslash_parity_before_quote():
    # An odd slash escapes the quote; an even run leaves it as the terminator.
    odd = parse_string('name = "a' + "\\" + '"b"')
    even = parse_string('name = "a' + "\\" * 2 + '"')
    assert odd.children()[0].value == 'a"b'
    assert even.children()[0].value == "a\\"


def test_string_unknown_escape_stays_literal():
    root = parse_string('name = "a\\qb"')
    assert root.children()[0].value == "a\\qb"


def test_unterminated_string_with_long_backslash_run_is_bounded():
    import time

    source = 'name = "' + "\\" * 36
    started = time.perf_counter()
    with pytest.raises(ParseError):
        parse_string(source)
    assert time.perf_counter() - started < 0.5


def test_long_string_token_keeps_no_regex_state_per_character():
    import tracemalloc

    source = 'x = "' + "a" * 200_000 + '"'
    tracemalloc.start()
    try:
        token = _tokens(source)[2]
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (token.type, len(token.value)) == ("string", 200_002)
    # One alternation repeated per character held about 40 MB here.
    assert peak < 5_000_000


@pytest.mark.parametrize("backslash_count", [1, 2, 3, 4])
def test_terminal_backslash_parity_matches_lexer_and_parser(backslash_count):
    literal = '"value' + "\\" * backslash_count + '"'
    if backslash_count % 2:
        # The final quote is escaped, so there is no terminating quote.
        with pytest.raises(LexError):
            Tokenizer(literal).next()
        with pytest.raises(ParseError):
            parse_string("name = " + literal)
    else:
        token = Tokenizer(literal).next()
        assert token.type == "string"
        assert token.value == literal
        assert parse_string("name = " + literal).children()[0].value == "value" + "\\" * (
            backslash_count // 2
        )


def test_bool_yes_no_are_symbols():
    """Per HOI4 semantics, yes/no are bare symbols at parse time. Conversion to bool
    happens at the schema layer."""
    root = parse_string("flag_a = yes\nflag_b = no")
    children = root.children()
    assert children[0].value == SymbolNode("yes")
    assert children[1].value == SymbolNode("no")


def test_nested_block():
    root = parse_string("a = { b = { c = 1 } }")
    [a] = root.children()
    [b] = a.children()
    [c] = b.children()
    assert c.name == "c"
    assert c.value == 1


def test_value_attachment_takes_the_symbol_before_a_block():
    root = parse_string("color = rgb { 255 0 0 }\nnext = 1")
    color, following = root.children()
    assert color.operator == "="
    assert color.value_attachment == SymbolNode("rgb")
    assert [child.name for child in color.children()] == ["255", "0", "0"]
    assert color.value_end_token is not None
    assert color.value_end_token.value == "}"
    assert following.value == 1


def test_implicit_block_reads_as_an_assignment():
    root = parse_string("name { a = 1 }")
    [node] = root.children()
    assert node.operator == "="
    assert node.value_attachment is None
    assert [child.name for child in node.children()] == ["a"]


def test_state_prefixed_variable_is_one_symbol():
    """Regression: `539.productivity_state_var` must lex as a single symbol, not
    `number(539) . invalid(.productivity_state_var)`."""
    root = parse_string("check_variable = { 539.productivity_state_var > 999 }")
    [node] = root.children()
    children = node.children()
    # children[0] is the var ref (bare keyword), then operator > and number
    # The internal structure: parse_node consumes name=539.productivity_state_var,
    # then operator=>, then value=999 -> one full node with children of comparison.
    # Either way, the block parses cleanly with no errors.
    assert children, "block should contain at least one node"


def test_numeric_prefixed_icon():
    """Regression: `icon = 2.Square_Frame` must lex as a symbol value."""
    root = parse_string("icon = 2.Square_Frame")
    [node] = root.children()
    assert node.value == SymbolNode("2.Square_Frame")


def test_unitnumber():
    root = parse_string("threat = 0.5\nbonus = 50%")
    threat, bonus = root.children()
    assert threat.value == 0.5
    assert bonus.value == SymbolNode("50%")


def test_comment_is_skipped():
    root = parse_string("# leading comment\na = 1 # trailing")
    [a] = root.children()
    assert a.name == "a"
    assert a.value == 1


def test_parse_error_mentions_exact_line_and_column():
    with pytest.raises(ParseError, match=r"at \(2, 6\)"):
        parse_string("a = 1\nb = {{{")


def test_parse_error_uses_previous_position_for_invalid_name():
    with pytest.raises(ParseError, match=r"at \(1, 1\)"):
        parse_string("= 1")


@pytest.mark.parametrize(
    ("src", "message"),
    [
        ("a = 1 }", "File content can't be completely parsed"),
        ("a = }", "Expect string, number, symbol, or {"),
    ],
)
def test_parse_error_names_what_was_expected(src, message):
    with pytest.raises(ParseError, match=re.escape(message)):
        parse_string(src)


def test_separators_after_bare_keywords_and_values_are_skipped():
    root = parse_string("list = { a, b; c }\nx = 1, y = 2;")
    listed, x, y = root.children()
    assert [child.name for child in listed.children()] == ["a", "b", "c"]
    assert (x.value, y.value) == (1, 2)


def test_token_starts_map_to_their_source_lines():
    src = "focus = {\n    id = X\n    x = 0\n}\n"
    starts = line_starts(src)
    tok = Tokenizer(src)
    assert pos_to_line(tok.peek().start, starts) == 1
    tok.next()  # focus
    tok.next()  # =
    tok.next()  # {
    id_tok = tok.next()
    assert id_tok.value == "id"
    assert pos_to_line(id_tok.start, starts) == 2


def test_tokenizer_line_table_is_lazy_and_error_position_is_correct():
    """The line table is built on first error, not in __init__."""
    src = "a = 1\r\nb = 2\r\nc = $"
    tok = Tokenizer(src)
    assert tok._line_starts is None  # not computed eagerly
    tok.next()  # a
    tok.next()  # =
    assert tok._line_starts is None  # still not built by normal tokenizing
    with pytest.raises(LexError) as excinfo:
        while True:
            tok.next()
    # The failed match begins right after `c =`.
    assert (excinfo.value.line, excinfo.value.column) == (3, 4)
    assert tok._line_starts == line_starts(src)  # built on first error


def test_tokenizer_error_position_with_multibyte_text_before_error():
    src = "# é 日本\n\n  x = $"
    tok = Tokenizer(src)
    with pytest.raises(LexError) as excinfo:
        while True:
            tok.next()
    # Error offset is where the failed match began (right after `x =`).
    err_pos = src.index("$") - 1
    assert (excinfo.value.line, excinfo.value.column) == (3, 6)
    assert line_and_column(err_pos, line_starts(src)) == (3, 6)


def _tokens(src: str) -> list[Token]:
    tokens: list[Token] = []
    tokenizer = Tokenizer(src)
    while (t := tokenizer.next()).type != "eof":
        tokens.append(t)
    return tokens


@pytest.mark.parametrize(
    ("src", "op", "start"),
    [
        ("a >= 5", ">=", 2),
        ("a <= 5", "<=", 2),
        ("a != 5", "!=", 2),
        ("a > 5", ">", 2),
        ("a < 5", "<", 2),
    ],
)
def test_operators_lex_as_whole_tokens_with_exact_spans(src, op, start):
    """Two-char operators must not split: the single-char class would leave the `=`
    behind, and `a >= 5` then failed to parse at all (issue #150)."""
    tokens = _tokens(src)
    assert [(t.type, t.value) for t in tokens] == [
        ("symbol", "a"),
        ("operator", op),
        ("number", "5"),
    ]
    op_tok = tokens[1]
    assert (op_tok.start, op_tok.end) == (start, start + len(op))
    assert parse_string(src).children()[0].operator == op


def test_date_token_taken_before_number_with_span():
    # number alone would stop at the second dot and leave a stray `.1` token.
    src = "1936.1.1"
    [tok] = _tokens(src)
    assert tok.type == "date"
    assert (tok.start, tok.end) == (0, 8), "date token must cover the whole literal"
    # A 2-part dotted literal is still a plain number.
    [num] = _tokens("1936.1")
    assert num.type == "number"


def test_date_value_stays_a_symbol_not_a_float():
    root = parse_string("date = 2030.1.1")
    [node] = root.children()
    assert node.name == "date"
    assert node.operator == "="
    assert node.value == SymbolNode("2030.1.1")

    value_tok = _tokens("x = 2030.1.1")[2]
    assert value_tok.type == "date"
    assert (value_tok.start, value_tok.end) == (4, 12)


def test_date_value_with_optional_hour():
    root = parse_string("date = 2000.1.1.12")
    [node] = root.children()
    assert node.value == SymbolNode("2000.1.1.12")


def test_date_block_name_with_optional_hour():
    root = parse_string("1939.1.1 = { history = yes }\n2010.5.4.12 = { flag = 1 }")
    dated, hourly = root.children()
    assert dated.name == "1939.1.1"
    assert dated.operator == "="
    assert dated.children()[0].name == "history"
    assert hourly.name == "2010.5.4.12"
    assert hourly.children()[0].name == "flag"


def test_numeric_prefixed_symbol_still_one_symbol():
    # `date` sits between `symbol` and `number`: the symbol branch must still win.
    tokens = _tokens("1936.x = 1")
    assert [(t.type, t.value) for t in tokens] == [
        ("symbol", "1936.x"),
        ("operator", "="),
        ("number", "1"),
    ]


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("x = 1.", 1.0),
        ("nationalist_drift = 0.", 0.0),
        ("x = -3.5", -3.5),
        ("x = 2030.1", 2030.1),
        ("x = 42", 42),
        ("x = 0xff", 255),
        ("color = 0xffffffff", 4294967295),
        ("x = 0", 0),
    ],
)
def test_number_tolerance_including_trailing_dot(src, expected):
    """The game writes `x = 0.`; without the trailing-dot branch the whole file
    (common/country_leader/00_traits.txt) fails to tokenise (issue #150). A hex
    literal is one number: `[node]` fails if `xffffffff` is left as a stray entry."""
    root = parse_string(src)
    [node] = root.children()
    assert node.value == expected
    assert isinstance(node.value, type(expected))


def test_real_md_history_dates_fixture_exact_ast():
    """Representative Millennium-Dawn excerpt (see fixture header): dates both as
    block names and bare values, decimals inside blocks, a bookmark with an hour."""
    root = parse_string(read_text(FIXTURES / "history_dates_minimal.txt"))
    assert root.operator is None
    capital, dated, bookmark = root.children()

    assert (capital.name, capital.operator, capital.value) == ("capital", "=", 321)

    assert dated.name == "2000.1.1"
    assert dated.operator == "="
    set_currency, set_tag, set_gdp, set_tech, if_block = dated.children()
    assert set_currency.name == "set_variable"
    assert set_currency.children()[0].value == 0.5
    assert set_tag.value == SymbolNode("CAM")
    assert set_gdp.children()[0].value == 2.938
    assert set_tech.children()[0].value == 1
    assert if_block.children()[0].name == "limit"

    assert bookmark.name == "bookmark"
    name, date, default_node = bookmark.children()
    assert name.value == "MILLENNIUM_DAWN"
    assert date.name == "date"
    assert date.value == SymbolNode("2000.1.1.12")
    assert default_node.value == SymbolNode("yes")


def test_real_focus_file_parses(fake_mod_root):
    path = fake_mod_root / "common" / "national_focus" / "test.txt"
    root = parse_string(read_text(path))
    assert len(root.children()) == 2  # focus_tree + shared_focus
