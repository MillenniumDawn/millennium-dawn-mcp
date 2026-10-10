"""Localisation YAML stub generator.

Returns a string suitable for writing to `localisation/english/<file>.yml`.
Per `localisation-rules.md`:

  * UTF-8 with BOM (the agent's Write must include `﻿` at the start)
  * `l_english:` header
  * 1-space indent
  * No trailing version suffix on keys

This generator emits just the *content* — the agent decides whether to prepend it
to an existing file or create a new one. When creating a new file, include the
BOM byte sequence at the very start (use `bom_prefix=True`).
"""

from __future__ import annotations

from typing import Sequence

from ..util.response import enforce_budget

_LOC_LINE_BREAKS = str.maketrans(
    {
        "\n": r"\n",
        "\r": r"\n",
        "\v": r"\n",
        "\f": r"\n",
        "\x1c": r"\n",
        "\x1d": r"\n",
        "\x1e": r"\n",
        "\x85": r"\n",
        "\u2028": r"\n",
        "\u2029": r"\n",
    }
)


def generate_loc_stub(
    keys: Sequence[dict],
    *,
    lang: str = "l_english",
    include_header: bool = True,
    bom_prefix: bool = False,
) -> dict:
    """Build a loc YAML payload from `[{key, value}, ...]`.

    Args:
      keys           — list of `{key, value}` dicts (extra fields ignored)
      lang           — language root (default `l_english`)
      include_header — emit `l_english:` first line; turn off to append to an
                       existing file that already has the header
      bom_prefix     — prefix `﻿` (the UTF-8 BOM as a character) for new files

    Returns:
      {txt: str}
    """
    parts: list[str] = []
    if include_header:
        parts.append(f"{lang}:")
    for entry in keys:
        k = entry["key"]
        v = entry["value"]
        parts.append(f' {k}: "{_escape(v)}"')
    body = "\n".join(parts) + "\n"

    if bom_prefix:
        body = "﻿" + body

    return enforce_budget({"txt": body}, heavy_keys=("txt",))


def _escape(value: str) -> str:
    """Escape embedded quotes and backslashes per HOI4 loc YAML rules.

    The format treats `\"` and `\\\\` as escapes — see `localisation-rules.md`.
    Physical separators recognized by ``str.splitlines()`` become the `\\n` loc escape.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r\n", "\n")
    return escaped.translate(_LOC_LINE_BREAKS)
