# CLAUDE.md — millennium-dawn-mcp

Instructions for AI agents (Claude Code and friends) working in
**`/Users/matthewscott/Programming/MD/millennium-dawn-mcp/`**. The repo is the MCP
server that fronts the Millennium Dawn toolchain. Read this before editing.

For an end-user / developer overview, see [`README.md`](./README.md). Deeper docs
live under [`docs/`](./docs/).

---

## What this repo is (and isn't)

`millennium-dawn-mcp` is a **Python FastMCP server** that wraps the existing
`Millennium-Dawn/tools/` validators and ports the `MD-VSCode-Utility-Tool`
paradox-script parser. It exposes:

- **24 tools** (`resolve_*`, `find_*`, `parse_*`, `validate*`, `generate_*`,
  `focus_graph`, `diff_summary`, `check_encoding`, `lint`, `review_branch`,
  `list_country_content`)
- **6 resources** under the `md://` URI scheme (`md://focus/{id}` etc.)

It is **read-only** by design. Generators return content as strings; the agent
writes via Edit/Write so the user sees diffs in the conversation. The server
must never call `os.write`/`open(..., "w")` against mod files.

It is a **sibling** of `Millennium-Dawn/`, `MD-VSCode-Utility-Tool/`, and
`focus-tree-creation-tool/` — not a monorepo entry. See the workspace
[`../CLAUDE.md`](../CLAUDE.md) for the surrounding context.

---

## Repo layout

```
src/md_mcp/
├── server.py            FastMCP instance + tool/resource registration + CLI
├── config.py            Settings dataclass; mod-root / vanilla / cache discovery
├── resources.py         md:// resource handlers (raw text streaming)
├── paradox/             Ported HOI4 parser
│   ├── lexer.py         Token regexes (verbatim port of hoiparser.ts)
│   ├── parser.py        Recursive-descent
│   ├── nodes.py         Node, Token, SymbolNode dataclasses
│   ├── schema.py        Typed projections (focus/event/decision/idea/sprite)
│   └── writer.py        AST → text (used by generators)
├── indexes/             Two-tier cache (in-process + persistent JSONL)
│   ├── base.py          GenericTxtIndex, IndexCache, staleness checking
│   └── {focus,event,decision,idea,localisation,gfx}.py
├── validators/runner.py Wraps Millennium-Dawn validate_*.py modules in-process
├── generators/          Pure-string generators (focus/event/decision/idea/gfx/loc)
├── analysis/            High-level queries
│   ├── focus_graph.py   Prereq/mutex DAG with tiered detail
│   ├── refs.py          Find-all-references over scan dirs
│   ├── manifest.py      Per-country file/ID inventory
│   ├── diff_summary.py  git diff + AST-level ID diff
│   └── encoding.py      BOM compliance check
├── tools/               Thin @mcp.tool() wrappers
└── util/
    ├── response.py      paginate, enforce_budget, clip_strings, BUDGET_BYTES
    ├── encoding.py      BOM-aware read_text
    └── pathing.py       mod_root / vanilla discovery
```

Tests live in `tests/`. Integration tests (gated on `MD_MOD_ROOT`) live in
`tests/integration/`.

---

## Non-negotiables

These rules are stricter than usual because the server runs in the agent's
inner loop. Breaking them produces **silent agent failures**, not just bad UX.

### 1. Every list-bearing tool must enforce an output budget

MCP clients cap per-call output at ~25K tokens (~100 KB). A single oversized
response fails the call and pollutes the recovery loop. Every tool that returns
a list MUST:

1. Default to small / summarised output (counts + samples).
2. Accept `limit` + `offset` (or `detail` tier + `include`).
3. Report `total` and `truncated` so the caller knows what they're missing.
4. Wrap the final dict in `enforce_budget(result, heavy_keys=(...))` as a
   last-line defence.

