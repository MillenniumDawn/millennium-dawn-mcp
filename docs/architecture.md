# Architecture

How `millennium-dawn-mcp` is laid out and what happens between a tool call landing on
stdio and a result going back out.

## Layering

```
┌────────────────────────────────────────────────────────────┐
│ MCP client (Claude Code, Cursor, …)                        │
│       JSON-RPC over stdio                                  │
└────────────────────────────┬───────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────┐
│ src/md_mcp/server.py                                       │
│   FastMCP instance · @mcp.tool() + @mcp.resource()         │
│   one-line descriptions are the agent's prompt budget      │
└──────┬──────────────────────────────┬──────────────────────┘
       │                              │
       ▼                              ▼
┌──────────────┐              ┌──────────────────┐
│ tools/*.py   │              │ resources.py     │
│ Thin wrappers│              │ md:// raw text   │
└──────┬───────┘              └──────────┬───────┘
       │                                 │
┌──────▼───────┐  ┌─────────────┐  ┌─────▼───────┐
│ analysis/    │  │ generators/ │  │ paradox/    │
│ Queries      │  │ str output  │  │ AST parser  │
└──────┬───────┘  └─────────────┘  └─────────────┘
       │
┌──────▼────────┐  ┌──────────────┐  ┌──────────────┐
│ indexes/      │  │ validators/  │  │ util/        │
│ Two-tier cache│  │ wrapper      │  │ response,    │
└───────────────┘  └──────────────┘  │ encoding,    │
                                     │ pathing      │
                                     └──────────────┘
```

The arrows are import direction. Lower modules don't know about higher ones.

## Request lifecycle

1. **Client sends `tools/call`** over stdio. FastMCP deserialises and routes
   to the matching `@mcp.tool()` function in `server.py`.
2. **Server wrapper** in `server.py` does parameter unpacking and calls the
   real implementation in `tools/`, `analysis/`, or `generators/`.
