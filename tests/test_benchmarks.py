"""Smoke tests for the live cache benchmark's MCP client."""

from __future__ import annotations

import asyncio

import pytest


def test_live_cache_sample_uses_current_sdk(fake_mod_root, cache_dir):
    pytest.importorskip("resource", reason="cache benchmark requires Unix resource accounting")
    from benchmarks.cache_paths import _live_sample

    focus = fake_mod_root / "common/national_focus/test.txt"
    focus.write_text(
        "focus_tree = { id = ISR focus = { id = ISR_root x = 0 y = 0 } }",
        encoding="utf-8",
    )

    sample = asyncio.run(asyncio.wait_for(_live_sample(fake_mod_root, cache_dir), timeout=20))

    assert sample["node_count"] == 1
    assert len(sample["payload_sha256"]) == 64
