"""Reproducible sequential-versus-concurrent full run-all validator benchmark."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from md_mcp.validators import (
    IMPLICIT_RUN_ALL_EXCLUDED,
    SLOW_VALIDATORS,
    ValidatorRunner,
    available_validators,
)


def _normalise(results: dict[str, dict[str, Any]]) -> str:
    """Stable issue content and success state; elapsed times are intentionally excluded."""
    normalised = {
        name: {
            "ok": result.get("ok"),
            "error": result.get("error"),
            "issues": sorted(
                result.get("issues", []),
                key=lambda issue: json.dumps(issue, sort_keys=True, ensure_ascii=False),
            ),
            "counts": result.get("counts", {}),
        }
        for name, result in sorted(results.items())
    }
    return json.dumps(normalised, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _run(
    mod_root: Path, names: list[str], workers: int, cpu_budget: int
) -> tuple[float, dict[str, dict[str, Any]]]:
    previous_max_workers = os.environ.get("MD_MAX_WORKERS")
    os.environ["MD_MAX_WORKERS"] = str(max(1, cpu_budget // workers))
    try:
        runner = ValidatorRunner(mod_root, mode="isolated")
        started = time.perf_counter()
        if workers == 1:
            results = {name: runner.run(name) for name in names}
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {name: pool.submit(runner.run, name) for name in names}
                results = {name: futures[name].result() for name in names}
        return time.perf_counter() - started, results
    finally:
        if previous_max_workers is None:
            os.environ.pop("MD_MAX_WORKERS", None)
        else:
            os.environ["MD_MAX_WORKERS"] = previous_max_workers


def _sample(
    mod_root: Path, names: list[str], cpu_budget: int, workers: int, *, serial_first: bool
) -> dict[str, Any]:
    if serial_first:
        serial_s, serial = _run(mod_root, names, 1, cpu_budget)
        concurrent_s, parallel = _run(mod_root, names, workers, cpu_budget)
    else:
        concurrent_s, parallel = _run(mod_root, names, workers, cpu_budget)
        serial_s, serial = _run(mod_root, names, 1, cpu_budget)
    failures = {
        mode: {
            name: result.get("error", "unknown failure")
            for name, result in results.items()
            if not result.get("ok")
        }
        for mode, results in (("sequential", serial), ("concurrent", parallel))
    }
    failures = {mode: failed for mode, failed in failures.items() if failed}
    if failures:
        raise RuntimeError(f"validator run failed: {failures}")
    serial_json = _normalise(serial)
    parallel_json = _normalise(parallel)
    if serial_json != parallel_json:
        raise RuntimeError("sequential/concurrent validator results differ")
    return {
        "sequential_wall_s": serial_s,
        "concurrent_wall_s": concurrent_s,
        "speedup": serial_s / concurrent_s if concurrent_s else None,
        "equivalent": True,
        "normalized_sha256": hashlib.sha256(serial_json.encode()).hexdigest(),
        "issue_count": sum(len(result.get("issues", [])) for result in serial.values()),
    }


def _validate_args(cpu_budget: int, workers: int, repetitions: int) -> None:
    if cpu_budget < 1 or workers < 2 or repetitions < 1:
        raise ValueError("cpu budget/repetitions must be positive and workers at least 2")
    if workers > cpu_budget:
        raise ValueError("outer workers cannot exceed the logical CPU budget")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument(
        "--cpu-budget",
        type=int,
        default=14,
        help="logical CPU affinity budget; does not imply distinct physical cores",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        _validate_args(args.cpu_budget, args.workers, args.repetitions)
    except ValueError as exc:
        parser.error(str(exc))
    mod_root = args.mod_root.resolve()
    if not (mod_root / "tools/validation").is_dir():
        parser.error(f"not a Millennium-Dawn checkout: {mod_root}")
    host_affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    if host_affinity is not None and len(host_affinity) < args.cpu_budget:
        parser.error(
            f"CPU budget {args.cpu_budget} exceeds available affinity CPUs ({len(host_affinity)})"
        )
    selected_affinity = None
    if host_affinity is not None:
        selected_affinity = host_affinity[: args.cpu_budget]
        os.sched_setaffinity(0, set(selected_affinity))
    os.environ["MD_NO_CACHE"] = "1"
    names = [
        info.name
        for info in available_validators(mod_root)
        if info.name not in SLOW_VALIDATORS | IMPLICIT_RUN_ALL_EXCLUDED
    ]
    if not names:
        parser.error("no run-all validators found")
    samples = [
        _sample(
            mod_root,
            names,
            args.cpu_budget,
            args.workers,
            serial_first=index % 2 == 0,
        )
        for index in range(args.repetitions)
    ]
    report = {
        "mod_root": str(mod_root),
        "mod_revision": subprocess.run(
            ["git", "-C", str(mod_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "code_revision": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "python": sys.version,
        "platform": platform.platform(),
        "host_logical_cpus": os.cpu_count(),
        "host_affinity_cpus": host_affinity,
        "selected_affinity_cpus": selected_affinity,
        "cpu_budget_logical_cpus": args.cpu_budget,
        "physical_core_budget": "not measured; selected affinity is a logical-CPU budget",
        "outer_workers": args.workers,
        "inner_md_max_workers_sequential": args.cpu_budget,
        "inner_md_max_workers_concurrent": max(1, args.cpu_budget // args.workers),
        "cache_conditions": (
            "MD_NO_CACHE=1; validator caches bypassed; OS filesystem cache uncontrolled"
        ),
        "validators": names,
        "repetitions": samples,
        "median_sequential_wall_s": statistics.median(
            item["sequential_wall_s"] for item in samples
        ),
        "median_concurrent_wall_s": statistics.median(
            item["concurrent_wall_s"] for item in samples
        ),
        "median_speedup": statistics.median(item["speedup"] for item in samples),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary_keys = (
        "median_sequential_wall_s",
        "median_concurrent_wall_s",
        "median_speedup",
        "validators",
    )
    print(json.dumps({key: report[key] for key in summary_keys}, indent=2))


if __name__ == "__main__":
    main()
