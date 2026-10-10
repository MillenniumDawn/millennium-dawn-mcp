# Validators

How the server discovers Millennium Dawn's Python validators in the configured
mod checkout and turns their output into structured JSON for the agent, in
isolated or in-process mode.

## What gets wrapped

Every `validate_*.py` under `<mod_root>/tools/validation/`. The wrapper:

- Discovers them via `glob("validate_*.py")`.
- Pulls the `TITLE` constant via cheap regex (no import cost for listing).
- Imports each on demand via `importlib`.
- Instantiates the `Validator` class (every validator exposes one).
- Calls `run_all_validations()`.
- Reads `_issues` (a list of `Issue` dataclasses from `validator_common.py`).
- Normalises to plain dicts via `Issue.to_dict()`.

Implementation: [`src/md_mcp/validators/runner.py`](../src/md_mcp/validators/runner.py).

## Isolated mode (default)

```bash
md-mcp serve --mod-root /path/to/Millennium-Dawn   # defaults to isolated
```

Each call runs the import/instantiate/`run_all_validations()`/read-`_issues`
sequence in a child process
([`_shim.py`](../src/md_mcp/validators/_shim.py)) and reads the issue list back
as JSON from a temp file.

This is not about crash isolation. On Millennium-Dawn commit
`b6bea58f93cd416e69fc42e91e77cfe833778b88`, 30 of the 49 discovered validator
modules contain a pool-aware path, counted
when a module references one of the shared pool helpers (including as a
callback) or a direct `Pool` constructor. Most use `_pool_map` in the shared
`validator_common.py` base class. Pool creation is conditional: the shared
helpers run serially for a single worker or batches under 10 items. Forking
from inside the server's stdio event
loop hangs the server outright: `validate(name="events")` never returns, where
the same call takes 3 seconds outside the loop. Same failure as CLAUDE.md
rule 6, one layer out, and it isn't ours to fix upstream. Running the validator
in a child sidesteps it and lets the suite keep its parallelism.

The child gets `stdin=DEVNULL` so it can never consume the server's JSON-RPC
input, and a 600 s timeout. The child runs in its own process group and a
timeout kills the whole group, so its pool workers don't keep running after it
(`util/process.py`). On Windows, `taskkill /T /F` kills the tree. If `taskkill`
fails or times out, the helper still kills and reaps the direct child, but tree
cleanup is not guaranteed. The lint scripts use the same helper.

Cost is one interpreter start per call, which is noise next to a multi-second
validator. Unlike in-process mode there's no module cache across calls.

**Failures are loud.** If the child dies before writing its payload, the runner
returns `ok: false` with the exit code and the tail of stderr. It never reports
a crashed validator as a clean run.

## In-process mode

