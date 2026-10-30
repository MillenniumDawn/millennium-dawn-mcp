from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import md_mcp
from benchmarks.validator_concurrency import (
    _normalise,
    _run,
    _sample,
    _select_physical_cpus,
    _validate_args,
)


def test_benchmark_imports_md_mcp_from_this_checkout():
    expected = Path(__file__).resolve().parents[1] / "src" / "md_mcp"
    assert Path(md_mcp.__file__).resolve().parent == expected


def test_normalise_ignores_issue_order_but_preserves_issue_content():
    first: dict[str, dict[str, Any]] = {
        "beta": {
            "ok": True,
            "issues": [{"message": "b"}, {"message": "a"}],
            "counts": {"warning": 2},
            "elapsed_s": 1.2,
            "suppressed": 1,
        },
        "alpha": {"ok": True, "issues": [], "counts": {}},
    }
    second: dict[str, dict[str, Any]] = {
        "alpha": {"ok": True, "issues": [], "counts": {}},
        "beta": {
            "ok": True,
            "issues": [{"message": "a"}, {"message": "b"}],
            "counts": {"warning": 2},
            "elapsed_s": 9.3,
            "suppressed": 1,
        },
    }

    assert _normalise(first) == _normalise(second)
    second["beta"]["issues"][0]["message"] = "different"
    assert _normalise(first) != _normalise(second)
    second["beta"]["issues"][0]["message"] = "a"
    second["beta"]["suppressed"] = 2
    assert _normalise(first) != _normalise(second)


@pytest.mark.parametrize("failed_workers", [1, 2])
def test_sample_reports_failure_from_either_mode(monkeypatch, tmp_path, failed_workers):
    def run(_mod_root, names, workers, _cpu_budget):
        ok = workers != failed_workers
        return 0.1, {name: {"ok": ok, "error": "broken" if not ok else None} for name in names}

    monkeypatch.setattr("benchmarks.validator_concurrency._run", run)
    with pytest.raises(RuntimeError, match="validator run failed") as exc_info:
        _sample(tmp_path, ["validator"], cpu_budget=2, workers=2, serial_first=True)
    assert ("sequential" if failed_workers == 1 else "concurrent") in str(exc_info.value)


def test_sample_fails_when_results_differ(monkeypatch, tmp_path):
    def run(_mod_root, names, workers, _cpu_budget):
        message = "serial" if workers == 1 else "parallel"
        results = {
            name: {"ok": True, "issues": [{"message": message}], "counts": {}} for name in names
        }
        return 0.1, results

    monkeypatch.setattr("benchmarks.validator_concurrency._run", run)
    with pytest.raises(RuntimeError, match="results differ"):
        _sample(tmp_path, ["validator"], cpu_budget=2, workers=2, serial_first=True)


def test_select_physical_cpus_deduplicates_by_package_and_core(tmp_path):
    for cpu, package, core in [(0, 0, 0), (1, 0, 0), (2, 1, 0), (3, 1, 1)]:
        topology = tmp_path / f"cpu{cpu}" / "topology"
        topology.mkdir(parents=True)
        (topology / "physical_package_id").write_text(str(package))
        (topology / "core_id").write_text(str(core))

    assert _select_physical_cpus([0, 1, 2, 3], 3, tmp_path) == [
        (0, 0, 0),
        (2, 1, 0),
        (3, 1, 1),
    ]
    with pytest.raises(ValueError, match=r"available physical cores \(3\)"):
        _select_physical_cpus([0, 1, 2, 3], 4, tmp_path)


def test_validate_args_rejects_invalid_physical_cpu_budget():
    with pytest.raises(ValueError, match="physical CPU budget"):
        _validate_args(cpu_budget=1, workers=2, repetitions=1)
    with pytest.raises(ValueError, match="positive"):
        _validate_args(cpu_budget=4, workers=2, repetitions=0)


def test_run_restores_md_max_workers(monkeypatch, tmp_path):
    class FakeRunner:
        def __init__(self, _mod_root, *, mode):
            assert mode == "isolated"

        def run(self, name):
            assert os.environ["MD_MAX_WORKERS"] == "3"
            return {"ok": True, "issues": [], "counts": {}}

    monkeypatch.setattr("benchmarks.validator_concurrency.ValidatorRunner", FakeRunner)
    monkeypatch.setenv("MD_MAX_WORKERS", "original")
    _run(tmp_path, ["one"], workers=2, cpu_budget=6)
    assert os.environ["MD_MAX_WORKERS"] == "original"
