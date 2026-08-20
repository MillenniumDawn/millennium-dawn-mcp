"""Performance budgets — guards against regressions in the cold-build path.

These run under the `@integration` marker so they only execute against a real
Millennium-Dawn checkout (`MD_MOD_ROOT` set). Budgets reflect what the post-M3+
ProcessPool+fast-GFX refactor achieves on a 14-core M-series Mac with the cache
cold.
"""

from __future__ import annotations

import os
import time

# pi-lens-ignore: reportMissingImports
import pytest

from md_mcp.indexes import (
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
)

# Per-index ceilings, in seconds. Generous enough to absorb CI variance but
# tight enough to flag obvious regressions (e.g. removing the GFX fast scanner
# would push that bucket past 15s). Calibrated on a 14-core Mac; MD_PERF_BUDGET_SCALE
# scales every budget for slower CI runners (the nightly workflow sets it to 3).
_BUDGET_SCALE = float(os.environ.get("MD_PERF_BUDGET_SCALE", "1.0"))
_BUDGETS = {
    "Focus": 10.0 * _BUDGET_SCALE,
    "Loc": 10.0 * _BUDGET_SCALE,
    "Event": 5.0 * _BUDGET_SCALE,
    "Decision": 3.0 * _BUDGET_SCALE,
    "Idea": 3.0 * _BUDGET_SCALE,
    "Gfx": 5.0 * _BUDGET_SCALE,
}
_TOTAL_BUDGET = 30.0 * _BUDGET_SCALE  # the plan's stated target


@pytest.mark.integration
def test_cold_build_under_budget(real_mod_root, tmp_path):
    """Build every index from a clean cache and assert each stays within budget."""
    cache = tmp_path / ".md-mcp-cache"
    timings: dict[str, float] = {}

    indexes: list[tuple[str, type]] = [
        ("Focus", FocusIndex),
        ("Loc", LocalisationIndex),
        ("Event", EventIndex),
        ("Decision", DecisionIndex),
        ("Idea", IdeaIndex),
        ("Gfx", GfxIndex),
    ]
    for name, cls in indexes:
        idx = cls(real_mod_root, cache, None)
        t0 = time.perf_counter()
        idx.ensure_fresh()
        timings[name] = time.perf_counter() - t0
        assert idx.list_keys(), f"{name} index produced no keys"

    breakdown = ", ".join(f"{n}={t:.2f}s" for n, t in timings.items())
    total = sum(timings.values())

    for name, elapsed in timings.items():
        budget = _BUDGETS[name]
        assert (
            elapsed < budget
        ), f"{name} cold build took {elapsed:.2f}s (budget {budget}s). All timings: {breakdown}"

    assert (
        total < _TOTAL_BUDGET
    ), f"Total cold build {total:.2f}s exceeds {_TOTAL_BUDGET}s budget. Breakdown: {breakdown}"