See [`docs/output-budgets.md`](./docs/output-budgets.md). The pattern lives in
[`src/md_mcp/util/response.py`](./src/md_mcp/util/response.py). `BUDGET_BYTES =
100_000`. The case study that drove this design is `focus_graph(tag="ISR")`,
which originally returned 407 KB / 17 K lines.

### 2. Generators return strings — never write files

The server is read-only against the mod. Generators in `src/md_mcp/generators/`
return `{"txt": "...", "loc_yml_keys": [...]}`. The agent uses Edit/Write to
place content. **Do not add `open(path, "w")` or `path.write_text(...)` to
generators or tools.**

Rationale: direct writes bypass the agent's normal file-state tracking and
hide diffs from the user. The `.md-mcp-cache/` write paths in
`indexes/base.py` are the only legitimate writes, and they go into a cache
directory the user can blow away safely.

### 3. Don't touch `Hearts of Iron IV/` (the vanilla install)

`vanilla_path` in `Settings` is **read-only reference material**, like in the
workspace root rule. Indexes may read it; nothing else does. Never enumerate
it for edits, copies, or commits.

### 4. The validator API is brittle

`ValidatorRunner` imports `Millennium-Dawn/tools/validation/validate_*.py` and
reads `validator._issues` — that underscore means it's not a public API. A
refactor in `Millennium-Dawn/tools` can break us. When the validator runner
breaks, **fix it in `runner.py` only** (single adapter point) and consider
whether the change should also tolerate older `Millennium-Dawn` checkouts.

If you need to hand the user a workaround, the env-var fallback is:

```bash
MD_MCP_VALIDATOR_MODE=subprocess md-mcp serve --mod-root ...
```

See [`docs/validators.md`](./docs/validators.md).

### 5. BOM rules on emitted files

When generators produce `.yml` localisation snippets, pass `bom_prefix=True`
for **new** files; existing-file inserts don't get a BOM (the file already has
one). `.txt` script files must never get a BOM. The `check_encoding` tool
catches violations after the fact.

### 6. Server stdio + forking don't mix

The MCP server runs on stdio. Forking from inside that loop deadlocks because
worker processes inherit the parent's stdin/stdout. `server.py` sets
`MD_MCP_SERIAL_PARSE=1` in the `serve` subcommand specifically to disable the
parser process pool. The pool is only used by `md-mcp build-index` (the CLI
sub-command that primes caches before the server starts).

**If you add work that uses `concurrent.futures` or `multiprocessing`**, make
sure it's gated on `MD_MCP_SERIAL_PARSE` and never reaches a fork inside
`mcp.run()`.

### 7. `Node.children()` is a method, not an attribute

