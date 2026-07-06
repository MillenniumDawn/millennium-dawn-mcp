"""Tokenizer for HOI4 paradox script.

Ported from the `tokenizer()` function in `hoiparser.ts`. The TS implementation uses a
sticky-flag regex (`y`); the Python equivalent is `re.compile(...).match(string, pos=...)`,
which anchors at `pos` and returns None if the pattern doesn't match there.
"""

from __future__ import annotations

import re
from typing import List, NoReturn, Optional

from .nodes import Token

# Order matches the priority sort in `hoiparser.ts`: lower priority value first.
#   comment(0) < operator(10), string(10) < symbol(40) < unitnumber(49) < number(50) < eof(1000)
#
# `symbol` must come BEFORE `number` so that `539.productivity_state_var` and
# `2.Square_Frame` parse as single symbols (the `(?:\d+\.)?` prefix lets a symbol
# start with `<int>.`), not as `539` followed by an invalid `.productivity_state_var`
# token.
_TOKEN_TYPES: List[tuple[str, str]] = [
    ("comment", r"#.*(?:[\r\n]|$)"),
    ("operator", r"[={}<>;,]|>=|<=|!="),
    ("string", r'"(?:\\"|\\\\|[^"])*"'),
    ("symbol", r"(?:\d+\.)?[a-zA-Z_@\[\]][\w:\._@\[\]\-\?\^\/ -ɏ|]*"),
    ("unitnumber", r"(?:-?\d*\.\d+|-?\d+)(?:%%?)"),
    ("number", r"-?\d*\.\d+|-?\d+|0x\d+"),
    ("eof", r"$"),
]

_TOKEN_REGEX = re.compile(
    r"\s*(?:" + "|".join(f"(?P<{name}>{pat})" for name, pat in _TOKEN_TYPES) + ")"
)
_TOKEN_NAMES = [name for name, _ in _TOKEN_TYPES]


class LexError(Exception):
    def __init__(self, message: str, line: int, column: int, snippet: str):
        super().__init__(f"{message} at ({line}, {column}): {snippet}")
        self.line = line
        self.column = column
        self.snippet = snippet


class Tokenizer:
    """Stateful token stream with one-token lookahead.

    Comments are silently consumed.
    """

    __slots__ = ("_error_prefix", "_input", "_line_ends", "_pending", "_pos", "_prev_pos")

    def __init__(self, input_text: str, error_prefix: str = ""):
        self._input = input_text
        self._pos = 0
        self._prev_pos = 0
        self._pending: Optional[Token] = None
        self._error_prefix = error_prefix

        # Precompute cumulative line-end offsets for error reporting.
        self._line_ends: List[int] = []
        running = 0
        for line in input_text.split("\n"):
            running += len(line) + 1
            self._line_ends.append(running)

    def _advance(self) -> Token:
        """Consume the next non-comment token from the input."""
        while True:
            self._prev_pos = self._pos
            match = _TOKEN_REGEX.match(self._input, self._pos)
            if match is None:
                self._raise("Invalid token")

            self._pos = match.end()
            for name in _TOKEN_NAMES:
                value = match.group(name)
                if value is not None:
                    token = Token(
                        value=value, start=self._pos - len(value), end=self._pos, type=name
                    )
                    break
            else:  # pragma: no cover — regex guarantees one group matches
                self._raise("Invalid token")

            if token.type != "comment":
                return token

    def peek(self) -> Token:
        if self._pending is None:
            self._pending = self._advance()
        return self._pending

    def next(self) -> Token:
        token = self.peek()
        self._pending = None
        return token

    def _raise(self, message: str, prev: bool = False) -> NoReturn:
        pos = self._prev_pos if prev else self._pos
        line_idx = next((i for i, end in enumerate(self._line_ends) if end > pos), -1)
        if line_idx == -1:
            line = len(self._line_ends)
            column = 1
        else:
            line = line_idx + 1
            column = (pos - (self._line_ends[line_idx - 1] if line_idx > 0 else 0)) + 1

        snippet = (self._input + "(EOF)")[pos : min(pos + 30, len(self._input) + 5)]
        raise LexError(self._error_prefix + message, line, column, snippet)

    def throw(self, message: str, prev: bool = False):
        self._raise(message, prev)
