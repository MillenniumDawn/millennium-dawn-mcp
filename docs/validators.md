# Validators

How the server runs Millennium Dawn's Python validators in-process
(auto-discovered, 26 at last count) and turns their output into structured
JSON for the agent.

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

## In-process mode (default)

```bash
md-mcp serve --mod-root /path/to/Millennium-Dawn   # defaults to in_process
```

The runner inserts `<mod_root>/tools` and `<mod_root>/tools/validation` at the
front of `sys.path` on first call, then `importlib.import_module(...)`s the
target validator. Modules are cached after first import, so repeated calls to
the same validator skip startup cost (a single import is multi-second on
some validators — they pull pandas, openpyxl, etc.).

**Output capture**: validators are chatty. The wrapper redirects their stdout
+ stderr into `io.StringIO()` buffers and discards them. Only the structured
issues come back to the agent.

**`SystemExit` guard**: some validators call `sys.exit(N)` to signal failure.
That would kill the server. The wrapper catches `SystemExit` and logs it as
info, then returns the issues collected up to that point.

**Memory**: the validators all hold their own caches. Running all serially
peaks around 500 MB on the real mod. The wrapper doesn't pool instances —
each call constructs fresh — so memory drops back after each call.

## Subprocess mode (fallback)

```bash
MD_MCP_VALIDATOR_MODE=subprocess md-mcp serve --mod-root /path/to/Millennium-Dawn
```

Spawns `python validate_<name>.py --mod-path ... --json <sidecar>` and reads
the JSON sidecar back. Slower (~3 s import cost per call) but isolates the
validator from server crashes — useful when the validator suite on `main` is
mid-refactor.

The sidecar lives at `<mod_root>/.md-mcp-cache/<name>.issues.json`. It's
overwritten on every call.

## When to use which mode

| | In-process | Subprocess |
|---|---|---|
| Speed (first call) | ~3 s | ~3 s |
| Speed (subsequent) | ~200 ms | ~3 s |
| Crash isolation | Server-fatal on hard crash | Always isolated |
| Memory | Persists in server | Reclaimed each call |
| Validator API change | Server fails until adapter is patched | Works as long as `--json` is supported |
| Debugging the validator | Use `python -m pdb tools/validation/validate_X.py` directly | Same |

**Default is in-process.** Switch to subprocess only if you hit a coupling
issue (see below).

## The coupling caveat

The wrapper reads `validator._issues` — that leading underscore means it's
not a public API. The Millennium-Dawn team can refactor it freely. When they
do, in-process mode breaks until we update `runner.py`.

Mitigations:

1. **Single adapter point.** All version-sensitive behaviour lives in
   `_run_inprocess` in `runner.py`. One file to patch.
2. **Subprocess fallback exists.** Users who can't wait for an MD-server
   patch can set `MD_MCP_VALIDATOR_MODE=subprocess` (the validator scripts
   have a stable CLI: `--mod-path`, `--staged`, `--json`).
3. **CI nightly check** runs the wrapper against `Millennium-Dawn` `main` and
   opens an issue on breakage. Wired: `.github/workflows/nightly.yml` runs
   `pytest -m integration` against a fresh sparse clone and files an issue
   labelled `nightly-failure`.

When you encounter a breakage:
- First check whether `BaseValidator._issues` or `Issue.to_dict()` signatures
  changed in [`validator_common.py`](../../Millennium-Dawn/tools/validation/validator_common.py).
- Try `MD_MCP_VALIDATOR_MODE=subprocess` to confirm the validator itself
  still works.
- Patch `_run_inprocess` to handle both the old and new shape during the
  rollout window.

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

## Running validators through `lint`

`lint(validators=["auto"])` runs domain-matched validators on the same
changed-file scope as the lint scripts and merges the issues into one
response (check names `validator:<name>`, with both on-scope and mod-wide
totals). See the lint section of [`docs/tools.md`](./tools.md). The bridge
consumes `ValidatorRunner.run()` output only — the coupling caveat above
still has a single adapter point.

## `lint` and `review_branch`

Two scripts in `Millennium-Dawn/tools/` aren't validator-shaped:

- `tools/linting/check_common_mistakes.py` produces text-line output
  (`file:line: message`). The `lint` tool parses this with a regex and returns
  structured issues (with severity hardcoded to `warning`), alongside the
  other `tools/linting/` scripts it dispatches.
- `tools/analysis/review_branch.py` produces a freeform human-readable
  report. The MCP tool returns the raw text as `report`; the agent extracts
  what it needs.

Both run via subprocess. They're independent of `ValidatorRunner`.

## Adding a new validator

When the Millennium-Dawn team adds a new `validate_<name>.py`, it picks up
automatically — no MCP-server code changes needed, as long as:

1. The file is at `Millennium-Dawn/tools/validation/validate_<name>.py`.
2. It defines a `Validator` class.
3. `Validator(mod_path=..., use_colors=False, staged_only=...)` constructs.
4. `instance.run_all_validations()` populates `instance._issues`.

If any of these change for a specific validator, special-case it in
`_run_inprocess` rather than weakening the general adapter.

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

This bypasses the MCP framing — exceptions surface directly, and you can
inspect the validator instance after the run via the in-process
`runner._modules` cache.
