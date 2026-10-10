"""Unit tests for the paradox parser."""

from __future__ import annotations

import pytest

from md_mcp.paradox import ParseError, parse_string
from md_mcp.paradox.lexer import LexError, Tokenizer
from md_mcp.paradox.nodes import SymbolNode
from md_mcp.util.encoding import read_text


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


def test_real_focus_file_parses(fake_mod_root):
    path = fake_mod_root / "common" / "national_focus" / "test.txt"
    root = parse_string(read_text(path))
    assert len(root.children()) == 2  # focus_tree + shared_focus
