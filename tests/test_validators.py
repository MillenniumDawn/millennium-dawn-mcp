"""Validator-wrapper tests.

The unit-test layer can't fully exercise the wrappers — they import Millennium-Dawn
validator modules from the real mod tree. Most assertions therefore live in the
integration suite (`@pytest.mark.integration`), gated on MD_MOD_ROOT.

Isolated-mode tests plant synthetic `validate_*.py` modules in the fake mod's
`tools/validation/`, so they exercise the real subprocess path without a checkout.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from md_mcp.validators import SLOW_VALIDATORS, ValidatorRunner, available_validators
from md_mcp.validators import runner as runner_module
from md_mcp.validators.runner import _collect
from tests.support.upstream import ISSUE_CLASS, plant_validator


def test_run_many_isolates_parallel_results_and_preserves_order(fake_mod_root, monkeypatch):
    for name in ("alpha", "beta", "gamma", "delta"):
        plant_validator(fake_mod_root, name, _PLAIN)
    monkeypatch.setattr(runner_module, "_validator_cpu_budget", lambda: 4)
    monkeypatch.setattr(runner_module, "_maintain_validator_cache", lambda _: None)
    barrier = threading.Barrier(4)
    release = {name: threading.Event() for name in ("alpha", "beta", "gamma", "delta")}
    observed_budgets = []

    def run_isolated(self, info, **kwargs):
        observed_budgets.append(kwargs["worker_budget"])
        barrier.wait(timeout=5)
        if info.name == "delta":
            release["gamma"].set()
        else:
            assert release[info.name].wait(timeout=5)
            if info.name == "gamma":
                release["beta"].set()
            elif info.name == "beta":
                release["alpha"].set()
        if info.name == "gamma":
            return {
                "ok": False,
                "validator": info.name,
                "error": "failed",
                "counts": {"error": 2},
                "elapsed_s": 0.0,
            }
        return {"ok": True, "validator": info.name, "issues": [], "counts": {}}

    monkeypatch.setattr(ValidatorRunner, "_run_isolated", run_isolated)
    results = ValidatorRunner(fake_mod_root).run_many(["alpha", "beta", "gamma", "delta"])

    assert [result["validator"] for result in results] == ["alpha", "beta", "gamma", "delta"]
    assert observed_budgets == [1, 1, 1, 1]
    assert results[2]["ok"] is False
    assert results[2]["error"] == "failed"
    assert results[2]["counts"] == {"error": 2}
    assert all("elapsed_s" in result for result in results)


@pytest.mark.parametrize(
    ("budget", "names", "expected"),
    [(1, ["alpha"], "1"), (4, ["alpha", "beta"], "2")],
)
def test_run_many_applies_child_budget_through_legacy_run_override(
    fake_mod_root, monkeypatch, budget, names, expected
):
    for name in ("alpha", "beta"):
        plant_validator(fake_mod_root, name, _WORKER_BUDGET)
    monkeypatch.setattr(runner_module, "_validator_cpu_budget", lambda: budget)
    monkeypatch.setenv("MD_MAX_WORKERS", "14")

    class LegacyRunner(ValidatorRunner):
        def __init__(self):
            super().__init__(fake_mod_root)
            self.calls = []

        def run(self, name, *, staged_only=False, files=None, post_filter=True, args=None):
            self.calls.append(name)
            return super().run(
                name, staged_only=staged_only, files=files, post_filter=post_filter, args=args
            )

    runner = LegacyRunner()
    results = runner.run_many(names)
    named_result = runner.run("alpha")

    assert sorted(runner.calls[:-1]) == sorted(names)
    assert runner.calls[-1] == "alpha"
    assert [issue["message"] for result in results for issue in result["issues"]] == [
        expected
    ] * len(names)
    assert [issue["message"] for issue in named_result["issues"]] == ["14"]
    assert os.environ["MD_MAX_WORKERS"] == "14"


def test_run_many_resets_worker_budget_after_override_failure(fake_mod_root, monkeypatch):
    runner = ValidatorRunner(fake_mod_root)
    monkeypatch.setattr(runner_module, "_validator_cpu_budget", lambda: 1)

    def fail(name):
        assert runner._worker_budget.get() == 1
        raise RuntimeError("override failed")

    monkeypatch.setattr(runner, "run", fail)
    with pytest.raises(RuntimeError, match="override failed"):
        runner.run_many(["alpha"])
    assert runner._worker_budget.get() is None


def test_run_many_empty_and_mode_fallback_do_not_allocate_threads(fake_mod_root, monkeypatch):
    monkeypatch.setattr(
        runner_module, "ThreadPoolExecutor", lambda **_: pytest.fail("unexpected thread pool")
    )
    runner = ValidatorRunner(fake_mod_root, mode="in_process")
    calls = []

    def run(name, **kwargs):
        calls.append((name, kwargs))
        return {"ok": True}

    monkeypatch.setattr(runner, "run", run)

    assert runner.run_many([]) == []
    assert runner.run_many(["alpha", "beta"]) == [{"ok": True}, {"ok": True}]
    assert calls == [("alpha", {}), ("beta", {})]


def test_validator_cpu_budget_matches_default_pool_and_caps(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 24)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(range(14)), raising=False)
    monkeypatch.setenv("MD_MAX_WORKERS", "14")
    assert runner_module._validator_cpu_budget() == 12

    monkeypatch.setenv("MD_MAX_WORKERS", "3")
    assert runner_module._validator_cpu_budget() == 3

    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(range(2)), raising=False)
    monkeypatch.delenv("MD_MAX_WORKERS")
    assert runner_module._validator_cpu_budget() == 2

    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setenv("MD_MAX_WORKERS", "14")
    assert runner_module._validator_cpu_budget() == 12
    monkeypatch.setenv("MD_MAX_WORKERS", "²")
    assert runner_module._validator_cpu_budget() == 12
    monkeypatch.setenv("MD_MAX_WORKERS", "invalid")
    assert runner_module._validator_cpu_budget() == 12

    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(), raising=False)
    assert runner_module._validator_cpu_budget() == 1
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 0)
    monkeypatch.setenv("MD_MAX_WORKERS", "0")
    assert runner_module._validator_cpu_budget() == 1


_WORKER_BUDGET = (
    ISSUE_CLASS
    + """
