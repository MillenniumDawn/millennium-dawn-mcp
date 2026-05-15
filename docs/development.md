# Development

Contributor notes — setting up, testing, releasing, and the patterns we
expect new tools to follow.

## Setup

```bash
cd /Users/matthewscott/Programming/MD/millennium-dawn-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

`pip install -e .` exposes the `md-mcp` console script and lets you edit
`src/md_mcp/` without reinstalling. The `[dev]` extras pull `pytest` and
`pytest-xdist`.

For validator-related work you'll also want the mod's tooling deps:

```bash
pip install -r ../Millennium-Dawn/tools/requirements.txt
```

## Running tests

```bash
pytest -q                            # unit suite, ~92 tests, no checkout needed
pytest -q -n auto                    # parallelised
pytest -m integration                # requires MD_MOD_ROOT (real mod)
pytest -m differential               # parser parity vs TS implementation
pytest tests/test_focus_graph.py -v  # focused
```

Markers are defined in [`pyproject.toml`](../pyproject.toml):

- `integration` — needs `MD_MOD_ROOT` set; will `pytest.skip` otherwise.
- `differential` — parser parity check that shells out to `bun run` against
  the TS implementation.

Conftest provides two fixtures:

- **`fake_mod_root`** (tmp_path-backed) — minimal mod tree under `tmp_path` with
  `descriptor.mod`, one focus file, one loc file, plus M2 fixture content
  (events, decisions, ideas, sprites). Use for fast unit tests.
- **`real_mod_root`** — resolves `MD_MOD_ROOT`; skips if unset or invalid.
  Use for integration tests that need realistic content volume.

## Repo conventions

- **No `Co-Authored-By` lines** in commits (per workspace `CLAUDE.md`).
- **Imperative subject** for commit messages; body explains *why*.
- **No `--no-verify`** unless explicitly required. Diagnose hook failures
  rather than skipping.
- **Don't edit `Hearts of Iron IV/`** — it's a read-only symlink to the
  vanilla install (workspace rule).
- **Don't edit non-English `.yml`** files — Paratranz manages them
  (Millennium-Dawn rule, but it's worth respecting in tests/fixtures too).

## Adding a new tool

See [`CLAUDE.md`](../CLAUDE.md#how-to-add-a-new-tool) for the inline skeleton.
The expanded checklist:

1. **Implementation location**:
   - Pure queries → `src/md_mcp/analysis/<name>.py`
   - Wraps a `Millennium-Dawn/tools/` script → `src/md_mcp/tools/<name>_tools.py`
   - Index-only lookup → `src/md_mcp/tools/resolver_tools.py`
2. **Defaults**:
   - Counts + sample, not full list.
   - `limit=100`–`200`, `offset=0`.
   - `detail="summary"` if the response has natural tiering.
3. **Always**:
   - `enforce_budget(result, heavy_keys=("most-likely-too-big-first",))` on
     every list-returning return path.
   - `ok: True | False` discriminator.
   - On error, include the target (id/key/path) so the agent can correlate.
4. **Register** in `server.py` with `@mcp.tool()`. The one-line description
   shows up in the agent's prompt **every turn** — make it count. Mention the
   important knobs (`detail`, `limit`, mode flags).
5. **Test** under `tests/test_<name>.py`:
   - Happy path with small input.
   - Signature guard (use `inspect.signature(...)` to lock the API).
   - One truncation/budget case.
   - One error case (missing id, malformed input).
6. **Document** in [`docs/tools.md`](./tools.md).

## Adding a new index

See [`docs/indexes.md`](./indexes.md#adding-a-new-index). Key gotcha: the parser
function MUST be a **module-level** function, not a method, so
`ProcessPoolExecutor` can pickle it.

## Adding a new resource

`md://` resource handlers live in [`src/md_mcp/resources.py`](../src/md_mcp/resources.py).
They take the same index objects as the resolvers but slice source text by
token byte offset to preserve original formatting.

```python
def my_resource(target: str, settings: Settings, idx: SomeIndex) -> str:
    rec = idx.resolve(target)
    if rec is None:
        raise KeyError(f"{target!r} not found")
    abs_path = _resolve(rec["file"], settings)
    text = read_text(abs_path)
    return _extract_named_block(text, target)
```

