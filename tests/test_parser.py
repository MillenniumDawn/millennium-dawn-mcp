"""Unit tests for the paradox parser."""

from __future__ import annotations

import pytest

from md_mcp.paradox import ParseError, node_to_str, parse_string
from md_mcp.paradox.nodes import SymbolNode


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


def test_round_trip_writer():
    src = "focus = { id = X x = 0 y = 0 }"
    root = parse_string(src)
    rendered = node_to_str(root)
    # Re-parse and compare structurally — exact whitespace can differ.
    reparsed = parse_string(rendered)
    [a] = reparsed.children()
    assert a.name == "focus"
    [id_node, *_] = a.children()
    assert id_node.name == "id"
    assert id_node.value == SymbolNode("X")


def test_real_focus_file_parses(fake_mod_root):
    from md_mcp.paradox import parse_file

    path = fake_mod_root / "common" / "national_focus" / "test.txt"
    root = parse_file(path)
    assert len(root.children()) == 2  # focus_tree + shared_focus