import os

class Validator:
    TITLE = "Budget"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self._issues = []

    def run_all_validations(self):
        budget = os.environ.get("MD_MAX_WORKERS", "missing")
        self._issues = [_Issue(severity="info", category="worker", message=budget)]
"""
)


_PLAIN = (
    ISSUE_CLASS
    + """
class Validator:
    TITLE = "Fake"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self.mod_path = mod_path
        self.staged_only = staged_only
        self._issues = []

    def run_all_validations(self):
        print("stdout chatter the runner must swallow")
        self._issues = [
            _Issue(severity="warning", category="fake", message="m1", file="events/a.txt", line=3),
            _Issue(severity="error", category="fake", message="staged=%s" % self.staged_only),
        ]
"""
)

# Mirrors validator_common.py, which forks a Pool from the shared base class.
_FORKING = (
    ISSUE_CLASS
    + """
from multiprocessing import Pool


def _double(n):
    return n * 2


class Validator:
    TITLE = "Forking"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self._issues = []

    def run_all_validations(self):
        with Pool(processes=2) as pool:
            got = pool.map(_double, list(range(20)))
        self._issues = [_Issue(severity="info", category="fork", message="sum=%d" % sum(got))]
"""
)

_EXITS = (
    ISSUE_CLASS
    + """
import sys


class Validator:
    TITLE = "Exits"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self._issues = []

    def run_all_validations(self):
        self._issues = [_Issue(severity="warning", category="x", message="found before exit")]
        sys.exit(1)
