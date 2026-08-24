# Development

Contributor notes — setting up, testing, releasing, and the patterns we
expect new tools to follow.

## Setup

```bash
cd /path/to/millennium-dawn-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pre-commit install
```

`pip install -e .` exposes the `md-mcp` console script and lets you edit
`src/md_mcp/` without reinstalling. The `[dev]` extras pull `pytest`,
`pytest-xdist`, `coverage`, `diff-cover`, `ruff`, `mypy`, and `pre-commit`.
The wrapped validators are stdlib-only; no extra deps needed for validator work.

A fresh git worktree has no `.venv`. `uv run pytest` (or `mypy`) then falls
back to whatever the ambient interpreter provides. On a machine with a stale
editable install or `.pth` pointing at the main checkout, tests collect from
the worktree but import the main checkout's `md_mcp`. Edits appear to have no
effect, or new modules raise `ModuleNotFoundError`.

After creating a worktree, run this before any `uv run` command:

```bash
uv sync --extra dev
```

## Running tests

```bash
pytest -q                            # unit suite, sub-second, no checkout needed
pytest -q -n auto                    # parallelised
pytest -m integration                # requires MD_MOD_ROOT (real mod)
pytest tests/test_focus_graph.py -v  # focused
```

To check coverage for the current branch, run the unit suite under coverage and
compare changed lines with the default branch:

```bash
coverage run --source=src -m pytest -q
coverage report
coverage xml
diff-cover coverage.xml --compare-branch=origin/main --fail-under=85
```

Markers are defined in [`pyproject.toml`](../pyproject.toml)
(`--strict-markers` is on, so typos fail loudly):

- `integration` — needs `MD_MOD_ROOT` set; will `pytest.skip` otherwise.

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

ruff and mypy are enforced. Pre-commit runs ruff on every commit; CI runs
all three:

```bash
ruff check .            # lint (B, E, F, I, RUF, SIM, W)
ruff format .           # formatter, line length 100
mypy                    # type-check src/ + tests/
```

The ruff version is pinned in `pyproject.toml` dev extras and mirrored in
`.pre-commit-config.yaml`; bump both together (`pre-commit autoupdate`).
Conventions the tools don't cover:

- Type hints required on public function signatures.
- `from __future__ import annotations` at the top of every module.
- Docstrings on every public function — at minimum one line explaining the
  contract.
- `src/md_mcp/server.py` is exempt from line-length checks: tool docstrings
  are the exact MCP descriptions sent to agents, so they stay one-line.

## Performance budgets

The only budget under test is the cold index build: `tests/test_perf.py`
asserts per-index ceilings (Focus/Loc 10 s, Event/Gfx 5 s, Decision/Idea 3 s)
and 30 s total. It is integration-marked, so it needs `MD_MOD_ROOT` and runs
in the nightly workflow, not per-PR CI.

Budgets were calibrated on a 14-core Mac. `MD_PERF_BUDGET_SCALE` (default 1.0)
multiplies every budget for slower runners; the nightly workflow sets it to 3
for CI.

The rest are targets to hold by hand, not assertions:

| Operation | Target |
| --- | --- |
| Warm `ensure_fresh()` (no changes) | < 50 ms |
| Single `resolve_*` after fresh | < 1 ms |
| `parse_string` on 50 KB input | < 10 ms |
| `find_references` warm path | < 2 s |
| Full validator suite (all fast) | < 60 s |

Treat regressions as bugs. If a feature legitimately needs more time, add a
new test rather than relaxing the existing budget.

## Registering in a mod repo

To expose the tools to an agent working inside a Millennium-Dawn checkout, drop
a project-scope `.mcp.json` there (see the README's "Register with Claude Code"
for the shape and the `md` naming note — Millennium-Dawn pre-approves
`mcp__md__*` tools, so use that name). The server auto-discovers the mod from
`cwd`, so no env block is needed. Note `.mcp.json` is gitignored in
Millennium-Dawn, so the file is local-only and isn't shared with teammates via
git.

## Live MCP probing

For end-to-end checks against a real mod, spin a stdio client in-process:

```python
import asyncio
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession


async def main():
    params = StdioServerParameters(
        command="md-mcp", args=["serve"], env={"MD_MOD_ROOT": "/path/to/Millennium-Dawn"}
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            result = await s.call_tool("focus_graph", {"tag": "ISR", "detail": "summary"})
            print(len(str(result.content)))  # bytes after MCP framing


asyncio.run(main())
```

Use this before claiming a budget optimisation is correct — unit fixtures
are smaller than real data.

## CI

Two workflows under `.github/workflows/`:

- **`ci.yml`** (every PR + push to main): ruff check, ruff format check, and
  mypy on 3.12; `pytest -q -n auto` on a 3.10/3.14 matrix. Integration tests
  (including the perf budget) skip there — no `MD_MOD_ROOT` on the runner.
- **`nightly.yml`** (cron + manual dispatch): sparse-clones Millennium-Dawn
  main (~380 MB instead of the 8 GB full tree) and runs every fast validator
  through `pytest -m integration`. This is the validator-coupling drift check
  from [`docs/validators.md`](./validators.md). The read-only integration job
  does not persist checkout credentials. A dependent job with only
  `issues: write` opens or updates a `nightly-failure` issue after integration
  failures or job timeouts.

### Future work: differential parser suite

A parity harness against the TS parser was advertised for a while but never
existed; the marker has been removed. If reviving it: the TS repo has no CLI,
but `parseHoi4File` is importable from the compiled
`out/src/hoiformat/hoiparser` (see `MD-VSCode-Utility-Tool/scripts/findallvals.js`
for the pattern). The shape would be a node/bun shim dumping canonical JSON,
an `MD_TS_PARSER_ROOT` env var pointing at the sibling checkout, and a
normaliser for the intentional port divergences (`Node.children` is a method,
`Token.start` is a byte offset).

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

1. Try `MD_MCP_VALIDATOR_MODE=in_process` outside the server for the raw traceback.
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