Then register in `server.py` with `@mcp.resource("md://kind/{target}")`.

## Linting / formatting

No automated formatter is enforced yet. Conventions:

- 4-space indent.
- Line length ~100 chars.
- Type hints required on public function signatures.
- `from __future__ import annotations` at the top of every module.
- Imports sorted: stdlib, third-party, first-party (`from ..something`).
- Docstrings on every public function — at minimum one line explaining the
  contract.

## Performance budgets

These are smoke-tested in `tests/test_perf.py`:

| Operation | Target |
|---|---|
| Cold index build (mod only) | < 6 s |
| Warm `ensure_fresh()` (no changes) | < 50 ms |
| Single `resolve_*` after fresh | < 1 ms |
| `parse_string` on 50 KB input | < 10 ms |
| `find_references` warm path | < 2 s |
| Full validator suite (all fast) | < 60 s |

Treat regressions as bugs. If a feature legitimately needs more time, add a
new test rather than relaxing the existing budget.

## Live MCP probing

For end-to-end checks against a real mod, spin a stdio client in-process:

```python
import asyncio
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

async def main():
    params = StdioServerParameters(command="md-mcp", args=["serve"], env={"MD_MOD_ROOT": "/path/to/Millennium-Dawn"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            result = await s.call_tool("focus_graph", {"tag": "ISR", "detail": "summary"})
            print(len(str(result.content)))  # bytes after MCP framing

asyncio.run(main())
```

Use this before claiming a budget optimisation is correct — unit fixtures
are smaller than real data.

## CI (TODO)

Not yet wired. Targets when we do:

- **Unit suite** on every PR (`pytest -q -n auto`).
- **Differential parser** on every PR if `bun` is available.
- **Integration** nightly against `Millennium-Dawn` `main` checkout; open an
  issue on failure (catches API drift in the validator coupling).
- **Performance smoke** on every PR (`pytest tests/test_perf.py`).

## Releasing

Versioning lives in [`pyproject.toml`](../pyproject.toml) (`project.version`).
No formal release artefacts yet; users install via `pip install -e .` from
a local checkout.

If/when we publish to PyPI:

```bash
python -m build
python -m twine upload dist/*
```

Bump the version, tag the commit (`git tag v0.x.y`), push tags
(`git push --tags`).

## Debugging the running server

### Logs

`md-mcp serve -v` enables `INFO` logs. `-vv` enables `DEBUG`. Logs go to
stderr; the MCP client typically captures and displays them.

### Direct tool calls (bypass MCP)

```python
from md_mcp.config import load
from md_mcp.indexes import FocusIndex
from md_mcp.analysis.focus_graph import focus_graph

settings = load("/path/to/Millennium-Dawn")
focus_index = FocusIndex(settings.mod_root, settings.cache_dir, settings.vanilla_path)
print(focus_graph("USA", settings.mod_root, focus_index, detail="summary"))
```

Exceptions surface directly — much easier than tracing through the MCP
framing layer.

### Validator-coupling failures

If `validate` starts failing after an upstream change:

1. Try `MD_MCP_VALIDATOR_MODE=subprocess` — does it work?
2. If yes: `Issue.to_dict()` or `_issues` semantics changed. Patch
   `_run_inprocess` in `runner.py`.
3. If no: the validator script itself is broken. File against
   `Millennium-Dawn`.

### Stale indexes

```bash
rm -rf /path/to/Millennium-Dawn/.md-mcp-cache
md-mcp build-index --mod-root /path/to/Millennium-Dawn
```

## Useful reading order for newcomers

1. [`README.md`](../README.md) — what the project is.
2. [`CLAUDE.md`](../CLAUDE.md) — invariants and how to add stuff.
3. [`docs/architecture.md`](./architecture.md) — the layering.
4. [`docs/output-budgets.md`](./output-budgets.md) — the most-violated invariant.
5. [`docs/tools.md`](./tools.md) — what we expose.
6. [`docs/indexes.md`](./indexes.md) and [`docs/parser.md`](./parser.md) — the
   load-bearing internals.
7. [`docs/validators.md`](./validators.md) — wrapping caveats.
