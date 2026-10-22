"""Tests for the response-shaping helpers: pagination and the byte budget."""

from __future__ import annotations

import json

import pytest

from md_mcp.util.response import (
    BUDGET_BYTES,
    MAX_TEXT_BYTES,
    bounded_text,
    clip_utf8,
    coerce_int,
    enforce_budget,
    fit_prefix,
    paginate,
)


def _byte_size(obj: object) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


@pytest.mark.parametrize(
    ("count", "offset", "limit", "sliced", "truncated"),
    [
        (10, 2, 3, [2, 3, 4], True),
        (5, 0, 10, [0, 1, 2, 3, 4], False),
        (5, 0, 5, [0, 1, 2, 3, 4], False),
        (5, -2, 2, [0, 1], True),
        (5, 0, -1, [], True),
        (5, 0, 0, [], True),
        (0, 0, -1, [], False),
        (300, 0, None, list(range(200)), True),
        (10, 0, "3", [0, 1, 2], True),
        (10, 0, 3.5, [0, 1, 2], True),
        (10, None, 3, [0, 1, 2], True),
        (10, "2", 3, [2, 3, 4], True),
        (10, 2.9, 3, [2, 3, 4], True),
        (5, "-3", 2, [0, 1], True),
    ],
)
def test_paginate(count, offset, limit, sliced, truncated):
    assert paginate(list(range(count)), offset=offset, limit=limit) == (sliced, truncated, count)


@pytest.mark.parametrize(
    ("offset", "limit", "name"),
    [
        (0, "not-a-number", "limit"),
        (0, float("inf"), "limit"),
        (0, float("nan"), "limit"),
        ("not-a-number", 3, "offset"),
    ],
)
def test_paginate_rejects_bounds_it_cannot_coerce(offset, limit, name):
    with pytest.raises(ValueError, match=name):
        paginate(list(range(5)), offset=offset, limit=limit)


def test_coerce_int_rejects_bool_pass_through_but_still_coerces():
    # bool is an int subclass; coerce_int explicitly excludes it from the
    # pass-through branch but still routes it through the float path, so
    # True/False coerce to plain ints 1/0 rather than being rejected.
    assert coerce_int(True, name="limit", default=0) == 1
    assert coerce_int(False, name="limit", default=0) == 0
    assert type(coerce_int(True, name="limit", default=0)) is int


def test_enforce_budget_pass_through_when_small():
    result = {"ok": True, "items": list(range(5))}
    out = enforce_budget(result, heavy_keys=("items",))
    assert out is result
    assert "size_truncated" not in out


def test_enforce_budget_default_constant_is_sane():
    assert BUDGET_BYTES >= 50_000


def test_enforce_budget_drops_heavy_keys_on_a_copy():
    result = {"ok": True, "items": ["x" * 50_000]}
    before = dict(result)

    out = enforce_budget(result, budget=1000, heavy_keys=("absent", "items"))

    assert out == {"ok": True, "items_dropped": 1, "size_truncated": True}
    assert result == before


def test_enforce_budget_measures_utf8_bytes_not_chars():
    # 400 three-byte characters: about 425 chars of JSON, but about 1225 bytes.
    result = {"ok": True, "items": ["€" * 400]}
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


@pytest.mark.parametrize(
    ("result", "heavy_keys", "lists_remaining_keys"),
    [
        ({"ok": True, "notes": "x" * 5000}, (), True),
        ({"ok": True, "items": ["x" * 100], "notes": "y" * 5000}, ("items",), True),
        # So many keys that the fallback's own per-key listing is over budget.
        ({f"key_{n}": "x" * 20 for n in range(200)}, (), False),
    ],
)
def test_enforce_budget_falls_back_when_drops_are_not_enough(
    result, heavy_keys, lists_remaining_keys
):
    out = enforce_budget(result, budget=1000, heavy_keys=heavy_keys)
    assert out.get("size_truncated") is True
    assert out.get("ok") is False
    assert ("remaining_key_sizes" in out) is lists_remaining_keys
    assert _byte_size(out) <= 1000


