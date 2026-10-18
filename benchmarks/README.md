# Cache-path measurements

The AST cache now refuses to publish a parse whose source signature changed
while it ran. A late old parse therefore cannot replace a newer cached tree.
This saves the next reader an otherwise redundant parse in that ordering.
No FocusIndex storage or tag-index optimization was made.

## Reproduce

Use one interpreter with the project's dev dependencies installed. The manual
harness uses Linux `resource` RSS units and is separate from the portable tests.

```sh
python benchmarks/cache_paths.py \
  --baseline /workspace/mcp-baseline \
  --candidate /workspace/millennium-dawn-mcp \
  --repetitions 8 \
  --mod-root /workspace/Millennium-Dawn \
  --output benchmarks/results/cache-paths-linux.json
```

Baseline: `858a5a99011c56ae81823fdcdd456885e95f0923`.
Candidate runtime: `fece5cff0940d0f9f5e775f35d913abfda2eb5f8`.
The report commit adds only this harness and evidence to that candidate.
The real mod is read-only at `532b046fd6fb8c19089a52b4a53b2578816a704d`.
Every sample verifies that `md_mcp` imports from the selected checkout.

Each scenario has eight fresh-process samples per revision, alternating AB/BA
order. The raw JSON retains all observations, elapsed times, RSS, sample order,
platform, interpreter, commits, parse/write counts, and payload hashes. It also
records medians, ranges, and sample standard deviations. Diagnostic runs and
cProfile timings are excluded from the timing sample set.

## Fixtures and controls

- **Focus edit:** 80 files with 250 focuses each, 20,000 records total. A
  one-file comment edit retains every record. The monolithic payload is
  1,123,051 bytes. Each edit parses one source and writes the payload plus
  manifest (1,128,091 bytes total). Cold build means serial first construction
  over newly written, OS-cache-hot files. It does not measure cold disk or the
  pooled CLI. Unchanged refresh forces a stat scan past the debounce.
- **AST edit race:** 3,000 focuses, 120,814 bytes in the new source. A barrier
  holds the completed old parse until the newer parse has published. Measure
  the following read, then average 2,000 ordinary cache hits. Both revisions
  return fresh text. The candidate retains the newer tree and avoids a third
  parse. Cache accounting uses the existing source-character contract, despite
  the `MD_MCP_AST_CACHE_BYTES` environment variable's name.
- **Live MCP:** real stdio subprocess, `focus_graph(tag="ISR", detail="summary")`,
  one first call and one warm call per fresh server. Both persistent indexes
  are primed before sampling. All calls must return identical hashes and
  node counts across both revisions. Server memory is the child process's
  lifetime peak; client memory is recorded separately.

RSS is a lifetime high-water mark including fixture construction, not retained
cache memory or an operation allocation delta. The AST worker retains the newer
caller's result during the next read, so its RSS comparison applies only to
this controlled overlapping-reader workload. No independent background test or
profiling run runs alongside the retained measurements.

## Results

Linux x86_64, Python 3.12.14, eight samples per revision and scenario.
Cells show **median [min, max]; sample standard deviation**. Elapsed units
are milliseconds except the explicitly marked ordinary-hit row.

| Operation | Baseline | Candidate |
| --- | --- | --- |
| Focus serial first build | 601.004 [561.862, 620.320]; 19.127 | 597.593 [559.031, 704.559]; 56.221 |
| Focus forced unchanged refresh | 4.897 [4.283, 5.695]; 0.414 | 4.902 [4.262, 5.470]; 0.406 |
| Focus one-file edit | 26.651 [24.442, 30.492]; 1.818 | 25.848 [24.784, 27.697]; 0.951 |
| AST race plus next read | 246.247 [222.196, 281.873]; 20.096 | 171.719 [158.860, 200.071]; 13.324 |
| AST next read after race | 78.510 [68.019, 95.478]; 9.116 | 0.062 [0.055, 0.104]; 0.016 |
| AST ordinary cache hit (microseconds) | 4.856 [4.676, 6.358]; 0.649 | 4.639 [4.464, 5.516]; 0.363 |
| Live first call (persisted index) | 298.722 [267.410, 334.296]; 22.146 | 296.628 [282.232, 358.125]; 27.579 |
| Live warm call | 22.384 [22.169, 23.485]; 0.459 | 22.559 [21.231, 26.830]; 1.785 |

Lifetime peak RSS in KiB, using the same cell format:

