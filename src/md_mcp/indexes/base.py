"""Index cache infrastructure.

Mirrors the proven design from `MD-VSCode-Utility-Tool/src/util/indexCache.ts`:

  * Each index has a name and a schema version. When the version bumps, the cache for
    that index is invalidated wholesale.
  * Per-index manifest stores `{relative_path: [mtime_ns, size]}` for every contributing
    file. On startup we stat the current files and reparse only the ones whose
    `(mtime, size)` moved.
  * Data is stored as JSON so cross-language inspection and corruption diagnosis are trivial.
    Small indexes keep one `<name>.data.json`; large ones (`sharded = True`) keep one JSON shard
    per contributing file under `<name>.data/` so a one-file edit rewrites one shard.

Size is tracked alongside mtime — guards against the rare same-second rewrite that
mtime alone would miss.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import multiprocessing
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

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
    # Per-process temp name: several warm servers (or a build-index beside one)
    # may write the same cache file, and a shared `<file>.tmp` collides on
    # Windows and can be pruned out from under another writer.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    # IndexCache files are always under cache_dir.
    # pi-lens-ignore: python-path-traversal
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


_SHARD_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


def shard_filename(relpath: str) -> str:
    """Stable, filesystem-safe shard name for a contributing file's relpath.

    A readable stem keeps `ls` useful when diagnosing a cache; the digest of the full
    relpath keeps names unique. The relpath is also stored inside the shard and
    checked on load, so a digest collision reads as a corrupt shard (re-parse), not
    as another file's data.
    """
    digest = hashlib.sha256(relpath.encode("utf-8")).hexdigest()[:16]
    stem = _SHARD_STEM_RE.sub("_", Path(relpath).name)[:60].strip("._") or "file"
    return f"{stem}-{digest}.json"


class IndexCache:
    """Versioned on-disk cache backing a single index.

    Layout:
        <cache_dir>/v<version>/<name>.manifest.json   — {file: [mtime_ns, size]}
        <cache_dir>/v<version>/<name>.data.json       — monolithic payload (default)
        <cache_dir>/v<version>/<name>.data/<shard>    — one shard per contributing file
                                                        (indexes with `sharded = True`)
    """

    def __init__(self, cache_dir: Path, name: str, version: int):
        self.dir = cache_dir / f"v{version}"
        self.manifest_path = self.dir / f"{name}.manifest.json"
        self.data_path = self.dir / f"{name}.data.json"
        self.shard_dir = self.dir / f"{name}.data"

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

    # ----- data (monolithic) ------------------------------------------------

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

    # ----- data (sharded, one shard per contributing file) -------------------

    def shard_path(self, relpath: str) -> Path:
        return self.shard_dir / shard_filename(relpath)

    def load_shard(self, relpath: str) -> tuple[list[dict] | None, str | None] | None:
        """Return `(records, error)` for one file, or None when the shard is missing or corrupt.

        `records` is None for a file whose parse failed (`error` then says why when
        the index tracks parse errors). A None return means "re-parse this file".
        """
        path = self.shard_path(relpath)
        try:
            # shard paths are derived from a digest under cache_dir, not caller input.
            # pi-lens-ignore: python-path-traversal
            with open(path, encoding="utf-8") as fh:
                raw = json.loads(fh.read())
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("relpath") != relpath:
            return None
        records = raw.get("records")
        error = raw.get("error")
        if records is not None and (
            not isinstance(records, list) or not set(map(type, records)) <= {dict}
        ):
            return None
        if error is not None and not isinstance(error, str):
            return None
        return records, error

    def save_shard(self, relpath: str, records: list[dict] | None, error: str | None) -> None:
        self.shard_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"relpath": relpath, "records": records}
        if error is not None:
            payload["error"] = error
        _atomic_write_text(self.shard_path(relpath), json.dumps(payload))

    def delete_shard(self, relpath: str) -> None:
        with contextlib.suppress(OSError):
            self.shard_path(relpath).unlink()

    def prune_shards(self, keep: Iterable[str]) -> int:
        """Delete every orphan shard: files in the shard dir that are not a shard of `keep`.

        `*.tmp` files are left alone; one may be another process's in-flight write.
        """
        wanted = {shard_filename(rel) for rel in keep}
        removed = 0
        try:
            entries = list(self.shard_dir.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if entry.name in wanted or entry.name.endswith(".tmp"):
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
        return removed


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
        # If callers supply duplicate relative paths, preserve the first root's
        # signature just like resolve_root/collect_files do.
        if rel not in sigs:
            sigs[rel] = sig
    return sigs


def roots_for(
    mod_root: Path, vanilla_path: Optional[Path], submod_root: Optional[Path] = None
) -> list[Path]:
    """Content roots in resolution order: submod, mod, then vanilla."""
    roots: list[Path] = []
    if submod_root is not None:
        roots.append(submod_root)
    roots.append(mod_root)
    if vanilla_path is not None:
        roots.append(vanilla_path)
    return roots


def resolve_root(roots: Iterable[Path], relpath: str) -> Optional[Path]:
    """Return the first root that actually holds `relpath`, or None."""
    for base in roots:
        if (base / relpath).exists():
            return base
    return None


def _normalise_specs(value: str | Sequence[str]) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else tuple(value)


def collect_files(
    roots: Iterable[Path],
    subdir: str | Sequence[str],
    pattern: str | Sequence[str],
    predicate: Optional[Callable[[Path], bool]] = None,
) -> list[Path]:
    """Walk each subdir under each root for each pattern, optionally filtering files."""
    results: list[Path] = []
    seen: set[str] = set()
    for base in roots:
        for subdir_name in _normalise_specs(subdir):
            d = base / subdir_name
            if not d.is_dir():
                continue
            for pattern_name in _normalise_specs(pattern):
                for p in d.rglob(pattern_name):
                    if not p.is_file() or (predicate is not None and not predicate(p)):
                        continue
                    try:
                        relpath = str(p.relative_to(base))
                    except ValueError:
                        relpath = str(p)
                    if relpath in seen:
                        continue
                    seen.add(relpath)
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


def plan_rebuild(
    cache: IndexCache,
    files: list[Path],
    roots: list[Path],
    loaded: bool,
    known: Optional[dict[str, FileSig]] = None,
) -> Optional[RebuildPlan]:
    """Diff the current files against what the caller already holds.

    On first load the baseline is the persistent manifest. Once loaded, the caller
    passes `known`, the signatures of the files its in-memory maps were built from,
    and the diff runs against that instead: the manifest is shared with every other
    process on the same cache dir (a second `md-mcp serve`, a `build-index` run), so
    after one of them rewrites it the manifest no longer describes this process's
    maps. Diffing against memory means a file another process removed, added or
    re-indexed still shows up as removed, added or stale here.

    Returns None on the fast path — nothing moved relative to the baseline and
    in-process state is already populated, so the caller can leave its maps alone.
    A missing or unreadable manifest reads as empty, which forces a full rebuild
    and rewrite.
    """
    current_sigs = signatures_for(files, roots)
    manifest = cache.load_manifest() or {}
    baseline = known if loaded and known is not None else manifest
    staleness = compute_staleness(baseline, current_sigs)

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
    """Shared scaffolding for indexes that collect and parse content files.

    Subclasses provide cache metadata, one or more `subdir`/`pattern` specs, a
    picklable module-level `parser_fn`, and a string or tuple `primary_key`.
    """

    cache_version: int = 1
    cache_name: str = ""
    subdir: str = ""
    pattern: str = "*.txt"
    subdirs: Sequence[str] = ()
    patterns: Sequence[str] = ()
    file_predicate: Optional[Callable[[Path], bool]] = None
    primary_key: str | tuple[str, ...] = "id"
    parse_chunksize: int = 4
    missing_result: Any = None
    track_parse_errors: bool = False
    warn_on_duplicates: bool = True
    # Enable the derived country-tag maps only for indexes queried by tag. In
    # particular, the localisation index can contain far more keys than a
    # definition index and does not use these lookups.
    tag_indexed: bool = False
    tag_file_indexed: bool = False
    source_tag_indexed: bool = False
    # One cache shard per contributing file instead of a single `data.json`; a one-file
    # edit then rewrites one shard, and a corrupt shard costs one re-parse.
    sharded: bool = False

    def __init__(
        self,
        mod_root: Path,
        cache_dir: Path,
        vanilla_path: Optional[Path] = None,
        *,
        include_vanilla: bool = True,
        submod_root: Optional[Path] = None,
    ):
        self.mod_root: Path = mod_root
        self.submod_root: Optional[Path] = submod_root
        self.vanilla_path: Optional[Path] = vanilla_path if include_vanilla else None
        self._cache = IndexCache(cache_dir, self.cache_name, self.cache_version)
        self._stale_check = StaleCheck()
        self._subdirs = _normalise_specs(self.subdirs or self.subdir)
        self._patterns = _normalise_specs(self.patterns or self.pattern)

        self._key_fn = self._build_key_fn()
        self._by_file: dict[str, list[dict]] = {}
        self._by_key: dict[Any, dict] = {}
        self._duplicates: dict[Any, list[str]] = {}
        self._parse_errors: dict[str, str] = {}
        self._ids_by_tag: dict[str, tuple[str, ...]] = {}
        self._files_by_tag: dict[str, tuple[str, ...]] = {}
        self._ids_by_file_tag: dict[str, tuple[Any, ...]] = {}
        # Signatures of the files the in-memory maps were built from. The warm
        # refresh diffs disk against these, not against the shared manifest.
        self._sigs: dict[str, FileSig] = {}
        self._loaded = False

    # ---------- public API ----------

    def resolve(self, key: Any) -> "Optional[dict]":
        self.ensure_fresh()
        return self._by_key.get(key)

    def list_keys(self) -> list[Any]:
        self.ensure_fresh()
        return sorted(self._by_key.keys())

    def list_files(self) -> list[str]:
        self.ensure_fresh()
        return sorted(self._by_file.keys())

    def records_for_file(self, relpath: str) -> list[dict]:
        self.ensure_fresh()
        return self._by_file.get(relpath, [])

    def ids_for_tag(self, tag: str) -> list[str]:
        """Sorted winning string keys whose prefix is ``<TAG>_`` (case-insensitive)."""
        self.ensure_fresh()
        canonical = tag.upper()
        if self.tag_indexed and "_" not in canonical:
            return list(self._ids_by_tag.get(canonical, ()))
        prefix = canonical + "_"
        return sorted(
            key
            for key in self._by_key
            if isinstance(key, str) and key.upper().startswith(prefix)
        )

    def files_for_tag(self, tag: str) -> list[str]:
        """Sorted unique winning files for keys whose prefix is ``<TAG>_``."""
        self.ensure_fresh()
        canonical = tag.upper()
        if self.tag_file_indexed and "_" not in canonical:
            return list(self._files_by_tag.get(canonical, ()))
        prefix = canonical + "_"
        return sorted(
            {
                str(record["file"])
                for key, record in self._by_key.items()
                if isinstance(key, str) and key.upper().startswith(prefix)
            }
        )

    def ids_for_country_tag(self, tag: str) -> list[Any]:
        """Winning IDs with a tag prefix or defined in a tag-named file.

        This mirrors the two inclusion rules used by the country manifest while
        avoiding a scan of every record on each request.
        """
        self.ensure_fresh()
        canonical = tag.upper()
        if "_" not in canonical and (self.tag_indexed or self.source_tag_indexed):
            keys = set(self._ids_by_tag.get(canonical, ()))
            keys.update(self._ids_by_file_tag.get(canonical, ()))
            return sorted(keys, key=str)
        prefix = canonical + "_"
        result = []
        for key, record in self._by_key.items():
            file_tag = Path(str(record["file"])).stem.upper()
            if (isinstance(key, str) and key.upper().startswith(prefix)) or (
                file_tag == canonical or file_tag.startswith(prefix)
            ):
                result.append(key)
        return sorted(set(result), key=str)

    def parse_errors(self) -> list[dict]:
        self.ensure_fresh()
        return [
            {"file": relpath, "error": error}
            for relpath, error in sorted(self._parse_errors.items())
        ]

    def duplicates(self) -> dict[Any, list[str]]:
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

    # Set by subclasses to a *module-level* function (picklable). It returns records,
    # None, or an object exposing `records` and an optional `error`.
    parser_fn: Optional[Callable[[str, str], Any]] = None

    # ---------- internals ----------

    def _roots(self) -> list["Path"]:
        return roots_for(self.mod_root, self.vanilla_path, self.submod_root)

    def _collect_files(self) -> list["Path"]:
        return collect_files(self._roots(), self._subdirs, self._patterns, self.file_predicate)

    def _rebuild(self) -> None:
        plan = plan_rebuild(
            self._cache,
            self._collect_files(),
            self._roots(),
            self._loaded,
            known=self._sigs if self._loaded else None,
        )
        if plan is None:
            return
        if self._loaded:
            self._rebuild_incremental(plan)
        else:
            self._rebuild_full(plan)
        self._sigs = plan.current_sigs
        if self.tag_indexed or self.tag_file_indexed or self.source_tag_indexed:
            self._rebuild_tag_indexes()

    def _rebuild_tag_indexes(self) -> None:
        """Derive tag maps from the resolved keys after a full or incremental rebuild.

        Building from `_by_key` (rather than raw per-file records) preserves the
        canonical last-write-wins behavior for duplicate IDs. The maps are
        process-local and reconstructed on cache load, so no cache schema bump is
        needed.
        """
        ids_by_tag: dict[str, list[str]] = {}
        files_by_tag: dict[str, set[str]] = {}
        ids_by_file_tag: dict[str, list[Any]] = {}
        file_tags: dict[str, str] = {}

        for key, record in self._by_key.items():
            file = record.get("file")
            if isinstance(key, str):
                tag, separator, _ = key.partition("_")
                if separator:
                    canonical = tag.upper()
                    if self.tag_indexed or self.source_tag_indexed:
                        ids_by_tag.setdefault(canonical, []).append(key)
                    if self.tag_file_indexed and isinstance(file, str):
                        files_by_tag.setdefault(canonical, set()).add(file)

            if self.source_tag_indexed and isinstance(file, str):
                file_tag = file_tags.get(file)
                if file_tag is None:
                    stem = Path(file).stem.upper()
                    file_tag, _, _ = stem.partition("_")
                    file_tags[file] = file_tag
                if file_tag:
                    ids_by_file_tag.setdefault(file_tag, []).append(key)

        self._ids_by_tag = {tag: tuple(sorted(keys)) for tag, keys in ids_by_tag.items()}
        self._files_by_tag = {tag: tuple(sorted(files)) for tag, files in files_by_tag.items()}
        self._ids_by_file_tag = {
            tag: tuple(sorted(keys, key=str)) for tag, keys in ids_by_file_tag.items()
        }

    def _parse_results(
        self, relpaths: list[str]
    ) -> dict[str, tuple[Optional[list[dict]], Optional[str]]]:
        """Parse `relpaths` and normalise each result to `(records, error)`."""
        parsed: dict[str, tuple[Optional[list[dict]], Optional[str]]] = {}
        if not relpaths:
            return parsed
        results = self._parse_parallel(relpaths)
        for relpath, result in zip(relpaths, results, strict=False):
            parsed[relpath] = (getattr(result, "records", result), getattr(result, "error", None))
        return parsed

    @staticmethod
    def _failure_message(error: Optional[str]) -> str:
        return error or "parser worker failed"

    def _rebuild_full(self, plan: RebuildPlan) -> None:
        """First load: reuse whatever the persistent cache still holds, parse the rest."""
        new_by_file: dict[str, list[dict]] = {}
        new_parse_errors: dict[str, str] = {}
        to_parse = list(plan.to_parse)

        if self.sharded:
            for relpath in plan.staleness.unchanged:
                shard = self._cache.load_shard(relpath)
                if shard is None:
                    # Missing or corrupt shard: re-parse just this file.
                    logger.info(
                        "%s index: re-parsing %s (shard unavailable)", self.cache_name, relpath
                    )
                    to_parse.append(relpath)
                    continue
                records, error = shard
                if records is not None:
                    new_by_file[relpath] = records
                elif self.track_parse_errors:
                    new_parse_errors[relpath] = self._failure_message(error)
        else:
            cached_data = self._cache.load_data() or {}
            cached_files = cached_data.get("files", {})
            if not isinstance(cached_files, dict):
                cached_files = {}
            for relpath in plan.staleness.unchanged:
                if relpath in cached_files:
                    new_by_file[relpath] = cached_files[relpath]
            if self.track_parse_errors:
                cached_errors = cached_data.get("parse_errors", {})
                if not isinstance(cached_errors, dict):
                    cached_errors = {}
                new_parse_errors = {
                    relpath: cached_errors[relpath]
                    for relpath in plan.staleness.unchanged
                    if relpath in cached_errors
                }
                for relpath in new_parse_errors:
                    new_by_file.pop(relpath, None)

        parsed = self._parse_results(to_parse)
        for relpath, (records, error) in parsed.items():
            if records is not None:
                new_by_file[relpath] = records
            elif self.track_parse_errors:
                new_parse_errors[relpath] = self._failure_message(error)

        # Last-write-wins in canonical relpath order, regardless of how files were
        # discovered, loaded from the manifest, or reparsed.
        new_by_file = dict(sorted(new_by_file.items()))
        new_by_key: dict[Any, dict] = {}
        new_duplicates: dict[Any, list[str]] = {}
        for relpath, recs in new_by_file.items():
            for rec in recs:
                k = self._record_key(rec)
                if k is None:
                    continue
                existing = new_by_key.get(k)
                if existing is not None:
                    shadowed_file = existing["file"]
                    if self.warn_on_duplicates and k not in new_duplicates:
                        self._warn_duplicate(k, shadowed_file, relpath)
                    new_duplicates.setdefault(k, []).append(shadowed_file)
                new_by_key[k] = {**rec, "file": relpath}

        self._by_file = new_by_file
        self._by_key = new_by_key
        self._duplicates = new_duplicates
        self._parse_errors = new_parse_errors

        if plan.should_save or len(to_parse) > len(plan.to_parse):
            self._persist(plan, parsed, full=True)

    def _rebuild_incremental(self, plan: RebuildPlan) -> None:
        """Warm refresh: patch the in-process maps for just the files that moved.

        Only keys that appear in a changed file (before or after) can change winner or
        duplicate state, so only those keys are recomputed; every other entry of
        `_by_key` / `_duplicates` is reused as-is. The result is identical to a full
        rebuild over the same files.
        """
        st = plan.staleness
        dropped = set(st.stale) | set(st.removed) | set(st.added)
        old_by_file = self._by_file
        old_by_key = self._by_key
        old_duplicates = self._duplicates

        parsed = self._parse_results(plan.to_parse)

        new_by_file = {rel: recs for rel, recs in old_by_file.items() if rel not in dropped}
        new_parse_errors = {
            rel: err for rel, err in self._parse_errors.items() if rel not in dropped
        }
        for relpath, (records, error) in parsed.items():
            if records is not None:
                new_by_file[relpath] = records
            elif self.track_parse_errors:
                new_parse_errors[relpath] = self._failure_message(error)
        new_by_file = dict(sorted(new_by_file.items()))

        # Keys touched by a changed file, and the files that now contribute each.
        touched: set[Any] = set()
        for relpath in dropped:
            for rec in old_by_file.get(relpath, ()):
                k = self._record_key(rec)
                if k is not None:
                    touched.add(k)
        added_contributors: dict[Any, list[str]] = {}
        for relpath in plan.to_parse:
            for rec in new_by_file.get(relpath, ()):
                k = self._record_key(rec)
                if k is not None:
                    touched.add(k)
                    added_contributors.setdefault(k, []).append(relpath)

        new_by_key = dict(old_by_key)
        new_duplicates = dict(old_duplicates)
        last_record_by_file: dict[str, dict[Any, dict]] = {}

        def last_record(relpath: str, key: Any) -> dict:
            per_file = last_record_by_file.get(relpath)
            if per_file is None:
                per_file = {}
                for rec in new_by_file[relpath]:
                    rk = self._record_key(rec)
                    if rk is not None:
                        per_file[rk] = rec
                last_record_by_file[relpath] = per_file
            return per_file[key]

        for k in touched:
            old_winner = old_by_key.get(k)
            # Contributing files in canonical order, one entry per record (a file may
            # define a key more than once). `_duplicates[k]` holds all but the winner.
            contributors: list[str] = list(old_duplicates.get(k, ()))
            if old_winner is not None:
                contributors.append(old_winner["file"])
            contributors = [rel for rel in contributors if rel not in dropped]
            contributors.extend(added_contributors.get(k, ()))
            contributors.sort()

            if not contributors:
                new_by_key.pop(k, None)
                new_duplicates.pop(k, None)
                continue

            winner_file = contributors[-1]
            if (
                old_winner is not None
                and old_winner["file"] == winner_file
                and winner_file not in dropped
            ):
                new_by_key[k] = old_winner
            else:
                new_by_key[k] = {**last_record(winner_file, k), "file": winner_file}

            shadowed = contributors[:-1]
            if shadowed:
                if self.warn_on_duplicates and k not in old_duplicates:
                    self._warn_duplicate(k, shadowed[-1], winner_file)
                new_duplicates[k] = shadowed
            else:
                new_duplicates.pop(k, None)

        self._by_file = new_by_file
        self._by_key = new_by_key
        self._duplicates = new_duplicates
        self._parse_errors = new_parse_errors

        if plan.should_save:
            self._persist(plan, parsed, full=False)

    def _warn_duplicate(self, key: Any, shadowed_file: str, winner_file: str) -> None:
        logger.warning(
            "Duplicate key %r in %s: %s shadowed by %s",
            key,
            self.cache_name,
            shadowed_file,
            winner_file,
        )

    def _persist(
        self,
        plan: RebuildPlan,
        parsed: dict[str, tuple[Optional[list[dict]], Optional[str]]],
        *,
        full: bool,
    ) -> None:
        """Write data then manifest. Data first: a crash in between leaves a manifest
        that still reads as stale, so the next start re-parses instead of trusting
        half-written state.

        The cache is best-effort: the in-memory maps are already correct, so a write
        that fails (another process holding a shard or the manifest open on Windows,
        a read-only cache dir) is logged and otherwise ignored. The manifest is then
        not updated, and the next start re-parses whatever it did not persist.
        """
        try:
            if self.sharded:
                for relpath, (records, error) in parsed.items():
                    self._cache.save_shard(
                        relpath, records, error if self.track_parse_errors else None
                    )
                for relpath in plan.staleness.removed:
                    self._cache.delete_shard(relpath)
                if full:
                    self._cache.prune_shards(plan.current_sigs)
            else:
                payload: dict[str, Any] = {"files": self._by_file}
                if self.track_parse_errors:
                    payload["parse_errors"] = self._parse_errors
                self._cache.save_data(payload)
            self._cache.save_manifest(plan.current_sigs)
        except OSError as exc:
            logger.warning(
                "%s cache: could not persist (%s); will re-parse next start", self.cache_name, exc
            )

    def _record_key(self, record: dict) -> Any:
        return self._key_fn(record)

    def _build_key_fn(self) -> Callable[[dict], Any]:
        """Specialised key extractor — it runs once per record (hundreds of thousands for loc)."""
        primary_key = self.primary_key
        if isinstance(primary_key, str):
            return lambda record: record.get(primary_key)
        if len(primary_key) == 2:
            first, second = primary_key

            def pair_key(record: dict) -> Any:
                a = record.get(first)
                b = record.get(second)
                return None if a is None or b is None else (a, b)

            return pair_key

        def tuple_key(record: dict) -> Any:
            values = tuple(record.get(field) for field in primary_key)
            return values if all(value is not None for value in values) else None

        return tuple_key

    def _parse_parallel(self, relpaths: list[str]) -> list[Any]:
        fn = type(self).parser_fn
        if fn is None:
            # Legit abstract-method guard, not a scaffolded stub.
            # pi-lens-ignore: no-raise-not-implemented
            raise NotImplementedError(
                f"{type(self).__name__} must set `parser_fn` to a module-level function"
            )
        return parse_files(
            fn,
            self._roots(),
            relpaths,
            missing=self.missing_result,
            chunksize=self.parse_chunksize,
        )


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
