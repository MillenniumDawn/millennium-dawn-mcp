# millennium-dawn-mcp

Model Context Protocol server for Millennium Dawn (Hearts of Iron IV) mod
development. Lets a coding agent (Claude Code, Cursor, Continue, …) resolve
focus IDs, localisation keys, sprite references, event chains, and validator
output structurally — without re-`grep`ing the codebase on every turn.

The server is **read-only**: generators return paradox-script fragments as
strings, and the agent writes them into place via its own Edit/Write tools so
diffs stay visible to the user.

- Wraps the validators in `Millennium-Dawn/tools/validation/` (auto-discovered,
  26 at last count).
- Ports the paradox parser from `MD-VSCode-Utility-Tool/src/hoiformat/`.
- 26 tools and 6 `md://` resources.

For agents working on the server itself: see [`CLAUDE.md`](./CLAUDE.md).

---

## Install

```bash
cd /path/to/millennium-dawn-mcp
pip install -e .
```

Requires Python 3.10+. The wrapped validators are stdlib-only; no extra deps
needed. For working on the server itself, install the dev extras instead:

```bash
pip install -e '.[dev]'
pre-commit install
```

Verify the install:

```bash
md-mcp doctor --mod-root /path/to/Millennium-Dawn
# mod_root:       /Users/.../Millennium-Dawn
# vanilla_path:   /Users/.../Hearts of Iron IV
# cache_dir:      /Users/.../Millennium-Dawn/.md-mcp-cache
# validator_mode: in_process
# default_lang:   en
```

Prime the indexes (cold build is ~6 s on a modern Mac, ~30 s under fully
serial parsing):

```bash
md-mcp build-index --mod-root /path/to/Millennium-Dawn
```

---

## Register with Claude Code

User scope (one-off, persists across projects):

```bash
claude mcp add md-mcp --scope user -- md-mcp serve \
    --mod-root /path/to/Millennium-Dawn
```

Project scope (commit a `.mcp.json` to the mod repo so teammates pick it up):

```json
{
  "mcpServers": {
    "md-mcp": {
      "command": "md-mcp",
      "args": ["serve"],
      "env": { "MD_MOD_ROOT": "${workspaceFolder}" }
    }
  }
}
```

Confirm it's healthy:

```bash
claude mcp list
```

---

## Configuration

Settings are resolved in this precedence order:

1. CLI flag (`--mod-root ...`)
2. Environment variable (`MD_MOD_ROOT=...`)
3. `~/.config/md-mcp/config.toml`
4. Auto-discovery: walk up from `cwd` for a directory containing **both**
   `descriptor.mod` and `tools/validation/`; then check `cwd/Millennium-Dawn/`
   and `../Millennium-Dawn/`.

Vanilla HOI4 (`HOI4_PATH`) is **opt-in**. When set, indexes include vanilla
content; when unset, validators that reference vanilla emit warnings, not
errors.

Full env-var reference:

| Variable | Purpose |
|---|---|
| `MD_MOD_ROOT` | Path to the `Millennium-Dawn/` checkout. |
| `HOI4_PATH` | Path to the vanilla `Hearts of Iron IV/` install (optional). |
| `MD_MCP_CACHE_DIR` | Override the cache location (use this for read-only checkouts). |
| `MD_MCP_VALIDATOR_MODE` | `in_process` (default, fast) or `subprocess` (isolated). |
| `MD_MCP_DEFAULT_LANG` | Default loc language for `resolve_loc` (defaults to `en`). |

Example `~/.config/md-mcp/config.toml`:

```toml
mod_root      = "/Users/me/Programming/MD/Millennium-Dawn"
hoi4_path     = "/Users/me/Programming/MD/Hearts of Iron IV"
validator_mode = "in_process"
default_lang   = "en"
```

---

## CLI commands

```bash
md-mcp doctor       # Print resolved configuration and exit
md-mcp build-index  # Build all indexes and exit (good before first `serve`)
md-mcp serve        # Run the MCP server on stdio
```

`serve` is what the MCP client invokes. Run `doctor` and `build-index` by
hand for setup / cache priming.

---

## Tool & resource catalogue

26 tools, 6 resources. Full reference in [`docs/tools.md`](./docs/tools.md).

### Resolvers — "where is X defined?"

`resolve_focus`, `resolve_loc`, `resolve_sprite`, `resolve_event`,
`resolve_decision`, `resolve_idea`, `list_country_content`.

### Parsers — direct AST

`parse_file` (refuses files over `max_bytes`; `top_level_only` for skim mode),
`parse_string` (snippets).

### Validation

`validate` (one validator or all), `validate_list`, `lint` (the whole linting
suite on changed files; `validators=["auto"]` folds in domain-matched
validators), `review_branch`, `check_encoding`.

### Analysis

`find_focuses`, `find_references` (paginated; `files_only` mode collapses to
a unique file list), `focus_graph` (tiered `summary`/`ids`/`full`/`paths`),
`check_refs` (scoped dangling-reference audit), `focus_layout` (grid
collisions and relative-position chains), `diff_summary` (kind-filterable,
`with_ids` opt-out for speed).

### Generators

`generate_focus`, `generate_event`, `generate_decision`, `generate_idea`,
`generate_gfx_entry`, `generate_loc_stub`. All return strings; never write.

### Resources

`md://focus/{id}`, `md://event/{id}`, `md://decision/{id}`, `md://idea/{id}`,
`md://loc/{key}`, `md://sprite/{name}`. Raw text for direct quotation.

---

## Output budgets

Every list-returning tool defaults to small, summarised output. The
`focus_graph(tag="ISR")` call originally returned 407 KB and broke MCP clients;
it now returns 1.2 KB by default and tops out at 99 KB when you ask for full
detail. See [`docs/output-budgets.md`](./docs/output-budgets.md) for the
pattern.

Quick rule for callers:

- **Start with the default**. Counts + sample tell you whether the tool is
  worth running with more detail.
- **Then escalate** via `detail="ids"`, `detail="full"`, `include=[...]`,
  `limit=...`, or `focus_ids=[...]` to pin a subset.

---

## Testing

```bash
pytest -q                       # unit suite, sub-second, no checkout needed
MD_MOD_ROOT=... pytest -m integration   # real-mod integration suite
```

Lint and type-check the server code:

```bash
ruff check . && ruff format --check . && mypy
```

---

## Documentation

- [`CLAUDE.md`](./CLAUDE.md) — instructions for AI agents working on the server
- [`docs/architecture.md`](./docs/architecture.md) — module map + request flow
- [`docs/tools.md`](./docs/tools.md) — tool & resource reference
- [`docs/output-budgets.md`](./docs/output-budgets.md) — the response-shaping pattern
- [`docs/indexes.md`](./docs/indexes.md) — two-tier cache design
- [`docs/parser.md`](./docs/parser.md) — paradox parser notes
- [`docs/validators.md`](./docs/validators.md) — validator wrapping caveats
- [`docs/development.md`](./docs/development.md) — contributing
