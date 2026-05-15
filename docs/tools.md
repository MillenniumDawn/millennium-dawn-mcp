# Tool & Resource Reference

24 tools and 6 resources, grouped by purpose. Output shapes show the
**default** behaviour — most tools have detail-tier or `limit` knobs.

All tools return either `{"ok": True, ...}` or `{"ok": False, "error": "..."}`.
Single-record resolvers add the target id/key to the error response so the
agent can correlate without remembering its own arguments.

Every list-returning tool is **output-budget aware**: see
[`output-budgets.md`](./output-budgets.md). The defaults below are designed
to fit in <5 KB; you opt into bigger responses explicitly.

---

## Resolvers — "where is X defined?"

### `resolve_focus(focus_id: str) -> dict`

Look up a focus by ID and return file/line plus parsed metadata.

```json
{
  "ok": true,
  "id": "ISR_idf_modernization",
  "file": "common/national_focus/MD_ISR_focus.txt",
  "line": 1234,
  "kind": "focus",
  "parsed": {
    "x": 8, "y": 3, "cost": 10,
    "icon": "GFX_focus_generic_military",
    "prerequisites": [["ISR_armed_forces"]],
    "mutually_exclusive": ["ISR_pacifist_route"],
    "relative_position_id": "ISR_armed_forces"
  }
}
```

`prerequisites` is a list of OR-groups; each inner list is a set of prereqs
where any one satisfies that requirement.

### `resolve_loc(key: str, lang?: str) -> dict`

Look up a localisation key. Falls back to English if missing in the requested
language. Returns `{value, file, line, lang}`.

### `resolve_sprite(name: str) -> dict`

Look up a sprite by name (e.g. `GFX_focus_generic_military`). Returns
`{name, kind, texturefile, file, line}`. `kind` is `spriteType`,
`corneredTileSpriteType`, etc.

### `resolve_event(event_id: str) -> dict`

`event_id` is `namespace.number` (`isr.42`, not `42`). Returns `{kind,
namespace, file, line, file_namespaces}`. `file_namespaces` lists *every*
namespace declared in the file — useful when an event uses a namespace that
isn't declared at the top.

### `resolve_decision(decision_id: str) -> dict`

Returns `{id, category, file, line}`.

### `resolve_idea(idea_id: str) -> dict`

Returns `{id, category, slot, file, line}`. `slot` is the ideas group
(country, political_advisor, mobilization_laws, etc.).

### `list_country_content(tag: str, include?: list[str], limit_per_category?: int) -> dict`

Per-country manifest. **Default returns counts + 5-item samples only.** Pass
`include=["focuses", "events"]` for full lists of those categories, or
`include=["*"]` for everything. Each included category is capped at
`limit_per_category` (default 100).

Categories: `focuses`, `events`, `event_files`, `decisions`, `ideas`,
`loc_files`, `mio_files`, `history_files`, `oob_files`, `namelist_files`.

```json
{
  "ok": true,
  "tag": "USA",
  "counts": { "focuses": 712, "events": 401, "decisions": 56, ... },
  "focuses_sample": ["USA_alpha", "USA_beta", ...],
  "hint": "Returning counts + samples only. Pass include=['focuses',...]..."
}
```

---

## Parsers — direct AST

### `parse_file(path: str, max_bytes?: int, top_level_only?: bool) -> dict`

Parse a `.txt` paradox script file. `path` is absolute or relative to mod root.

- **`max_bytes=500_000`** (default) — files larger than this are refused with
  a pointer to the right `resolve_*` tool. Set to `0` to disable.
- **`top_level_only=True`** — returns only `{name, line}` per top-level node.
  A compact orienting view for big files (e.g. an entire `focus_tree`).

Default returns `{"ok": true, "path": ..., "size": ..., "root": <AST>}`.

### `parse_string(text: str) -> dict`

Parse a snippet of paradox script. Useful for validating a generator's output
before writing it.

Returns `{"ok": true, "root": <AST>}` or `{"ok": false, "error": ...}`.

The `<AST>` shape is a tagged-union JSON: every node is `{name, operator, value, line}`
where `value` is `null | str | num | {symbol: str} | [Node, ...]` (block) — see
[`parser.md`](./parser.md) for the full grammar.

---

## Validation

