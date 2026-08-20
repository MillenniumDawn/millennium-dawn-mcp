"""BOM-aware file IO. Per `general-rules.md`:

  * `.txt` files (focus trees, events, ideas, decisions): UTF-8 **without** BOM
  * `.yml` localisation files:                            UTF-8 **with** BOM

Reading is permissive (strips BOM if present). Generators write according to type.
"""

from __future__ import annotations

from pathlib import Path

UTF8_BOM = b"\xef\xbb\xbf"


def read_text(path: str | Path) -> str:
    """Read a file as UTF-8, transparently stripping the BOM if present."""
    raw = Path(path).read_bytes()
    if raw.startswith(UTF8_BOM):
        raw = raw[len(UTF8_BOM) :]
    return raw.decode("utf-8", errors="replace")


def has_bom(path: str | Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(3) == UTF8_BOM
    except OSError:
        return False
