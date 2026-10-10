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
* The cache holds at most `MD_MCP_AST_CACHE_SIZE` files (default 32) and at most
  `MD_MCP_AST_CACHE_BYTES` of source text (default 8 MB, measured in characters).
  An AST is roughly 23x its source size (`05_usa.txt`: 1.5 MB of text, ~29 MB of
  nodes), so the byte bound is what keeps a scope walk over hundreds of files from
  pinning a gigabyte of trees: 8 MB of source pinned ~184 MB on the real mod. A
  file larger than the byte bound is parsed but not cached, and an edited file
  replaces its own stale entry rather than sitting beside it.
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
DEFAULT_CACHE_BYTES = 8_000_000
_ENV_SIZE = "MD_MCP_AST_CACHE_SIZE"
_ENV_BYTES = "MD_MCP_AST_CACHE_BYTES"

_Key = tuple[str, int, int]

_lock = threading.Lock()
_cache: "OrderedDict[_Key, tuple[str, Node]]" = OrderedDict()
# path -> the key currently cached for it, so an edit replaces the old tree
# instead of leaving a dead (path, old mtime, old size) entry to hold memory.
_by_path: dict[str, _Key] = {}
_total_chars = 0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def max_entries() -> int:
    """Configured LRU capacity in files; invalid or negative env values fall back to the default."""
    return _env_int(_ENV_SIZE, DEFAULT_CACHE_SIZE)


def max_bytes() -> int:
    """Configured bound on cached source text (characters); invalid values fall back."""
    return _env_int(_ENV_BYTES, DEFAULT_CACHE_BYTES)


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

    global _total_chars
    limit = max_entries()
    byte_limit = max_bytes()
    if limit > 0 and len(text) <= byte_limit:
        with _lock:
            stale_key = _by_path.get(key[0])
            if stale_key is not None:
                old = _cache.pop(stale_key, None)
                if old is not None:
                    _total_chars -= len(old[0])
            _cache[key] = (text, root)
            _by_path[key[0]] = key
            _total_chars += len(text)
            while _cache and (len(_cache) > limit or _total_chars > byte_limit):
                evicted_key, (evicted_text, _) = _cache.popitem(last=False)
                _total_chars -= len(evicted_text)
                if _by_path.get(evicted_key[0]) == evicted_key:
                    del _by_path[evicted_key[0]]
    return text, root


def clear() -> None:
    """Drop every cached entry (tests, or a caller that knows files changed)."""
    global _total_chars
    with _lock:
        _cache.clear()
        _by_path.clear()
        _total_chars = 0


def total_chars() -> int:
    """Characters of source text currently held."""
    with _lock:
        return _total_chars


def size() -> int:
    """Number of cached files."""
    with _lock:
        return len(_cache)
