"""Response-shaping helpers — pagination + last-line-defense budget guard.

Every list-bearing MCP tool returns JSON over stdio, and the client (e.g. Claude
Code) enforces a per-call output token cap. A single oversized response not only
fails the call, it pollutes the agent's recovery loop. So tools should:

  1. Default to small, summarised output.
  2. Accept `limit` / `offset` (`paginate`) and report `total` + `truncated`.
  3. Wrap their final result in `enforce_budget(result, heavy_keys=...)` as a
     belt-and-braces guard if the caller passes oversized limits.

`BUDGET_BYTES` is the JSON-byte ceiling we self-impose. ~100 KB ≈ 25 K tokens at
the usual 4-byte/token heuristic, which clears every MCP client cap we've seen
with headroom for the protocol envelope.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, List, Sequence, Tuple

BUDGET_BYTES = 100_000


def paginate(items: Sequence[Any], offset: int = 0, limit: int = 200) -> Tuple[List[Any], bool, int]:
    """Slice `items[offset : offset+limit]`. Returns (slice, truncated, total)."""
    total = len(items)
    if offset < 0:
        offset = 0
    if limit < 0:
        limit = 0
    sliced = list(items[offset : offset + limit])
    truncated = (offset + limit) < total
    return sliced, truncated, total


def enforce_budget(
    result: dict,
    *,
    budget: int = BUDGET_BYTES,
    heavy_keys: Sequence[str] = (),
) -> dict:
    """If `result` JSON-encodes larger than `budget`, drop heavy list keys.

    Drops in `heavy_keys` order until the result fits or we run out of keys.
    Replaces each dropped key with `<key>_dropped: <original_count>` and sets
    `size_truncated=True` on the result. This is a safety net — tools should
    still apply their own paginate/detail defaults so they never reach here.
    """
    if _jsize(result) <= budget:
        return result
    for k in heavy_keys:
        if k in result:
            v = result[k]
            count = len(v) if hasattr(v, "__len__") else None
            del result[k]
            result[f"{k}_dropped"] = count
            result["size_truncated"] = True
            if _jsize(result) <= budget:
                return result
    result["size_truncated"] = True
    return result


def _jsize(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


def clip_strings(items: Iterable[dict], key: str, max_chars: int) -> List[dict]:
    """Return a copy of `items` with `item[key]` clipped to `max_chars`."""
    out: List[dict] = []
    for it in items:
        v = it.get(key)
        if isinstance(v, str) and len(v) > max_chars:
            it = {**it, key: v[:max_chars]}
        out.append(it)
    return out
