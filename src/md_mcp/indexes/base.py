"""Index cache infrastructure.

Mirrors the proven design from `MD-VSCode-Utility-Tool/src/util/indexCache.ts`:

  * Each index has a name and a schema version. When the version bumps, the cache for
    that index is invalidated wholesale.
  * Per-index manifest stores `{relative_path: [mtime_ns, size]}` for every contributing
    file. On startup we stat the current files and reparse only the ones whose
    `(mtime, size)` moved.
  * Data is stored as JSON (one file per index) so cross-language inspection and corruption
    diagnosis are trivial.

Size is tracked alongside mtime — guards against the rare same-second rewrite that
mtime alone would miss.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


@dataclass
class FileSig:
    mtime_ns: int
    size: int

    def to_json(self) -> list:
        return [self.mtime_ns, self.size]

    @classmethod
    def from_json(cls, data: list) -> "FileSig":
        return cls(mtime_ns=int(data[0]), size=int(data[1]))


def file_signature(path: Path) -> FileSig | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return FileSig(mtime_ns=st.st_mtime_ns, size=st.st_size)


@dataclass
class Staleness:
    stale: List[str]  # known files whose mtime/size moved
    removed: List[str]  # known files now missing
    added: List[str]  # new files not in the manifest
    unchanged: List[str]  # safe to reuse


def compute_staleness(manifest: Dict[str, FileSig], current: Dict[str, FileSig]) -> Staleness:
    stale: List[str] = []
    removed: List[str] = []
    unchanged: List[str] = []
    added: List[str] = []

    for path, sig in manifest.items():
        cur = current.get(path)
        if cur is None:
            removed.append(path)
        elif cur.mtime_ns != sig.mtime_ns or cur.size != sig.size:
            stale.append(path)
        else:
            unchanged.append(path)

    for path in current.keys() - manifest.keys():
        added.append(path)

    return Staleness(stale=stale, removed=removed, added=added, unchanged=unchanged)


class IndexCache:
    """Versioned on-disk cache backing a single index.

    Layout:
        <cache_dir>/v<version>/<name>.manifest.json   — {file: [mtime_ns, size]}
        <cache_dir>/v<version>/<name>.data.json       — index-specific payload
    """

    def __init__(self, cache_dir: Path, name: str, version: int):
        self.dir = cache_dir / f"v{version}"
        self.manifest_path = self.dir / f"{name}.manifest.json"
        self.data_path = self.dir / f"{name}.data.json"
        self.name = name
        self.version = version

    # ----- manifest ---------------------------------------------------------

    def load_manifest(self) -> Dict[str, FileSig] | None:
        if not self.manifest_path.exists():
            return None
        try:
            raw = json.loads(self.manifest_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return {path: FileSig.from_json(sig) for path, sig in raw.items()}

    def save_manifest(self, sigs: Dict[str, FileSig]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {path: sig.to_json() for path, sig in sigs.items()}
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), "utf-8")
        os.replace(tmp, self.manifest_path)

    # ----- data -------------------------------------------------------------

    def load_data(self) -> dict | None:
        if not self.data_path.exists():
            return None
        try:
            return json.loads(self.data_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save_data(self, payload: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.data_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), "utf-8")
        os.replace(tmp, self.data_path)


def signatures_for(paths: Iterable[Path], roots: Path | list[Path]) -> Dict[str, FileSig]:
    """Build a {relative_path: signature} map. Missing files are skipped.

    `roots` may be a single root or a list. The first matching root is used for
    relativisation — this lets indexes that span mod + vanilla report a single
    relative path key (e.g. `common/national_focus/USA.txt`) regardless of which
    base directory the absolute file lives under.
    """
    root_list = [roots] if isinstance(roots, Path) else roots
    sigs: Dict[str, FileSig] = {}
    for p in paths:
        sig = file_signature(p)
        if sig is None:
            continue
        rel: str | None = None
        if p.is_absolute():
            for root in root_list:
                try:
                    rel = str(p.relative_to(root))
                    break
                except ValueError:
                    continue
        if rel is None:
            rel = str(p)
        sigs[rel] = sig
    return sigs


class GenericTxtIndex:
    """Shared scaffolding for paradox-script indexes that walk a single subdir for `.txt`.

    Subclasses provide:
        * `cache_version: int`
        * `cache_name: str`     — file basename under `<cache_dir>/v<ver>/`
        * `subdir: str`         — path under each root, e.g. `events`
        * `pattern: str`        — glob like `*.txt`
        * `content_prefilter`   — cheap substring test before parsing
        * `parser_fn`           — *module-level* function
                                  `(abs_path: str, relpath: str) -> Optional[List[dict]]`
                                  (must be picklable for ProcessPoolExecutor)
        * `primary_key`         — record field used for the reverse map

    Subclasses no longer override `parse_one`; instead they set `parser_fn = <some_top_level_fn>`
    so the rebuild loop can dispatch through a process pool.

    The class itself handles file collection, manifest, cache, and incremental rebuild.
    """

    cache_version: int = 1
    cache_name: str = ""
    subdir: str = ""
    pattern: str = "*.txt"
    content_prefilter: tuple = ()
    primary_key: str = "id"

    def __init__(
        self,
        mod_root: "Path",
        cache_dir: "Path",
        vanilla_path: "Optional[Path]" = None,
        *,
        include_vanilla: bool = True,
    ):
        from pathlib import Path as _Path

        self.mod_root: _Path = mod_root
        self.vanilla_path: "Optional[_Path]" = vanilla_path if include_vanilla else None
        self._cache = IndexCache(cache_dir, self.cache_name, self.cache_version)
        self._stale_check = StaleCheck()

        self._by_file: Dict[str, List[dict]] = {}
        self._by_key: Dict[str, dict] = {}
        self._loaded = False

    # ---------- public API ----------

    def resolve(self, key: str) -> "Optional[dict]":
        self.ensure_fresh()
        return self._by_key.get(key)

    def list_keys(self) -> List[str]:
        self.ensure_fresh()
        return sorted(self._by_key.keys())

    def list_files(self) -> List[str]:
        self.ensure_fresh()
        return sorted(self._by_file.keys())

    def records_for_file(self, relpath: str) -> List[dict]:
        self.ensure_fresh()
        return self._by_file.get(relpath, [])

    def ensure_fresh(self) -> None:
        if self._loaded and not self._stale_check.should_check():
            return
        self._rebuild()
        self._loaded = True

    # ---------- subclass hooks ----------

    # Set by subclasses to a *module-level* function (picklable). Signature:
    #   parser_fn(abs_path: str, relpath: str) -> Optional[List[dict]]
    # Returning None means "could not parse"; returning [] means "no records found".
    parser_fn: Optional[Callable[[str, str], "Optional[List[dict]]"]] = None

    # ---------- internals ----------

    def _roots(self) -> List["Path"]:
        roots = [self.mod_root]
        if self.vanilla_path is not None:
            roots.append(self.vanilla_path)
        return roots

    def _resolve_root(self, relpath: str) -> "Optional[Path]":
        for base in self._roots():
            if (base / relpath).exists():
                return base
        return None

    def _collect_files(self) -> List["Path"]:
        results: List["Path"] = []
        for base in self._roots():
            d = base / self.subdir
            if not d.is_dir():
                continue
            for p in d.rglob(self.pattern):
                if p.is_file():
                    results.append(p)
        return results

    def _rebuild(self) -> None:
        files = self._collect_files()
        current_sigs = signatures_for(files, self._roots())
        manifest = self._cache.load_manifest() or {}
        staleness = compute_staleness(manifest, current_sigs)

        if self._loaded and not staleness.stale and not staleness.added and not staleness.removed:
            return

        if self._loaded:
            cached_files = self._by_file
        else:
            cached_data = self._cache.load_data() or {}
            cached_files = cached_data.get("files", {})

        new_by_file: Dict[str, List[dict]] = {}
        for relpath in staleness.unchanged:
            if relpath in cached_files:
                new_by_file[relpath] = cached_files[relpath]

        to_parse = staleness.stale + staleness.added

        if to_parse:
            results = self._parse_parallel(to_parse)
            for relpath, recs in zip(to_parse, results, strict=False):
                if recs is not None:
                    new_by_file[relpath] = recs

        new_by_key: Dict[str, dict] = {}
        for relpath, recs in new_by_file.items():
            for rec in recs:
                k = rec.get(self.primary_key)
                if k is None:
                    continue
                new_by_key[k] = {**rec, "file": relpath}

        self._by_file = new_by_file
        self._by_key = new_by_key

        if to_parse or staleness.removed or not manifest:
            self._cache.save_data({"files": new_by_file})
            self._cache.save_manifest(current_sigs)

    def _parse_parallel(self, relpaths: List[str]) -> List["Optional[List[dict]]"]:
        """Resolve each relpath to its absolute path and dispatch the parser fn in a process pool.

        Falls back to serial execution under two conditions: (a) a single file is
        being processed (warm-path edits — pool overhead would dominate), or
        (b) `MD_MCP_SERIAL_PARSE=1` is set (useful for debugging / coverage).
        """
        fn = type(self).parser_fn
        if fn is None:
            raise NotImplementedError(
                f"{type(self).__name__} must set `parser_fn` to a module-level function"
            )

        # Build (abs_path, relpath) jobs once.
        jobs: List[Tuple[str, str]] = []
        for rp in relpaths:
            base = self._resolve_root(rp)
            if base is None:
                jobs.append(("", rp))  # marker so we keep alignment
            else:
                jobs.append((str(base / rp), rp))

        serial = os.environ.get("MD_MCP_SERIAL_PARSE") == "1" or len(jobs) < 4
        if serial:
            return [fn(abs_path, rp) if abs_path else None for abs_path, rp in jobs]

        # Use fork on macOS/Linux where it's available — vastly cheaper startup than
        # spawn, which re-imports the package per worker (multi-second on cold start).
        ctx = _safe_process_context()
        workers = min(_default_workers(), len(jobs))
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            return list(pool.map(_parser_dispatch, [(fn, *job) for job in jobs], chunksize=4))


def _safe_process_context():
    """Return a multiprocessing context. `fork` on POSIX for speed; `spawn` on Windows.

    Caller must guarantee fork-safety: don't call this from inside the MCP server's
    stdio loop. The server sets `MD_MCP_SERIAL_PARSE=1` at startup specifically so
    parse work runs serially in-process and never reaches this function.

    Fork avoids spawn's multi-second per-worker import cost — critical for the
    `md-mcp build-index` CLI flow where 5.8s vs 71s is the difference.
    """
    if sys.platform != "win32":
        try:
            return multiprocessing.get_context("fork")
        except ValueError:
            pass
    return multiprocessing.get_context("spawn")


def _default_workers() -> int:
    """Pick worker count: cpu_count - 1, capped at 8 (returns diminishing as it grows)."""
    n = os.cpu_count() or 4
    return max(2, min(8, n - 1))


def _parser_dispatch(args: Tuple[Callable, str, str]) -> "Optional[List[dict]]":
    """Top-level helper so the process pool can pickle the call site.

    `args` is `(parser_fn, abs_path, relpath)`. We can't pass a bound method through
    ProcessPoolExecutor (it pickles by name and bound methods can capture state).
    """
    fn, abs_path, relpath = args
    if not abs_path:
        return None
    try:
        return fn(abs_path, relpath)
    except Exception:
        # Swallow per-file failures so one bad file doesn't kill the rebuild;
        # the parser fn itself logs warnings before returning None.
        return None


class StaleCheck:
    """Time-bounded in-process debounce so repeated tool calls inside one turn don't re-stat."""

    def __init__(self, ttl_seconds: float = 2.0):
        self.ttl = ttl_seconds
        self._last_check: float = 0.0

    def should_check(self) -> bool:
        now = time.monotonic()
        if now - self._last_check < self.ttl:
            return False
        self._last_check = now
        return True

    def force_next(self) -> None:
        self._last_check = 0.0