### `validate(validator?, staged_only?, files?, strict?, severity_min?, limit?, counts_only?) -> dict`

Run one validator or the full fast suite.

- **`validator`** — name from `validate_list` (`localisation`, `focus_id`,
  etc.). Omit to run all *fast* validators (slow `unused_scripted` and
  `unused_textures` are skipped by default — call them by name when you want them).
- **`staged_only=True`** — restrict to git-staged files. Much faster mid-edit.
- **`files=[...]`** — post-filter issues to ones in this set of paths.
- **`strict=True`** — treat warnings as errors in the summary counts.
- **`severity_min="info"`** — drop issues below this floor. `"info"`,
  `"warning"`, `"error"`.
- **`limit=500`** — cap issues returned (counts stay accurate). `-1` for no cap.
- **`counts_only=True`** — return just per-validator counts; skip the issues array.

Returns `{ok, validators, counts: {error, warning, info}, issues, issues_total_after_filter, truncated}`.

### `validate_list() -> dict`

Enumerate available validators with their titles.

```json
{
  "ok": true,
  "validators": [
    {"name": "localisation", "title": "Localisation Validator", "module": "validate_localisation"},
    {"name": "focus_id",     "title": "Focus ID Uniqueness", "module": "validate_focus_id"},
    ...
  ]
}
```

### `lint_common_mistakes(mode?: str, files?: list) -> dict`

Run `tools/linting/check_common_mistakes.py`: threat scale, scope expansion,
modifier validation. Modes: `staged` (default, fast) or `all`. Pass `files`
to override mode.

Returns `{ok, issues, count, mode, exit_code}` where each issue is
`{file, line, message, severity: "warning"}`.

### `review_branch(base?: str) -> dict`

Run `tools/analysis/review_branch.py` against `base` (default `main`).
Returns the script's full text report as `report`. The agent can quote
or extract sections.

### `check_encoding(files?: list) -> dict`

Verify BOM rules per `general-rules.md`:
- `.txt` files must have **no** BOM
- `localisation/*.yml` files **must** have a BOM

Pass `files` for a targeted scan; without it, walks the standard subdirs
(`common/`, `events/`, `history/`, `interface/`, `localisation/`).

Returns `{ok, checked, violations: [{file, expected, actual}], counts}`.

---

## Analysis

### `find_focuses(tag?, has_prereq?, mutex_with?, kind?, limit?) -> dict`

Predicate search over the focus index. Filters are AND-combined.

- `tag` — id starts with `<tag>_` (case-insensitive)
- `has_prereq` — focus lists this id in any prerequisite group
- `mutex_with` — focus lists this id in its `mutually_exclusive`
- `kind` — `focus_tree`, `shared_focus`, `joint_focus`
- `limit=200`

Returns `{ok, count, truncated, matches: [{id, file, line, kind}]}`.

### `find_references(kind, target, limit?, offset?, snippet_chars?, files_only?) -> dict`

Reverse-lookup: every place a focus / event / decision / idea / loc-key /
sprite / flag / variable is referenced.

- **`kind`** — one of `focus, event, decision, idea, loc, sprite, flag, variable`.
- **`limit=100`**, **`offset=0`** — pagination over the match list.
- **`snippet_chars=120`** — per-match snippet length.
- **`files_only=True`** — collapse to a unique file list with hit counts (much
  smaller for hot loc keys; default cap is 100 KB scan budget — see source).

Returns `{ok, kind, target, total, returned, truncated, scan_truncated, matches}` or, with
`files_only`, `{..., mode: "files_only", total_files, files_returned, files}`.

The internal **scan budget** caps how many matches the scanner accumulates
before bailing. `scan_truncated=True` means the response is incomplete; raise
`limit` or set `files_only=True` for hot targets.

### `focus_graph(tag, detail?, focus_ids?, node_limit?, edge_limit?, include_nodes?, include_edges?) -> dict`

Prereq/mutex DAG for a tag's focuses, with **three detail tiers**:

- **`detail="summary"`** (default) — `{node_count, edge_count, roots, cycles,
  dangling_prereqs, sample_node_ids}`. ~1 KB even for ISR (667 focuses).
- **`detail="ids"`** — adds `nodes: [{id, line, kind, file}]` and `edges: [{from, to, kind}]`.
  ~70 KB for ISR.
