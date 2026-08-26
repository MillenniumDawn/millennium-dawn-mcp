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
from typing import Any, Iterable, Sequence

BUDGET_BYTES = 100_000


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
        try:
            if _jsize(result) <= budget:
                return result
        except (TypeError, ValueError) as exc:
            return _unserializable_result(exc, budget)

    result["size_truncated"] = True
    try:
        if _jsize(result) <= budget:
            return result
    except (TypeError, ValueError) as exc:
        return _unserializable_result(exc, budget)
    return _bounded_fallback(result, budget)


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


def clip_strings(items: Iterable[dict], key: str, max_bytes: int) -> list[dict]:
    """Return a copy of `items` with `item[key]` clipped to `max_bytes` UTF-8 bytes.

    Clipping is by UTF-8 byte length, matching the byte budget the rest of this
    module works in (see `enforce_budget` and `BUDGET_BYTES`). Counting Unicode
    characters instead let a clipped multibyte string stay up to four times its
    intended byte size, so a tool budgeting with `clip_strings` overshot before
    `enforce_budget` caught it and dropped whole keys. A clip that would fall in
    the middle of a multi-byte code point drops that partial code point rather
    than emitting invalid UTF-8.
    """
    out: list[dict] = []
    for it in items:
        v = it.get(key)
        if isinstance(v, str) and len(v.encode("utf-8")) > max_bytes:
            clipped = v.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
            it = {**it, key: clipped}
        out.append(it)
    return out
