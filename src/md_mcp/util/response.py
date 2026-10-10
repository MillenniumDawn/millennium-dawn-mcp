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
from typing import Any, Callable, Sequence

BUDGET_BYTES = 100_000

# Cap for a text payload (report, fixed file); leaves room for status fields and warnings.
MAX_TEXT_BYTES = BUDGET_BYTES - 12_000


def coerce_int(value: Any, *, name: str, default: int) -> int:
    """Coerce a pagination bound (`limit`/`offset`) to `int`.

    `None` falls back to `default` (matches the argument being omitted); a
    plain `int` (not `bool`) passes through with a single isinstance check;
    numeric `str`/`float` coerce via `int(float(value))`. Anything else
    raises `ValueError` naming `name` and the offending value.
    """
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def paginate(
    items: Sequence[Any],
    offset: int | float | str | None = 0,
    limit: int | float | str | None = 200,
) -> tuple[list[Any], bool, int]:
    """Slice `items[offset : offset+limit]`. Returns (slice, truncated, total).

    `offset`/`limit` accept `int`, numeric `str`/`float`, or `None` (falls
    back to the default shown in the signature); anything else raises
    `ValueError`.
    """
    offset = coerce_int(offset, name="offset", default=0)
    limit = coerce_int(limit, name="limit", default=200)
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
    """If `result` JSON-encodes larger than `budget` bytes, drop heavy list keys.

    Drops in `heavy_keys` order until the result fits or we run out of keys.
    Replaces each dropped key with `<key>_dropped: <original_count>` and sets
    `size_truncated=True` on the result. This is a safety net — tools should
    still apply their own paginate/detail defaults so they never reach here.

    The caller's input dict is never mutated: when over budget, drops are applied
    to a shallow copy that is returned.

    Guarantees: the returned dict always JSON-encodes to `<= budget` UTF-8
    bytes and is always JSON-serializable, even if `result` itself isn't or
    if dropping every heavy key still leaves it over budget.
    """
    try:
        if _jsize(result) <= budget:
            return result
    except (TypeError, ValueError) as exc:
        return _unserializable_result(exc, budget)

    # Over budget: work on a shallow copy so the caller's original dict is left
    # unchanged. Drops only touch top-level keys, so a shallow copy is enough.
    # The happy path above returned before this point, so the copy only happens
    # in the over-budget case.
    result = dict(result)

    for k in heavy_keys:
        if k not in result:
            continue
        v = result[k]
        count = len(v) if hasattr(v, "__len__") else None
        del result[k]
        result[f"{k}_dropped"] = count
        result["size_truncated"] = True
        if _jsize(result) <= budget:
            return result

    return _bounded_fallback(result, budget)


def fit_prefix(
    result: dict,
    length: int,
    build: Callable[[int], dict],
    *,
    budget: int = BUDGET_BYTES,
) -> dict:
    """Return `result` if it fits `budget`, else `build(n)` for the largest fitting `n <= length`.

    For keeping a useful prefix of one list or string where `enforce_budget`
    would drop the whole key. `build(n)` must grow with `n`.
    """
    if _jsize(result) <= budget:
        return result
    low, high = 0, length
    while low < high:
        middle = (low + high + 1) // 2
        if _jsize(build(middle)) <= budget:
            low = middle
        else:
            high = middle - 1
    return build(low)


def _jsize(obj: Any) -> int:
    """UTF-8 byte length of `obj`'s JSON encoding. Raises on unserializable input."""
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def _unserializable_result(exc: Exception, budget: int) -> dict:
    return {
        "ok": False,
        "error": f"response is not JSON-serializable: {exc}"[:200],
        "size_truncated": True,
        "budget": budget,
    }


def _bounded_fallback(result: dict, budget: int) -> dict:
    """Last-resort response when `result` is serializable but still over budget
    after dropping every heavy key. Keeps only counts, never content."""
    dropped_keys = sorted(k[: -len("_dropped")] for k in result if k.endswith("_dropped"))
    remaining_key_sizes = {
        k: (len(v) if hasattr(v, "__len__") else None)
        for k, v in result.items()
        if k != "size_truncated" and not k.endswith("_dropped")
    }
    fallback = {
        "ok": False,
        "error": "response exceeded byte budget even after dropping heavy keys",
        "size_truncated": True,
        "budget": budget,
        "dropped_keys": dropped_keys,
        "remaining_key_sizes": remaining_key_sizes,
    }
    if _jsize(fallback) <= budget:
        return fallback
    return {
        "ok": False,
        "error": "response exceeded byte budget even after dropping heavy keys",
        "size_truncated": True,
        "budget": budget,
    }


def clip_utf8(text: str, max_bytes: int) -> tuple[str, int, int, bool]:
    """Return text clipped at a UTF-8 boundary plus size metadata."""
    raw = text.encode("utf-8")
    total = len(raw)
    if total <= max_bytes:
        return text, total, total, False
    clipped = raw[:max_bytes].decode("utf-8", "ignore")
    return clipped, total, len(clipped.encode("utf-8")), True