3. **Implementation may**:
   - Read the **indexes** (`focus_index.resolve("ISR_x")`). The first call
     `ensure_fresh()`s the index — stat all contributing files, reparse only
     ones whose `(mtime, size)` moved, persist `<mod_root>/.md-mcp-cache/v<N>/`.
   - Read a source file's text and AST through `paradox.ast_cache.parse_cached`
     (BOM-aware `read_text` + `parse_string` behind a process-wide LRU; see
     [AST cache](#ast-cache)). Tools that only need raw text still call
     `util.encoding.read_text` directly.
   - For validators: hand off to `ValidatorRunner` which imports
     `Millennium-Dawn/tools/validation/validate_*.py` in-process.
4. **Result shaping**: every list-returning tool calls `paginate(...)` (or its
   detail-tier equivalent) and wraps the dict in `enforce_budget(..., heavy_keys=(...))`
   from `util/response.py`. This is the **last-line defence** that keeps
   output below the ~100 KB MCP client cap.
5. **FastMCP serialises** the dict to JSON and writes it to stdout.

## Why this layering matters

- **Tools never bypass indexes for hot paths.** `find_focuses` could grep the
  filesystem; instead it walks the in-memory focus map, which is ~50× faster
  and lets `resolve_*` callers compose without paying a re-scan.
- **Indexes never invoke tools.** They depend only on `paradox/` and `util/`.
  This keeps the index layer testable without spinning up FastMCP.
- **Generators never invoke validators or indexes.** They take their args at
  face value and emit script. Validation of the *result* is the agent's job
  via a follow-up `validate` call.

## Configuration flow

`config.py` `load(mod_root_arg)` returns a `Settings`:

```python
@dataclass
class Settings:
    mod_root: Path
    vanilla_path: Optional[Path]
    cache_dir: Path
    validator_mode: str = "isolated"
    default_lang: str = "en"
    submod_root: Optional[Path] = None
    loc_langs: tuple[str, ...] = ()  # empty -> (default_lang,)
```

Resolution order: CLI flag > env var > `~/.config/md-mcp/config.toml` >
auto-discovery (`util.pathing.find_mod_root`). Vanilla path is opt-in only;
indexes degrade gracefully when it's `None`.

`Settings` is constructed once at `serve` startup and threaded through to
every tool and index. No per-call lookups.

## Index lifecycle

Each index inherits `GenericTxtIndex` (`indexes/base.py`):

- **In-process state**: `self._by_file` (relpath → records) and `self._by_key`
  (id → record) dicts, lazily built on first call.
- **Persistent state**: `<cache_dir>/v<N>/<name>.manifest.json` plus either
  `<name>.data.json` or, for sharded indexes (localisation), one shard per file
  under `<name>.data/`. Warm refreshes patch the maps incrementally.
- **Staleness**: `StaleCheck` debounces re-stat for 2 seconds inside a single
  agent turn. Past that, `ensure_fresh()` stats the contributing files and
  diffs against the on-disk manifest.

Parallel rebuild via `ProcessPoolExecutor` is only used by the
`md-mcp build-index` CLI. The `serve` subcommand sets `MD_MCP_SERIAL_PARSE=1`
because forking from inside the MCP stdio loop deadlocks (workers inherit the
parent's stdin/stdout FDs).

See [`indexes.md`](./indexes.md) for the full design.

## AST cache

`paradox/ast_cache.py` keeps parsed files in a process-wide LRU so the tools that
re-read the same large file within one agent turn (`resolve_focus`,
`find_focuses` deep filters, `focus_graph`, `focus_layout`, `check_refs(tag=)`
via `analysis/scope.py`, and every `md://` resource that locates a block) parse
it once. `05_usa.txt` is 1.5 MB and costs ~700-850 ms to parse; a repeat read is
a dict lookup.

- **Key**: `(str(path), st_mtime_ns, st_size)` from `os.stat`. Editing a file
  changes the key, so the next call misses and re-parses; the stale entry ages
  out of the LRU. If `stat` fails the call falls through to a plain read and
  raises the `OSError` as before.
- **Capacity**: 32 files (`MD_MCP_AST_CACHE_SIZE`, `0` disables) and 8 MB of
  source text (`MD_MCP_AST_CACHE_BYTES`), whichever bound is hit first. An AST
  is roughly 30x its source size, so the byte bound caps resident trees at a few
  hundred MB even when a scope walk streams hundreds of files through the cache.
  A file larger than the byte bound is parsed but not cached.
- **Errors are not cached.** A `ParseError` propagates and the next call
  re-parses. `error_prefix` only decorates error messages and is not part of the
  key, so it never leaks between callers.
- **Shared and read-only**: callers get the same `Node` tree and text on every
  hit and must not mutate them. Lookups are guarded by a lock; parsing runs
  outside it, so two racing misses may both parse and the last write wins.
- Tests call `ast_cache.clear()` (an autouse fixture in `tests/conftest.py`).

This is separate from the persistent per-index caches below: those store
extracted records on disk across processes, the AST cache holds full trees in
memory for the lifetime of the server.

## Resources vs tools

| | Tools (`@mcp.tool()`) | Resources (`@mcp.resource()`) |
|---|---|---|
| Output | JSON dict | Raw text |
| URL | `tools/call` RPC | `md://focus/{id}` URI |
| Use when | Agent will act on structured fields | Agent will quote / rewrite verbatim |
| Example | `resolve_focus("ISR_x")` → `{file, line, x, y, ...}` | `md://focus/ISR_x` → `focus = { ... }` raw |

Resources share the index layer with tools — both call `focus_index.resolve(...)`.
Where they differ is the **slicing**: resources walk the source text by token
position to preserve original comments and whitespace, whereas tools return
the parsed projection from `schema.py`.

## Output budgets

The most-violated invariant if you're not paying attention. Cap is ~25K
tokens (~100 KB serialised JSON) per MCP call. Designed-around in three
layers:

1. **Default to summary** at the tool level — counts + sample, no full lists.
2. **Pagination** on opt-in detail — `limit` / `offset` (`paginate(...)`) or
   `detail` tier (`summary`/`ids`/`full`).
3. **`enforce_budget`** drops listed heavy keys if the result still exceeds
   `BUDGET_BYTES` (100 KB). Sets `size_truncated=True` + `<key>_dropped` so
   the caller knows.

The full pattern, including the focus_graph case study, is in
[`output-budgets.md`](./output-budgets.md).

## Why generators don't write files

A direct write from `generate_focus` would:

- Bypass the agent's Edit/Write tracking — the user sees no diff in chat.
- Bypass the harness's file-state model — Edit on the same file afterwards
  would fail "needs to be Read first".
- Make rollbacks invisible — the user has no checkpoint to revert to.

So generators always return `{"txt": "...", "loc_yml_keys": [...]}` and let
the agent's Edit/Write tools do the actual insertion. This keeps the user in
the loop.

## Where each subsystem lives

| Concern | Module |
|---|---|
| HOI4 paradox-script parsing | `src/md_mcp/paradox/` |
| Cache (in-process + on-disk) | `src/md_mcp/indexes/` |
| Validator wrapping | `src/md_mcp/validators/runner.py` |
| String generators (focus/event/…) | `src/md_mcp/generators/` |
| High-level queries | `src/md_mcp/analysis/` |
| Tool wrappers | `src/md_mcp/tools/` |
| MCP registration + CLI | `src/md_mcp/server.py` |
| Response shaping helpers | `src/md_mcp/util/response.py` |
| BOM-aware file IO | `src/md_mcp/util/encoding.py` |
| Mod-root discovery | `src/md_mcp/util/pathing.py` |
| Config | `src/md_mcp/config.py` |
| Raw text resources | `src/md_mcp/resources.py` |