- **`detail="full"`** — full per-node metadata (x, y, cost, icon, prereqs, mutex).
  Always combine with `focus_ids=[...]` or `node_limit` for big tags.

`cycles` and `dangling_prereqs` are always computed (they're small and
load-bearing for review).

### `diff_summary(base?, kinds?, with_ids?, limit?) -> dict`

Structured branch diff vs `base` (default `main`).

- **`kinds=[...]`** — restrict to specific content kinds (focus/event/...).
- **`with_ids=False`** — skip per-file ID diff (much faster; doesn't re-parse
  both revisions).
- **`limit=200`** — cap returned `files`. `total_files` + `counts_by_kind` stay
  accurate regardless.

Returns `{ok, base, total_files, files_returned, counts_by_kind, truncated, files}`.
Each file record is `{path, status, kind, added_ids?, removed_ids?}`.

---

## Generators — return strings, never write

All generators return `{"txt": "...", ...}` containing the paradox-script
text. The agent uses Edit/Write to place the content; the server never writes
to mod files.

### `generate_focus(id, tag, x, y, ...) -> dict`

Scaffold a `focus = { ... }` block. Optional fields: `cost`, `icon`,
`relative_position_id`, `prerequisites`, `mutually_exclusive`,
`search_filters`, `available`, `completion_reward`, `ai_base`, `title`,
`description`.

Returns `{txt, loc_yml_keys: [{key, value}, ...]}`. The loc rows are stubs
the agent should add to the country's `_l_english.yml`.

### `generate_event(namespace, number, kind?, ...) -> dict`

Scaffold a `country_event`/`news_event`/`state_event` block.

Returns `{txt, namespace_directive, loc_yml_keys}`. If the file is new, prepend
`namespace_directive` (`add_namespace = isr`) before the event blocks.

### `generate_decision(id, ...) -> dict`

Scaffold a decision. Goes inside an existing `<category> = { ... }` container.

### `generate_idea(id, ...) -> dict`

Scaffold an idea. Goes inside `ideas = { <category> = { ... } }`.

### `generate_gfx_entry(name, texturefile, kind?, frames?, legacy_lazy_load?) -> dict`

Scaffold a `spriteType = { ... }` entry. Goes inside `spriteTypes = { }` in a
`.gfx` file.

### `generate_loc_stub(keys: [{key, value}], lang?, include_header?, bom_prefix?) -> dict`

Build a localisation YAML stub.

- **`lang="l_english"`** — language tag prefix.
- **`include_header=True`** — emit the `l_english:` line.
- **`bom_prefix=True`** — prepend the UTF-8 BOM. Use for **new** files only;
  existing files already have a BOM.

---

## Resources (`md://` URIs)

Resources stream **raw paradox-script text**, not JSON. Use for verbatim
quotation, comparison, or rewrite via Edit. They share the index layer with
the resolvers but slice the source text by token position so comments and
whitespace are preserved.

| URI | Returns |
|---|---|
| `md://focus/{focus_id}` | The full `focus = { ... }` block, or `shared_focus`/`joint_focus` variant. |
| `md://event/{event_id}` | The full event block (`country_event` / `news_event` / etc.). `event_id` is `namespace.number`. |
| `md://decision/{decision_id}` | The full `<decision_id> = { ... }` block. |
| `md://idea/{idea_id}` | The full `<idea_id> = { ... }` block. |
| `md://loc/{key}` | The localised value (just the string, not the YAML line). |
| `md://sprite/{name}` | The full `spriteType = { ... }` block (or variant kind). |

`{focus_id}`, `{key}`, `{name}` are path-safe — URL-encode embedded slashes or
spaces. (HOI4 IDs by convention don't contain either.)

---

## Calling tools from Python (debugging)

The tools are plain functions; you can call them directly without spinning up
the MCP server:

```python
from md_mcp.config import load
from md_mcp.indexes import FocusIndex
from md_mcp.analysis.focus_graph import focus_graph

settings = load("/path/to/Millennium-Dawn")
focus_index = FocusIndex(settings.mod_root, settings.cache_dir, settings.vanilla_path)

result = focus_graph("ISR", settings.mod_root, focus_index, detail="summary")
print(result)
```

This bypasses MCP framing — exceptions surface directly, which is the easiest
way to diagnose a tool that's misbehaving inside the protocol layer.
