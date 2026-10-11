"""Validator-wrapper tests.

The unit-test layer can't fully exercise the wrappers — they import Millennium-Dawn
validator modules from the real mod tree. Most assertions therefore live in the
integration suite (`@pytest.mark.integration`), gated on MD_MOD_ROOT.

Isolated-mode tests plant synthetic `validate_*.py` modules in the fake mod's
`tools/validation/`, so they exercise the real subprocess path without a checkout.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from md_mcp.validators import SLOW_VALIDATORS, ValidatorRunner, available_validators
from md_mcp.validators.runner import _collect

_ISSUE_CLASS = """
class _Issue:
    def __init__(self, **kw):
        self.kw = kw

    def to_dict(self):
        return dict(self.kw)
"""

_PLAIN = (
    _ISSUE_CLASS
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
    _ISSUE_CLASS
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
    _ISSUE_CLASS
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


def _plant(mod_root: Path, name: str, source: str) -> None:
    (mod_root / "tools" / "validation" / f"validate_{name}.py").write_text(source, encoding="utf-8")


# Emits the non-uniform `Issue.file` shapes the upstream suite produces.
_BASENAMES = (
    _ISSUE_CLASS
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


def test_files_filter_resolves_nonuniform_issue_paths(fake_mod_root):
    """A bare-basename issue must land in scope via attribution, not be dropped."""
    _plant(fake_mod_root, "basenames", _BASENAMES)
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
    _plant(fake_mod_root, "literal", source)
    (info,) = available_validators(fake_mod_root)
    assert info.title == expected
    assert info.title_source == "scraped"


def test_title_derived_for_computed_expression(fake_mod_root):
    _plant(fake_mod_root, "my_check", 'TITLE = "Prefix" + suffix\n')
    (info,) = available_validators(fake_mod_root)
    assert info.title == "My Check"
    assert info.title_source == "derived"


def test_title_derived_when_file_read_fails(fake_mod_root, monkeypatch):
    _plant(fake_mod_root, "unread", 'TITLE = "Fine"\n')

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


def test_isolated_returns_issues_and_swallows_stdout(fake_mod_root):
    _plant(fake_mod_root, "plain", _PLAIN)
    result = ValidatorRunner(fake_mod_root).run("plain")
    assert result["ok"] is True
    assert [i["message"] for i in result["issues"]] == ["m1", "staged=False"]
    assert result["issues"][0]["file"] == "events/a.txt"
    assert result["counts"] == {"error": 1, "warning": 1, "info": 0}


def test_isolated_forwards_staged_only(fake_mod_root):
    import subprocess

    _plant(fake_mod_root, "plain", _PLAIN)
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


@pytest.mark.parametrize("mode", ["isolated", "in_process"])
def test_staged_validator_reports_git_failure(fake_mod_root, mode):
    _plant(fake_mod_root, "plain", _PLAIN)
    (fake_mod_root / ".git").mkdir()
    (fake_mod_root / ".git" / "index").write_bytes(b"broken")
    result = ValidatorRunner(fake_mod_root, mode=mode).run("plain", staged_only=True)
    assert result["ok"] is False
    assert "Could not read staged paths" in result["error"]


def test_isolated_survives_a_forking_validator(fake_mod_root):
    # The whole point of isolated mode: the fork happens in a child with no
    # asyncio loop and no inherited stdio, so it can't deadlock the server.
    _plant(fake_mod_root, "forking", _FORKING)
    result = ValidatorRunner(fake_mod_root).run("forking")
    assert result["ok"] is True
    assert result["issues"][0]["message"] == "sum=380"


def test_isolated_keeps_issues_when_validator_exits(fake_mod_root):
    _plant(fake_mod_root, "exits", _EXITS)
    result = ValidatorRunner(fake_mod_root).run("exits")
    assert result["ok"] is True
    assert [i["message"] for i in result["issues"]] == ["found before exit"]


def test_isolated_reports_broken_validator_instead_of_zero_issues(fake_mod_root):
    # Regression: the old subprocess mode ignored the exit code and treated a
    # missing sidecar as a clean run, so a crashing validator reported ok/0.
    _plant(fake_mod_root, "broken", "this is not valid python (\n")
    result = ValidatorRunner(fake_mod_root).run("broken")
    assert result["ok"] is False
    assert "SyntaxError" in result["error"]


def test_isolated_reports_missing_validator_class(fake_mod_root):
    _plant(fake_mod_root, "classless", "X = 1\n")
    result = ValidatorRunner(fake_mod_root).run("classless")
    assert result["ok"] is False
    assert "Validator" in result["error"]


def test_subprocess_mode_is_an_alias_for_isolated(fake_mod_root):
    _plant(fake_mod_root, "plain", _PLAIN)
    result = ValidatorRunner(fake_mod_root, mode="subprocess").run("plain")
    assert result["ok"] is True
    assert len(result["issues"]) == 2


@pytest.mark.parametrize("mode", ["isolated", "in_process"])
def test_validator_args_reach_instance(fake_mod_root, mode):
    source = (
        _ISSUE_CLASS
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
    _plant(fake_mod_root, "arg_aware", source)

    result = ValidatorRunner(fake_mod_root, mode=mode).run(
        "arg_aware", args=["--enable-extra-check"]
    )

    assert result["ok"] is True
    assert result["issues"][0]["message"] == "True"


def test_in_process_mode_still_available(fake_mod_root):
    _plant(fake_mod_root, "plain", _PLAIN)
    result = ValidatorRunner(fake_mod_root, mode="in_process").run("plain")
    assert result["ok"] is True
    assert len(result["issues"]) == 2


# In-process imports stay in sys.modules for the session, so these use names no
# other test plants.
def test_in_process_keeps_issues_when_validator_exits(fake_mod_root):
    _plant(fake_mod_root, "exits_inproc", _EXITS)
    result = ValidatorRunner(fake_mod_root, mode="in_process").run("exits_inproc")
    assert result["ok"] is True
    assert [i["message"] for i in result["issues"]] == ["found before exit"]


def test_in_process_reports_broken_validator(fake_mod_root):
    _plant(fake_mod_root, "broken_inproc", "this is not valid python (\n")
    result = ValidatorRunner(fake_mod_root, mode="in_process").run("broken_inproc")
    assert result["ok"] is False
    assert result["validator"] == "broken_inproc"
    assert result["error"].startswith("SyntaxError: ")


def test_in_process_reports_missing_validator_class(fake_mod_root):
    _plant(fake_mod_root, "classless_inproc", "X = 1\n")
    result = ValidatorRunner(fake_mod_root, mode="in_process").run("classless_inproc")
    assert result["ok"] is False
    assert "Validator" in result["error"]


def test_collect_raises_where_run_returns_an_error(fake_mod_root):
    # The docs send people to _collect for a traceback because run() catches.
    source = _PLAIN.replace(
        'print("stdout chatter the runner must swallow")',
        'raise RuntimeError("boom from validator")',
    )
    _plant(fake_mod_root, "raises_inproc", source)

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
    _plant(fake_mod_root, "raises_with_stderr_inproc", source)

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
    from types import SimpleNamespace

    from md_mcp.validators import runner

    monkeypatch.setattr(
        runner.subprocess,
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

    _plant(fake_mod_root, "plain", _PLAIN)
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

    _plant(fake_mod_root, "plain", _PLAIN)
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
    from types import SimpleNamespace

    from md_mcp.validators import runner

    monkeypatch.setattr(
        runner.subprocess,
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
