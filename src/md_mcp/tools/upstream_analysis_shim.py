from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
from collections import UserDict
from pathlib import Path
from typing import Any, cast


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_TICK_LISTS = ("hooks", "events", "decisions")
_TICK_ROW_FIELDS = (
    "name",
    "timer_field",
    "timer_value",
    "bucket",
    "recurring",
    "file",
    "line",
)


def _tick_tag_ok(scope: Any, tag: Any) -> bool:
    # Mirrors render_list's tag_ok: the focused tag always retains global hooks.
    return tag is None or scope in (tag, "GLOBAL")


def _tick_cadence_ok(cadence: Any, cadence_filter: Any) -> bool:
    return cadence_filter is None or cadence == cadence_filter


def _tick_list_rows(report: dict, mode: str, cadence_filter: Any, tag: Any) -> list[dict]:
    """Structured rows shaped like render_list's sections, from the report it reads."""
    rows: list[dict] = []
    if mode == "hooks":
        for hook in sorted(
            report["hooks"], key=lambda item: (-item["work_units"], item["cadence"], item["scope"])
        ):
            if not _tick_cadence_ok(hook["cadence"], cadence_filter):
                continue
            if not _tick_tag_ok(hook["scope"], tag):
                continue
            rows.append(
                {
                    "file": hook["file"],
                    "line": hook["line"],
                    "cadence": hook["cadence"],
                    "scope": hook["scope"],
                }
            )
    elif mode == "events":
        for event_id, record in sorted(report["event_fires"].items()):
            fired_by = [
                fire
                for fire in record["fired_by"]
                if _tick_cadence_ok(fire["cadence"], cadence_filter)
                and _tick_tag_ok(fire["scope"], tag)
            ]
            if not fired_by:
                continue
            rows.append(
                {
                    "id": event_id,
                    "cadences": sorted({fire["cadence"] for fire in fired_by}),
                    "scopes": sorted({fire["scope"] for fire in fired_by}),
                    "def_location": record["def"],
                }
            )
    else:  # decisions
        for decision in sorted(
            report["timed_decisions"], key=lambda item: (item["bucket"], item["name"])
        ):
            if not _tick_cadence_ok(decision["bucket"], cadence_filter):
                continue
            rows.append({field: decision[field] for field in _TICK_ROW_FIELDS})
    return rows


def _tick_tree_stats(tree: dict) -> tuple[int, bool]:
    """Count call-tree nodes and whether any subtree hit the upstream node budget."""
    count = 0
    truncated = False
    stack = [tree]
    while stack:
        node = stack.pop()
        count += 1
        if node.get("truncated"):
            truncated = True
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(children)
    return count, truncated


def _prune_tick_tree(node: dict, limit: int) -> tuple[dict, int, bool]:
    """Keep the first `limit` nodes. Upstream's node budget resets per hook."""
    kept = 0
    dropped = False

    def take(item: dict) -> dict | None:
        nonlocal kept, dropped
        if kept >= limit:
            dropped = True
            return None
        kept += 1
        copied = dict(item)
        children = item.get("children")
        if not isinstance(children, list):
            return copied
        kept_children = []
        for child in children:
            pruned = take(child)
            if pruned is None:
                break
            kept_children.append(pruned)
        if len(kept_children) != len(children):
            dropped = True
            copied["truncated"] = True
        copied["children"] = kept_children
        return copied

    pruned = take(node)
    if pruned is None:
        return (
            {
                "name": node.get("name"),
                "kind": node.get("kind"),
                "children": [],
                "truncated": True,
            },
            0,
            True,
        )
    return pruned, kept, dropped


def _attach_tick_tree(module: Any, result: dict, requested: bool, node_limit: int) -> dict:
    if not requested:
        return result
    built = module.build_call_tree(
        module.index_scripted_effects(),
        module.index_events(),
        module.index_effect_locations(),
        max_nodes=node_limit,
    )
    tree, node_count, pruned_away = _prune_tick_tree(built, node_limit)
    _, flagged = _tick_tree_stats(tree)
    result["tree"] = tree
    result["node_limit"] = node_limit
    result["node_count"] = node_count
    result["tree_truncated"] = pruned_away or flagged
    return result


