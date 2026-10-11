from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tick_audit(mod_root: Path, payload: dict) -> dict:
    script = mod_root / "tools" / "analysis" / "tick_audit.py"
    module = _load_module(script, "_md_mcp_upstream_tick_audit")
    tag = payload.get("tag")
    report = module.build_report(tag_filter=tag)
    hooks = report.get("focus_hooks", []) if tag else report.get("hooks", [])
    total = len(hooks)
    try:
        limit = max(0, int(payload.get("limit", 20)))
        offset = max(0, int(payload.get("offset", 0)))
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": f"Invalid pagination values: {exc}"}
    selected = hooks[offset : offset + limit]
    cadence_summary = {}
    for cadence, data in report["cadences"].items():
        cadence_summary[cadence] = {
            "global_hooks": data["global_hooks"],
            "per_country_hooks": data["per_country_hooks"],
            "countries_with_own_hook": len(data["countries_with_own_hook"]),
            "global_work_units": data["global_work_units"],
            "events_direct": len(data["events_direct"]),
            "events_random_pool": len(data["events_random_pool"]),
            "timed_decisions": len(data["timed_decisions"]),
            "event_loops": len(data["event_loops"]),
        }
    return {
        "ok": True,
        "tag": tag,
        "totals": report["totals"],
        "cadences": cadence_summary,
        "timed_decisions": len(report["timed_decisions"]),
        "event_loops": len(report["event_loops"]),
        "event_fires": len(report["event_fires"]),
        "total": total,
        "returned": len(selected),
        "truncated": offset + limit < total,
        "hooks": [
            {
                "cadence": hook["cadence"],
                "scope": hook["scope"],
                "file": hook["file"],
                "line": hook["line"],
                "work_units": hook["work_units"],
                "direct_effect_calls": len(hook["direct_effect_calls"]),
                "reached_effects": len(hook["reached_effects"]),
                "events_direct": len(hook["events_direct"]),
                "events_random": len(hook["events_random"]),
            }
            for hook in selected
        ],
    }


def _estimate_gdp(mod_root: Path, payload: dict) -> dict:
    tag = str(payload.get("tag", "")).upper()
    if not re.fullmatch(r"[A-Z0-9]{2,4}", tag):
        return {"ok": False, "error": "tag must be a 2-4 character country tag"}

    script = mod_root / "tools" / "analysis" / "estimate_gdp.py"
    module = _load_module(script, "_md_mcp_upstream_estimate_gdp")
    states_dir = mod_root / "history" / "states"
    module.__dict__.update(
        {
            "BASE_DIR": str(mod_root),
            "STATES_DIR": str(states_dir),
            "COUNTRIES_DIR": str(mod_root / "history" / "countries"),
            "IDEAS_DIR": str(mod_root / "common" / "ideas"),
        }
    )

    idea_db = module.parse_all_ideas()
    states = []
    try:
        filenames = sorted(os.listdir(states_dir))
    except OSError as exc:
        return {"ok": False, "error": f"Could not list state history: {exc}", "tag": tag}
    for filename in filenames:
        if not filename.endswith(".txt"):
            continue
        path = states_dir / filename
        with path.open("r", encoding="utf-8") as state_file:
            content = state_file.read()
        owner = re.search(r"^\s*owner\s*=\s*(\w+)", content, re.MULTILINE)
        if owner and owner.group(1).upper() == tag:
            states.append(module.parse_state_file_from_content(content, filename))

    if not states:
        return {"ok": False, "error": f"No states found for {tag}", "tag": tag}
    result = module.compute_country_gdp(tag, states, idea_db)
    if result is None:
        return {"ok": False, "error": f"GDP calculation returned no result for {tag}", "tag": tag}
    return {
        "ok": True,
        "tag": tag,
        "gdp_total": round(result["gdp_total"], 2),
        "gdp_per_capita": round(result["gdp_per_capita"], 2),
        "population": result["population"],
        "population_m": round(result["population_m"], 2),
        "states": result["num_states"],
        "overall_productivity": round(result["overall_productivity"], 2),
        "breakdown": {
            "buildings": round(result["gdp_from_buildings_scaled"], 2),
            "healthcare": round(result["gdp_from_healthcare"], 2),
            "agriculture": round(result["gdp_from_agriculture_scaled"], 2),
            "resources": round(result["gdp_from_resources_scaled"], 2),
        },
    }


def _paginate_tree(value: Any, prefix: str, limit: int, offset: int, metadata: dict) -> Any:
    if isinstance(value, list):
        total = len(value)
        nested = any(_contains_list(item) for item in value)
        page = value if nested else value[offset : offset + limit]
        metadata[prefix] = {
            "total": total,
            "returned": len(page),
            "truncated": not nested and offset + limit < total,
        }
        return [
            _paginate_tree(
                item,
                f"{prefix}[{index if nested else offset + index}]",
                limit,
                offset,
                metadata,
            )
            for index, item in enumerate(page)
        ]
    if isinstance(value, dict):
        return {
            key: _paginate_tree(item, f"{prefix}.{key}" if prefix else key, limit, offset, metadata)
            for key, item in value.items()
        }
    return value


def _contains_list(value: Any) -> bool:
    if isinstance(value, list):
        return True
    if isinstance(value, dict):
        return any(_contains_list(item) for item in value.values())
    return False


def _ai_path_report(mod_root: Path, payload: dict) -> dict:
    tag = str(payload.get("tag", "")).upper()
    if not re.fullmatch(r"[A-Z0-9]{2,4}", tag):
        return {"ok": False, "error": "tag must be a 2-4 character country tag"}
    try:
        limit = max(0, int(payload.get("limit", 15)))
        offset = max(0, int(payload.get("offset", 0)))
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": f"Invalid pagination values: {exc}"}
    requested = payload.get("section")
    if requested is None:
        sections = list(
            (
                "rule",
                "wiring",
                "owners",
                "matrix",
                "graph",
                "plans",
                "rewards",
                "mechanics",
                "government",
            )
        )
    elif isinstance(requested, str):
        sections = [requested]
    elif isinstance(requested, list) and all(isinstance(item, str) for item in requested):
        sections = requested
    else:
        return {"ok": False, "error": "section must be a string or list of strings"}
    script = mod_root / "tools" / "analysis" / "ai_path_report.py"
    module = _load_module(script, "_md_mcp_upstream_ai_path_report")
    allowed = set(module.SECTIONS) | {"all"}
    if not sections or any(section not in allowed for section in sections):
        return {"ok": False, "error": f"section must be one of: {', '.join(sorted(allowed))}"}
    report = module.build_report(str(mod_root), tag, 0)
    if "all" not in sections:
        selected = set(sections)
        if "wiring" in selected:
            selected.add("rule")
        for name in module.SECTIONS:
            if name not in selected:
                report.pop(name, None)
    metadata: dict = {}
    result = _paginate_tree(report, "", limit, offset, metadata)
    result.update({"ok": True, "sections": sections, "pagination": metadata})
    return result


def run(operation: str, mod_root: Path, payload: dict) -> dict:
    if operation == "tick_audit":
        return _tick_audit(mod_root, payload)
    if operation == "estimate_gdp":
        return _estimate_gdp(mod_root, payload)
    if operation == "ai_path_report":
        return _ai_path_report(mod_root, payload)
    return {"ok": False, "error": f"Unknown operation: {operation}"}


def main() -> int:
    try:
        operation, root, encoded = sys.argv[1:4]
        payload = json.loads(encoded)
        with contextlib.redirect_stdout(io.StringIO()):
            result = run(operation, Path(root), payload)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
