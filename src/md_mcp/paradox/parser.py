"""HOI4 paradox-script parser.

Ported from `MD-VSCode-Utility-Tool/src/hoiformat/hoiparser.ts`. The structure mirrors
the TS source one-to-one: a recursive-descent over `parseBlockContent` / `parseNode` /
`parseNodeValue`, using a single-token-lookahead tokenizer.

Public entry point:
    * `parse_string(text)` — parse a snippet to a root Node whose `value` is the list of children
"""

from __future__ import annotations

import re

from .lexer import LexError, Tokenizer
from .nodes import Node, SymbolNode, Token


class ParseError(Exception):
    """Raised when input is not valid paradox script. Wraps LexError for the public API."""


_TAIL_SEP_RE = re.compile(r"^[,;]$")
_NAME_OR_BRACE_BREAK_RE = re.compile(r"^[,;}]$")
_STRING_ESC_DQUOTE = re.compile(r'\\"')
_STRING_ESC_BSLASH = re.compile(r"\\\\")


def parse_string(text: str, error_prefix: str = "") -> Node:
    """Parse a snippet of paradox script. Returns the file-root node.

    The root node has name=None, operator=None, and value=list[Node] holding top-level entries.
    """
    try:
        tokens = Tokenizer(text, error_prefix)
        value = _parse_block_content(tokens)
        if tokens.peek().type != "eof":
            tokens.throw("File content can't be completely parsed")
        return Node(name=None, operator=None, value=value)
    except LexError as e:
        raise ParseError(str(e)) from e


def _unescape_string(quoted: str) -> str:
    """Strip surrounding quotes and resolve `\\"` and `\\\\` escapes."""
    inner = quoted[1:-1]
    inner = _STRING_ESC_DQUOTE.sub('"', inner)
    inner = _STRING_ESC_BSLASH.sub("\\\\", inner)
    return inner


def _parse_node(tokens: Tokenizer) -> Node:
    name = tokens.next()
    if name.type not in ("string", "symbol", "number"):
        tokens.throw("Expect name to be symbol, string or number", prev=True)

    next_token = tokens.peek()
    if next_token.type != "operator" or _NAME_OR_BRACE_BREAK_RE.match(next_token.value):
        # Bare keyword (e.g. inside an enum-style block): consume any trailing , or ;
        while _TAIL_SEP_RE.match(next_token.value):
            tokens.next()
            next_token = tokens.peek()

        return Node(
            name=name.value,
            name_token=name,
        )

    # An implicit `name { ... }` block reads as `name = { ... }`.
    operator = "=" if next_token.value == "{" else tokens.next().value

    value, value_end = _parse_node_value(tokens)

    # Handle `value @attachment` — a symbol followed by another block becomes attachment + block.
    value_attachment: SymbolNode | None = None
    if isinstance(value, SymbolNode) and tokens.peek().value == "{":
        value_attachment = value
        value, value_end = _parse_node_value(tokens)

    # Skip trailing separators.
    tail = tokens.peek()
    while _TAIL_SEP_RE.match(tail.value):
        tokens.next()
        tail = tokens.peek()

    return Node(
        name=name.value,
        name_token=name,
        operator=operator,
        value=value,
        value_end_token=value_end,
        value_attachment=value_attachment,
    )


def _parse_node_value(
    tokens: Tokenizer,
) -> tuple[str | int | float | SymbolNode | list | None, Token]:
    """Parse one value. Returns it with its last token (the `}` of a block)."""
    next_token = tokens.next()
    t = next_token.type
    if t == "string":
        return _unescape_string(next_token.value), next_token

    if t == "number":
        # The lexer only emits "number" tokens for strings matching its number
        # regex, so these int()/float() conversions cannot fail.
        v = next_token.value
        # Every branch is guaranteed parseable by the lexer's `number` regex.
        if v.startswith("0x"):
            # pi-lens-ignore: unchecked-throwing-call-python
            num: int | float = int(v[2:], 16)
        elif "." in v:
            # pi-lens-ignore: unchecked-throwing-call-python
            num = float(v)
        else:
            # pi-lens-ignore: unchecked-throwing-call-python
            num = int(v)
        return num, next_token

    if t in ("symbol", "unitnumber"):
        return SymbolNode(name=next_token.value), next_token

    if t == "operator" and next_token.value == "{":
        children = _parse_block_content(tokens)
        right = tokens.next()
        if right.value != "}":
            tokens.throw("Expect a '}'", prev=True)
        return children, right

    tokens.throw("Expect string, number, symbol, or {", prev=True)


def _parse_block_content(tokens: Tokenizer) -> list[Node]:
    nodes: list[Node] = []
    while True:
        peek = tokens.peek()
        if peek.type == "eof" or peek.value == "}":
            return nodes
        nodes.append(_parse_node(tokens))
