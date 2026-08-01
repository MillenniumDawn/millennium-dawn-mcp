# Output Budgets

How the server keeps tool responses small enough to survive the MCP client's
per-call output cap. This is the most important invariant in the codebase.

## The constraint

MCP clients (Claude Code, Cursor, Continue) enforce a per-call output cap of
roughly **25K tokens ≈ 100 KB** of serialised JSON. Exceed it and the call
fails — and the failure mode is *worse* than a normal exception, because the
client's recovery loop interprets oversized output as a tool malfunction.

Mod content is large. USA has 700+ focuses; a hot loc key like
`TT_IF_THEY_ACCEPT` has thousands of references. The naive "dump everything"
response shape blows the cap in seconds.

## The case study: `focus_graph(tag="ISR")`

Before optimisation, `focus_graph("ISR")` returned:

- **407,061 bytes** of JSON
- **17,492 lines**
- 667 nodes + 1,081 edges, each with full metadata

After optimisation, the **same call** returns:

| `detail` | Bytes | Notes |
|---|---|---|
| `summary` *(default)* | **1,243** | Counts, roots, cycles, dangling, 20 sample IDs |
| `ids` | 71,176 | Adds `{id, line, kind, file}` per node + edges |
| `full` | 99,302 | Full per-node metadata, capped at `node_limit=100` |

The default-case improvement is 99.7%. The agent picks the right tier by
escalating: `summary` → `ids` (with `focus_ids=[...]` if a subset is enough)
→ `full`.

## The pattern (apply to every list-returning tool)

```
┌─────────────────────────────────────────────────────────┐
│ 1. Default to small output                              │
│    Return counts + sample. No full lists unless asked.  │
├─────────────────────────────────────────────────────────┤
│ 2. Accept opt-in detail                                 │
│    detail="summary"|"ids"|"full",                       │
│    include=[...], limit=N, offset=M,                    │
│    focus_ids=[...] for subset pinning                   │
├─────────────────────────────────────────────────────────┤
│ 3. Always report total + truncated                      │
│    So the caller knows what they're missing.            │
├─────────────────────────────────────────────────────────┤
│ 4. Wrap in enforce_budget(result, heavy_keys=(...))     │
│    Last-line defence. Drops listed keys in order if     │
│    serialised result exceeds BUDGET_BYTES.              │
└─────────────────────────────────────────────────────────┘
```

The pattern lives in [`src/md_mcp/util/response.py`](../src/md_mcp/util/response.py).

## Helpers

### `BUDGET_BYTES`

```python
BUDGET_BYTES = 100_000  # ~25K tokens at 4-bytes/token, with envelope headroom
```

Self-imposed JSON-byte ceiling. 100 KB clears every MCP client cap we've seen
and leaves room for protocol framing.

### `paginate(items, offset=0, limit=200) -> (slice, truncated, total)`

Slice `items[offset : offset + limit]`. Clamps negatives. Returns the slice,
a `truncated` flag, and the original total.

```python
sliced, truncated, total = paginate(matches, offset=0, limit=100)
return {"total": total, "returned": len(sliced), "truncated": truncated, "matches": sliced}
```

### `enforce_budget(result, *, budget=BUDGET_BYTES, heavy_keys=())`

If `result` JSON-encodes larger than `budget`, drop keys from `heavy_keys` (in
order) until it fits. Each dropped key gets a `<key>_dropped: <original_count>`
sibling and the result is tagged `size_truncated=True`.

```python
return enforce_budget(
    {"ok": True, "nodes": all_nodes, "edges": all_edges, "cycles": cycles, ...},
    heavy_keys=("nodes", "edges", "cycles"),
)
```

The drop order matters: list *largest* first (so we shed the most byte budget
per drop) but consider how useful each key is to the caller. For `focus_graph`
we drop `nodes` first (they're the heaviest and the caller can always re-call
with `focus_ids=` to pin a subset), then `edges`, then `cycles` last because
cycles are small and load-bearing for review.

### `clip_strings(items, key, max_chars)`

Trim `item[key]` on each dict to `max_chars`. Used for snippet fields in
`find_references` to keep per-match payload modest.

## Detail tiers

The cleanest API for variable-size output is a **tier knob** rather than
per-field include flags:

```python
def my_tool(target, *, detail: str = "summary") -> dict:
    if detail == "summary":
        return small_response()
    if detail == "ids":
        return medium_response()
    if detail == "full":
        return everything_with_caps()
```

Tiers used in this codebase:

- **`summary`** — counts, top-level shape, a few sample IDs. <5 KB.
- **`ids`** — adds id/line/file/kind per item. 20–80 KB.
- **`full`** — every parsed field. Always paginated.

For `list_country_content` the equivalent is `include`: counts by default,
specific categories on demand (`include=["focuses", "events"]`), or all
(`include=["*"]`).

## Pagination

`limit` + `offset` are conventional for list outputs. Always return:

```json
{
  "total": 5101,
  "returned": 100,
  "truncated": true,
  "matches": [...]
}
```

Past N=`limit + offset` the response is incomplete; the caller knows to
re-call with a higher offset or use `files_only`/`detail` to shrink.

## Scan budgets

Distinct from output budgets — these bound the *cost* of producing a result,
not its size. `find_references` has a `scan_cap` that bails out of the
filesystem walk after N matches; the caller sees `scan_truncated=True`. Hot
loc keys can have 10K+ matches, and you don't want to walk all of them just
to truncate at the end.

```python
scan_cap = max(limit + offset, 100) * 50 if not files_only else 100_000
```

50× the requested window is enough headroom for pagination but bounds total
work. `files_only` raises the cap because the per-match cost is `dict[k] +=
1`, much cheaper than building a snippet.

## When to break the rules

`validate` is the one tool that can legitimately return a lot of data — the
issue stream is the entire point. The pattern there:

- Default `severity_min="info"` and `limit=500` — both can be raised.
- `counts_only=True` skips the issue array entirely.
- `strict=True` flips warnings → errors for reporting.

If you need to return a lot of data, **make the caller ask for it.** Don't
default to dumping.

## Checking your work

Two complementary tests:

1. **Unit test** with a synthetic large payload + `enforce_budget`:
   ```python
   huge = "x" * 50_000
   out = enforce_budget({"items": [huge]}, budget=1000, heavy_keys=("items",))
   assert out["size_truncated"] is True
   assert "items" not in out
   ```
2. **Live MCP probe** against a real mod. Spin up a stdio client, call the
   tool with a known-big input (e.g. `focus_graph("USA")`), and assert the
   JSON byte length is under your target. See the ISR probe in git history.

## Anti-patterns

- ❌ Returning the entire index dump because "let the agent filter."
  *Why it's bad: blows the budget before the agent ever sees it.*
- ❌ Returning detail "in case the caller needs it."
  *Why it's bad: detail is cheap to fetch on a second call; cap-breaking is irreversible.*
- ❌ Skipping `enforce_budget` because "the data is small in practice."
  *Why it's bad: data isn't small in practice for every mod / branch / language.
   It's free safety; always add it.*
- ❌ Hard-coding a different budget per tool.
  *Why it's bad: `BUDGET_BYTES` is the contract with the client. Tools that exceed
   it are server-side bugs.*
