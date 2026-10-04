"""Process-wide LRU of parsed paradox files, keyed by on-disk identity.

`resolve_focus`, `focus_graph`, `check_refs`, the scope walkers and the `md://`
resources all need the parsed AST of the same big files (`05_usa.txt` is 1.5 MB and
takes ~850 ms to parse). Without a shared cache a single agent turn pays that cost
three or four times. `parse_cached` makes every repeat read a dict lookup.

Invalidation is stat-based, like the persistent indexes: the key is
`(str(path), st_mtime_ns, st_size)`, so an edited file misses automatically and its
stale entry ages out of the LRU.

Rules for callers:

* The returned `Node` tree and text are **shared**. Treat them as read-only.
* `error_prefix` only decorates `ParseError` messages. It is not part of the key;
  the root is stored under whatever prefix the first caller used. Failed parses are
  never cached, so every failure re-raises with the caller's own prefix.
* The cache holds at most `MD_MCP_AST_CACHE_SIZE` files (default 32). Each entry
  keeps the decoded text plus the AST, so very large trees cost tens of MB each;
  lower the size on memory-constrained hosts.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path

from ..util.encoding import read_text
from .nodes import Node
from .parser import parse_string

DEFAULT_CACHE_SIZE = 32
_ENV_SIZE = "MD_MCP_AST_CACHE_SIZE"

_Key = tuple[str, int, int]

_lock = threading.Lock()
_cache: "OrderedDict[_Key, tuple[str, Node]]" = OrderedDict()


def max_entries() -> int:
    """Configured LRU capacity; invalid or negative env values fall back to the default."""
    raw = os.environ.get(_ENV_SIZE)
    if raw is None or not raw.strip():
        return DEFAULT_CACHE_SIZE
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_CACHE_SIZE


def parse_cached(abs_path: Path, *, error_prefix: str = "") -> tuple[str, Node]:
    """Return `(text, root)` for `abs_path`, parsing only when the file changed.

    A stat failure raises the `OSError` as an uncached plain read would. Parse errors
    propagate and are not cached.
    """
    path = Path(abs_path)
    try:
        st = os.stat(path)
    except OSError:
        # Plain read: raises the same OSError the caller would have seen before.
        text = read_text(path)
        return text, parse_string(text, error_prefix=error_prefix)

    key: _Key = (str(path), st.st_mtime_ns, st.st_size)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit

    # Parse outside the lock: it takes ~1 s on the biggest files and must not block
    # readers of other entries. Two racing misses may both parse; the last one wins.
    text = read_text(path)
    root = parse_string(text, error_prefix=error_prefix)

    limit = max_entries()
    if limit > 0:
        with _lock:
            _cache[key] = (text, root)
            _cache.move_to_end(key)
            while len(_cache) > limit:
                _cache.popitem(last=False)
    return text, root


def clear() -> None:
    """Drop every cached entry (tests, or a caller that knows files changed)."""
    with _lock:
        _cache.clear()


def size() -> int:
    """Number of cached files."""
    with _lock:
        return len(_cache)