def test_fit_prefix_returns_result_untouched_when_it_fits():
    result = {"ok": True, "items": ["a", "b"]}

    def build(count: int) -> dict:
        raise AssertionError("build must not run when the result already fits")

    assert fit_prefix(result, 2, build, budget=1000) is result


def test_fit_prefix_keeps_the_longest_prefix_that_fits():
    items = ["x" * 10] * 50
    result = {"ok": True, "items": items}

    def build(count: int) -> dict:
        return {**result, "truncated": True, "items": items[:count]}

    out = fit_prefix(result, len(items), build, budget=300)
    kept = len(out["items"])
    assert 0 < kept < len(items)
    assert out["truncated"] is True
    assert _byte_size(out) <= 300
    assert _byte_size(build(kept + 1)) > 300


def test_fit_prefix_returns_the_empty_prefix_when_nothing_fits():
    result = {"txt": "é" * 500}
    out = fit_prefix(result, 500, lambda chars: {"txt": result["txt"][:chars]}, budget=5)
    assert out == {"txt": ""}


@pytest.mark.parametrize(
    ("text", "max_bytes", "expected"),
    [
        ("héllo", 6, ("héllo", 6, 6, False)),
        # Five bytes would split the third two-byte character, so it is dropped whole.
        ("éééé", 5, ("éé", 8, 4, True)),
    ],
)
def test_clip_utf8_cuts_on_a_character_boundary(text, max_bytes, expected):
    assert clip_utf8(text, max_bytes) == expected


def test_bounded_text_binary_search_trims_txt_to_fit():
    junk = "y" * 99_000
    out = bounded_text(
        {"ok": True, "kind": "focus", "changed": False, "junk": junk},
        "x" * 2_000,
    )

    assert out["txt_truncated"] is True
    assert _byte_size(out) <= BUDGET_BYTES
    assert len(out["txt"]) < 2_000


def test_bounded_text_returns_bounded_fallback_when_nothing_fits():
    junk = "y" * 99_900
    out = bounded_text(
        {"ok": True, "kind": "focus", "changed": False, "junk": junk},
        "x" * 2_000,
    )

    assert _byte_size(out) <= BUDGET_BYTES
    assert "exceeded byte budget" in out["error"]


@pytest.mark.parametrize("text", ["", "small é\n", "x" * MAX_TEXT_BYTES])
def test_bounded_text_preserves_complete_text_and_existing_note(text):
    out = bounded_text({"ok": True, "note": "Keep the BOM", "warnings": ["review"]}, text)

    assert out == {
        "ok": True,
        "note": "Keep the BOM",
        "warnings": ["review"],
        "txt": text,
        "txt_bytes": len(text.encode("utf-8")),
        "txt_returned_bytes": len(text.encode("utf-8")),
        "txt_truncated": False,
    }


def test_bounded_text_keeps_the_longest_json_prefix_and_existing_diagnostics():
    text = "é\x01" * 20_000
    out = bounded_text({"ok": True, "note": "Keep the BOM", "warnings": ["review"]}, text)

    assert 0 < len(out["txt"]) < len(text)
    assert text.startswith(out["txt"])
    assert out["txt_truncated"] is True
    assert out["txt_bytes"] == len(text.encode("utf-8"))
    assert out["txt_returned_bytes"] == len(out["txt"].encode("utf-8"))
    assert out["warnings"] == ["review"]
    assert out["note"].startswith("Keep the BOM; ")
    assert "do NOT write clipped content back" in out["note"]
    assert _byte_size(out) <= BUDGET_BYTES
    longer = text[: len(out["txt"]) + 1]
    assert (
        _byte_size({**out, "txt": longer, "txt_returned_bytes": len(longer.encode("utf-8"))})
        > BUDGET_BYTES
    )


def test_bounded_text_dropped_payload_does_not_claim_returned_bytes():
    out = bounded_text(
        {"ok": True, "warnings": ["x" * BUDGET_BYTES]},
        "text",
        heavy_keys=("txt", "warnings"),
    )

    assert out["ok"] is True
    assert "txt" not in out
    assert out["txt_returned_bytes"] == 0
    assert out["txt_truncated"] is True
    assert out["warnings_dropped"] == 1
    assert _byte_size(out) <= BUDGET_BYTES
