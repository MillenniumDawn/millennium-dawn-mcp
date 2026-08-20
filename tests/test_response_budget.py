"""Tests for the response-shaping helpers and the new tool size guards."""

from __future__ import annotations

import json

import pytest

from md_mcp.tools.validation_tools import _filter_and_cap
from md_mcp.util.response import BUDGET_BYTES, clip_strings, enforce_budget, paginate


def _byte_size(obj: object) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def test_paginate_basic():
    items = list(range(10))
    sliced, truncated, total = paginate(items, offset=2, limit=3)
    assert sliced == [2, 3, 4]
    assert truncated is True
    assert total == 10


def test_paginate_past_end_not_truncated():
    items = list(range(5))
    sliced, truncated, total = paginate(items, offset=0, limit=10)
    assert sliced == items
    assert truncated is False
    assert total == 5


def test_paginate_clamps_negatives():
    items = list(range(5))
    sliced, _, _ = paginate(items, offset=-2, limit=2)
    assert sliced == [0, 1]


@pytest.mark.parametrize("limit", [-1, 0])
def test_paginate_nonpositive_limit_yields_empty_and_truncated(limit):
    items = list(range(5))
    sliced, truncated, total = paginate(items, offset=0, limit=limit)
    assert sliced == []
    assert total == 5
    assert truncated is True


def test_paginate_limit_at_total_not_truncated():
    items = list(range(5))
    sliced, truncated, total = paginate(items, offset=0, limit=5)
    assert sliced == items
    assert truncated is False
    assert total == 5


def test_paginate_empty_with_negative_limit():
    sliced, truncated, total = paginate([], offset=0, limit=-1)
    assert sliced == []
    assert total == 0
    assert truncated is False


def test_enforce_budget_pass_through_when_small():
    result = {"ok": True, "items": list(range(5))}
    out = enforce_budget(result, heavy_keys=("items",))
    assert out is result
    assert "size_truncated" not in out


def test_enforce_budget_drops_heavy_keys_when_over():
    # 50 KB of payload, budget 1 KB → dropped.
    huge = "x" * 50_000
    result = {"ok": True, "items": [huge]}
    out = enforce_budget(result, budget=1000, heavy_keys=("items",))
    assert out.get("size_truncated") is True
    assert "items" not in out
    assert out.get("items_dropped") == 1


def test_enforce_budget_default_constant_is_sane():
    assert BUDGET_BYTES >= 50_000


def test_enforce_budget_measures_utf8_bytes_not_chars():
    # 400 chars of 3-byte-UTF8 content: ~425 chars (under budget by the old
    # char-counting bug) but ~1225 UTF-8 bytes (over budget for real).
    multibyte = "€" * 400
    result = {"ok": True, "items": [multibyte]}
    out = enforce_budget(result, budget=1000, heavy_keys=("items",))
    assert out.get("size_truncated") is True
    assert "items" not in out
    assert out.get("items_dropped") == 1
    assert _byte_size(out) <= 1000


def test_enforce_budget_circular_reference_returns_bounded_error():
    circular: dict = {"ok": True}
    circular["self"] = circular
    out = enforce_budget(circular, budget=1000)
    assert out.get("ok") is False
    assert out.get("size_truncated") is True
    assert "error" in out
    assert _byte_size(out) <= 1000


def test_enforce_budget_oversized_without_heavy_keys_falls_back():
    result = {"ok": True, "notes": "x" * 5000}
    out = enforce_budget(result, budget=1000)
    assert out.get("size_truncated") is True
    assert out.get("ok") is False
    assert _byte_size(out) <= 1000


def test_enforce_budget_heavy_keys_insufficient_falls_back():
    result = {"ok": True, "items": ["x" * 100], "notes": "y" * 5000}
    out = enforce_budget(result, budget=1000, heavy_keys=("items",))
    assert out.get("size_truncated") is True
    assert out.get("ok") is False
    assert _byte_size(out) <= 1000


def test_clip_strings():
    items = [{"snippet": "abcdefghij"}, {"snippet": "xy"}]
    out = clip_strings(items, "snippet", 3)
    assert out[0]["snippet"] == "abc"
    assert out[1]["snippet"] == "xy"


def test_clip_strings_counts_utf8_bytes_not_chars():
    # Each euro sign is three UTF-8 bytes. Clipping to 4 bytes keeps one euro
    # sign (3 bytes) and drops the partial second one instead of emitting
    # invalid UTF-8 or overshooting to 4 characters (12 bytes).
    items = [{"snippet": "€€€"}]
    out = clip_strings(items, "snippet", 4)
    assert out[0]["snippet"] == "€"
    assert len(out[0]["snippet"].encode("utf-8")) <= 4


def test_clip_strings_leaves_short_multibyte_untouched():
    items = [{"snippet": "€"}]
    out = clip_strings(items, "snippet", 3)
    assert out[0]["snippet"] == "€"


def test_validate_filter_and_cap_severity():
    issues = [
        {"severity": "info", "msg": "i"},
        {"severity": "warning", "msg": "w"},
        {"severity": "error", "msg": "e"},
    ]
    kept, _, total = _filter_and_cap(issues, severity_min="warning", limit=10)
    assert {i["severity"] for i in kept} == {"warning", "error"}
    assert total == 2


def test_validate_filter_and_cap_limit():
    issues = [{"severity": "error"} for _ in range(20)]
    kept, truncated, total = _filter_and_cap(issues, severity_min="info", limit=5)
    assert len(kept) == 5
    assert truncated is True
    assert total == 20
