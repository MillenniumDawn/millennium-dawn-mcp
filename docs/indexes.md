# Indexes

How the server caches paradox-script structure so the warm-path tool calls
are dict lookups rather than filesystem walks.

The design is ported from `MD-VSCode-Utility-Tool/src/util/indexCache.ts` —
mtime+size invalidation, JSONL persistence, lazy build. Same lessons apply.

## Two tiers

**In-process** (RAM): `IndexInstance._by_file` (relpath → records) and
`_by_key` (id → record). Populated lazily on the first `ensure_fresh()` call.

**Persistent** (disk): `<cache_dir>/v<N>/<name>.data.json` +
`<name>.manifest.json`. Survives server restarts; rebuilt incrementally.

The cache dir defaults to `<mod_root>/.md-mcp-cache/`. Override via
`MD_MCP_CACHE_DIR` (use this when the mod checkout is on a read-only mount).

## Invalidation

For each contributing file, the manifest stores `[mtime_ns, size]`. On
`ensure_fresh()`:

1. Stat every current contributing file.
2. Diff against the persisted manifest:
   - **stale** — known file whose `(mtime, size)` moved
   - **removed** — known file now missing
   - **added** — new file not in the manifest
   - **unchanged** — safe to reuse
3. Reparse only `stale + added`. Drop `removed`. Keep `unchanged` from cache.
4. Write the new data + manifest back, atomically (`.tmp` rename).

**Why size alongside mtime?** Same-second rewrites can leave mtime unchanged
on filesystems with second-granularity timestamps; the size check catches
this. Lifted directly from `indexCache.ts`.

## In-process debounce

```python
class StaleCheck:
    def __init__(self, ttl_seconds: float = 2.0): ...
```

Inside a single agent turn the same tool may be called several times. `StaleCheck`
suppresses re-stat for 2 seconds. Past that, `ensure_fresh()` re-stats. Cold
startup always stats (the `_loaded` flag wasn't set yet).

## Parallel build

`GenericTxtIndex._parse_parallel` dispatches the per-file parser to a
`ProcessPoolExecutor` (multiprocessing). It falls back to serial under two
conditions:

1. Fewer than 4 files to parse (pool startup would dominate).
2. `MD_MCP_SERIAL_PARSE=1` is set.

The second flag is **critical for `md-mcp serve`**: forking from inside the
stdio loop deadlocks because workers inherit the parent's stdin/stdout. The
`serve` subcommand sets it at startup so the server's incremental updates
stay serial. The CLI subcommand `md-mcp build-index` doesn't set it, so cold
builds get the full parallel speedup (~6 s vs ~30 s on the real mod).

The fork context is preferred (`fork` on POSIX) over `spawn` because `spawn`
re-imports the whole package per worker — multi-second overhead. Falls back
to `spawn` on Windows.

## What's indexed

| Index | Subdir | Primary key | Notes |
|---|---|---|---|
| `FocusIndex` | `common/national_focus/` | `id` | Detects `focus_tree`, `shared_focus`, `joint_focus`. |
| `EventIndex` | `events/` | `id` (namespace.n) | Tracks file-level namespaces too. |
| `DecisionIndex` | `common/decisions/` | `id` | Stores category. |
| `IdeaIndex` | `common/ideas/` | `id` | Stores category + slot. |
| `GfxIndex` | `interface/` | `name` | Sprite name → texture path. |
| `LocalisationIndex` | `localisation/<lang>/` | `(lang, key)` | One sub-index per language. |

Each index is independent — they stat their own subdirs and don't coordinate.

## Vanilla content

When `vanilla_path` is set, every index also walks the vanilla subdir. The
relative path in `_by_file` is preserved (`common/national_focus/USA.txt`)
regardless of which root the absolute path lived under; `_resolve_root(rel)`
finds the right base on demand.

Vanilla content **doubles** the cold-build cost on a big install. It's opt-in
via `HOI4_PATH` or `hoi4_path` in the config file.

## Cache versioning

Each index has `cache_version: int`. Bump it when the on-disk schema changes
(adding fields to the cached record, changing key semantics). Old `v<N>/`
directories are simply ignored; users can blow them away manually.

```
.md-mcp-cache/
└── v1/
    ├── focus.data.json
    ├── focus.manifest.json
    ├── localisation.l_english.data.json
    ├── localisation.l_english.manifest.json
    └── ...
```

JSON (not JSONL) was chosen for simplicity — atomic rewrite is straightforward,
and cross-language inspection / corruption diagnosis with `jq` is trivial.

## What the cache stores (and doesn't)

For each record, the cache holds only what's needed for the **resolve →
file/line** path:

```json
{ "id": "ISR_idf_modernization", "line": 42, "kind": "focus_tree" }
```

Heavy fields (`x`, `y`, `cost`, `prerequisites`, `mutually_exclusive`,
`icon`, …) are **recomputed on demand** from source — they're only needed
when the caller explicitly asks for them via `resolve_focus`. This keeps the
cache file small enough to load and parse in <50 ms cold.

The trade-off: `resolve_focus` re-parses the focus's file on every call. In
practice that's a few KB of paradox script and ~1 ms.

## Adding a new index

1. Inherit `GenericTxtIndex` in `src/md_mcp/indexes/<name>.py`.
2. Set the class attributes: `cache_version`, `cache_name`, `subdir`,
   `pattern`, `content_prefilter`, `primary_key`.
3. Define a **module-level** parser fn (so `ProcessPoolExecutor` can pickle it)
   with signature `(abs_path: str, relpath: str) -> Optional[List[dict]]`.
   Return `None` for "can't parse"; `[]` for "no records found".
4. Set `parser_fn = _your_parser_fn` on the class.
5. Add the class to `src/md_mcp/indexes/__init__.py`.
6. Wire into `server.py`: instantiate once, pass to resolver/analysis tools.
7. Test: at least cold-build, warm-resolve, and one stale-invalidation case.

Don't subclass `parse_one` — that hook was removed in favour of the
module-level function so pickling works.

## Common operations

```python
from md_mcp.indexes import FocusIndex

idx = FocusIndex(mod_root, cache_dir, vanilla_path)
idx.ensure_fresh()                       # cold-build or refresh

idx.resolve("ISR_idf_modernization")     # → {id, file, line, kind}
idx.list_keys()                          # → sorted list of all IDs
idx.list_files()                         # → sorted list of all contributing files
idx.records_for_file("common/national_focus/MD_ISR_focus.txt")
```

## Performance budget

| Operation | Target | Why |
|---|---|---|
| Cold build (mod only) | < 6 s | Acceptable one-time cost; runs via `md-mcp build-index`. |
| Cold build (mod + vanilla) | < 30 s | Vanilla doubles work. |
| Warm `ensure_fresh()` (no changes) | < 50 ms | Stat-walk only. |
| Warm `ensure_fresh()` (1 file changed) | < 200 ms | Stat + re-parse one file. |
| Single `resolve()` after fresh | < 1 ms | Dict lookup. |

These are asserted as smoke tests in `tests/test_perf.py`. Treat regressions
as bugs.

## Debugging stale data

```bash
# Blow away the cache entirely.
rm -rf /path/to/Millennium-Dawn/.md-mcp-cache

# Rebuild verbosely.
md-mcp -v build-index --mod-root /path/to/Millennium-Dawn
```

If `resolve_*` returns "indexed file missing on disk", the manifest is
stale-but-not-yet-rebuilt; the next `ensure_fresh()` (within 2s) will fix it.
