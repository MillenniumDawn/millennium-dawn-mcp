# CLAUDE.md — millennium-dawn-mcp

Instructions for AI agents (Claude Code and friends) working in this repo. It
is the MCP server that fronts the Millennium Dawn toolchain. Read this before
editing.

For an end-user / developer overview, see [`README.md`](./README.md). Deeper docs
live under [`docs/`](./docs/).

---

## What this repo is (and isn't)

`millennium-dawn-mcp` is a **Python FastMCP server** that wraps the existing
`Millennium-Dawn/tools/` validators and ports the `MD-VSCode-Utility-Tool`
paradox-script parser. It exposes:

- Tool and resource inventory: [`docs/tools.md`](./docs/tools.md).
- Resource templates use the `md://` URI scheme (`md://focus/{id}` etc.).

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
├── server.py     FastMCP instance + tool/resource registration + CLI
├── config.py     Settings dataclass; mod-root / vanilla / cache discovery
├── resources.py  md:// resource handlers (raw text)
├── paradox/      Ported HOI4 parser, typed projections, parsed-AST cache
├── indexes/      Two-tier cache (in-process + persistent JSON), one module per kind
├── validators/   Runs Millennium-Dawn validate_*.py modules; issue attribution
├── generators/   Pure-string generators (focus/event/decision/idea/gfx/loc)
├── analysis/     Queries: focus graph and layout, refs, manifest, diff, issue delta
├── tools/        Tool implementations behind the @mcp.tool() wrappers
└── util/         Shared helpers: response budgets, encoding, pathing, line numbers,
                  process groups, git, thread offload, upstream module loading
```

Shared code goes in `util/`; there is no separate `lib/`.

Tests live in `tests/`, one flat directory. Integration tests are marker-gated
(`@pytest.mark.integration`, skipped unless `MD_MOD_ROOT` points at a real
checkout). Helpers used by more than one test module live in `tests/support/`;
test modules do not import from each other.

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
refactor in `Millennium-Dawn/tools` can break us. The import/read sequence
lives in one place: `_collect` in `runner.py`. `_run_inprocess` calls it
directly and the isolated child (`_shim.py`) calls it after the exec. Patch it
there and consider whether the change should also tolerate older
`Millennium-Dawn` checkouts.

`Issue.file` is not uniform: mod-relative path, bare basename, `""`, or the
literal `"unknown"`, sometimes several within one validator. Anything keying on
it must go through `IssueAttributor`
([`validators/attribution.py`](./src/md_mcp/validators/attribution.py)), which
resolves by shape against the real file list — never compare `issue["file"]` to
a scope set directly.

To debug a validator failure with a real traceback, call `_collect` outside the
server; the snippet is in
[`docs/validators.md`](./docs/validators.md#debugging). `ValidatorRunner.run()`
catches every exception and returns `{ok: false, error}` in both modes;
`_collect` raises. `_collect` and `in_process` both deadlock under `serve` (see
rule 6), so `serve` overrides the mode back to isolated. `md-mcp doctor` only
prints settings; it runs no validators.

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

The bounded `ThreadPoolExecutor` in `md_mcp/util/offload.py` is thread-only and
is intentionally active under `md-mcp serve`; it does not use a process pool or
fork. Keep its worker and admission limits in place. Do not gate this offload on
`MD_MCP_SERIAL_PARSE`.

The mod validators are another fork source: many have pool-aware paths, and
actual pool creation is conditional on worker count and batch size. The shared
pool implementation is in `validator_common.py`, which we don't control. That's
why `ValidatorRunner`
defaults to `isolated` mode (runs each validator in a child via `_shim.py`) and
`serve` forces it — in-process validation hangs the server the same way. Don't
route validators through the in-process path from inside `mcp.run()`.

Server code under `src/` runs git only through `run_git` in
[`util/git.py`](./src/md_mcp/util/git.py). It closes stdin, so git can never
read the JSON-RPC stream, and it requires a timeout. Catch `GitError` and shape
the response at the call site. Test helpers that build fixture repos call git
directly.

### 7. `Node.children()` is a method, not an attribute

The Python AST diverges from the TS port in two places. `Node.children` is a
**method** (returns `[]` if value isn't a block, otherwise the block contents).
Earlier code wrote `for c in node.children:` and crashed with
`'method' object is not iterable`. Always call it: `node.children()`.

`Node` also keeps only `name_token` and `value_end_token`. The TS operator,
value-start, and value-attachment tokens are not stored: nothing read them and
each cost memory on every node. See [`docs/parser.md`](./docs/parser.md).

Similarly, `Token.start` is a Python **str index**, not a line number. To translate,
use `line_starts(text)` + `pos_to_line(pos, line_starts)` from
[`util/line_numbers.py`](./src/md_mcp/util/line_numbers.py).

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
6. **Document** it in [`docs/tools.md`](./docs/tools.md).

The skeleton and the longer checklist are in
[`docs/development.md`](./docs/development.md#adding-a-new-tool).

---

## Testing

Run the full unit suite:

```bash
pytest -q
```

About 20 s, no mod checkout needed.

Run the integration suite (requires a real `Millennium-Dawn/` checkout):

```bash
MD_MOD_ROOT=/path/to/Millennium-Dawn pytest -m integration
```

Lint and type-check before committing (pre-commit runs ruff automatically;
mypy is CI-only):

```bash
ruff check . && ruff format --check . && mypy
```

Before claiming an optimisation works, **also probe the live MCP protocol**
end-to-end against a real mod — unit tests serialise smaller fixtures than
production data. Script the probe with `mcp.client.stdio` + `pytest`.
The ISR focus_graph probe in the git history is a useful template.

---

## Configuration knobs (env vars)

The env-var table is in [`README.md`](./README.md#configuration). Add new
variables there, not here. Config-file equivalents live in
`~/.config/md-mcp/config.toml`. CLI flag > env > file > computed default.

Two matter while working on the server: `MD_MCP_SERIAL_PARSE=1` disables the
parser process pool (`serve` sets it; see rule 6), and
`MD_MCP_VALIDATOR_MODE=in_process` is unsafe under `serve`.

---

## Commits in this repo

Standard project conventions: meaningful imperative subject, no
`Co-Authored-By` lines (per the workspace `CLAUDE.md`), no `--no-verify`.

Pre-commit is wired (`pre-commit install` after cloning): hygiene hooks plus
`ruff check --fix` and `ruff format`. Mypy runs in CI (`.github/workflows/ci.yml`),
not in the hook. Run `pytest -q` yourself; it isn't a hook either.

---

## When something looks wrong

1. **First** — read [`docs/architecture.md`](./docs/architecture.md). Most
   confusion comes from not knowing the parser → index → tool layering.
2. The parser is a **direct port** of `hoiparser.ts`. When the AST disagrees
   with vanilla content, read the TS source
   (`MD-VSCode-Utility-Tool/src/hoiformat/hoiparser.ts`) and confirm the
   behaviour there before changing the Python side. The string token is the
   one deliberate parsing difference, and `Node` stores fewer tokens; see
   [`docs/parser.md`](./docs/parser.md).
3. The index cache is **stat-based**, not content-based. If an index seems
   stale, blow away `<mod_root>/.md-mcp-cache/` and rerun
   `md-mcp build-index`.
4. The MCP framing layer can swallow exceptions raised inside a `@mcp.tool()`.
   When debugging mid-call failures, run the tool directly; the snippet is in
   [`docs/development.md`](./docs/development.md#direct-tool-calls-bypass-mcp).

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
