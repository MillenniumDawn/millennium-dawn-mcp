"""Stat-keyed in-process cache of decoded file contents for the reference scanner.

`find_references` greps tens of megabytes of script on every call. Decoding the
files dominates once the substring prefilter has removed the regex cost, so the
decoded text is kept in memory keyed by absolute path and validated by
``(mtime_ns, size)``: an unchanged file is never re-read, a changed one is
re-read on the next call. Directory walks stay per-call so added/removed files
are always seen; only contents are cached.

Total cached size is bounded (default 128 MB of decoded text as held in memory,
override with ``MD_MCP_TEXT_CACHE_BYTES``). Once the bound is reached further
files are read but not cached: evicting during a scan whose working set exceeds
the bound would churn every entry and never hit, so the cache keeps what it has
and only drops an entry when its file changes or disappears. Memory only —
nothing is ever written to disk. Thread-safe.
"""

from __future__ import annotations

import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from ..util.encoding import read_text as decode_text

DEFAULT_MAX_BYTES = 128 * 1024 * 1024
ENV_VAR = "MD_MCP_TEXT_CACHE_BYTES"


def _env_max_bytes() -> int:
    raw = os.environ.get(ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_BYTES


class TextCache:
    """Cache of ``path -> (mtime_ns, size, text)`` bounded by total decoded text size.

    ``total_bytes`` counts ``sys.getsizeof(text)`` (what the strings cost in
    memory), not the on-disk size. Entries are never evicted to make room; see the
    module docstring.
    """

    def __init__(self, max_bytes: Optional[int] = None) -> None:
        self.max_bytes = _env_max_bytes() if max_bytes is None else max_bytes
        self._entries: OrderedDict[str, tuple[int, int, str]] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._total = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def total_bytes(self) -> int:
        return self._total

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, path: object) -> bool:
        return str(path) in self._entries

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._sizes.clear()
            self._total = 0
            self.hits = 0
            self.misses = 0

    def read(self, path: Path) -> Optional[str]:
        """Return decoded text (UTF-8, errors replaced, BOM stripped) or None on OSError."""
        key = str(path)
        try:
            st = os.stat(key)
        except OSError:
            self._drop(key)
            return None
        stamp = (st.st_mtime_ns, st.st_size)

        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (entry[0], entry[1]) == stamp:
                self._entries.move_to_end(key)
                self.hits += 1
                return entry[2]

        try:
            text = decode_text(key)
        except OSError:
            self._drop(key)
            return None

        cost = sys.getsizeof(text)
        with self._lock:
            self.misses += 1
            self._evict_key(key)
            # Cache only while it fits; never push other entries out for it.
            if self._total + cost <= self.max_bytes:
                self._entries[key] = (stamp[0], stamp[1], text)
                self._sizes[key] = cost
                self._total += cost
        return text

    def _evict_key(self, key: str) -> None:
        if self._entries.pop(key, None) is not None:
            self._total -= self._sizes.pop(key, 0)

    def _drop(self, key: str) -> None:
        with self._lock:
            self._evict_key(key)


_CACHE = TextCache()


def read_text(path: Path) -> Optional[str]:
    """Read through the process-wide cache."""
    return _CACHE.read(path)


def clear() -> None:
    """Empty the process-wide cache (tests)."""
    _CACHE.clear()


def get_cache() -> TextCache:
    return _CACHE