| Process/workload | Baseline | Candidate |
| --- | --- | --- |
| Focus worker | 32868.000 [32848.000, 32872.000]; 7.690 | 33042.000 [33040.000, 33144.000]; 42.494 |
| AST overlapping-reader worker | 38908.000 [38908.000, 38912.000]; 1.852 | 32380.000 [32380.000, 32384.000]; 2.070 |
| Live MCP server | 96692.000 [96524.000, 96784.000]; 91.338 | 96746.000 [96260.000, 97032.000]; 213.348 |
| Live MCP client | 50404.000 [50332.000, 50476.000]; 38.884 | 50358.000 [50080.000, 50456.000]; 140.996 |

Every AST baseline sample performs three parses and fails to reuse the newer
caller's tree on the next read; every candidate sample performs two parses and
reuses it. That controlled race is the demonstrated improvement. It does not
measure how frequently the ordering occurs in normal use.

Focus edit phase medians (baseline/candidate) are parsing **6.913/6.836 ms**,
persistence **7.252/6.904 ms**, and tag maps **6.832/7.154 ms**. These data do
not support changing persistence first. No FocusIndex or general live MCP
speedup is claimed; their small differences sit within observed variation.
All 32 measured live calls return **667 nodes** and the same SHA-256 payload
hash, `ce683e23fc4363e58dc702fe89c16bee57b7a749bc5d02c38d44073483d1ec26`.
The raw report is [cache-paths-linux.json](results/cache-paths-linux.json).

## Profile before the change

The initial synthetic Focus edit profile preceded the AST implementation.
A retained replay is [focus-baseline-profile.txt](results/focus-baseline-profile.txt).
It profiles only the edit refresh, using the same fixture and baseline source:

```sh
PYTHONPATH=/workspace/mcp-baseline/src python benchmarks/cache_paths.py \
  --worker focus --profile benchmarks/results/focus-baseline-profile.txt
```

The retained cProfile run takes 103 ms: tag-map rebuild accounts for 50 ms,
parsing for 23 ms, and persistence for 8 ms. Profiling disproportionately charges
Python loops relative to C JSON serialization; these are diagnostic cumulative
times, not speedup evidence. The unprofiled per-phase times are retained in the
JSON. Persistence is not established as the dominant current bottleneck, so
this work does not change FocusIndex storage. The historical 222/208.5/260 ms
edits and 1.9 MB payload used another fixture and are not comparable samples.

## Validation and review

On Linux/Python 3.12.14:

- Focused portable command: `pytest -q tests/test_index_reload.py
  tests/test_ast_cache.py tests/test_process.py`: **39 passed in 4.16 s**.
- `pytest -q`: **1,801 passed, 66 skipped in 25.46 s**.
- `pytest -q -n auto`: **1,801 passed, 66 skipped, 8 warnings in 12.22 s**.
  Warnings come from the two existing tests that explicitly fork in xdist workers.
- `coverage run --source=src -m pytest -q`: **1,801 passed, 66 skipped in
  31.46 s**. Total source coverage **98%**; changed source lines **100%**.
- `ruff check .`, `ruff format --check .`, `mypy`, actionlint 1.7.12, and
  changed-file pre-commit hooks pass.
- Independent `gpt-6.1-sol` implementation review found no actionable issue.
  Its benchmark review caught dictionary-valued phase aggregation; that was
  fixed and smoke-tested before sampling. Re-review found no remaining finding.

The 16 added tests cover FocusIndex and English localisation parity, duplicate
winners, preserved parse errors, parse/write counts after edits/additions/removals,
real process reloads, another process updating a shared manifest, explicit spawn
workers, headless native CLI startup, paths with spaces and Unicode, concurrent
AST misses, both edit completion orders, and edit/removal during parsing.
The existing 60,000-record localisation smoke test is unchanged.

## Limits and deferred work

The new CI job runs only the three focused modules on Windows and macOS,
Python 3.12, with a ten-minute job timeout. It has not run remotely because
this branch is local. Forced spawn on Linux proves pickle/import execution
there; it cannot establish native Windows or macOS success. The aggregate
suites report 66 skips; the full integration suite was not run with
`MD_MOD_ROOT`. The manual live MCP
probe above uses the real mod, with all cache writes outside that repository.

Single-flight remains deferred: duplicate same-version parses are permitted,
and this task does not measure their frequency in production. Focus storage
and incremental tag-map changes need representative production profiling.
Drafts #286, #287, and #288 are untouched. Unterminated-string work in #269
remains deferred until #286 lands. No pushes, PRs, merges, mod edits, or user
configuration changes were made.
