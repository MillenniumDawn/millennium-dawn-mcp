"""Interleave cache-path measurements from two checkouts on Linux.

Run with the same interpreter for both revisions. Every sample uses a fresh
process. RSS is the process lifetime high-water mark, including fixture setup;
wall times exclude setup unless named cold_build. No timing thresholds are tests.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import hashlib
import io
import json
import os
import platform
import pstats
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path


def _write(path: Path, text: str, step: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    stamp = 1_700_000_000_000_000_000 + step * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _focus_sample(folder: Path, profile_path: Path | None = None) -> dict:
    from md_mcp.indexes import FocusIndex, base

    root = folder / "mod"
    for number in range(80):
        text = (
            "focus_tree = {\n"
            + "".join(f"focus = {{ id = TST_{number}_{i} x = {i} y = 0 }}\n" for i in range(250))
            + "}\n"
        )
        _write(root / "common/national_focus" / f"{number:03d}.txt", text, 1)
    index = FocusIndex(root, folder / "cache", include_vanilla=False)
    started = time.perf_counter()
    index.ensure_fresh()
    cold = time.perf_counter() - started
    index._stale_check.force_next()
    started = time.perf_counter()
    index.ensure_fresh()
    unchanged = time.perf_counter() - started
    phases = {}
    parsed = []
    writes = []

    def instrument(name):
        original = getattr(index, name)

        def timed(*args, **kwargs):
            start = time.perf_counter()
            result = original(*args, **kwargs)
            phases[name] = (time.perf_counter() - start) * 1000
            if name == "_parse_parallel":
                parsed.extend(args[0])
            return result

        setattr(index, name, timed)

    for name in ("_parse_parallel", "_persist", "_rebuild_tag_indexes"):
        instrument(name)
    write = base._atomic_write_text

    def counted_write(path, text):
        writes.append(len(text.encode("utf-8")))
        return write(path, text)

    base._atomic_write_text = counted_write
    path = root / "common/national_focus/007.txt"
    _write(path, path.read_text(encoding="utf-8") + "# edited\n", 10)
    index._stale_check.force_next()
    started = time.perf_counter()
    if profile_path is None:
        index.ensure_fresh()
    else:
        profile = cProfile.Profile()
        profile.runcall(index.ensure_fresh)
        stream = io.StringIO()
        pstats.Stats(profile, stream=stream).sort_stats("cumulative").print_stats(25)
        profile_path.write_text(stream.getvalue(), encoding="utf-8")
    edit = time.perf_counter() - started
    assert len(index._by_key) == 20_000
    assert len(parsed) == 1 and len(writes) == 2
    return {
        "cold_build_ms": cold * 1000,
        "unchanged_ms": unchanged * 1000,
        "edit_ms": edit * 1000,
        "phase_ms": phases,
        "parsed_files": len(parsed),
        "cache_writes": len(writes),
        "written_bytes": sum(writes),
        "payload_bytes": index._cache.data_path.stat().st_size,
    }


def _ast_sample(folder: Path) -> dict:
    from md_mcp.paradox import ast_cache

    old_text = (
        "focus_tree = {\n"
        + "".join(f"focus = {{ id = TST_{i} x = {i} y = 0 }}\n" for i in range(3000))
        + "}\n"
    )
    new_text = old_text + "# edited version\n"
    path = folder / "race.txt"
    _write(path, old_text, 1)
    parse = ast_cache.parse_string
    old_ready = threading.Event()
    release = threading.Event()
    errors = []
    calls = []

    def blocked(text, **kwargs):
        calls.append(len(text))
        root = parse(text, **kwargs)
        if text == old_text:
            old_ready.set()
            assert release.wait(timeout=20)
        return root

    ast_cache.parse_string = blocked

    def old_request():
        try:
            assert ast_cache.parse_cached(path)[0] == old_text
        except BaseException as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=old_request, daemon=True)
    started = time.perf_counter()
    thread.start()
    try:
        assert old_ready.wait(timeout=20)
        _write(path, new_text, 2)
        new_result = ast_cache.parse_cached(path)
    finally:
        release.set()
        thread.join(timeout=20)
    assert not thread.is_alive() and not errors, errors
    before = ast_cache.total_chars()
    start = time.perf_counter()
    result = ast_cache.parse_cached(path)
    next_read = time.perf_counter() - start
    total = time.perf_counter() - started
    assert result[0] == new_text
    start = time.perf_counter()
    for _ in range(2000):
        ast_cache.parse_cached(path)
    hit_us = (time.perf_counter() - start) * 1_000_000 / 2000
    return {
        "source_bytes": len(new_text.encode("utf-8")),
        "race_and_next_read_ms": total * 1000,
        "next_read_ms": next_read * 1000,
        "parse_calls": len(calls),
        "cache_before_next_read_chars": before,
        "expected_chars": len(new_text),
        "new_tree_reused": result[1] is new_result[1],
        "warm_hit_us": hit_us,
    }


async def _live_sample(mod_root: Path, cache: Path) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import TextContent

    code = (
        "from pathlib import Path; from md_mcp import config; "
        "config.CONFIG_PATH = Path('absent-benchmark-config.toml'); "
        "from md_mcp.server import main; main()"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MD_") and k != "HOI4_PATH"}
    env["MD_MCP_CACHE_DIR"] = str(cache)
    env["MD_MCP_LOC_LANGS"] = "en"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", code, "serve", "--mod-root", str(mod_root)],
        env=env,
        cwd=str(cache.parent),
    )
    timings = []
    payloads = []
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session,
    ):
        await session.initialize()
        for _ in range(2):
            started = time.perf_counter()
            result = await session.call_tool("focus_graph", {"tag": "ISR", "detail": "summary"})
            timings.append((time.perf_counter() - started) * 1000)
            assert not result.isError
            content = result.content[0]
            assert isinstance(content, TextContent)
            payload = json.loads(content.text)
            assert payload["ok"] and payload["node_count"] > 0, payload
            payloads.append(payload)
    assert payloads[0] == payloads[1]
    return {
        "first_call_ms": timings[0],
        "warm_call_ms": timings[1],
        "node_count": payloads[0]["node_count"],
        "payload_sha256": hashlib.sha256(
            json.dumps(payloads[0], sort_keys=True).encode()
        ).hexdigest(),
        "server_peak_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    }


def main() -> None:
    """Run samples in alternating AB/BA order and retain every observation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mod-root", type=Path)
    parser.add_argument("--worker", choices=("focus", "ast", "live"))
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--profile", type=Path, help="Profile a focus worker's edit separately")
    args = parser.parse_args()
    if args.worker:
        import md_mcp

        os.environ["MD_MCP_SERIAL_PARSE"] = "1"
        with tempfile.TemporaryDirectory() as folder:
            if args.worker == "focus":
                sample = _focus_sample(Path(folder), args.profile)
            elif args.worker == "ast":
                sample = _ast_sample(Path(folder))
            else:
                sample = asyncio.run(asyncio.wait_for(_live_sample(args.mod_root, args.cache), 90))
        sample["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        sample["source_file"] = str(Path(md_mcp.__file__).resolve())
        print(json.dumps(sample))
        return
    checkouts = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
    commits = {
        name: subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
        ).strip()
        for name, path in checkouts.items()
    }
    report = {
        "platform": platform.platform(),
        "python": sys.version,
        "commits": commits,
        "repetitions": args.repetitions,
        "order": "AB on even repetitions, BA on odd repetitions; each sample is a fresh process",
        "rss": "Linux KiB high-water RSS over process lifetime including fixture setup",
        "samples": [],
    }
    scenarios = ["focus", "ast"] + (["live"] if args.mod_root else [])
    with tempfile.TemporaryDirectory() as folder:

        def run(name, scenario):
            env = os.environ.copy()
            env["PYTHONPATH"] = str(checkouts[name] / "src")
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", scenario]
            if scenario == "live":
                command += ["--mod-root", str(args.mod_root), "--cache", str(Path(folder) / name)]
            proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
            assert proc.returncode == 0, proc.stderr
            sample = json.loads(proc.stdout)
            assert sample["source_file"] == str(checkouts[name] / "src/md_mcp/__init__.py")
            return sample

        if args.mod_root:
            report["mod_commit"] = subprocess.check_output(
                ["git", "-C", str(args.mod_root), "rev-parse", "HEAD"], text=True
            ).strip()
            # Persist both indexes before measuring fresh servers over a warm disk cache.
            for name in checkouts:
                run(name, "live")
        for repetition in range(args.repetitions):
            order = list(checkouts) if repetition % 2 == 0 else list(reversed(checkouts))
            for scenario in scenarios:
                for name in order:
                    sample = run(name, scenario)
                    report["samples"].append(
                        {"revision": name, "scenario": scenario, "repetition": repetition, **sample}
                    )
                    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = {}
    for scenario in scenarios:
        for name in checkouts:
            rows = [
                s for s in report["samples"] if s["scenario"] == scenario and s["revision"] == name
            ]
            summary[f"{scenario}:{name}"] = {
                field: {
                    "median": statistics.median(row[field] for row in rows),
                    "min": min(row[field] for row in rows),
                    "max": max(row[field] for row in rows),
                    "stdev": statistics.stdev(row[field] for row in rows) if len(rows) > 1 else 0,
                }
                for field in rows[0]
                if isinstance(rows[0][field], (int, float))
                and field.endswith(("_ms", "_us", "_kib"))
            }
    report["summary"] = summary
    live_rows = [sample for sample in report["samples"] if sample["scenario"] == "live"]
    if live_rows:
        assert len({sample["payload_sha256"] for sample in live_rows}) == 1
        assert len({sample["node_count"] for sample in live_rows}) == 1
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
