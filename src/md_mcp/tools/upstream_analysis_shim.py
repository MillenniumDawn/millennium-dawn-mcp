from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path


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


def _game_log_summary(mod_root: Path, payload: dict) -> dict:
    raw_path = payload.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        return {"ok": False, "error": "path must be an absolute path"}
    log_path = Path(raw_path)
    if log_path.suffix.lower() not in {".log", ".txt"}:
        return {"ok": False, "error": "path must end in .log or .txt"}
    if not log_path.is_file():
        return {"ok": False, "error": f"Log file not found: {log_path}"}
    try:
        top = max(0, int(payload.get("top", 15)))
        limit = max(0, int(payload.get("limit", 100)))
        offset = max(0, int(payload.get("offset", 0)))
        countries = payload.get("countries") or []
        if not isinstance(countries, list) or any(not isinstance(item, str) for item in countries):
            return {"ok": False, "error": "countries must be a list of country names"}
        since = payload.get("since")
        until = payload.get("until")
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": f"Invalid numeric parameter: {exc}"}
    script = mod_root / "tools" / "summarize_game_log.py"
    module = _load_module(script, "_md_mcp_upstream_game_log")
    try:
        data = module.parse(
            str(log_path),
            since=module.parse_date_arg(since),
            until=module.parse_date_arg(until),
        )
    except (OSError, ValueError, IndexError) as exc:
        return {"ok": False, "path": str(log_path), "error": str(exc)}
    if data["stats"]["parsed"] == 0:
        return {"ok": False, "path": str(log_path), "error": "No scripted MD log entries found"}
    selected_countries = module.pick_countries(data, countries, 0)
    summary = json.loads(module.to_json(data, countries=selected_countries, top=top))
    paged = []
    for key in ("conflicts", "politics", "annexations"):
        records = summary.get(key, [])
        page = records[offset : offset + limit]
        summary[key] = page
        paged.append((key, len(records), len(page), offset + limit < len(records)))
    for key in ("economy", "inflation", "focus_countries"):
        mapping = summary.get(key, {})
        ordered = sorted(mapping)
        page_keys = ordered[offset : offset + limit]
        summary[key] = {country: mapping[country] for country in page_keys}
        paged.append((key, len(ordered), len(page_keys), offset + limit < len(ordered)))
    summary.update({"ok": True, "path": str(log_path), "offset": offset, "limit": limit})
    for key, total, returned, truncated in paged:
        summary[f"{key}_total"] = total
        summary[f"{key}_returned"] = returned
        summary[f"{key}_truncated"] = truncated
    return summary


def run(operation: str, mod_root: Path, payload: dict) -> dict:
    if operation == "tick_audit":
        return _tick_audit(mod_root, payload)
    if operation == "estimate_gdp":
        return _estimate_gdp(mod_root, payload)
    if operation == "game_log_summary":
        return _game_log_summary(mod_root, payload)
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
