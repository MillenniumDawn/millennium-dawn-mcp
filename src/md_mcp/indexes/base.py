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
import logging
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


@dataclass
class FileSig:
    mtime_ns: int
    size: int

    def to_json(self) -> list:
        return [self.mtime_ns, self.size]

    @classmethod
    def from_json(cls, data: list) -> "FileSig":
        try:
            mtime_ns, size = data[0], data[1]
        except (TypeError, IndexError, KeyError) as e:
            raise ValueError(f"corrupt manifest signature: {data!r}") from e
        # bool is an int subclass; JSON true must not become mtime 1.
        if type(mtime_ns) is not int or type(size) is not int:
            raise ValueError(f"corrupt manifest signature: {data!r}")
        return cls(mtime_ns=mtime_ns, size=size)


def file_signature(path: Path) -> FileSig | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return FileSig(mtime_ns=st.st_mtime_ns, size=st.st_size)


@dataclass
class Staleness:
    stale: list[str]  # known files whose mtime/size moved
    removed: list[str]  # known files now missing
    added: list[str]  # new files not in the manifest
    unchanged: list[str]  # safe to reuse


def compute_staleness(manifest: dict[str, FileSig], current: dict[str, FileSig]) -> Staleness:
    stale: list[str] = []
    removed: list[str] = []
    unchanged: list[str] = []
    added: list[str] = []

    for path, sig in manifest.items():
        cur = current.get(path)
        if cur is None:
            removed.append(path)
        elif cur.mtime_ns != sig.mtime_ns or cur.size != sig.size:
            stale.append(path)
        else:
            unchanged.append(path)

    for path in sorted(current.keys() - manifest.keys()):
        added.append(path)

    return Staleness(stale=stale, removed=removed, added=added, unchanged=unchanged)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(".json.tmp")
    # IndexCache files are always under cache_dir.
    # pi-lens-ignore: python-path-traversal
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


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

    def load_manifest(self) -> dict[str, FileSig] | None:
        if not self.manifest_path.exists():
            return None
        try:
            # manifest_path is under cache_dir, not caller input.
            # pi-lens-ignore: python-path-traversal
            with open(self.manifest_path, encoding="utf-8") as fh:
                text = fh.read()
            raw = json.loads(text)
            # from_json stays inside the try so a wrong-shape manifest rebuilds, not raises.
            return {path: FileSig.from_json(sig) for path, sig in raw.items()}
        except (OSError, AttributeError, TypeError, ValueError, IndexError):
            return None

    def save_manifest(self, sigs: dict[str, FileSig]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {path: sig.to_json() for path, sig in sigs.items()}
        _atomic_write_text(self.manifest_path, json.dumps(payload))

    # ----- data -------------------------------------------------------------

    def load_data(self) -> dict | None:
        if not self.data_path.exists():
            return None
        try:
            # data_path is under cache_dir, not caller input.
            # pi-lens-ignore: python-path-traversal
            with open(self.data_path, encoding="utf-8") as fh:
                return json.loads(fh.read())
        except (OSError, json.JSONDecodeError):
            return None

    def save_data(self, payload: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.data_path, json.dumps(payload))


def signatures_for(paths: Iterable[Path], roots: Path | list[Path]) -> dict[str, FileSig]:
    """Build a {relative_path: signature} map. Missing files are skipped.

    `roots` may be a single root or a list. The first matching root is used for
    relativisation — this lets indexes that span mod + vanilla report a single
    relative path key (e.g. `common/national_focus/USA.txt`) regardless of which
    base directory the absolute file lives under.
    """
    root_list = [roots] if isinstance(roots, Path) else roots
    sigs: dict[str, FileSig] = {}
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


def roots_for(mod_root: Path, vanilla_path: Optional[Path]) -> list[Path]:
    """Content roots in resolution order: mod first, vanilla second when configured."""
    return [mod_root] if vanilla_path is None else [mod_root, vanilla_path]


def resolve_root(roots: Iterable[Path], relpath: str) -> Optional[Path]:
    """Return the first root that actually holds `relpath`, or None."""
    for base in roots:
        if (base / relpath).exists():
            return base
    return None


def collect_files(
    roots: Iterable[Path],
    subdir: str,
    pattern: str,
    predicate: Optional[Callable[[Path], bool]] = None,
) -> list[Path]:
    """Walk `subdir` under each root for `pattern`, optionally narrowed by `predicate`."""
    results: list[Path] = []
    for base in roots:
        d = base / subdir
        if not d.is_dir():
            continue
        for p in d.rglob(pattern):
            if p.is_file() and (predicate is None or predicate(p)):
                results.append(p)
    return results


@dataclass
class RebuildPlan:
    """The work a rebuild has to do, once the manifest has been diffed against disk."""

    manifest: dict[str, FileSig]
    current_sigs: dict[str, FileSig]
    staleness: Staleness
    to_parse: list[str]

    @property
    def should_save(self) -> bool:
        return bool(self.to_parse or self.staleness.removed or not self.manifest)


@dataclass
class RebuildState:
    """A rebuild plan with current or persisted index data ready for reuse."""

    plan: RebuildPlan
    data: dict

    def reused_files(self) -> dict:
        cached_files = self.data.get("files", {})
        return {
            relpath: cached_files[relpath]
            for relpath in self.plan.staleness.unchanged
            if relpath in cached_files
        }


def prepare_rebuild(
    cache: IndexCache,
    paths: list[Path],
    roots: list[Path],
    loaded: bool,
    **in_memory: Any,
) -> Optional[RebuildState]:
    """Plan a rebuild and load reusable data, or return None when the index is current."""
    plan = plan_rebuild(cache, paths, roots, loaded)
    if plan is None:
        return None
    data = in_memory if loaded else cache.load_data() or {}
    return RebuildState(plan, data)


def plan_rebuild(
    cache: IndexCache, files: list[Path], roots: list[Path], loaded: bool
) -> Optional[RebuildPlan]:
    """Diff the current files against the manifest.

    Returns None on the fast path — nothing moved on disk and in-process state is
    already populated, so the caller can leave its maps alone. A missing or
    unreadable manifest reads as empty, which forces a full rebuild and rewrite.
    """
    current_sigs = signatures_for(files, roots)
    manifest = cache.load_manifest() or {}
    staleness = compute_staleness(manifest, current_sigs)

    if loaded and not staleness.stale and not staleness.added and not staleness.removed:
        return None

    return RebuildPlan(
        manifest=manifest,
        current_sigs=current_sigs,
        staleness=staleness,
        to_parse=staleness.stale + staleness.added,
    )


def parse_files(
    parser_fn: Callable[[str, str], Any],
    roots: list[Path],
    relpaths: list[str],
    *,
    missing: Any = None,
    chunksize: int = 4,
) -> list[Any]:
    """Resolve each relpath to an absolute path and run `parser_fn` over the batch.

    Falls back to serial execution under two conditions: (a) fewer than four files
    (warm-path edits — pool overhead would dominate), or (b) `MD_MCP_SERIAL_PARSE=1`
    is set, which `md-mcp serve` does because forking under stdio deadlocks.

    Results stay aligned with `relpaths`. On the serial path, a file that resolves
    under no root yields `missing`; pooled parsers retain their existing handling.
    """
    jobs: list[tuple[str, str]] = []
    for rp in relpaths:
        base = resolve_root(roots, rp)
        jobs.append((str(base / rp), rp) if base is not None else ("", rp))

    if os.environ.get("MD_MCP_SERIAL_PARSE") == "1" or len(jobs) < 4:
        return [parser_fn(abs_path, rp) if abs_path else missing for abs_path, rp in jobs]

    # Use fork on macOS/Linux where it's available — vastly cheaper startup than
    # spawn, which re-imports the package per worker (multi-second on cold start).
    ctx = _safe_process_context()
    workers = min(_default_workers(), len(jobs))
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        return list(
            pool.map(_parser_dispatch, [(parser_fn, *job) for job in jobs], chunksize=chunksize)
        )


class GenericTxtIndex:
    """Shared scaffolding for paradox-script indexes that walk a single subdir for `.txt`.

    Subclasses provide:
        * `cache_version: int`
        * `cache_name: str`     — file basename under `<cache_dir>/v<ver>/`
        * `subdir: str`         — path under each root, e.g. `events`
        * `pattern: str`        — glob like `*.txt`
        * `parser_fn`           — *module-level* function
                                  `(abs_path: str, relpath: str) -> Optional[list[dict]]`
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

        self._by_file: dict[str, list[dict]] = {}
        self._by_key: dict[str, dict] = {}
        self._duplicates: dict[str, list[str]] = {}
        self._loaded = False

    # ---------- public API ----------

    def resolve(self, key: str) -> "Optional[dict]":
        self.ensure_fresh()
        return self._by_key.get(key)

    def list_keys(self) -> list[str]:
        self.ensure_fresh()
        return sorted(self._by_key.keys())

    def list_files(self) -> list[str]:
        self.ensure_fresh()
        return sorted(self._by_file.keys())

    def records_for_file(self, relpath: str) -> list[dict]:
        self.ensure_fresh()
        return self._by_file.get(relpath, [])

    def duplicates(self) -> dict[str, list[str]]:
        """Return {key: [shadowed files]} recorded by the last rebuild.

        `_rebuild` recomputes this on every call, including a fresh instance's
        first load from the persistent cache (no reparse needed) — so this
        reflects the mod's current duplicate state even on a cache hit. It only
        stays stale (unchanged) when `ensure_fresh` skips `_rebuild` outright,
        i.e. an already-loaded instance within the staleness-check TTL.
        """
        self.ensure_fresh()
        return self._duplicates

    def ensure_fresh(self) -> None:
        if self._loaded and not self._stale_check.should_check():
            return
        self._rebuild()
        self._loaded = True

    # ---------- subclass hooks ----------

    # Set by subclasses to a *module-level* function (picklable). Signature:
    #   parser_fn(abs_path: str, relpath: str) -> Optional[list[dict]]
    # Returning None means "could not parse"; returning [] means "no records found".
    parser_fn: Optional[Callable[[str, str], "Optional[list[dict]]"]] = None

    # ---------- internals ----------

    def _roots(self) -> list["Path"]:
        return roots_for(self.mod_root, self.vanilla_path)

    def _collect_files(self) -> list["Path"]:
        return collect_files(self._roots(), self.subdir, self.pattern)

    def _rebuild(self) -> None:
        plan = plan_rebuild(self._cache, self._collect_files(), self._roots(), self._loaded)
        if plan is None:
            return

        if self._loaded:
            cached_files = self._by_file
        else:
            cached_data = self._cache.load_data() or {}
            cached_files = cached_data.get("files", {})

        new_by_file: dict[str, list[dict]] = {}
        for relpath in plan.staleness.unchanged:
            if relpath in cached_files:
                new_by_file[relpath] = cached_files[relpath]

        if plan.to_parse:
            results = self._parse_parallel(plan.to_parse)
            for relpath, recs in zip(plan.to_parse, results, strict=False):
                if recs is not None:
                    new_by_file[relpath] = recs

        # Last-write-wins in canonical relpath order, regardless of how files were
        # discovered, loaded from the manifest, or reparsed.
        new_by_file = dict(sorted(new_by_file.items()))
        new_by_key: dict[str, dict] = {}
        new_duplicates: dict[str, list[str]] = {}
        for relpath, recs in new_by_file.items():
            for rec in recs:
                k = rec.get(self.primary_key)
                if k is None:
                    continue
                existing = new_by_key.get(k)
                if existing is not None:
                    shadowed_file = existing["file"]
                    if k not in new_duplicates:
                        logger.warning(
                            "Duplicate key %r in %s: %s shadowed by %s",
                            k,
                            self.cache_name,
                            shadowed_file,
                            relpath,
                        )
                    new_duplicates.setdefault(k, []).append(shadowed_file)
                new_by_key[k] = {**rec, "file": relpath}

        self._by_file = new_by_file
        self._by_key = new_by_key
        self._duplicates = new_duplicates

        if plan.should_save:
            self._cache.save_data({"files": new_by_file})
            self._cache.save_manifest(plan.current_sigs)

    def _parse_parallel(self, relpaths: list[str]) -> list["Optional[list[dict]]"]:
        fn = type(self).parser_fn
        if fn is None:
            # Legit abstract-method guard, not a scaffolded stub.
            # pi-lens-ignore: no-raise-not-implemented
            raise NotImplementedError(
                f"{type(self).__name__} must set `parser_fn` to a module-level function"
            )
        return parse_files(fn, self._roots(), relpaths)


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


def _parser_dispatch(args: tuple[Callable, str, str]) -> Any:
    """Top-level helper so the process pool can pickle the call site.

    `args` is `(parser_fn, abs_path, relpath)`. We can't pass a bound method through
    ProcessPoolExecutor (it pickles by name and bound methods can capture state).
    Return type follows the dispatched parser fn (list-of-records or single dict).
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
