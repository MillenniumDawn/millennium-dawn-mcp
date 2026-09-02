# Changelog

## 1.0.0 - 2026-09-02

First tagged release. Cumulative changes from the initial commit through PR #120. Read-only MCP
server for Millennium Dawn HOI4 mod development: 28 tools and 6 `md://` resources that resolve
identifiers, audit cross-references, parse and lint content, and scaffold new blocks for the agent
to write.

### Added

- 28 read-only tools and 6 resources (`md://focus/{id}`, `md://loc/{key}`, `md://sprite/{name}`,
  `md://event/{event_id}`, `md://decision/{decision_id}`, `md://idea/{idea_id}`).
- Paradox parser ported from `MD-VSCode-Utility-Tool/src/hoiformat/hoiparser.ts` (lexer,
  recursive-descent parser, AST dataclasses,
  schema projections for focus/event/decision/idea/sprite).
- Generators that return content as strings: `generate_focus`, `generate_event`,
  `generate_decision`, `generate_idea`, `generate_gfx_entry`, `generate_gfx_merge`,
  `generate_loc_stub`.
- Indexes and caches for focus, event, decision, idea, localisation, and GFX, with a per-index
  `(mtime_ns, size)` manifest and an atomic JSON write path.
- Vanilla fallback for sprites: `resolve_sprite` and `check_refs` answer vanilla-only sprites from
  the mod's committed `tools/validation/vanilla_sprites.txt` manifest when `HOI4_PATH` is unset.
- CLI subcommands via `md-mcp`: `serve`, `build-index`, `doctor`. `Settings` reads CLI flags, env
  (`MD_MOD_ROOT`, `HOI4_PATH`, `MD_MCP_CACHE_DIR`, `MD_MCP_VALIDATOR_MODE`, `MD_MCP_DEFAULT_LANG`,
  `MD_MCP_SERIAL_PARSE`), and `~/.config/md-mcp/config.toml`.
- Validator wrapping: `ValidatorRunner` runs each `validate_*.py` in an isolated child process by
  default to avoid forking the MCP stdio loop. `IssueAttributor` resolves non-uniform `Issue.file`
  (empty, basename, mod-relative, `"unknown"`) against the real file list.
- Validator auto-routing: `VALIDATOR_AUTO_MAP` selects validators from a file scope; nightly drift
  snapshots guard against upstream routing changes.
- Analysis tools: `find_focuses`, `find_references`, `list_country_content`, `focus_graph` (summary,
  ids, full, paths tiers), `check_refs`, `focus_layout`, `diff_summary`, `check_encoding`.
- `check_equipment_variant`: audits a `create_equipment_variant` block against current hull slots
  and module categories, paginated by `limit`/`offset`.
- Output-budget system: `enforce_budget` measures UTF-8 byte length of `json.dumps` output (not
  character count) and trims heavy keys until the ceiling is met; `paginate`, `clip_strings` (UTF-8
  byte aware), and per-tool `total`/`returned`/`truncated` fields.
- Lint: `mode=changed|staged|all`, `checks=...` subsets scripts, `validators=...` runs
  domain-matched mod validators on the scope (`auto`, `*`, or explicit names), with `unattributed`
  issues surfaced instead of dropped.
- GenericTxtIndex shared across focus, event, decision, idea, localisation, and GFX indexes,
  including multi-directory scanning and tuple-key record support.
- Focus index reports partial search status with skipped-file/skipped-record diagnostics.
- Resource handlers anchor matches to the indexed AST node (decision, idea, event, sprite) so a
  nested block sharing the requested ID cannot be returned.
- Path containment consolidated into `util.pathing.contained` and `validate_user_path`; every
  user-supplied source read goes through a containment check.
- GitHub Actions: per-PR Ruff/mypy/test matrix and a nightly integration run against
  `Millennium-Dawn` main that files a deduped issue on failure. Workflows pin GitHub Actions
  by SHA.