`md-mcp doctor` only prints settings — it runs no validators. To exercise a
validator in your own process, use the runner directly. For a real traceback,
call `_collect` (both snippets are in [Debugging](#debugging) below).

**Not safe under `md-mcp serve`.** A forking validator deadlocks the stdio
loop, so the `serve` subcommand logs a warning and overrides this back to
isolated. It stays available for the CLI, for tests, and for library callers
that aren't inside `mcp.run()`, where it's faster and easier to debug.

The runner inserts `<mod_root>/tools` and `<mod_root>/tools/validation` at the
front of `sys.path` on first call, then `importlib.import_module(...)`s the
target validator. Modules are cached after first import, so repeated calls to
the same validator skip startup cost (a single import is multi-second on
some validators — they pull pandas, openpyxl, etc.).

**Output capture**: validators are chatty. The wrapper redirects their stdout
and stderr into `io.StringIO()` buffers. On success, only the structured issues
come back to the agent; if validator execution raises, the error result also
includes the last 2,000 characters of captured stderr for diagnosis.

**`SystemExit` guard**: some validators call `sys.exit(N)` to signal failure.
That would kill the server. The wrapper catches `SystemExit` and logs it as
info, then returns the issues collected up to that point.

**Memory**: the validators all hold their own caches. Running all serially
peaks around 500 MB on the real mod. The wrapper doesn't pool instances —
each call constructs fresh — so memory drops back after each call.

`MD_MCP_VALIDATOR_MODE=subprocess` is accepted as an alias for `isolated`.
It used to mean something different: shelling out to
`python validate_<name>.py --mod-path ... --json <sidecar>` and reading the
sidecar. That path ignored the child's exit code and treated a missing sidecar
as an empty issue list, so a validator that failed to even import reported
`ok: true` with zero issues. It's gone.

## When to use which mode

| | Isolated (default) | In-process |
|---|---|---|
| Safe under `md-mcp serve` | Yes | **No** — forking validators deadlock it |
| Speed (first call) | ~3 s | ~3 s |
| Speed (subsequent) | ~3 s | ~200 ms (module cache) |
| Crash isolation | Always isolated | Server-fatal on hard crash |
| Memory | Reclaimed each call | Persists in server |
| Debugging the validator | Use `python -m pdb tools/validation/validate_X.py` directly | Same |

Reach for in-process when you're driving the runner from a script or a test and
want the module cache. The server picks isolated for you either way.

## The coupling caveat

The wrapper reads `validator._issues` — that leading underscore means it's
not a public API. The Millennium-Dawn team can refactor it freely. When they
do, both modes break until we update `runner.py`.

Mitigations:

1. **Single adapter point.** All version-sensitive behaviour lives in
   `_collect` in `runner.py`. `_run_inprocess` calls it directly and `_shim.py`
   calls it in the child, so there is one place to patch.
2. **`_collect` for triage.** `run()` reports a failure as `{ok: false, error}`
   in both modes. When you want the traceback, call `_collect` yourself outside
   the server (see [Debugging](#debugging)).
3. **CI nightly check** runs every fast validator wrapper against
   `Millennium-Dawn` `main` and opens an issue on breakage. Wired:
   `.github/workflows/nightly.yml` runs `pytest -m integration` against a fresh
   sparse clone and files an issue labelled `nightly-failure`. The integration
   job is read-only; a dependent issues-only job reports integration failures
   and job timeouts.

When you encounter a breakage:

- First check whether `BaseValidator._issues` or `Issue.to_dict()` signatures
  changed in [`validator_common.py`](../../Millennium-Dawn/tools/validation/validator_common.py).
- Run the validator's own CLI directly to confirm it still works at all.
- Patch `_collect` in `runner.py` to handle both the old and new shape during
  the rollout window.

## Validator output shape

Each issue, after `Issue.to_dict()`, is roughly:

```json
{
  "file": "common/national_focus/MD_ISR_focus.txt",
  "line": 42,
  "column": 1,
  "severity": "warning",
  "code": "LOC_KEY_MISSING",
  "message": "Loc key 'ISR_focus_xyz_desc' has no English value"
}
```

The `severity` field is one of `info`, `warning`, `error`. The MCP wrapper
filters and caps by severity:

```python
result = validate(severity_min="warning", limit=200)
# Drops "info"-level issues. Caps the array at 200 (counts stay accurate).
```

See [`docs/output-budgets.md`](./output-budgets.md#severity-floors) for the
severity-filter pattern. Run-all path skips two known-slow validators by
default: `unused_scripted` and `unused_textures`. Call them by name when
you want them:

```python
validate(validator="unused_textures")
```

## Suppression

The runner applies upstream-documented false-positive suppressions in the tail
shared by both modes (`_finish`) before returning, so callers always see the
post-suppression issue list with a count alongside it:

```json
{
  "ok": true,
  "validator": "focus_tree",
  "issues": [],
  "counts": {"error": 0, "warning": 0, "info": 0},
  "suppressed": 12,
  "suppression_source": ".claude/docs/known-false-positives.md"
}
```

The count represents **hidden real findings**: issues the validator reported
that we deliberately dropped because upstream `.claude/docs/known-false-positives.md`
covers them (today: `missing-focus-icon` and `missing-decision-icon` matched
against `tools/validation/vanilla_sprites.txt`). Tools that aggregate per-
validator totals (`validate`, `lint`) read this count and surface it
unchanged; `lint` renames it to `suppressed_mod_wide` on each check and the
summary to distinguish it from on-scope counts. The suppression helper
([`analysis/suppressions.py`](../src/md_mcp/analysis/suppressions.py)) implements
this upstream rule using the mod's sprite manifest.

## Staged-only mode

```python
validate(staged_only=True)
```

Restricts the validator to git-staged files. Native to each validator — the
wrapper just passes the flag through. Useful mid-edit when you want fast
feedback on what you just touched.

`files=[...]` is post-filter: validators don't expose a path-filter API, so
the wrapper runs the full validator and filters the resulting issue list by
file. Slower than `staged_only` for big trees.

For a file-scoped `validate`, a validator reports an `unattributed` count when
some of its issues name a file that could not be resolved. Run-all keeps that
count on each validator entry and returns the sum at the top level. The key is
left out when the count is zero, like `suppressed`. These findings are not
added to the scoped `issues` list or severity `counts`. The counts are also
returned with `counts_only=True` and in delta mode. Delta's separate
`unclassified` count covers issues that could not be compared with the
baseline.

## Running validators through `lint`

`lint()` runs the `style` validator by default in full-tree mode or when the
resolved scope contains `.txt` files under `common/`, `events/`, or `history/`.
A clean tree or a non-script-only scope runs no validator. Explicit selections
retain their current behavior: `lint(validators=["auto"])` uses domain-matched
validators, and `validators=[]` disables validators. Validator issues merge into
the lint response as `validator:<name>` checks with both on-scope and mod-wide
totals. See the lint section of [`docs/tools.md`](./tools.md). The bridge
consumes `ValidatorRunner.run()` output only, so the coupling caveat above still
has a single adapter point.

`lint(validators=["auto"])` also selects `equipment_variants` for `.txt` changes
under `common/`, `events/`, or `history/`. It warns when a created variant is
consumed before its equipment technology is assured. A technology, country, or
event context edit may affect an unchanged consumer. Lint can therefore include
off-scope warnings at their original consumer file and line with
`scope: "related"`; this is a potential relationship, not a proven dependency.
The check's `total` counts on-scope issues and `related` counts all potentially
related warnings. Overall lint counts include related warnings; the final
`issues` array may be truncated by the response limits. The separate
`check_equipment_variant` tool checks hull slots and module categories.

Scoping can't compare `Issue.file` to the changed-file set directly, because
that field isn't uniform (mod-relative, basename, `""`, `"unknown"`, and it
varies within a single validator). `IssueAttributor`
([`attribution.py`](../src/md_mcp/validators/attribution.py)) resolves each
issue to a real path first: an exact path passes through, a bare basename or
partial path resolves within the validator's scan directories if it's
unambiguous, an empty/placeholder field falls back to filename tokens scraped
from the message, and anything left over is reported as an `unattributed` count
plus a small sample. Two upstream shapes made this necessary — basename-only
issues (`validate_localisation.py`) were dropped from the scope entirely, and
fileless issues (`validate_events`, 762 on the real mod) flooded the response
regardless of scope.

For current Millennium Dawn checkouts, `lint(validators=["auto"])` derives
content routing from `tools/validation/change_groups.py` and the groups in
`tools/validation/validator_batches.py`. Changes to validator tooling use
upstream's `select_for_changed_files`, including its impact exclusions. The
per-spec CLI arguments are passed to the validator instance in both runner
modes. Older or partial checkouts without those routing modules retain the
local scan-domain map as a compatibility fallback. Integration tests compare
the live adapter behavior with the upstream definitions and check that the
sparse CI workspace includes its current routing inputs.

## `lint` and `review_branch`

Two scripts in `Millennium-Dawn/tools/` aren't validator-shaped:

- `tools/linting/check_common_mistakes.py` produces text-line output
  (`file:line: message`). The `lint` tool parses this with a regex and returns
  structured issues (with severity hardcoded to `warning`), alongside the
  other `tools/linting/` scripts it dispatches.
- `tools/analysis/review_branch.py` produces a freeform human-readable
  report. The MCP tool returns it as a UTF-8-byte-bounded `report`, with
  `report_bytes`, `report_returned_bytes`, and `report_truncated` metadata.
  `ok`, `base`, `exit_code`, and `stderr` remain present on subprocess success
  and failure paths.

Both run via subprocess. They're independent of `ValidatorRunner`.

Under `md-mcp serve`, `lint`, `validate`, and `review_branch` run in a bounded
thread pool so their blocking filesystem scans and subprocess waits do not
stall unrelated MCP requests. The server has four worker threads and admits at
most eight jobs to the pool or its work queue at once; excess calls wait
asynchronously for capacity. `lint` and `validate` share a `ValidatorRunner`;
they acquire an async lock before requesting an admission slot and then use its
lazy import and attribution caches. Calls waiting for that lock occupy neither
an admission slot nor a worker thread. If a client cancels a call after its
worker starts, that worker keeps the lock and its slot until it finishes.
`review_branch` runs independently. These tools do not mutate the shared
content indexes.

Cancelling a client request stops waiting for its result, but Python cannot
stop a worker thread or its subprocess wait safely. A submitted worker
continues, and its pool slot stays occupied until it finishes. A request
cancelled before submission, while waiting for the validator lock or pool
capacity, releases any lock it holds and does not keep an admission slot.

## Adding a new validator

When the Millennium-Dawn team adds a new `validate_<name>.py`, it picks up
automatically — no MCP-server code changes needed, as long as:

1. The file is at `Millennium-Dawn/tools/validation/validate_<name>.py`.
2. It defines a `Validator` class.
3. `Validator(mod_path=..., use_colors=False, staged_only=...)` constructs.
4. `instance.run_all_validations()` populates `instance._issues`.

If any of these change for a specific validator, special-case it in
`_collect` rather than weakening the general adapter.

## Debugging

```python
from md_mcp.config import load
from md_mcp.validators import ValidatorRunner

settings = load("/path/to/Millennium-Dawn")
runner = ValidatorRunner(settings.mod_root, mode="in_process")

# List
for info in runner.list():
    print(info.name, info.title)

# Run one
result = runner.run("localisation", staged_only=False)
print(result["counts"], len(result["issues"]))
```

This bypasses the MCP framing, and you can inspect the validator module after
the run through `sys.modules`. The runner still catches whatever the validator
raises and returns `{ok: false, error}`. For the traceback, call the run
sequence itself:

```python
from md_mcp.validators.runner import _collect

payload = _collect(str(settings.mod_root), "validate_localisation", staged_only=False)
print(len(payload["issues"]))
```

Most validators fork a `Pool`, so run this from a script or a REPL, never from
inside `md-mcp serve`.
