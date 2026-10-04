# Indexes

How the server caches paradox-script structure so the warm-path tool calls
are dict lookups rather than filesystem walks.

The design is ported from `MD-VSCode-Utility-Tool/src/util/indexCache.ts` —
mtime+size invalidation, JSONL persistence, lazy build. Same lessons apply.

## Two tiers

**In-process** (RAM): `IndexInstance._by_file` (relpath → records) and
`_by_key` (id → record). Populated lazily on the first `ensure_fresh()` call.

**Persistent** (disk): `<cache_dir>/v<N>/<name>.manifest.json` plus the data,
either one `<name>.data.json` (small indexes) or one shard per contributing
file under `<name>.data/` (`sharded = True`, used by localisation). Survives
server restarts; rebuilt incrementally. See [Sharded cache](#sharded-cache).

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
4. Write the new data + manifest back, atomically (`.tmp` rename). Data is
   written first, manifest last, so a crash in between reads as stale (re-parse)
   rather than as trusted half-written state.

### Incremental `_rebuild`

The first `ensure_fresh()` of a process builds `_by_file`, `_by_key` and
`_duplicates` from scratch (from the cache plus whatever was reparsed). Every
later refresh is **incremental**: only the keys that appear in a changed file —
in its old records or its new ones — are recomputed. For each such key the
contributing files are rebuilt from `_duplicates[key] + [winner file]`, minus the
changed files, plus the changed files' new records, sorted by relpath; the last
one wins, exactly as in a full rebuild (so removing a shadowing file un-shadows
the earlier definition). Untouched entries are reused as-is, and the maps are
swapped in as new dict objects so concurrent readers never see a half-patched
index. `tests/test_index_sharding.py` checks the result against a from-scratch
rebuild after random edit/add/remove sequences.

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
| `LocalisationIndex` | `localisation/**/*_l_<lang>.yml` | `(lang, key)` | Only the configured `loc_langs` (default English); see [Localisation languages](#localisation-languages). Sharded cache. |

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
├── v3/                           (focus v3, loc v3 — each index has its own N)
│   ├── focus.data.json
│   ├── focus.manifest.json
│   ├── loc.manifest.json
│   └── loc.data/                 sharded: one JSON file per contributing .yml
│       ├── MD_GCC_membership_l_english.yml-3fa9c1…json
│       └── ...
└── ...
```

JSON (not JSONL) was chosen for simplicity — atomic rewrite is straightforward,
and cross-language inspection / corruption diagnosis with `jq` is trivial.

Loc cache v3 changed meaning twice over (only the configured languages are
indexed; data is sharded), so v2 caches are ignored.

## Sharded cache

A monolithic `data.json` is rewritten whole on every change. For localisation
that is 40 MB / 232 k records for English alone, so a one-file edit used to cost
about 1.8 s and the cache would reach ~400 MB with all 10 languages. An index
sets `sharded = True` to store one shard per contributing file instead:

```
<cache_dir>/v<N>/<name>.data/<basename>-<sha256[:16] of relpath>.json
{"relpath": "localisation/english/x_l_english.yml", "records": [...]}   # + "error" when tracked
```

- **Edit** — only the shards of `stale + added` files are written; `removed`
  files' shards are deleted. Other shards keep their mtime.
- **Startup** — only shards for files present in the manifest are read.
- **Corrupt or missing shard** — unreadable JSON, wrong shape, or a `relpath`
  that doesn't match (digest collision) reads as "no data": that one file is
  re-parsed and its shard rewritten. Nothing else is touched.
- **Orphans** — a full (first-load) rebuild that saves also prunes any file in the
  shard dir that isn't a shard of a current file (leftovers from a crash, `.tmp`).
- **Parse errors** — a shard with `"records": null` is a file whose parse failed
  (its `error` is kept when the index tracks parse errors).

Indexes that don't opt in keep the single `data.json`. Focus could be sharded the
same way (it already carries per-file errors) but its payload is small, so it
stays monolithic.

## Localisation languages

The real mod has ~10 languages at ~297 files each, and every key's `value` is
cached, so indexing all of them multiplies cache size, startup, and edit latency.
`LocalisationIndex(langs=...)` therefore indexes only the languages in
`Settings.loc_langs`:

| Source | Example |
|---|---|
| default | `default_lang` only (`en`) |
| env `MD_MCP_LOC_LANGS` | `en,de` or `*` (all) |
| `config.toml` `loc_langs` | `"en,de"` or `["en", "de"]` |

Only files whose `_l_<language>.yml` suffix maps to an indexed ISO code are
collected (`LANG_ISO_TO_SUFFIX`); switching the setting just adds/removes files
from the manifest, so shards for languages that were dropped are deleted.

`resolve_loc(lang=X)` for a language that is **not** indexed still works through
an on-demand scan of `localisation/**/*_l_<X>.yml`: files are parsed with the same
`_parse_loc_file`, kept in memory keyed by their `(mtime, size)`, stat-checked at
most every 2 s, and only the files that moved are re-parsed. Nothing is written to
disk, and parsing is always serial (no fork inside the server). The first lookup
in a language costs about as much as a cold build of that language alone (~2 s for
English); later ones are dict lookups. The English fallback uses the same path, so
it works even when `en` is not indexed. `list_files()` and the per-country loc
file list in `list_country_content` cover indexed languages only.

## What the cache stores (and doesn't)

For each record, the cache holds only what's needed for the **resolve →
file/line** path:

```json
{ "id": "ISR_idf_modernization", "line": 42, "kind": "focus_tree" }
```

Heavy fields (`x`, `y`, `cost`, `prerequisites`, `mutually_exclusive`,
`icon`, …) are **recomputed on demand** from source — they're only needed
when the caller explicitly asks for them via `resolve_focus`. This keeps the
cache file small enough to load and parse in <50 ms cold. Focus cache v3 also
persists per-file read and parse errors so callers can distinguish an incomplete
index from a clean search.

The trade-off: `resolve_focus` re-parses the focus's file on every call. In
practice that's a few KB of paradox script and ~1 ms.

## Adding a new index

1. Inherit `GenericTxtIndex` in `src/md_mcp/indexes/<name>.py`.
2. Set the class attributes: `cache_version`, `cache_name`, `primary_key`,
   and either `subdir`/`pattern` or `subdirs`/`patterns`.
3. Define a **module-level** parser fn (so `ProcessPoolExecutor` can pickle it)
   with signature `(abs_path: str, relpath: str) -> Optional[List[dict]]`.
   Return `None` for "can't parse"; `[]` for "no records found". Have it read
   the file and do its own cheap substring check up front (see `event.py`,
   `idea.py`, `gfx.py`) so files that obviously don't contain the record kind
   skip the full parse.
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
idx.ensure_fresh()  # cold-build or refresh

idx.resolve("ISR_idf_modernization")  # → {id, file, line, kind}
idx.list_keys()  # → sorted list of all IDs
idx.list_files()  # → sorted list of all contributing files
idx.records_for_file("common/national_focus/MD_ISR_focus.txt")
```

## Performance budget

| Operation | Target | Why |
|---|---|---|
| Cold build (mod only) | < 6 s | Acceptable one-time cost; runs via `md-mcp build-index`. |
| Cold build (mod + vanilla) | < 30 s | Vanilla doubles work. |
| Warm `ensure_fresh()` (no changes) | < 50 ms | Stat-walk only. |
| Warm `ensure_fresh()` (1 file changed) | < 200 ms | Stat + re-parse one file + patch the touched keys + rewrite one shard. Loc, English: ~25-60 ms (was 1.0-1.9 s). |
| Startup from cache, loc (English) | < 1 s | Read ~300 shards. ~0.65 s (was ~0.9-1.1 s). |
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