"""
)


# Emits the non-uniform `Issue.file` shapes the upstream suite produces.
_BASENAMES = (
    ISSUE_CLASS
    + """
class Validator:
    TITLE = "Basenames"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self._issues = []

    def run_all_validations(self):
        self._issues = [
            _Issue(severity="warning", category="x", message="bad", file="test_events.txt"),
            _Issue(severity="warning", category="x", message="other", file="test.txt"),
            _Issue(severity="info", category="x", message="nowhere", file="unknown"),
        ]
"""
)

# Simulate a CLI cache payload whose class no longer exists in __main__.
_UNPICKLABLE = (
    r"""
import pickle
from types import SimpleNamespace


disk_cache = SimpleNamespace(_DB_ERRORS=(pickle.UnpicklingError,))


def _get():
    try:
        pickle.loads(b"c__main__\nVariantDefinition\n.")
    except disk_cache._DB_ERRORS:
        return False, None
    return True, object()


"""
    + ISSUE_CLASS
    + """
class Validator:
    TITLE = "Unpicklable"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self._issues = []

    def run_all_validations(self):
        hit, _ = _get()
        if not hit:
            self._issues = [
                _Issue(
                    severity="warning",
                    category="cache",
                    message="recomputed after miss (attribute_error=%d)"
                    % disk_cache._DB_ERRORS.count(AttributeError),
                )
            ]
