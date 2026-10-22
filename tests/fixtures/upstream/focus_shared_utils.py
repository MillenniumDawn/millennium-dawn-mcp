"""Small public-API fixture for the two `tools/shared_utils.py` text passes the renderer uses.

Like upstream, `strip_comments` deletes comment text (keeping newlines), so it
shortens the text, while `blank_quoted_strings` keeps its length.
"""

from __future__ import annotations

import re

_COMMENT = re.compile(r"#[^\n]*")
_QUOTED = re.compile(r'"[^"\n]*"')


def strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def blank_quoted_strings(text: str) -> str:
    return _QUOTED.sub(lambda m: '"' + " " * (len(m.group(0)) - 2) + '"', text)
