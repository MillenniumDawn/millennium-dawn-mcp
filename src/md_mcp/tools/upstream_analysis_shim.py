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


def _event_load(mod_root: Path, payload: dict) -> dict:
    script = mod_root / "tools" / "analysis" / "event_load.py"
    module = _load_module(script, "_md_mcp_upstream_event_load")
    tag = str(payload.get("tag", "USA")).upper()
    if not re.fullmatch(r"[A-Z0-9]{2,4}", tag):
        return {"ok": False, "error": "tag must be a 2-4 character country tag"}
    try:
        window = max(1, int(payload.get("window", 45)))
        threshold = max(0, int(payload.get("threshold", 3)))
        limit = max(0, int(payload.get("limit", 20)))
        offset = max(0, int(payload.get("offset", 0)))
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": f"Invalid numeric parameter: {exc}"}
    rows = {
        year: deliveries
        for year, deliveries in module.collect(tag, str(mod_root)).items()
        if deliveries
    }
    years = []
    flagged = []
    for year in sorted(rows):
        deliveries = sorted(rows[year], key=lambda item: item[1])
        days = [day for _, day in deliveries]
        peak = module.busiest(days, window)
        events = deliveries[offset : offset + limit]
        entry = {
            "year": year,
            "total": len(deliveries),
            "busiest_window": peak,
            "flagged": peak >= threshold,
            "events_total": len(deliveries),
            "events_returned": len(events),
            "events_truncated": offset + limit < len(deliveries),
            "events": [{"id": event, "day_offset": day} for event, day in events],
        }
        years.append(entry)
        if peak >= threshold:
            flagged.append(
                {
                    "year": year,
                    "busiest_window": peak,
                    "threshold": threshold,
                    "events": entry["events"],
                    "events_truncated": entry["events_truncated"],
                }
            )
    return {
        "ok": True,
        "tag": tag,
        "window": window,
        "threshold": threshold,
        "total": len(years),
        "returned": len(years),
        "truncated": any(year["events_truncated"] for year in years),
        "years": years,
        "flagged_years": flagged,
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


def _focus_overlap_scenarios(mod_root: Path, payload: dict) -> dict:
    raw_file = payload.get("file")
    if not isinstance(raw_file, str) or not Path(raw_file).is_absolute():
        return {"ok": False, "error": "file must be an absolute path"}
    roots: list[Path] = []
    raw_roots = payload.get("allowed_roots", [str(mod_root)])
    if not isinstance(raw_roots, list) or any(not isinstance(value, str) for value in raw_roots):
        return {"ok": False, "error": "invalid allowed roots"}
    try:
        roots = [Path(value).resolve(strict=True) for value in raw_roots]
        candidate = Path(raw_file)
        source = candidate.resolve(strict=True)
        inside = any(source.is_relative_to(root) for root in roots)
    except (OSError, ValueError):
        return {"ok": False, "file": raw_file, "error": "focus file does not exist"}
    linked = candidate.is_symlink()
    parent = candidate.parent
    while not linked and parent != parent.parent:
        linked = parent.is_symlink()
        if parent in roots:
            break
        parent = parent.parent
    if not inside or linked or not source.is_file() or source.suffix.casefold() != ".txt":
        return {
            "ok": False,
            "file": raw_file,
            "error": "file must be a regular .txt focus file inside an allowed mod root",
        }
    if source.stat().st_size > 2_000_000:
        return {
            "ok": False,
            "file": raw_file,
            "error": "focus file exceeds the 2 MB analysis limit",
        }

    tag = payload.get("tag")
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9]{2,4}", tag):
        return {"ok": False, "file": raw_file, "error": "tag must be a 2-4 character country tag"}
    completed = payload.get("completed", [])
    if not isinstance(completed, list) or any(not isinstance(value, str) for value in completed):
        return {"ok": False, "file": raw_file, "error": "completed must be a list of focus ids"}
    if not isinstance(payload.get("include_positions", False), bool):
        return {"ok": False, "file": raw_file, "error": "include_positions must be a boolean"}
    try:
        limit = min(100, max(0, int(payload.get("limit", 50))))
        offset = max(0, int(payload.get("offset", 0)))
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "file": raw_file, "error": f"Invalid pagination values: {exc}"}

    tools_dir = mod_root / "tools"
    script = tools_dir / "analysis" / "focus_overlap_report.py"
    if not script.is_file():
        return {
            "ok": False,
            "unsupported": True,
            "file": raw_file,
            "error": "This Millennium-Dawn checkout has no focus overlap analysis helper.",
        }
    try:
        # These scripts live in a review-only mod checkout; imports must not add __pycache__ files.
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(tools_dir))
        # The helper's bare shared_utils import must resolve beside the selected mod.
        sys.modules.pop("shared_utils", None)
        module = _load_module(script, "_md_mcp_upstream_focus_overlap_report")
    except Exception as exc:
        return {
            "ok": False,
            "unsupported": True,
            "file": raw_file,
            "error": f"Could not load upstream focus overlap APIs: {exc}",
        }
    required = (
        "iter_focus_blocks",
        "parse_focus_file",
        "parse_date",
        "Scenario",
        "evaluate",
        "resolve_positions",
        "resolve_visibility",
        "build_report",
    )
    shared_utils = sys.modules.get("shared_utils")
    missing = [name for name in required if not callable(getattr(module, name, None))]
    if shared_utils is None:
        return {
            "ok": False,
            "unsupported": True,
            "file": raw_file,
            "error": "Upstream focus overlap API is missing shared_utils.",
        }
    missing.extend(
        f"shared_utils.{name}"
        for name in ("strip_comments", "blank_quoted_strings")
        if not callable(getattr(shared_utils, name, None))
    )
    if missing:
        return {
            "ok": False,
            "unsupported": True,
            "file": raw_file,
            "error": f"Upstream focus overlap API is missing: {', '.join(missing)}",
        }

    try:
        raw_date = payload.get("date")
        if raw_date is not None and not isinstance(raw_date, str):
            return {"ok": False, "file": raw_file, "error": "date must be a YYYY.M.D string"}
        scenario_date = module.parse_date(raw_date) if raw_date is not None else None
        if scenario_date is not None:
            from datetime import date as calendar_date

            try:
                calendar_date(*scenario_date)
            except (TypeError, ValueError) as exc:
                return {
                    "ok": False,
                    "file": raw_file,
                    "error": f"date must be a valid calendar date in YYYY.M.D form: {exc}",
                }
        scenario = module.Scenario(
            tag=tag.upper(), completed=frozenset(completed), date=scenario_date
        )
        raw_text = source.read_text(encoding="utf-8-sig")
        from md_mcp.paradox import ParseError, parse_string
        from md_mcp.paradox.nodes import SymbolNode

        try:
            syntax_tree = parse_string(raw_text, error_prefix=f"{source}: ")
        except ParseError as exc:
            return {
                "ok": False,
                "file": raw_file,
                "error": f"Focus file is malformed Paradox script: {exc}",
            }
        expected_focus_ids: set[str] = set()
        for top_level in syntax_tree.children():
            if top_level.name == "focus_tree":
                focus_nodes = [node for node in top_level.children() if node.name == "focus"]
            elif top_level.name in {"shared_focus", "joint_focus"}:
                focus_nodes = [top_level]
            else:
                continue
            for focus_node in focus_nodes:
                id_node = focus_node.get("id")
                id_token = id_node.value_end_token if id_node is not None else None
                if id_token is not None and id_token.type == "string":
                    return {
                        "ok": False,
                        "file": raw_file,
                        "error": (
                            "Quoted focus ids are not supported by the upstream overlap helper; "
                            "use unquoted focus ids."
                        ),
                    }
                if id_node is None:
                    continue
                if isinstance(id_node.value, SymbolNode):
                    expected_focus_ids.add(id_node.value.name)
                elif isinstance(id_node.value, (str, int, float)):
                    expected_focus_ids.add(str(id_node.value))
        text = shared_utils.blank_quoted_strings(shared_utils.strip_comments(raw_text))
        focuses = module.parse_focus_file(text)
        if not isinstance(focuses, dict):
            return {
                "ok": False,
                "file": raw_file,
                "error": "Upstream focus parser returned invalid data",
            }
        parsed_focus_ids = set(focuses)
        if parsed_focus_ids != expected_focus_ids:
            missing = sorted(expected_focus_ids - parsed_focus_ids)[:10]
            unexpected = sorted(parsed_focus_ids - expected_focus_ids)[:10]
            return {
                "ok": False,
                "file": raw_file,
                "error": (
                    "Upstream helper did not recognize the focus definitions in this file; "
                    f"parsed {len(parsed_focus_ids)} of {len(expected_focus_ids)} ids, "
                    f"missing={missing}, unexpected={unexpected}."
                ),
            }
        positions = module.resolve_positions(focuses, scenario)
        visible = module.resolve_visibility(focuses, scenario)
        report = module.build_report(
            focuses,
            positions,
            visible,
            scenario,
            None,
            False,
            bool(payload.get("include_positions", False)),
        )
        collision_groups = report.get("collisions")
        if not isinstance(collision_groups, list):
            return {"ok": False, "file": raw_file, "error": "Upstream report has no collision list"}
        all_positions = report.get("map", []) if payload.get("include_positions") else []
        if not isinstance(all_positions, list):
            return {
                "ok": False,
                "file": raw_file,
                "error": "Upstream report has an invalid position list",
            }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"ok": False, "file": raw_file, "error": f"Focus analysis failed: {exc}"}

    duplicate_counts: dict[str, int] = {}
    for focus_id, _kind, _line, _body in module.iter_focus_blocks(text):
        duplicate_counts[focus_id] = duplicate_counts.get(focus_id, 0) + 1
    diagnostics: list[dict] = [
        {"kind": "duplicate_id", "focus_id": focus_id, "definitions": count}
        for focus_id, count in duplicate_counts.items()
        if count > 1
    ]
    unknown_total = 0
    unresolved_total = 0
    for focus_id, focus in focuses.items():
        if focus.allow_branch is not None and module.evaluate(focus.allow_branch, scenario) is None:
            diagnostics.append({"kind": "unknown_allow_branch", "focus_id": focus_id})
            unknown_total += 1
        for offset_block in focus.offsets:
            if module.evaluate(offset_block.trigger, scenario) is None:
                diagnostics.append({"kind": "unknown_offset_trigger", "focus_id": focus_id})
                unknown_total += 1
        chain_seen: set[str] = set()
        current = focus_id
        while current in focuses:
            if current in chain_seen:
                diagnostics.append({"kind": "cyclic_relative_chain", "focus_id": focus_id})
                unresolved_total += 1
                break
            chain_seen.add(current)
            parent_id = focuses[current].relative_to
            if parent_id is None:
                break
            if parent_id not in focuses:
                diagnostics.append(
                    {"kind": "missing_relative_target", "focus_id": current, "target": parent_id}
                )
                unresolved_total += 1
                break
            current = parent_id

    collisions_page = collision_groups[offset : offset + limit]
    position_total = int(report.get("visible", 0))
    positions_page = all_positions[offset : offset + limit]
    diagnostics_page = diagnostics[offset : offset + limit]
    return {
        "ok": True,
        "file": str(source),
        "scenario": report.get("scenario", {}),
        "evaluation": {
            "kind": "upstream helper approximation",
            "exact_engine_evaluation": False,
            "unknown_offsets_applied": False,
            "unknown_allow_branch_keeps_visibility": True,
        },
        "focus_total": int(report.get("focuses", len(focuses))),
        "visible_total": int(report.get("visible", 0)),
        "collision_groups_total": len(collision_groups),
        "collision_groups_returned": len(collisions_page),
        "collision_groups_truncated": offset + limit < len(collision_groups),
        "collision_groups": collisions_page,
        "positions_included": bool(payload.get("include_positions", False)),
        "positions_total": position_total,
        "positions_returned": len(positions_page),
        "positions_truncated": bool(payload.get("include_positions"))
        and offset + limit < position_total,
        "positions": positions_page,
        "diagnostics_total": len(diagnostics),
        "diagnostics_returned": len(diagnostics_page),
        "diagnostics_truncated": offset + limit < len(diagnostics),
        "diagnostics": diagnostics_page,
        "unknown_condition_total": unknown_total,
        "unresolved_chain_total": unresolved_total,
        "positions_are_partial": unknown_total > 0 or unresolved_total > 0,
    }


def run(operation: str, mod_root: Path, payload: dict) -> dict:
    if operation == "tick_audit":
        return _tick_audit(mod_root, payload)
    if operation == "estimate_gdp":
        return _estimate_gdp(mod_root, payload)
    if operation == "event_load":
        return _event_load(mod_root, payload)
    if operation == "game_log_summary":
        return _game_log_summary(mod_root, payload)
    if operation == "ai_path_report":
        return _ai_path_report(mod_root, payload)
    if operation == "focus_overlap_scenarios":
        return _focus_overlap_scenarios(mod_root, payload)
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
