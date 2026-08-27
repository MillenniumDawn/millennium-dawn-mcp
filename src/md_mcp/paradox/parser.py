"""HOI4 paradox-script parser.

Ported from `MD-VSCode-Utility-Tool/src/hoiformat/hoiparser.ts`. The structure mirrors
the TS source one-to-one: a recursive-descent over `parseBlockContent` / `parseNode` /
`parseNodeValue`, using a single-token-lookahead tokenizer.

Public entry points:
    * `parse_string(text)` — parse a snippet to a root Node whose `value` is the list of children
    * `parse_file(path)`   — read and parse a UTF-8 (with optional BOM) `.txt` file
"""

from __future__ import annotations

import re
from pathlib import Path

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


def parse_file(path: str | Path) -> Node:
    """Read a `.txt` file (UTF-8 with optional BOM) and parse it.

    Per `general-rules.md`, `.txt` files are saved without BOM, but some legacy or
    third-party files may include one; we accept either.
    """
    p = Path(path)
    raw = p.read_bytes()
    # Strip UTF-8 BOM if present.
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")
    return parse_string(text, error_prefix=f"In file {p}:\n")


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

    # operator phase
    if next_token.value == "{":
        # Implicit `= { ... }` — TS code synthesises an `=` token here.
        operator_token = Token(
            value="=", start=next_token.start, end=next_token.end, type="operator"
        )
    else:
        operator_token = tokens.next()

    value, value_start, value_end = _parse_node_value(tokens)

    # Handle `value @attachment` — a symbol followed by another block becomes attachment + block.
    value_attachment: SymbolNode | None = None
    value_attachment_token: Token | None = None
    if isinstance(value, SymbolNode):
        peek = tokens.peek()
        if peek.value == "{":
            value_attachment = value
            value_attachment_token = value_start
            value, value_start, value_end = _parse_node_value(tokens)

    # Skip trailing separators.
    tail = tokens.peek()
    while _TAIL_SEP_RE.match(tail.value):
        tokens.next()
        tail = tokens.peek()

    return Node(
        name=name.value,
        name_token=name,
        operator=operator_token.value,
        operator_token=operator_token,
        value=value,
        value_start_token=value_start,
        value_end_token=value_end,
        value_attachment=value_attachment,
        value_attachment_token=value_attachment_token,
    )


def _parse_node_value(
    tokens: Tokenizer,
) -> tuple[str | int | float | SymbolNode | list | None, Token, Token]:
    next_token = tokens.next()
    t = next_token.type
    if t == "string":
        return _unescape_string(next_token.value), next_token, next_token

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
        return num, next_token, next_token

    if t in ("symbol", "unitnumber"):
        return SymbolNode(name=next_token.value), next_token, next_token

    if t == "operator" and next_token.value == "{":
        children = _parse_block_content(tokens)
        right = tokens.next()
        if right.value != "}":
            tokens.throw("Expect a '}'", prev=True)
        return children, next_token, right

    tokens.throw("Expect string, number, symbol, or {", prev=True)
    raise AssertionError("unreachable")  # pragma: no cover


def _parse_block_content(tokens: Tokenizer) -> list[Node]:
    nodes: list[Node] = []
    while True:
        peek = tokens.peek()
        if peek.type == "eof" or peek.value == "}":
            return nodes
        nodes.append(_parse_node(tokens))
