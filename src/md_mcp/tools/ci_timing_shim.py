from __future__ import annotations

import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 1_000_000
MAX_EXPORTS = 20
MAX_JOBS = 100
MAX_STEPS_PER_JOB = 200
MAX_TOTAL_STEPS = 2_000
MAX_METADATA_FIELDS = 32
MAX_RETURNED_JOBS = 200
MAX_RETURNED_STEPS = 200
PAGE_SIZE_MAX = 20


class InputError(ValueError):
    """The supplied content is outside the supported timing-export shape."""


def _reject_constant(value: str) -> None:
    raise InputError(f"non-finite JSON value {value} is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def _bounded_tree(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > 20_000 or depth > 12:
        raise InputError("JSON nesting or field count exceeds the analysis limit")
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise InputError("JSON contains invalid Unicode text") from exc
        if "\x00" in value or len(value) > MAX_INPUT_BYTES:
            raise InputError("JSON text fields exceed the supported bounds")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise InputError("non-finite JSON numbers are not allowed")
    elif isinstance(value, dict):
        if len(value) > 1_000:
            raise InputError("JSON object has too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise InputError("JSON object keys exceed the supported bounds")
            _bounded_tree(item, depth=depth + 1, nodes=nodes)
    elif isinstance(value, list):
        if len(value) > 2_000:
            raise InputError("JSON array has too many entries")
        for item in value:
            _bounded_tree(item, depth=depth + 1, nodes=nodes)


def _preflight_exports(exports: list[dict[str, Any]]) -> None:
    total_steps = 0
    for export in exports:
        jobs = export.get("jobs", [])
        if not isinstance(jobs, list):
            raise InputError("jobs must be an array")
        if len(jobs) > MAX_JOBS:
            raise InputError(f"an export may contain at most {MAX_JOBS} jobs")
        for job in jobs:
            if not isinstance(job, dict):
                raise InputError("every job must be an object")
            steps = job.get("steps", [])
            if not isinstance(steps, list):
                raise InputError("job steps must be an array")
            if len(steps) > MAX_STEPS_PER_JOB:
                raise InputError(f"a job may contain at most {MAX_STEPS_PER_JOB} steps")
            total_steps += len(steps)
            if any(not isinstance(step, dict) for step in steps):
                raise InputError("every job step must be an object")
        metadata = export.get("metadata", {})
        if (
            metadata is not None
            and isinstance(metadata, dict)
            and len(metadata) > MAX_METADATA_FIELDS
        ):
            raise InputError(f"metadata may contain at most {MAX_METADATA_FIELDS} fields")
    if total_steps > MAX_TOTAL_STEPS:
        raise InputError(f"exports may contain at most {MAX_TOTAL_STEPS} steps in total")


def _load_module(root: Path):
    tools = root / "tools"
    script = tools / "analysis" / "validation_timing_report.py"
    if not script.is_file():
        raise FileNotFoundError("upstream validation timing helper is unavailable")
    spec = importlib.util.spec_from_file_location("_md_mcp_validation_timing_report", script)
    if spec is None or spec.loader is None:
        raise ImportError("could not load upstream validation timing helper")
    sys.path.insert(0, str(tools))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    required = ("parse_export", "TimingSample")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise ImportError(f"upstream timing API is missing: {', '.join(missing)}")
    annotations = getattr(module.TimingSample, "__annotations__", {})
    for name in (
        "database_id",
        "attempt",
        "head_sha",
        "status",
        "conclusion",
        "overall_seconds",
        "jobs",
        "metadata",
        "metadata_error",
    ):
        if name not in annotations:
            raise ImportError(f"upstream timing API is missing TimingSample.{name}")
    for name in (
        "summed_job_seconds",
        "eligible_for_summary",
        "compatibility_key",
    ):
        if not hasattr(module.TimingSample, name):
            raise ImportError(f"upstream timing API is missing TimingSample.{name}")
    return module


def _pair_key(sample: Any) -> str:
    compatibility_key = sample.compatibility_key
    if not isinstance(compatibility_key, str):
        return ""
    compatible = json.loads(compatibility_key)
    metadata = compatible.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("cache", None)
    return json.dumps(compatible, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _stats(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    return {
        "count": len(values),
        "median_seconds": round(statistics.median(values), 3),
        "min_seconds": round(min(values), 3),
        "max_seconds": round(max(values), 3),
    }


def _short(value: Any, length: int = 160) -> Any:
    return value[:length] if isinstance(value, str) else value


def _sample_record(
    sample: Any, include_steps: bool, job_budget: list[int], step_budget: list[int]
) -> dict:
    jobs = []
    for job in sample.jobs[: min(len(sample.jobs), 20, job_budget[0])]:
        job_budget[0] -= 1
        record = {
            "name": job.name[:160],
            "status": _short(job.status),
            "conclusion": _short(job.conclusion),
            "duration_seconds": job.duration_seconds,
            "steps_total": len(job.steps),
        }
        if include_steps:
            steps = []
            for step in job.steps[: min(len(job.steps), 20, step_budget[0])]:
                step_budget[0] -= 1
                steps.append(
                    {
                        "name": step.name[:160],
                        "status": _short(step.status),
                        "conclusion": _short(step.conclusion),
                        "duration_seconds": step.duration_seconds,
                    }
                )
            record.update(
                {
                    "steps_returned": len(steps),
                    "steps_truncated": len(steps) < len(job.steps),
                    "steps": steps,
                }
            )
        jobs.append(record)
    return {
        "database_id": _short(sample.database_id),
        "attempt": sample.attempt,
        "head_sha": sample.head_sha[:160],
        "status": _short(sample.status),
        "conclusion": _short(sample.conclusion),
        "overall_seconds": sample.overall_seconds,
        "summed_job_seconds": sample.summed_job_seconds,
        "eligible_for_summary": bool(sample.eligible_for_summary),
        "metadata_provenance": "user_supplied_unverified",
        "metadata_usable": sample.metadata_error is None,
        "metadata_error": sample.metadata_error,
        "jobs_total": len(sample.jobs),
        "jobs_returned": len(jobs),
        "jobs_truncated": len(jobs) < len(sample.jobs),
        "jobs": jobs,
    }


def _group_record(samples: list[Any], paired_counts: dict[str, int]) -> dict:
    first = samples[0]
    metadata = first.metadata
    job_values: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        for job in sample.jobs:
            if job.duration_seconds is not None:
                job_values[job.name].append(job.duration_seconds)
    job_names = sorted(job_values)
    return {
        "head_sha": first.head_sha[:160],
        "base_sha": str(metadata.get("baseSha", ""))[:160],
        "workload": str(metadata.get("workload", ""))[:160],
        "cache": first.cache_label,
        "sample_count": len(samples),
        "minimum_sample_rule_met": len(samples) >= 3,
        "overall": _stats([s.overall_seconds for s in samples if s.overall_seconds is not None]),
        "summed_job_time": _stats(
            [s.summed_job_seconds for s in samples if s.summed_job_seconds is not None]
        ),
        "jobs_total": len(job_names),
        "jobs_returned": min(20, len(job_names)),
        "jobs_truncated": len(job_names) > 20,
        "jobs": [
            {
                "name": name[:160],
                "name_truncated": len(name) > 160,
                **(_stats(job_values[name]) or {}),
            }
            for name in job_names[:20]
        ],
        "paired_cache_sample_counts": dict(paired_counts),
        "paired_cache_minimum_rule_met": min(paired_counts.values()) >= 3,
        "metadata_provenance": "user_supplied_unverified",
    }


def analyze(root: Path, payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "error": "request must be an object"}
    content = payload.get("content")
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a JSON string"}
    try:
        byte_count = len(content.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return {"ok": False, "error": "content contains invalid Unicode text"}
    if byte_count > MAX_INPUT_BYTES:
        return {"ok": False, "error": f"content exceeds {MAX_INPUT_BYTES} UTF-8 bytes"}
    try:
        decoded = json.loads(
            content,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
        _bounded_tree(decoded)
    except (json.JSONDecodeError, InputError, RecursionError) as exc:
        return {"ok": False, "error": f"invalid timing JSON: {exc}"}
    exports = [decoded] if isinstance(decoded, dict) else decoded
    if not isinstance(exports, list) or not exports:
        return {"ok": False, "error": "content must contain one export object or a non-empty array"}
    if len(exports) > MAX_EXPORTS:
        return {"ok": False, "error": f"content may contain at most {MAX_EXPORTS} exports"}
    if any(not isinstance(export, dict) for export in exports):
        return {"ok": False, "error": "each timing export must be an object"}
    try:
        raw_limit = payload.get("limit", 20)
        raw_offset = payload.get("offset", 0)
        if (
            isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
            or isinstance(raw_offset, bool)
            or not isinstance(raw_offset, int)
        ):
            raise ValueError
        limit = min(PAGE_SIZE_MAX, max(0, raw_limit))
        offset = max(0, raw_offset)
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "error": "limit and offset must be integers"}
    include_steps = payload.get("include_steps", False)
    if not isinstance(include_steps, bool):
        return {"ok": False, "error": "include_steps must be a boolean"}
    try:
        _preflight_exports(exports)
        module = _load_module(root)
        samples = []
        seen: dict[tuple[str, int], Any] = {}
        duplicates = 0
        for index, export in enumerate(exports, 1):
            sample = module.parse_export(export, Path(f"supplied-export-{index}"))
            key = (sample.database_id, sample.attempt)
            if key in seen:
                if seen[key] != sample:
                    return {
                        "ok": False,
                        "error": "conflicting exports share the same databaseId and attempt",
                    }
                duplicates += 1
                continue
            seen[key] = sample
            samples.append(sample)
    except ImportError as exc:
        return {"ok": False, "unsupported": True, "error": str(exc)}
    except FileNotFoundError as exc:
        return {"ok": False, "unsupported": True, "error": str(exc)}
    except (ValueError, TypeError, KeyError, OSError, OverflowError) as exc:
        return {"ok": False, "error": f"timing export could not be parsed: {exc}"}

    groups_by_key: dict[str, list[Any]] = {}
    pair_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"cold": 0, "warm": 0})
    for sample in samples:
        if sample.eligible_for_summary and sample.compatibility_key is not None:
            groups_by_key.setdefault(sample.compatibility_key, []).append(sample)
            pair_counts[_pair_key(sample)][sample.cache_label] += 1
    groups = [
        _group_record(group, pair_counts[_pair_key(group[0])]) for group in groups_by_key.values()
    ]
    start = min(offset, len(samples))
    selected = samples[start : start + limit]
    job_budget = [MAX_RETURNED_JOBS]
    step_budget = [MAX_RETURNED_STEPS]
    sample_rows = [
        _sample_record(sample, include_steps, job_budget, step_budget) for sample in selected
    ]
    group_rows = groups[offset : offset + limit]
    return {
        "ok": True,
        "exports_received": len(exports),
        "duplicates_ignored": duplicates,
        "samples_total": len(samples),
        "samples_returned": len(sample_rows),
        "samples_truncated": start + limit < len(samples),
        "groups_total": len(groups),
        "groups_returned": len(group_rows),
        "groups_truncated": offset + limit < len(groups),
        "samples": sample_rows,
        "groups": group_rows,
        "warnings_total": 3,
        "warnings_returned": 3,
        "warnings_truncated": False,
        "warnings": [
            "Metadata is user-supplied and unverified; groups are descriptive only.",
            "Three matching cold and three matching warm samples are required before a paired "
            "cache-baseline claim.",
            "A group meets the minimum-sample rule at three compatible successful samples; "
            "this is not evidence of a performance gain.",
        ],
    }


def main() -> int:
    try:
        root = Path(sys.argv[1])
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES * 8 + 100_000)
        payload = json.loads(raw)
        sys.dont_write_bytecode = True
        result = analyze(root, payload)
    except Exception as exc:
        result = {"ok": False, "error": f"timing analysis failed: {type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
