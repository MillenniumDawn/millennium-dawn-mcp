"""Validation MCP tools.

* `validate(validator?, staged_only?, files?, severity_min?, limit?)` — run one or all validators
* `validate_list(limit?, offset?)` — enumerate available validators
"""

from __future__ import annotations

from typing import Optional

from ..config import Settings
from ..util.response import coerce_int, enforce_budget, paginate
from ..validators import SEVERITY_RANK, SLOW_VALIDATORS, ValidatorRunner, available_validators


def validate_list_tool(
    settings: Settings,
    *,
    limit: int | float | str | None = 200,
    offset: int | float | str | None = 0,
) -> dict:
    """List validators known to this mod checkout, with a bounded page."""
    try:
        limit = coerce_int(limit, name="limit", default=200)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return enforce_budget({"ok": False, "error": str(exc)})

    infos = available_validators(settings.mod_root)
    all_validators = [
        {"name": v.name, "title": v.title, "title_source": v.title_source, "module": v.module_name}
        for v in infos
    ]
    validators, truncated, total = paginate(all_validators, offset=offset, limit=limit)
    return enforce_budget(
        {
            "ok": True,
            "total": total,
            "returned": len(validators),
            "truncated": truncated,
            "validators": validators,
        },
        heavy_keys=("validators",),
    )


def _apply_strict(counts: dict) -> dict:
    """Fold the warning count into error and zero it out, in place."""
    counts["error"] = counts.get("error", 0) + counts.get("warning", 0)
    counts["warning"] = 0
    return counts


def _filter_and_cap(
    issues: list[dict],
    *,
    severity_min: str,
    limit: int,
) -> tuple[list[dict], bool, int]:
    """Apply severity floor + cap. Returns (kept, truncated, total_after_filter)."""
    floor = SEVERITY_RANK.get(severity_min, 0)
    filtered = [i for i in issues if SEVERITY_RANK.get(i.get("severity", "info"), 0) >= floor]
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
    files: Optional[list[str]] = None,
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
        if strict and "counts" in result:
            result["counts"] = _apply_strict(result["counts"])
        issues = result.get("issues", [])
        kept, truncated, total = _filter_and_cap(issues, severity_min=severity_min, limit=limit)
        result["issues_total_after_filter"] = total
        result["truncated"] = truncated
        if counts_only:
            result.pop("issues", None)
        else:
            result["issues"] = kept
        return enforce_budget(result, heavy_keys=("issues",))

    # Run-all path: skip the slow opt-in validators by default.
    infos = available_validators(settings.mod_root)
    targets = [v for v in infos if v.name not in SLOW_VALIDATORS]

    aggregated: list[dict] = []
    per_validator: list[dict] = []
    overall = {"error": 0, "warning": 0, "info": 0}

    for v in targets:
        result = runner.run(v.name, staged_only=staged_only, files=files)
        per_validator.append(
            {
                "name": v.name,
                "title": v.title,
                "ok": result.get("ok"),
                # Copied, not aliased: the strict fold below rewrites this dict,
                # and _apply_strict mutates in place -- without the copy it would
                # reach back into the runner's own result.
                "counts": dict(result.get("counts", {})),
                "error": result.get("error"),
            }
        )
        if result.get("ok"):
            aggregated.extend(result["issues"])
            for k, n in result["counts"].items():
                overall[k] = overall.get(k, 0) + n

    if strict:
        overall = _apply_strict(overall)
        # And each per-validator breakdown, or it no longer sums to the strict
        # total and a caller reconciling the two sees inconsistent numbers.
        # Must come after the accumulation above, which reads the raw counts.
        for entry in per_validator:
            # Guarded the same way the single-validator path is: a validator
            # that failed to run has no counts, and folding {} would invent an
            # {"error": 0, "warning": 0} it never reported.
            if entry["counts"]:
                entry["counts"] = _apply_strict(entry["counts"])

    kept, truncated, total = _filter_and_cap(aggregated, severity_min=severity_min, limit=limit)

    summary: dict = {
        "ok": all(v["ok"] for v in per_validator),
        "skipped_slow": sorted(SLOW_VALIDATORS),
        "validators": per_validator,
        "counts": overall,
        "issues_total_after_filter": total,
        "truncated": truncated,
    }
    if not counts_only:
        summary["issues"] = kept

    return enforce_budget(summary, heavy_keys=("issues",))