"""
)


def test_files_filter_resolves_nonuniform_issue_paths(fake_mod_root):
    """A bare-basename issue must land in scope via attribution, not be dropped."""
    plant_validator(fake_mod_root, "basenames", _BASENAMES)
    result = ValidatorRunner(fake_mod_root).run("basenames", files=["events/test_events.txt"])
    assert result["ok"] is True
    assert [i["file"] for i in result["issues"]] == ["events/test_events.txt"]
    # "test.txt" resolves to common/national_focus/test.txt — attributed but out
    # of scope. "unknown" with no filename in the message stays unattributed.
    assert result["unattributed"] == 1
    assert result["counts"] == {"error": 0, "warning": 1, "info": 0}


def test_available_validators_empty_for_fake_mod(fake_mod_root):
    # Our fixture has `tools/validation/` empty.
    infos = available_validators(fake_mod_root)
    assert infos == []


# ---------------------------------------------------------------------------
# TITLE scraping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('TITLE = "Double Quoted"\n', "Double Quoted", id="double-quoted"),
        pytest.param("TITLE = 'Single Quoted'\n", "Single Quoted", id="single-quoted"),
        pytest.param('TITLE: str = "Annotated"\n', "Annotated", id="annotated"),
        pytest.param(
            'TITLE = "Trailing comment"  # display name\n',
            "Trailing comment",
            id="trailing-comment",
        ),
        pytest.param('TITLE = "Escaped \\"quote\\""\n', 'Escaped "quote"', id="escaped"),
    ],
)
def test_title_scraped_from_quoted_literal(fake_mod_root, source, expected):
    plant_validator(fake_mod_root, "literal", source)
    (info,) = available_validators(fake_mod_root)
    assert info.title == expected
    assert info.title_source == "scraped"


def test_title_derived_for_computed_expression(fake_mod_root):
    plant_validator(fake_mod_root, "my_check", 'TITLE = "Prefix" + suffix\n')
    (info,) = available_validators(fake_mod_root)
    assert info.title == "My Check"
    assert info.title_source == "derived"


def test_title_derived_for_a_literal_python_cannot_evaluate(fake_mod_root):
    plant_validator(fake_mod_root, "bad_escape", 'TITLE = "bad \\x escape"\n')
    (info,) = available_validators(fake_mod_root)
    assert info.title == "Bad Escape"
    assert info.title_source == "derived"


def test_no_validators_without_a_validation_directory(tmp_path):
    assert available_validators(tmp_path) == []


def test_only_validate_modules_are_listed(fake_mod_root):
    validation = fake_mod_root / "tools" / "validation"
    for name in ("validator_common.py", "run_all_validators.py", "equipment_module_slots.py"):
        (validation / name).write_text("X = 1\n", encoding="utf-8")
    plant_validator(fake_mod_root, "real", 'TITLE = "Real"\n')

    assert [info.name for info in available_validators(fake_mod_root)] == ["real"]


def test_unknown_validator_is_a_shaped_error(fake_mod_root):
    result = ValidatorRunner(fake_mod_root).run("absent")
    assert result["ok"] is False
    assert result["validator"] == "absent"


def test_title_derived_when_file_read_fails(fake_mod_root, monkeypatch):
    plant_validator(fake_mod_root, "unread", 'TITLE = "Fine"\n')

    def raise_oserror(*_args, **_kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", raise_oserror)
    (info,) = available_validators(fake_mod_root)
    assert info.title == "Unread"
    assert info.title_source == "derived"


# ---------------------------------------------------------------------------
# isolated mode — the default; runs each validator in a clean child process
# ---------------------------------------------------------------------------


def test_default_mode_is_isolated(fake_mod_root):
    assert ValidatorRunner(fake_mod_root).mode == "isolated"


_MODES = ["isolated", "in_process"]


# `subprocess` is an alias for isolated.
@pytest.mark.parametrize("mode", [*_MODES, "subprocess"])
def test_runner_returns_issues_and_swallows_stdout(fake_mod_root, mode):
    plant_validator(fake_mod_root, "plain", _PLAIN)
    result = ValidatorRunner(fake_mod_root, mode=mode).run("plain")
    assert result["ok"] is True
    assert [i["message"] for i in result["issues"]] == ["m1", "staged=False"]
    assert result["issues"][0]["file"] == "events/a.txt"
    assert result["counts"] == {"error": 1, "warning": 1, "info": 0}


# In-process imports stay in sys.modules for the session, so each mode plants
# its own module name.
@pytest.mark.parametrize("mode", _MODES)
def test_runner_keeps_issues_when_validator_exits(fake_mod_root, mode):
    plant_validator(fake_mod_root, f"exits_{mode}", _EXITS)
    result = ValidatorRunner(fake_mod_root, mode=mode).run(f"exits_{mode}")
    assert result["ok"] is True
    assert [i["message"] for i in result["issues"]] == ["found before exit"]


@pytest.mark.parametrize("mode", _MODES)
def test_runner_tolerates_unpicklable_cache_rows(fake_mod_root, mode):
    """A cached class missing from this __main__ is a cache miss, not a crash."""
    plant_validator(fake_mod_root, f"unpicklable_{mode}", _UNPICKLABLE)
    runner = ValidatorRunner(fake_mod_root, mode=mode)
    result = runner.run(f"unpicklable_{mode}")
    assert result["ok"] is True
    assert [i["message"] for i in result["issues"]] == ["recomputed after miss (attribute_error=1)"]
    assert result["counts"] == {"error": 0, "warning": 1, "info": 0}

    again = runner.run(f"unpicklable_{mode}")
    assert again["ok"] is True
    assert [i["message"] for i in again["issues"]] == ["recomputed after miss (attribute_error=1)"]


@pytest.mark.parametrize("mode", _MODES)
def test_runner_still_surfaces_real_validator_errors_outside_cache(fake_mod_root, mode):
    """AttributeError raised by validator logic must not be swallowed as a miss."""
    plant_validator(
        fake_mod_root,
        f"raising_{mode}",
        ISSUE_CLASS
        + """
import pickle
from types import SimpleNamespace


disk_cache = SimpleNamespace(_DB_ERRORS=(pickle.UnpicklingError,))


class Validator:
    TITLE = "Raising"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self._issues = []

    def run_all_validations(self):
        raise AttributeError("variant lookup exploded in validator logic")
