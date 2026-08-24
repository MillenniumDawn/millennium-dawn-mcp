"""Validator-wrapper tests.

The unit-test layer can't fully exercise the wrappers — they import Millennium-Dawn
validator modules from the real mod tree. Most assertions therefore live in the
integration suite (`@pytest.mark.integration`), gated on MD_MOD_ROOT.

Isolated-mode tests plant synthetic `validate_*.py` modules in the fake mod's
`tools/validation/`, so they exercise the real subprocess path without a checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from md_mcp.validators import SLOW_VALIDATORS, ValidatorRunner, available_validators

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
    _plant(fake_mod_root, "plain", _PLAIN)
    result = ValidatorRunner(fake_mod_root).run("plain", staged_only=True)
    assert result["issues"][1]["message"] == "staged=True"


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


def test_in_process_mode_still_available(fake_mod_root):
    _plant(fake_mod_root, "plain", _PLAIN)
    result = ValidatorRunner(fake_mod_root, mode="in_process").run("plain")
    assert result["ok"] is True
    assert len(result["issues"]) == 2


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
