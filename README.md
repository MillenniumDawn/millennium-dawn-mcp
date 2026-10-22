# millennium-dawn-mcp

Model Context Protocol server for Millennium Dawn (Hearts of Iron IV) mod
development. Lets a coding agent (Claude Code, Cursor, Continue, …) resolve
focus IDs, localisation keys, sprite references, event chains, and validator
output structurally — without re-`grep`ing the codebase on every turn.

The server is **read-only**: generators return paradox-script fragments as
strings, and the agent writes them into place via its own Edit/Write tools so
diffs stay visible to the user.

- Wraps validators discovered in `Millennium-Dawn/tools/validation/` at runtime.
- Ports the paradox parser from `MD-VSCode-Utility-Tool/src/hoiformat/`.
- See [`docs/tools.md`](./docs/tools.md) for the current registered tool and
  resource catalogue.

For agents working on the server itself: see [`CLAUDE.md`](./CLAUDE.md).

---

## Install

```bash
cd /path/to/millennium-dawn-mcp
pip install -e .
```

Requires Python 3.10+. The wrapped validators are stdlib-only; no extra deps
needed. `render_focus_tree` needs Pillow for PNG output and focus icons; without
it the tool still writes HTML with placeholder icons:

```bash
pip install -e '.[render]'
```

For working on the server itself, install the dev extras instead (they include
Pillow):

```bash
pip install -e '.[dev]'
pre-commit install
```

Verify the install:

```bash
md-mcp doctor --mod-root /path/to/Millennium-Dawn
# mod_root:       /Users/.../Millennium-Dawn
# submod_root:    (not configured)
# vanilla_path:   (not detected)
# cache_dir:      /Users/.../Millennium-Dawn/.md-mcp-cache
# validator_mode: isolated
# default_lang:   en
# loc_langs:      en
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

Project scope: drop a `.mcp.json` in the mod repo so every agent session there
picks it up automatically:

```json
{
  "mcpServers": {
    "md": {
      "command": "md-mcp",
      "args": ["serve"]
    }
  }
}
```

Notes:

- **Name the server `md`** if the mod repo already pre-approves tools as
  `mcp__md__*` (Millennium-Dawn's `.claude/settings.local.json` does). Using a
  different name silently bypasses those permissions.
- **Don't set `MD_MOD_ROOT` from a `"/path"` env string.** The server
  auto-discovers the mod by walking up from `cwd` (the project root), so it
  works with no env block at all. If you do want to pin the path, set
  `MD_MOD_ROOT` in the environment the client launches with, or use
  `~/.config/md-mcp/config.toml`.
- **`${workspaceFolder}` is a VS Code macro, not an env var.** Claude Code
  does not expand it — a `.mcp.json` using it reports "Missing environment
  variables: workspaceFolder" and the server starts with `MD_MOD_ROOT` unset.
  It belongs in a VS Code `.vscode/mcp.json` if you use that editor, not in
  the Claude project file.
- **`.mcp.json` is gitignored in Millennium-Dawn**, so the project file there
  is local-only. It won't be committed or shared with teammates via git; they
  need to create their own (or use the user-scope registration above).

Confirm it's healthy (run from the mod repo root so the project scope loads):

```bash
claude mcp list
# md: md-mcp serve - ✔ Connected
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
| `MD_MCP_SUBMOD_ROOT` | Optional submod/worktree overlay; matching files take precedence over the mod root. The default cache moves under it. |
| `HOI4_PATH` | Path to the vanilla `Hearts of Iron IV/` install (optional; doubles cold-build time). |
| `MD_MCP_CACHE_DIR` | Override the cache location (use this for read-only checkouts). |
| `MD_MCP_VALIDATOR_MODE` | `isolated` (default) or `in_process` (faster outside the stdio server; `serve` forces `isolated`). |
| `MD_MCP_DEFAULT_LANG` | Default loc language for `resolve_loc` (defaults to `en`). |
| `MD_MCP_LOC_LANGS` | Loc languages to index, e.g. `en,de` or `*` for all (defaults to `default_lang` plus English, which is always included). Other languages still resolve through a slower on-demand scan. Each language set has its own cache (`loc-en`, `loc-de_en`). |
| `MD_MCP_SERIAL_PARSE` | `1` forces serial parsing. `md-mcp serve` sets it. |
| `MD_MCP_TEXT_CACHE_BYTES` | Bound on decoded text held by `find_references`' in-memory file cache, measured as the strings cost in memory (default 134217728 = 128 MB; `0` disables). Once full, further files are read but not cached. |
| `MD_MCP_AST_CACHE_SIZE` | Parsed-AST LRU capacity in files (default 32, `0` disables). |
| `MD_MCP_AST_CACHE_BYTES` | Bound on source text held by the parsed-AST cache (default 8000000). Resident cost is ~23x that: 8 MB of source is ~184 MB of trees on the real mod. |

Example `~/.config/md-mcp/config.toml`:

```toml
mod_root      = "/Users/me/Programming/MD/Millennium-Dawn"
submod_root   = "/Users/me/Programming/MD/my-submod"
hoi4_path     = "/Users/me/Programming/MD/Hearts of Iron IV"
validator_mode = "isolated"
default_lang   = "en"
loc_langs      = "en"   # or ["en", "de"], or "*"
```

### Submods and worktree overlays

Set `MD_MCP_SUBMOD_ROOT` (or `submod_root` in the TOML config) to an existing
overlay directory. Files are resolved submod-first, then mod, then vanilla; the
default cache is stored under the overlay. For a project-local setup:

```json
{
  "mcpServers": {
    "md": {
      "command": "md-mcp",
      "args": ["serve", "--submod-root", "/path/to/my-submod"]
    }
  }
}
```

Validators intentionally remain base-only and run against `MD_MOD_ROOT`; use
lint/diff tools for submod worktree changes.

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

[`docs/tools.md`](./docs/tools.md) lists every registered tool and resource,
grouped as resolvers, parsers, script documentation, validation, analysis,
generators, and `md://` resources.

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
pytest -q                       # unit suite, about 20 s, no checkout needed
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