""",
    )
    result = ValidatorRunner(fake_mod_root, mode=mode).run(f"raising_{mode}")
    assert result["ok"] is False
    assert "AttributeError" in result["error"]
    assert "variant lookup exploded" in result["error"]


@pytest.mark.parametrize("mode", _MODES)
def test_runner_reports_broken_validator_instead_of_zero_issues(fake_mod_root, mode):
    # Regression: the old subprocess mode ignored the exit code and treated a
    # missing sidecar as a clean run, so a crashing validator reported ok/0.
    plant_validator(fake_mod_root, f"broken_{mode}", "this is not valid python (\n")
    result = ValidatorRunner(fake_mod_root, mode=mode).run(f"broken_{mode}")
    assert result["ok"] is False
    assert result["validator"] == f"broken_{mode}"
    assert result["error"].startswith("SyntaxError: ")


@pytest.mark.parametrize("mode", _MODES)
def test_runner_reports_missing_validator_class(fake_mod_root, mode):
    plant_validator(fake_mod_root, f"classless_{mode}", "X = 1\n")
    result = ValidatorRunner(fake_mod_root, mode=mode).run(f"classless_{mode}")
    assert result["ok"] is False
    assert "Validator" in result["error"]


def test_isolated_forwards_staged_only(fake_mod_root):
    import subprocess

    plant_validator(fake_mod_root, "plain", _PLAIN)
    subprocess.run(["git", "init", str(fake_mod_root)], check=True, capture_output=True)
    result = ValidatorRunner(fake_mod_root).run("plain", staged_only=True)
    assert result["issues"][1]["message"] == "staged=True"


def test_staged_file_environment_uses_utf8_nul_paths(tmp_path):
    import subprocess

    from md_mcp.validators.runner import _staged_files_env

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    path = tmp_path / "events" / "café.txt"
    path.parent.mkdir()
    path.write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", "events/café.txt"], check=True)
    assert _staged_files_env(tmp_path) == "events/café.txt"


def test_staged_file_environment_includes_rename_source_and_destination(tmp_path):
    import subprocess

    from md_mcp.validators.runner import _staged_files_env

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    old = tmp_path / "events" / "café.txt"
    old.parent.mkdir()
    old.write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    new = old.with_name("renamed.txt")
    old.rename(new)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert set(_staged_files_env(tmp_path).splitlines()) == {
        "events/café.txt",
        "events/renamed.txt",
    }


def test_staged_file_environment_reports_git_failure(tmp_path):
    import subprocess

    from md_mcp.validators.runner import _staged_files_env

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".git" / "index").write_bytes(b"broken")
    with pytest.raises(RuntimeError, match="Could not read staged paths"):
        _staged_files_env(tmp_path)


def test_staged_file_environment_reports_git_timeout(monkeypatch, tmp_path):
    import subprocess

    from md_mcp.validators.runner import _staged_files_env

    def hung(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hung)
    with pytest.raises(RuntimeError, match="from git: Git command timed out after 15s"):
        _staged_files_env(tmp_path)


@pytest.mark.parametrize("mode", _MODES)
def test_staged_validator_reports_git_failure(fake_mod_root, mode):
    plant_validator(fake_mod_root, "plain", _PLAIN)
    (fake_mod_root / ".git").mkdir()
    (fake_mod_root / ".git" / "index").write_bytes(b"broken")
    result = ValidatorRunner(fake_mod_root, mode=mode).run("plain", staged_only=True)
    assert result["ok"] is False
    assert "Could not read staged paths" in result["error"]


def test_isolated_survives_a_forking_validator(fake_mod_root):
    # The whole point of isolated mode: the fork happens in a child with no
    # asyncio loop and no inherited stdio, so it can't deadlock the server.
    plant_validator(fake_mod_root, "forking", _FORKING)
    result = ValidatorRunner(fake_mod_root).run("forking")
    assert result["ok"] is True
    assert result["issues"][0]["message"] == "sum=380"


@pytest.mark.parametrize("mode", _MODES)
def test_validator_args_reach_instance(fake_mod_root, mode):
    source = (
        ISSUE_CLASS
        + """