def _tick_audit(mod_root: Path, payload: dict) -> dict:
    script = mod_root / "tools" / "analysis" / "tick_audit.py"
    list_mode = payload.get("list")
    if list_mode is not None and not (isinstance(list_mode, str) and list_mode in _TICK_LISTS):
        return {"ok": False, "error": f"list must be one of: {', '.join(_TICK_LISTS)}"}
    tree_requested = payload.get("tree", False)
    if not isinstance(tree_requested, bool):
        return {"ok": False, "error": "tree must be a boolean"}
    try:
        node_limit = max(1, int(payload.get("node_limit", 100)))
        limit = max(0, int(payload.get("limit", 20)))
        offset = max(0, int(payload.get("offset", 0)))
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": f"Invalid pagination values: {exc}"}
    module = _load_module(script, "_md_mcp_upstream_tick_audit")
    cadence_filter = payload.get("cadence")
    if cadence_filter is not None and cadence_filter not in module.CADENCES:
        return {
            "ok": False,
            "error": f"cadence must be one of: {', '.join(module.CADENCES)}",
        }
    tag = payload.get("tag")
    report = module.build_report(tag_filter=tag)
    hooks = report.get("focus_hooks", []) if tag else report.get("hooks", [])
    if cadence_filter is not None:
        hooks = [hook for hook in hooks if hook.get("cadence") == cadence_filter]
    total = len(hooks)
    selected = hooks[offset : offset + limit]
    if list_mode is not None:
        rows = _tick_list_rows(report, list_mode, cadence_filter, tag)
        result = {
            "ok": True,
            "tag": tag,
            "list": list_mode,
            "cadence": cadence_filter,
            "totals": report["totals"],
            "total": len(rows),
            "returned": len(rows[offset : offset + limit]),
            "truncated": offset + limit < len(rows),
            "rows": rows[offset : offset + limit],
        }
        return _attach_tick_tree(module, result, tree_requested, node_limit)
    cadence_items = report["cadences"].items()
    if cadence_filter is not None:
        cadence_items = (
            ((cadence_filter, report["cadences"][cadence_filter]),)
            if cadence_filter in report["cadences"]
            else ()
        )
    cadence_summary = {}
    for cadence, data in cadence_items:
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
    result = {
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
    if cadence_filter is not None:
        result["cadence"] = cadence_filter
    return _attach_tick_tree(module, result, tree_requested, node_limit)


def _tick_spot_check(mod_root: Path, payload: dict) -> dict:
    script = mod_root / "tools" / "analysis" / "tick_audit.py"
    try:
        module = _load_module(script, "_md_mcp_upstream_tick_audit")
    except Exception as exc:
        return {"ok": False, "error": f"Could not load upstream tick spot-check API: {exc}"}
    scanner = getattr(module, "scan_spot_checks", None)
    if scanner is None:
        return {
            "ok": False,
            "unsupported": True,
            "error": (
                "This Millennium-Dawn checkout does not support tick spot checks "
                "(scan_spot_checks is unavailable)."
            ),
        }
    files = payload.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(path, str) for path in files):
        return {"ok": False, "error": "files must be a non-empty list of changed file paths"}
    try:
        limit = max(0, int(payload.get("limit", 100)))
        offset = max(0, int(payload.get("offset", 0)))
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": f"Invalid pagination values: {exc}"}
    scan_root_value = payload.get("scan_root")
    roots = [Path(scan_root_value), mod_root] if isinstance(scan_root_value, str) else [mod_root]
    scan_files, invalid = _validated_spot_files(roots, files)
    if invalid:
        return {
            "ok": False,
            "error": "tick spot checks accept only existing regular .txt files inside the mod root",
            "invalid_files": invalid[:20],
        }
    # The upstream helpers resolve and label every file against their module
    # level root. Build a temporary view containing only the selected files so
    # overlay files shadow base files and base-only paths remain available to
    # the same cross-file scan (for example, scripted effect references).
    original_root = getattr(module, "REPO_ROOT", None)
    try:
        with tempfile.TemporaryDirectory(prefix="md-mcp-tick-spot-") as temp_root:
            selected_root = Path(temp_root)
            safe_files = []
            for scan_root, relpaths in scan_files.items():
                for relpath in relpaths:
                    destination = selected_root / relpath
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes((scan_root / relpath).read_bytes())
                    safe_files.append(relpath)
            if original_root is not None:
                module.__dict__["REPO_ROOT"] = str(selected_root)
            report = scanner(sorted(safe_files), scope=payload.get("scope"))
    finally:
        if original_root is not None:
            module.__dict__["REPO_ROOT"] = original_root
    findings = report.get("findings", [])
    diagnostics = report.get("diagnostics", [])
    total_findings = len(findings)
    total_diagnostics = len(diagnostics)
    fatal_diagnostics = sum(
        bool(item.get("fatal")) and not item.get("suppressed") for item in diagnostics
    )
    suppressed_diagnostics = sum(bool(item.get("suppressed")) for item in diagnostics)
    severity_counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for finding in findings:
        severity = str(finding.get("severity", "medium")).casefold()
        mapped = (
            "error"
            if severity in {"critical", "high", "error"}
            else ("info" if severity in {"low", "info"} else "warning")
        )
        severity_counts[mapped] += 1
    severity_counts["error"] += fatal_diagnostics
    severity_counts["warning"] += max(
        0, total_diagnostics - fatal_diagnostics - suppressed_diagnostics
    )
    return {
        "ok": True,
        "mode": report.get("mode"),
        "filters": report.get("filters"),
        "summary": report.get("summary", {}),
        "assumptions": report.get("assumptions", []),
        "total": total_findings,
        "returned": len(findings[offset : offset + limit]),
        "truncated": offset + limit < total_findings,
        "diagnostics_total": total_diagnostics,
        "diagnostics_returned": len(diagnostics[offset : offset + limit]),
        "diagnostics_truncated": offset + limit < total_diagnostics,
        "diagnostics_summary": {
            "total": total_diagnostics,
            "fatal": fatal_diagnostics,
            "suppressed": suppressed_diagnostics,
            "warning": max(0, total_diagnostics - fatal_diagnostics - suppressed_diagnostics),
        },
        "severity_counts": severity_counts,
        "findings": findings[offset : offset + limit],
        "diagnostics": diagnostics[offset : offset + limit],
    }