- Changed-line coverage gate via `diff-cover` in CI.
- Pre-commit: hygiene hooks plus Ruff check/format. Fixtures excluded (Paradox-format files).

### Changed

- `validate` strict mode applied to single-validator runs and folded through per-validator
  breakdowns in the run-all path, so top-level and per-validator counts reconcile.
- `lint` deduplicates the script-runner code paths; four dead lint wrappers (`lint_braces_tool`,
  `lint_basic_style_tool`, `lint_basic_style_2_tool`, `lint_coding_standards_tool`) removed after
  the upstream validators they shelled out to were consolidated.
- `diff_summary` hardened against option-like `base` values, rename ID mismatches, and uses
  real-repo regression tests.
- `focus_layout` clamps negative `limit` to 0 at entry and routes `include_positions` through
  `paginate()` for consistent edge-case semantics.
- Index rebuild uses deterministic file iteration; duplicate-ID shadowing during rebuild now logs
  a warning naming the duplicate key and files.
- `EVENT_KINDS` and `SPRITE_KINDS` are canonical tuples in `paradox/schema.py`; `SEVERITY_RANK`
  lives in `validators/__init__.py`.
- Lexer line/column calculations routed through `util/line_numbers`; offset-to-line conversion
  clamps negative positions.
- `validate_mios` auto-routing widened to cover upstream's expanded scan domain (MIO tree,
  company traits, equipment stats, MIO policies).
- Mod-encoding lint check skips cleanly when no `.mod` files exist, matching the empty-scope skip
  shape used by scoped modes.
- Collection type annotations modernised (`list[...]`, `dict[...]`, `tuple[...]`) across the
  package.
- MCP registration docs updated for Claude Code project-scope setup.
- `py.typed` marker added; `tomli` import narrowed to Python <3.11.

### Fixed

- `parse_file` reads restricted to `mod_root` and `vanilla_path` with containment, regular-file,
  and `.txt`/`.gfx` extension checks.
- GFX scanner restricted to sprite containers so nested impostor blocks never enter the index.
- `enforce_budget` now measures UTF-8 bytes (not JSON character length); unserializable payloads
  return a bounded error response instead of being swallowed.
- `check_refs` now audits tags whose focus files live in the vanilla install (previously reported
  "not found").
- `focus_graph` paths tier flags `estimate_unreliable` on nodes inside a prerequisite cycle and
  counts unparsable string costs.
- Fileless validator issues (some events/decisions validators) surfaced as `unattributed` instead
  of dropped under scoped lint runs.
- `_looks_like_slot_wrapper` no longer misclassifies empty or unknown-property ideas.
- Resource handlers anchor to the indexed AST node (decision, idea, event, sprite), rejecting
  ambiguous matches.
- Malformed focus blocks raise in `resources.py` instead of returning an empty body.
- Index cache rebuilds on a wrong-shape manifest instead of raising `TypeError`/`ValueError` out
  of `load_manifest`.
- `find_references` event regex built from `EVENT_KINDS`; boundary and nonempty cases covered.
- Equipment variant fixture refreshed against current upstream modules and hull slots.
- Pathing tests isolated from `MD_MOD_ROOT` so the default unit suite is hermetic.

### Development

- Ruff 0.8.6 pinned in `dev` extras with `line-length = 100`, `target-version = "py310"`, and the
  `B/E/F/I/RUF/SIM/UP006/W` selection.
- Mypy checks `src` and `tests` with `warn_redundant_casts` and `warn_unused_ignores` enabled;
  `pytest`, `yaml`, and `mcp` typed-stubs ignored.
- Pre-commit hooks include Ruff check/format; mypy runs in CI only.
- `uv.lock` tracked for reproducible installs.
- Python support: `>=3.10`.
- Nightly integration workflow runs against `Millennium-Dawn` main with a drift snapshot that
  fails on routing changes; validator routing refresh lands via snapshot regen.
- Validator routing and lint snapshot tests catch upstream drift before merge.
