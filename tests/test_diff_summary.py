"""diff_summary analysis tests (unit-level — git-touching paths covered in integration)."""

from __future__ import annotations

import inspect

from md_mcp.analysis import diff_summary as diff_summary_mod


def test_diff_summary_signature_supports_new_params():
    """Guard against accidental signature regression."""
    sig = inspect.signature(diff_summary_mod.diff_summary)
    params = sig.parameters
    for required in ("kinds", "with_ids", "limit"):
        assert required in params, f"diff_summary missing param: {required}"
    assert params["with_ids"].default is True
    assert params["limit"].default == 200


def test_diff_summary_non_repo_returns_error(tmp_path):
    out = diff_summary_mod.diff_summary(tmp_path)
    assert out["ok"] is False
    assert "git diff failed" in out["error"]