The Python AST diverges from the TS port in one place: `Node.children` is a
**method** (returns `[]` if value isn't a block, otherwise the block contents).
Earlier code wrote `for c in node.children:` and crashed with
`'method' object is not iterable`. Always call it: `node.children()`.

Similarly, `Token.start` is a **byte offset**, not a line number. To translate,
use `_line_starts(text)` + `_pos_to_line(pos, line_starts)` from
[`tools/parser_tools.py`](./src/md_mcp/tools/parser_tools.py).

---

## How to add a new tool

1. **Implementation** lives under `src/md_mcp/analysis/` (queries) or
   `src/md_mcp/tools/` (thin tool layer) — never directly in `server.py`.
2. **Default to summary output.** First-cut behaviour: return counts +
   small sample. Add `include`/`detail`/`limit` params for the opt-in full
   list.
3. **Always wrap returns in `enforce_budget`** with the heavy keys listed in
   drop order (most-likely-too-big first).
4. **Register in `server.py`** with `@mcp.tool()` and a one-line description.
   That description shows up in the agent's prompt **every turn**; it's
   prompt budget, so keep it tight and informative (mention the key knobs).
5. **Tests** under `tests/test_<name>.py` with at least: signature guard,
   small-output happy path, and one truncation case.

Example skeleton:

```python
def my_query(mod_root, *, detail="summary", limit=100, offset=0) -> dict:
    raw = _expensive_scan(mod_root)
    sliced, truncated, total = paginate(raw, offset, limit)
    return enforce_budget(
        {
            "ok": True,
            "detail": detail,
            "total": total,
            "returned": len(sliced),
            "truncated": truncated,
            "items": sliced if detail != "summary" else [],
        },
        heavy_keys=("items",),
    )
```

---

## Testing

Run the full unit suite:

```bash
pytest -q
```

92 tests, ~0.5 s. No mod checkout needed.

Run the integration suite (requires a real `Millennium-Dawn/` checkout):

```bash
MD_MOD_ROOT=/path/to/Millennium-Dawn pytest -m integration
```

Run parser parity tests against the TypeScript implementation (optional):

```bash
pytest -m differential
```

Before claiming an optimisation works, **also probe the live MCP protocol**
end-to-end against a real mod — unit tests serialise smaller fixtures than
production data. Script the probe with `mcp.client.stdio` + `pytest`.
The ISR focus_graph probe in the git history is a useful template.

---

## Configuration knobs (env vars)

| Env var | Effect |
|---|---|
| `MD_MOD_ROOT` | Path to the `Millennium-Dawn/` checkout. Required when not auto-discovered. |
| `HOI4_PATH` | Path to vanilla `Hearts of Iron IV/`. Optional; doubles cold-build time. |
| `MD_MCP_CACHE_DIR` | Override `.md-mcp-cache/` location (use for read-only checkouts). |
| `MD_MCP_VALIDATOR_MODE` | `in_process` (default) or `subprocess`. |
| `MD_MCP_DEFAULT_LANG` | Default loc language for `resolve_loc` (default `en`). |
| `MD_MCP_SERIAL_PARSE` | `1` forces serial parsing — auto-set by `md-mcp serve`. |

Config-file equivalents in `~/.config/md-mcp/config.toml`. CLI flag > env >
file > computed default.

---

## Commits in this repo

Standard project conventions: meaningful imperative subject, no
`Co-Authored-By` lines (per the workspace `CLAUDE.md`), no `--no-verify`.

Pre-commit isn't wired here yet; the only check is `pytest` and a manual
`pip install -e .` to make sure the entry point still resolves.

---

## When something looks wrong

1. **First** — read [`docs/architecture.md`](./docs/architecture.md). Most
   confusion comes from not knowing the parser → index → tool layering.
2. The parser is a **direct port** of `hoiparser.ts`. When the AST disagrees
   with vanilla content, run `pytest -m differential` to confirm against the
   TS implementation before changing the port.
3. The index cache is **stat-based**, not content-based. If an index seems
   stale, blow away `<mod_root>/.md-mcp-cache/v1/` and rerun
   `md-mcp build-index`.
4. The MCP framing layer can swallow exceptions raised inside a `@mcp.tool()`.
   When debugging mid-call failures, run the tool directly:
   ```python
   from md_mcp.config import load
   from md_mcp.indexes import FocusIndex
   from md_mcp.analysis.focus_graph import focus_graph
   s = load("/path/to/Millennium-Dawn")
   focus_graph("ISR", s.mod_root, FocusIndex(s.mod_root, s.cache_dir, s.vanilla_path))
   ```

---

## Pointers

- [`README.md`](./README.md) — install / register / quickstart
- [`docs/architecture.md`](./docs/architecture.md) — module + data flow
- [`docs/tools.md`](./docs/tools.md) — tool & resource reference
- [`docs/output-budgets.md`](./docs/output-budgets.md) — the budget pattern
- [`docs/indexes.md`](./docs/indexes.md) — cache design
- [`docs/parser.md`](./docs/parser.md) — paradox parser notes
- [`docs/validators.md`](./docs/validators.md) — validator wrapping
- [`docs/development.md`](./docs/development.md) — contributor flow