def _validated_spot_files(
    roots: list[Path], files: list[str]
) -> tuple[dict[Path, list[str]], list[str]]:
    """Resolve explicit files overlay-first without allowing unsafe shadow fallback."""
    available_roots: list[Path] = []
    for root in roots:
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            continue
        if resolved_root.is_dir() and resolved_root not in available_roots:
            available_roots.append(resolved_root)

    safe: dict[Path, list[str]] = {}
    invalid: list[str] = []
    selected_roots: dict[str, tuple[Path, str]] = {}
    for value in files:
        requested = Path(value)
        if not requested.is_absolute() and ".." in requested.parts:
            invalid.append(value)
            continue
        selected: tuple[Path, Path] | None = None
        for root in available_roots:
            candidate = requested if requested.is_absolute() else root / requested
            # An absolute path can only be considered by the root containing
            # it. Relative paths are tested overlay-first; once any overlay
            # entry exists (including a directory or symlink), it shadows the
            # base and must be valid rather than silently falling through.
            try:
                lexical_inside = os.path.commonpath((str(root), str(candidate.absolute()))) == str(
                    root
                )
            except ValueError:
                lexical_inside = False
            if requested.is_absolute() and not lexical_inside:
                continue
            linked = candidate.is_symlink()
            parent = candidate.parent
            while not linked and parent != root and parent != parent.parent:
                linked = parent.is_symlink()
                parent = parent.parent
            present = os.path.lexists(candidate) or linked
            if not present:
                continue
            try:
                resolved = candidate.resolve(strict=True)
                inside = os.path.commonpath((str(root), str(resolved))) == str(root)
            except (OSError, ValueError):
                invalid.append(value)
                break
            if inside and not linked and resolved.is_file() and resolved.suffix == ".txt":
                selected = (root, resolved)
                break
            invalid.append(value)
            break
        if selected is None:
            if value not in invalid:
                invalid.append(value)
            continue
        root, resolved = selected
        relpath = resolved.relative_to(root).as_posix()
        previous = selected_roots.get(relpath)
        if previous is not None and previous[0] != root:
            invalid.extend((previous[1], value))
            continue
        selected_roots[relpath] = (root, value)
        safe.setdefault(root, []).append(relpath)
    return {root: sorted(set(paths)) for root, paths in safe.items()}, list(dict.fromkeys(invalid))


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
    if not isinstance(payload.get("adjacent", False), bool):
        return {"ok": False, "file": raw_file, "error": "adjacent must be a boolean"}
    raw_region = payload.get("region")
    if raw_region is not None and not isinstance(raw_region, str):
        return {"ok": False, "file": raw_file, "error": "region must be an x0,x1,y0,y1 string"}
    if raw_region is not None and len(raw_region.split(",")) != 4:
        return {
            "ok": False,
            "file": raw_file,
            "error": f"Invalid region: expected x0,x1,y0,y1, got {raw_region!r}",
        }
    try:
        if raw_region is not None:
            tuple(int(part) for part in raw_region.split(","))
    except ValueError as exc:
        return {"ok": False, "file": raw_file, "error": f"Invalid region: {exc}"}
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
        # Helpers that decide has_focus_tree read the tree from the file, as their CLI does.
        file_focus_tree = getattr(module, "file_focus_tree", None)
        if callable(file_focus_tree):
            scenario.focus_tree = file_focus_tree(text)
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
        if raw_region is None:
            region = None
        else:
            parse_region = getattr(module, "parse_region", None)
            if not callable(parse_region):
                return {
                    "ok": False,
                    "unsupported": True,
                    "file": raw_file,
                    "error": "Upstream focus overlap API is missing: parse_region",
                }
            region = cast(tuple[int, int, int, int], parse_region(raw_region))
        adjacent = bool(payload.get("adjacent", False))
        report = module.build_report(
            focuses,
            positions,
            visible,
            scenario,
            region,
            adjacent,
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
    if region is None:
        position_total = int(report.get("visible", 0))
    else:
        position_total = sum(
            shown
            and region[0] <= positions[focus_id][0] <= region[1]
            and region[2] <= positions[focus_id][1] <= region[3]
            for focus_id, shown in visible.items()
        )
        if payload.get("include_positions"):
            position_total = len(all_positions)
    positions_page = all_positions[offset : offset + limit]
    diagnostics_page = diagnostics[offset : offset + limit]
    adjacent_pairs = report.get("adjacent", []) if adjacent else []
    if not isinstance(adjacent_pairs, list):
        return {
            "ok": False,
            "file": raw_file,
            "error": "Upstream report has an invalid adjacent list",
        }
    adjacent_page = adjacent_pairs[offset : offset + limit]
    result = {
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
    if adjacent:
        result.update(
            {
                "adjacent_included": True,
                "adjacent_pairs_total": len(adjacent_pairs),
                "adjacent_pairs_returned": len(adjacent_page),
                "adjacent_pairs_truncated": offset + limit < len(adjacent_pairs),
                "adjacent_pairs": adjacent_page,
            }
        )
    return result


RENAME_SCAN_TREES = ("common", "events", "history", "interface")
RENAME_SCAN_SUFFIXES = frozenset({".txt", ".gui", ".gfx", ".mod"})


class _CountingMapping(UserDict[str, str]):
    """Records which occurrences of mapped IDs upstream `_replace_ids` actually changes.

    Only lookups whose value differs from the key count: upstream also stores
    already-canonical IDs in the mapping, and replacing them changes nothing.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        super().__init__(mapping)
        self.changed = 0

    def __getitem__(self, key: str) -> str:
        value = super().__getitem__(key)
        if value != key:
            self.changed += 1
        return value


def _rename_scan_roots(payload: dict) -> list[tuple[Path, str]] | dict:
    """Resolved scan roots with their labels, or an error envelope."""
    raw = payload.get("allowed_roots")
    if not isinstance(raw, list) or not raw or any(not isinstance(value, str) for value in raw):
        return {"ok": False, "error": "no mod scan roots were provided"}
    pairs: list[tuple[Path, str]] = []
    for value in raw:
        try:
            root = Path(value).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return {"ok": False, "error": f"scan root does not exist: {value}"}
        if not root.is_dir():
            return {"ok": False, "error": f"scan root is not a directory: {value}"}
        pairs.append((root, "root"))
    labels = payload.get("root_labels")
    if isinstance(labels, list) and len(labels) == len(pairs):
        pairs = [(root, str(label)) for (root, _), label in zip(pairs, labels, strict=False)]
    return pairs


def _resolve_rename_input(
    value: object, pairs: list[tuple[Path, str]], suffixes: frozenset[str] | set[str], field: str
) -> tuple[Path | None, dict | None]:
    """Re-check a resolved tool-layer path against the scan roots, or produce an error."""
    if not isinstance(value, str):
        return None, {"ok": False, "error": f"{field} must be a mod file path"}
    try:
        resolved = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        return None, {"ok": False, "error": f"{field} does not resolve to an existing file: {exc}"}
    try:
        inside = any(resolved.is_relative_to(root) for root, _ in pairs)
    except ValueError:
        inside = False
    if not inside:
        return None, {"ok": False, "error": f"{field} is outside the mod scan roots"}
    if not resolved.is_file() or resolved.suffix not in suffixes:
        allowed = ", ".join(sorted(suffixes))
        return None, {
            "ok": False,
            "error": (
                f"{field} must be an existing regular file with one of these extensions: {allowed}"
            ),
        }
    return resolved, None


def _rename_focus_ids(mod_root: Path, payload: dict) -> dict:
    """Plan (or preview) upstream focus-ID renames in memory; never writes files.

    Loads the mod's pure `extract_focus_ids`/`_replace_ids` helpers only. The
    upstream `rename_focus_ids` entry point writes files and is never called.
    """
    script = mod_root / "tools" / "standardization" / "rename_focus_ids.py"
    if not script.is_file():
        return {
            "ok": False,
            "unsupported": True,
            "error": f"This Millennium-Dawn checkout has no focus ID rename helper ({script}).",
        }
    # Imports would otherwise leave __pycache__ files in the review-only mod checkout.
    sys.dont_write_bytecode = True
    try:
        module = _load_module(script, "_md_mcp_upstream_rename_focus_ids")
    except Exception as exc:
        return {
            "ok": False,
            "unsupported": True,
            "error": f"Could not load upstream focus ID rename APIs: {exc}",
        }
    missing = [
        name
        for name in ("extract_focus_ids", "_replace_ids")
        if not callable(getattr(module, name, None))
    ]
    if missing:
        return {
            "ok": False,
            "unsupported": True,
            "error": f"Upstream focus ID rename API is missing: {', '.join(missing)}",
        }

    pairs_result = _rename_scan_roots(payload)
    if isinstance(pairs_result, dict):
        return pairs_result
    pairs = pairs_result

    suffixes = set(RENAME_SCAN_SUFFIXES) | {".yml"}
    focus_path, error = _resolve_rename_input(
        payload.get("focus_file"), pairs, {".txt"}, "focus_file"
    )
    if error:
        return error
    assert focus_path is not None
    loc_path: Path | None = None
    if payload.get("loc_file") is not None:
        loc_path, error = _resolve_rename_input(
            payload.get("loc_file"), pairs, {".yml"}, "loc_file"
        )
        if error:
            return error
    text_path: Path | None = None
    if payload.get("path") is not None:
        text_path, error = _resolve_rename_input(payload.get("path"), pairs, suffixes, "path")
        if error:
            return error

    tag = payload.get("tag")
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9]{2,4}", tag):
        return {"ok": False, "error": "tag must be a 2-4 character country tag"}
    tag = tag.upper()
    try:
        limit = max(0, int(payload.get("limit", 50)))
        offset = max(0, int(payload.get("offset", 0)))
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": f"Invalid pagination values: {exc}"}

    # Upstream reads .txt files as plain UTF-8 (any BOM stays in the text) and
    # localisation with utf-8-sig; newline= keeps CRLF bytes through the rewrite.
    try:
        focus_text = focus_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        return {"ok": False, "error": f"focus file is not valid UTF-8: {exc}"}
    try:
        mapping = module.extract_focus_ids(focus_text, tag)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not mapping:
        return {"ok": False, "error": f"No focus IDs found in {focus_path.name}"}

    roots = [root for root, _ in pairs]
    labels = [label for _, label in pairs]

    def owner_index(path: Path) -> int | None:
        for index, root in enumerate(roots):
            try:
                if path.is_relative_to(root):
                    return index
            except ValueError:
                continue
        return None

    # Effective file set across all roots; submod files shadow base-mod files at
    # the same relative path, and the focus/localisation files shadow their own
    # relative paths so the mapping source is the file that gets rewritten.
    entries: dict[str, tuple[Path, int, str]] = {}
    for index, root in enumerate(roots):
        for base_name in RENAME_SCAN_TREES:
            base = root / base_name
            if not base.is_dir():
                continue
            # Resolve the scan base itself before enumerating: a symlinked base
            # directory must never expand into files outside this root.
            try:
                base_resolved = base.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                base_resolved = None
            if base_resolved is None or not base_resolved.is_relative_to(root):
                return {
                    "ok": False,
                    "error": (
                        f"{base_name}/ links outside the mod scan roots; "
                        "the rename plan refuses to read it"
                    ),
                }
            for candidate in base.rglob("*"):
                if candidate.suffix not in RENAME_SCAN_SUFFIXES:
                    continue
                # rglob and is_file follow links, so resolve before deciding: a
                # scanned entry may only be read when it still lives in this root
                # and lands in one of the four scan trees as a supported file.
                try:
                    resolved = candidate.resolve(strict=False)
                except (OSError, RuntimeError, ValueError):
                    resolved = None
                if resolved is None or not resolved.is_relative_to(root):
                    return {
                        "ok": False,
                        "error": (
                            f"{candidate.relative_to(root).as_posix()} links outside the mod "
                            "scan roots; the rename plan refuses to read it"
                        ),
                    }
                if not resolved.is_file():
                    continue
                rel = resolved.relative_to(root).as_posix()
                if rel.split("/", 1)[0] not in RENAME_SCAN_TREES or (
                    resolved.suffix not in RENAME_SCAN_SUFFIXES
                ):
                    return {
                        "ok": False,
                        "error": (
                            f"{candidate.relative_to(root).as_posix()} resolves to {rel}, which "
                            "is not a .txt/.gui/.gfx/.mod file under common/, events/, history/, "
                            "or interface/"
                        ),
                    }
                entries.setdefault(rel, (resolved, index, "script"))
    focus_owner = owner_index(focus_path)
    if focus_owner is None:
        return {"ok": False, "error": "focus_file is outside the mod scan roots"}
    owned: list[tuple[Path, int, str]] = [(focus_path, focus_owner, "focus")]
    loc_owner: int | None = None
    if loc_path is not None:
        loc_owner = owner_index(loc_path)
        if loc_owner is None:
            return {"ok": False, "error": "loc_file is outside the mod scan roots"}
        owned.append((loc_path, loc_owner, "loc"))
    for path, owner, kind in owned:
        rel = path.relative_to(roots[owner]).as_posix()
        # Do not rename a base definition hidden by the overlay.
        for prior_root in roots[:owner]:
            if os.path.lexists(prior_root / rel):
                field = "focus_file" if kind == "focus" else "loc_file"
                return {
                    "ok": False,
                    "error": (
                        f"{field} {path} is shadowed at {rel} by a higher-priority content "
                        "root; select the shadowing overlay copy explicitly so the plan "
                        "cannot leave dangling references"
                    ),
                }
        entries[rel] = (path, owner, kind)

    counting = _CountingMapping(mapping)

    def read_entry(entry: tuple[Path, int, str]) -> tuple[bytes, str] | dict:
        path, _index, kind = entry
        raw = path.read_bytes()
        encoding = "utf-8-sig" if kind == "loc" else "utf-8"
        try:
            return raw, raw.decode(encoding)
        except UnicodeDecodeError as exc:
            return {"ok": False, "error": f"{path.name} is not valid {encoding} text: {exc}"}

    def transform(kind: str, text: str) -> str:
        if kind == "loc":
            return module._replace_ids(text, counting, localization=True)
        return "".join(
            module._replace_ids(line, counting, focus_file=kind == "focus")
            for line in text.splitlines(keepends=True)
        )

    ordered = sorted(entries.items(), key=lambda item: (item[1][1], item[0]))

    if text_path is not None:
        entry = next((item for item in ordered if item[1][0] == text_path), None)
        if entry is None:
            return {
                "ok": False,
                "error": (
                    "path is not part of the rename plan (it must be the focus or localisation "
                    "file, or a .txt/.gui/.gfx/.mod file under common/, events/, history/, or "
                    "interface/)"
                ),
            }
        path, index, kind = entry[1]
        read = read_entry(entry[1])
        if isinstance(read, dict):
            return read
        raw, text = read
        output = transform(kind, text)
        text_changed = output != text
        had_bom = raw.startswith(b"\xef\xbb\xbf")
        notes: list[str] = []
        if kind == "loc":
            if had_bom:
                notes.append("the input has a UTF-8 BOM and the returned txt does not")
            if text_changed:
                notes.append(
                    "upstream writes changed localisation back with a UTF-8 BOM; "
                    "write the file back with one"
                )
        elif had_bom:
            notes.append("the returned txt retains the file's UTF-8 BOM character")
        preview: dict = {
            "ok": True,
            "tag": tag,
            "path": path.relative_to(roots[index]).as_posix(),
            "root": labels[index],
            "kind": kind,
            "changed": text_changed,
            "mapping_count": len(mapping),
            "text": output,
        }
        if had_bom:
            preview["had_bom"] = True
        if notes:
            preview["note"] = "; ".join(dict.fromkeys(notes))
        return preview

    changed: list[dict] = []
    focus_changed = False
    loc_changed = False
    for rel, item in ordered:
        read = read_entry(item)
        if isinstance(read, dict):
            return read
        _raw, text = read
        output = transform(item[2], text)
        if output == text:
            counting.changed = 0
            continue
        changed.append({"path": rel, "root": labels[item[1]], "replacements": counting.changed})
        counting.changed = 0
        focus_changed = focus_changed or item[2] == "focus"
        loc_changed = loc_changed or item[2] == "loc"

    total = len(changed)
    page = changed[offset : offset + limit]
    result = {
        "ok": True,
        "tag": tag,
        "focus_file": focus_path.relative_to(roots[focus_owner]).as_posix(),
        "focus_changed": focus_changed,
        "mapping": dict(mapping),
        "mapping_count": len(mapping),
        "changed_total": total,
        "limit": limit,
        "offset": offset,
        "total": total,
        "returned": len(page),
        "truncated": offset + limit < total,
        "files": page,
    }
    if loc_path is not None:
        assert loc_owner is not None
        result["loc_file"] = loc_path.relative_to(roots[loc_owner]).as_posix()
        result["loc_changed"] = loc_changed
    return result


def run(operation: str, mod_root: Path, payload: dict) -> dict:
    if operation == "tick_audit":
        return _tick_audit(mod_root, payload)
    if operation == "rename_focus_ids":
        return _rename_focus_ids(mod_root, payload)
    if operation == "tick_spot_check":
        return _tick_spot_check(mod_root, payload)
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
