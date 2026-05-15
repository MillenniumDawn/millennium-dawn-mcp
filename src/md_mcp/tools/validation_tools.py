"""Validation MCP tools.

  * `validate(validator?, staged_only?, files?, severity_min?, limit?)` — run one or all validators
  * `validate_list()` — enumerate available validators
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from ..config import Settings
from ..util.response import enforce_budget
from ..validators import ValidatorRunner, available_validators


_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


def validate_list_tool(settings: Settings) -> dict:
    """List every validator known to this mod checkout."""
    infos = available_validators(settings.mod_root)
    return {
        "ok": True,
        "validators": [
            {"name": v.name, "title": v.title, "module": v.module_name}
            for v in infos
        ],
    }


def _filter_and_cap(
    issues: List[dict],
    *,
    severity_min: str,
    limit: int,
) -> tuple[List[dict], bool, int]:
    """Apply severity floor + cap. Returns (kept, truncated, total_after_filter)."""
    floor = _SEVERITY_RANK.get(severity_min, 0)
    filtered = [
        i for i in issues
        if _SEVERITY_RANK.get(i.get("severity", "info"), 0) >= floor
    ]
    total = len(filtered)
    if limit >= 0 and total > limit:
        return filtered[:limit], True, total
    return filtered, False, total


def validate_tool(
    settings: Settings,
    runner: ValidatorRunner,
    *,
    validator: Optional[str] = None,
    staged_only: bool = False,
    files: Optional[List[str]] = None,
    strict: bool = False,
    severity_min: str = "info",
    limit: int = 500,
    counts_only: bool = False,
) -> dict:
    """Run validators and return structured issues.

    Args:
      validator     — name from `validate_list`; if omitted, runs every fast validator
      staged_only   — restrict to git-staged files (much faster for mid-edit checks)
      files         — post-filter issues to ones in this set of paths (mod-relative)
      strict        — treat warnings as errors in the summary counts
      severity_min  — "info" | "warning" | "error" — drop issues below this floor
      limit         — cap issues returned (counts remain accurate). Use -1 for no cap.
      counts_only   — skip the issues array entirely, return just per-validator counts
    """
    if validator is not None:
        result = runner.run(validator, staged_only=staged_only, files=files)
        if not result.get("ok"):
            return result
        issues = result.get("issues", [])
        kept, truncated, total = _filter_and_cap(
            issues, severity_min=severity_min, limit=limit
        )
        result["issues"] = [] if counts_only else kept
        result["issues_total_after_filter"] = total
        result["truncated"] = truncated
        if counts_only:
            result.pop("issues", None)
        return enforce_budget(result, heavy_keys=("issues",))

    # Run-all path: skip the slow opt-in validators by default.
    infos = available_validators(settings.mod_root)
    slow = {"unused_scripted", "unused_textures"}
    targets = [v for v in infos if v.name not in slow]

    aggregated: List[dict] = []
    per_validator: List[dict] = []
    overall = {"error": 0, "warning": 0, "info": 0}

    for v in targets:
        result = runner.run(v.name, staged_only=staged_only, files=files)
        per_validator.append(
            {
                "name": v.name,
                "title": v.title,
                "ok": result.get("ok"),
                "counts": result.get("counts", {}),
                "error": result.get("error"),
            }
        )
        if result.get("ok"):
            aggregated.extend(result["issues"])
            for k, n in result["counts"].items():
                overall[k] = overall.get(k, 0) + n

    if strict:
        overall["error"] = overall.get("error", 0) + overall.get("warning", 0)
        overall["warning"] = 0

    kept, truncated, total = _filter_and_cap(
        aggregated, severity_min=severity_min, limit=limit
    )

    summary: dict = {
        "ok": True,
        "skipped_slow": sorted(slow),
        "validators": per_validator,
        "counts": overall,
        "issues_total_after_filter": total,
        "truncated": truncated,
    }
    if not counts_only:
        summary["issues"] = kept

    return enforce_budget(summary, heavy_keys=("issues",))
