from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
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
                module.REPO_ROOT = str(selected_root)
            report = scanner(sorted(safe_files), scope=payload.get("scope"))
    finally:
        if original_root is not None:
            module.REPO_ROOT = original_root
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


def run(operation: str, mod_root: Path, payload: dict) -> dict:
    if operation == "tick_audit":
        return _tick_audit(mod_root, payload)
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