import argparse

def _add_extra_args(parser):
    parser.add_argument("--enable-extra-check", action="store_true")

class Validator:
    TITLE = "Arg-aware"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self.enabled = kw.get("enable_extra_check", False)
        self._issues = []

    def run_all_validations(self):
        self._issues = [_Issue(severity="info", message=str(self.enabled))]
"""
    )
    plant_validator(fake_mod_root, "arg_aware", source)

    result = ValidatorRunner(fake_mod_root, mode=mode).run(
        "arg_aware", args=["--enable-extra-check"]
    )

    assert result["ok"] is True
    assert result["issues"][0]["message"] == "True"


@pytest.mark.parametrize("mode", _MODES)
def test_validator_args_are_rejected_when_the_module_cannot_take_them(fake_mod_root, mode):
    plant_validator(fake_mod_root, "plain", _PLAIN)
    result = ValidatorRunner(fake_mod_root, mode=mode).run("plain", args=["--anything"])
    assert result["ok"] is False
    assert "does not accept validator arguments" in result["error"]


def test_isolated_reports_a_child_that_exits_without_a_payload(fake_mod_root):
    source = _PLAIN.replace(
        'print("stdout chatter the runner must swallow")', '__import__("os")._exit(0)'
    )
    plant_validator(fake_mod_root, "vanishes", source)

    result = ValidatorRunner(fake_mod_root).run("vanishes")

    assert result["ok"] is False
    assert result["validator"] == "vanishes"


# In-process imports stay in sys.modules for the session, so these use names no
# other test plants.
def test_collect_raises_where_run_returns_an_error(fake_mod_root):
    # The docs send people to _collect for a traceback because run() catches.
    source = _PLAIN.replace(
        'print("stdout chatter the runner must swallow")',
        'raise RuntimeError("boom from validator")',
    )
    plant_validator(fake_mod_root, "raises_inproc", source)

    result = ValidatorRunner(fake_mod_root, mode="in_process").run("raises_inproc")
    assert result["ok"] is False
    assert result["error"] == "RuntimeError: boom from validator"

    with pytest.raises(RuntimeError, match="boom from validator"):
        _collect(str(fake_mod_root), "validate_raises_inproc", False)


def test_in_process_failure_preserves_validator_stderr(fake_mod_root):
    source = _PLAIN.replace(
        'print("stdout chatter the runner must swallow")',
        'print("validator diagnostic", file=__import__("sys").stderr)\n'
        '        raise RuntimeError("boom from validator")',
    )
    plant_validator(fake_mod_root, "raises_with_stderr_inproc", source)

    result = ValidatorRunner(fake_mod_root, mode="in_process").run("raises_with_stderr_inproc")

    assert result["ok"] is False
    assert result["error"] == "RuntimeError: boom from validator"
    assert result["stderr"] == "validator diagnostic\n"


@pytest.mark.integration
def test_validator_list_against_real_mod(real_mod_root):
    infos = available_validators(real_mod_root)
    names = {v.name for v in infos}
    derived_names = [v.name for v in infos if v.title_source != "scraped"]
    # At least the headline validators we wrap exist.
    assert {"localisation", "ideas", "events", "decisions", "variables"} <= names
    assert not derived_names, f"Validators with derived titles: {derived_names}"


@pytest.mark.integration
def test_run_all_fast_validators(real_mod_root):
    runner = ValidatorRunner(real_mod_root)
    names = [v.name for v in available_validators(real_mod_root) if v.name not in SLOW_VALIDATORS]
    failures = []
    for name in names:
        try:
            result = runner.run(name)
        except Exception as e:
            failures.append(f"{name}: raised {type(e).__name__}: {e}")
            continue
        if not result.get("ok"):
            failures.append(f"{name}: {result.get('error', 'reported ok=false')}")
            continue
        if "counts" not in result or not isinstance(result.get("issues"), list):
            failures.append(f"{name}: invalid result shape")

    assert not failures, "Fast validator failures:\n" + "\n".join(failures)


@pytest.mark.integration
def test_unknown_validator_returns_error(real_mod_root):
    runner = ValidatorRunner(real_mod_root)
    result = runner.run("does_not_exist")
    assert result["ok"] is False
    assert "Unknown validator" in result["error"]


def test_shim_write_failure_returns_1(monkeypatch):
    """A failed result-write surfaces as a nonzero exit, not a silent crash."""
    import builtins
    import sys

    from md_mcp.validators import _shim

    monkeypatch.setattr(
        sys, "argv", ["_shim", "--mod-root", "/x", "--module", "stub", "--out", "/out.json"]
    )
    monkeypatch.setattr(_shim, "_collect", lambda *a, **k: {"ok": True, "issues": []})
    real_open = builtins.open

    def _raising_open(path, *a, **k):
        if str(path) == "/out.json":
            raise OSError("denied")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", _raising_open)
    assert _shim.main() == 1


def test_staged_file_environment_empty_list_and_newline_rejection(tmp_path):
    import subprocess

    from md_mcp.validators.runner import _staged_files_env

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    assert _staged_files_env(tmp_path) == ""
    path = tmp_path / "events" / "line\nbreak.txt"
    path.parent.mkdir()
    path.write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", str(path)], check=True)
    with pytest.raises(ValueError, match="newline characters"):
        _staged_files_env(tmp_path)


def test_staged_file_environment_rejects_malformed_records(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace

    from md_mcp.validators import runner

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=b"R100\0only-old\0", stderr=b""
        ),
    )
    with pytest.raises(ValueError, match="Malformed staged path record"):
        runner._staged_files_env(tmp_path)


def test_in_process_staged_environment_is_restored(fake_mod_root, monkeypatch):
    import subprocess

    import md_mcp.validators.runner as runner_module

    plant_validator(fake_mod_root, "plain", _PLAIN)
    subprocess.run(["git", "init", str(fake_mod_root)], check=True, capture_output=True)
    staged = fake_mod_root / "events" / "café.txt"
    staged.write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(fake_mod_root), "add", "--", "events/café.txt"], check=True)
    previous = os.environ.get("MD_STAGED_FILES")
    os.environ["MD_STAGED_FILES"] = "previous-value"
    observed = {}

    def collect(*args, **kwargs):
        observed["staged"] = os.environ.get("MD_STAGED_FILES")
        return {"ok": True, "issues": []}

    monkeypatch.setattr(runner_module, "_collect", collect)
    try:
        result = ValidatorRunner(fake_mod_root, mode="in_process").run("plain", staged_only=True)
        assert result["ok"] is True
        assert observed["staged"] == "events/café.txt"
        assert os.environ["MD_STAGED_FILES"] == "previous-value"
    finally:
        if previous is None:
            os.environ.pop("MD_STAGED_FILES", None)
        else:
            os.environ["MD_STAGED_FILES"] = previous


def test_in_process_staged_environment_removes_temporary_value(fake_mod_root, monkeypatch):
    import subprocess

    import md_mcp.validators.runner as runner_module

    plant_validator(fake_mod_root, "plain", _PLAIN)
    subprocess.run(["git", "init", str(fake_mod_root)], check=True, capture_output=True)
    original = os.environ.pop("MD_STAGED_FILES", None)
    monkeypatch.setattr(
        runner_module, "_collect", lambda *args, **kwargs: {"ok": True, "issues": []}
    )
    try:
        result = ValidatorRunner(fake_mod_root, mode="in_process").run("plain", staged_only=True)
        assert result["ok"] is True
        assert "MD_STAGED_FILES" not in os.environ
    finally:
        if original is not None:
            os.environ["MD_STAGED_FILES"] = original


def test_staged_file_environment_rejects_unknown_status(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace

    from md_mcp.validators import runner

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"?\0odd\0", stderr=b""),
    )
    with pytest.raises(ValueError, match="Malformed staged path status"):
        runner._staged_files_env(tmp_path)


def test_temporary_staged_environment_removes_absent_previous_value(monkeypatch):
    from md_mcp.validators.runner import _temporary_staged_env

    monkeypatch.delenv("MD_STAGED_FILES", raising=False)
    with _temporary_staged_env(None):
        assert "MD_STAGED_FILES" not in os.environ
    assert "MD_STAGED_FILES" not in os.environ


def test_cache_maintenance_honors_no_cache_and_runs_upstream_startup_once(
    monkeypatch, tmp_path, caplog
):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from md_mcp.validators import runner

    calls: list[tuple] = []

    def clear_if_stale(path, days):
        calls.append(("clear", path, days))
        return True

    def prune_old_versions(path):
        calls.append(("prune", path))
        return ["v8"]

    disk_cache = SimpleNamespace(
        clear_if_stale=clear_if_stale, prune_old_versions=prune_old_versions
    )
    caplog.set_level("INFO", logger=runner.__name__)

    class Loader:
        def load(self, root, names):
            return {"disk_cache": disk_cache}

    monkeypatch.setattr(runner, "_CACHE_MAINTENANCE", Loader())
    monkeypatch.setattr(runner, "_CACHE_MAINTAINED", set())
    monkeypatch.setenv("MD_NO_CACHE", "1")
    runner._maintain_validator_cache(tmp_path)
    assert calls == []

    monkeypatch.delenv("MD_NO_CACHE")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: runner._maintain_validator_cache(tmp_path), range(24)))
    root = str(tmp_path.resolve())
    assert calls == [("clear", root, 7.0), ("prune", root)]
    assert "cleared validator cache older than 7 days" in caplog.text
    assert "pruned validator cache versions: v8" in caplog.text


@pytest.mark.parametrize("disk_cache", [None, object()])
def test_cache_maintenance_tolerates_checkouts_without_helpers(monkeypatch, tmp_path, disk_cache):
    from md_mcp.validators import runner

    class Loader:
        def load(self, root, names):
            if disk_cache is None:
                raise ImportError("no disk_cache")
            return {"disk_cache": disk_cache}

    monkeypatch.setattr(runner, "_CACHE_MAINTENANCE", Loader())
    monkeypatch.setattr(runner, "_CACHE_MAINTAINED", set())
    runner._maintain_validator_cache(tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("age_days", [1, 8])
def test_cache_maintenance_with_real_disk_cache(real_mod_root, monkeypatch, tmp_path, age_days):
    import sys
    import time

    from md_mcp.util.upstream_modules import UpstreamModules
    from md_mcp.validators import runner

    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    mod_root = tmp_path / "mod"
    mod_root.mkdir()
    (mod_root / "tools").symlink_to(real_mod_root / "tools", target_is_directory=True)
    loader = UpstreamModules("validation", ("disk_cache", "shared_utils"))
    monkeypatch.setattr(runner, "_CACHE_MAINTENANCE", loader)
    monkeypatch.setattr(runner, "_CACHE_MAINTAINED", set())
    try:
        current = f"v{loader.load(mod_root, ('disk_cache',))['disk_cache'].CACHE_VERSION}"
        cache = mod_root / ".validation_cache"
        for version in ("v8", current):
            (cache / version).mkdir(parents=True)
            (cache / version / "cache.db").write_text("rows", encoding="utf-8")
        created = time.time() - age_days * 86400
        (cache / "created").write_text(str(created), encoding="utf-8")

        runner._maintain_validator_cache(mod_root)
        runner._maintain_validator_cache(mod_root)
    finally:
        loader.reset()

    assert not (cache / "v8").exists()
    stamp = float((cache / "created").read_text(encoding="utf-8"))
    if age_days > 7:
        assert not (cache / current).exists()
        assert stamp > created
    else:
        assert (cache / current / "cache.db").exists()
        assert stamp == created
