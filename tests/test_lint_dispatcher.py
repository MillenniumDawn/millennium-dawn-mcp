"""Tests for the unified `lint` dispatcher + the secondary lint wrappers.

Strategy: write fake stand-in scripts at the expected paths and assert the
dispatcher routes correctly, parses each script's output flavour, aggregates
issues, and respects the `checks=[...]` / `severity_min=` / `limit=` /
`counts_only=` knobs.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from md_mcp.tools import linting_tools
from md_mcp.tools.linting_tools import (
    _ALL_CHECKS,
    _changed_files,
    _staged_files,
    lint_loc_encoding_tool,
    lint_tool,
)


def _make_script(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _seed_all_scripts(root: Path, body_map: dict[str, str]) -> None:
    """Create stub scripts for every linter, defaulting to clean (exit 0, no output)."""
    defaults = {
        "tools/linting/check_common_mistakes.py": "import sys\nsys.exit(0)\n",
        "tools/linting/validate_mod_encoding.py": "import sys\nsys.exit(0)\n",
        "tools/linting/validate_localization_encoding.py": "import sys\nsys.exit(0)\n",
    }
    defaults.update(body_map)
    for rel, body in defaults.items():
        _make_script(root, rel, body)


# ---------------------------------------------------------------------------
# Secondary wrapper signatures + happy paths
# ---------------------------------------------------------------------------


def test_secondary_wrapper_signatures():
    cases: list[tuple[Callable, tuple[str, ...]]] = [
        (lint_loc_encoding_tool, ("mod_root", "files", "limit")),
    ]
    for fn, required in cases:
        params = inspect.signature(fn).parameters
        for p in required:
            assert p in params, f"{fn.__name__} missing param: {p}"


def test_loc_encoding_parses_missing_bom(tmp_path):
    _make_script(
        tmp_path,
        "tools/linting/validate_localization_encoding.py",
        """import sys
print("localisation/english/foo_l_english.yml: Missing UTF-8 BOM (required for HOI4 localization)")
sys.exit(1)
""",
    )
    out = lint_loc_encoding_tool(tmp_path)
    assert out["ok"] is True
    assert out["total"] == 1
    assert "Missing UTF-8 BOM" in out["issues"][0]["message"]
    assert out["issues"][0]["severity"] == "error"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_lint_runs_every_script_check_when_validators_disabled(tmp_path):
    _seed_all_scripts(tmp_path, {})
    (tmp_path / "descriptor.mod").write_text('name = "x"\n')
    out = lint_tool(tmp_path, mode="all", validators=[])
    assert out["ok"] is True
    # Every check ran with ok=true, even when there's no work.
    names = {c["name"] for c in out["checks"]}
    assert names == set(_ALL_CHECKS)
    assert out["counts"] == {"error": 0, "warning": 0, "info": 0}


def test_lint_rejects_unknown_check(tmp_path):
    _seed_all_scripts(tmp_path, {})
    out = lint_tool(tmp_path, checks=["common_mistakes", "bogus"])
    assert out["ok"] is False
    assert "Unknown check(s)" in out["error"]


def test_lint_subset_only_runs_requested(tmp_path):
    _seed_all_scripts(tmp_path, {})
    out = lint_tool(tmp_path, checks=["common_mistakes", "loc_encoding"], mode="all", validators=[])
    assert out["ok"] is True
    ran = {c["name"] for c in out["checks"]}
    assert ran == {"common_mistakes", "loc_encoding"}


def test_lint_aggregates_issues_from_multiple_checks(tmp_path):
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": """import sys
print("common/a.txt:1: is_in_faction = TAG is not valid")
sys.exit(1)
""",
            "tools/linting/validate_mod_encoding.py": """import sys
print("descriptor.mod: Invalid UTF-8 encoding - byte 0x80 at position 3", file=sys.stderr)
sys.exit(1)
""",
        },
    )
    out = lint_tool(
        tmp_path,
        checks=["common_mistakes", "mod_encoding"],
        files=["common/a.txt", "descriptor.mod"],
        validators=[],
    )
    assert out["ok"] is True
    assert out["counts"]["error"] == 1
    assert out["counts"]["warning"] == 1
    # Each issue is tagged with which check produced it.
    by_check = {i["check"] for i in out["issues"]}
    assert by_check == {"common_mistakes", "mod_encoding"}


def test_lint_severity_floor_filters_warnings(tmp_path):
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": """import sys
print("common/a.txt:1: foo")
print("common/a.txt:2: bar")
sys.exit(1)
""",
            "tools/linting/validate_mod_encoding.py": """import sys
print("descriptor.mod: Invalid UTF-8 encoding - byte 0x80 at position 3", file=sys.stderr)
sys.exit(1)
""",
        },
    )
    out = lint_tool(
        tmp_path,
        checks=["common_mistakes", "mod_encoding"],
        files=["common/a.txt", "descriptor.mod"],
        severity_min="error",
        validators=[],
    )
    # Counts are pre-filter, issues are post-filter.
    assert out["counts"]["warning"] == 2
    assert out["counts"]["error"] == 1
    assert out["issues_total_after_filter"] == 1
    assert out["issues"][0]["severity"] == "error"


def test_lint_counts_only_omits_issues(tmp_path):
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": """import sys
print("common/a.txt:1: foo")
sys.exit(1)
""",
        },
    )
    out = lint_tool(
        tmp_path,
        checks=["common_mistakes"],
        files=["common/a.txt"],
        counts_only=True,
        validators=[],
    )
    assert out["ok"] is True
    assert "issues" not in out
    assert out["counts"]["warning"] == 1


def test_lint_common_mistakes_reports_total_per_check(tmp_path):
    # lint_common_mistakes_tool returns its count as `count`; the dispatcher's
    # per-check summary reads `total`. Regression: total used to stay 0 even
    # when the check produced issues.
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": """import sys
print("common/a.txt:1: foo")
sys.exit(1)
""",
        },
    )
    out = lint_tool(
        tmp_path,
        checks=["common_mistakes"],
        files=["common/a.txt"],
        validators=[],
    )
    cm = next(c for c in out["checks"] if c["name"] == "common_mistakes")
    assert cm["ok"] is True
    assert cm["total"] == 1


def test_lint_limit_truncates(tmp_path):
    body = "\n".join(f'print("common/a.txt:{i}: msg{i}")' for i in range(1, 21))
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": f"import sys\n{body}\nsys.exit(1)\n",
        },
    )
    out = lint_tool(
        tmp_path,
        checks=["common_mistakes"],
        files=["common/a.txt"],
        limit=5,
        validators=[],
    )
    assert out["issues_total_after_filter"] == 20
    assert len(out["issues"]) == 5
    assert out["truncated"] is True


def test_lint_preserves_complete_counts_when_check_wrapper_caps_diagnostics(tmp_path):
    count = 225
    lines = "\n".join(
        f"print('localisation/english/file_{i}_l_english.yml: Missing UTF-8 BOM')"
        for i in range(count)
    )
    _make_script(
        tmp_path,
        "tools/linting/validate_localization_encoding.py",
        lines + "\n",
    )

    out = lint_tool(
        tmp_path,
        mode="all",
        checks=["loc_encoding"],
        validators=[],
        limit=500,
    )

    assert out["counts"]["error"] == count
    assert out["issues_total_after_filter"] == count
    assert len(out["issues"]) == 200
    assert out["truncated"] is True


def test_lint_per_check_failure_isolated(tmp_path):
    """One missing script doesn't bring down the rest of the run.

    In `mode=all` both checks always invoke (script-side auto-discovery).
    Delete one script and confirm the other still completes ok.
    """
    _seed_all_scripts(tmp_path, {})
    (tmp_path / "tools" / "linting" / "check_common_mistakes.py").unlink()
    out = lint_tool(tmp_path, checks=["common_mistakes", "loc_encoding"], mode="all", validators=[])
    assert out["ok"] is False
    assert out["failed_checks"] == ["common_mistakes"]
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["common_mistakes"]["ok"] is False
    assert by_name["loc_encoding"]["ok"] is True


@pytest.mark.parametrize(
    "check,script,files",
    [
        (
            "common_mistakes",
            "tools/linting/check_common_mistakes.py",
            ["common/x.txt"],
        ),
        (
            "mod_encoding",
            "tools/linting/validate_mod_encoding.py",
            ["descriptor.mod"],
        ),
        (
            "loc_encoding",
            "tools/linting/validate_localization_encoding.py",
            ["localisation/english/x_l_english.yml"],
        ),
    ],
)
def test_lint_script_crash_without_recognized_output_fails(tmp_path, check, script, files):
    _seed_all_scripts(tmp_path, {script: "raise RuntimeError('synthetic crash')\n"})

    out = lint_tool(tmp_path, checks=[check], files=files, validators=[])

    assert out["ok"] is False
    assert out["failed_checks"] == [check]
    check_result = out["checks"][0]
    assert check_result["ok"] is False
    assert "crashed mid-run" in check_result["error"]
    assert "synthetic crash" in check_result["stderr_tail"]


@pytest.mark.parametrize(
    "check,script,files,diagnostic",
    [
        (
            "common_mistakes",
            "tools/linting/check_common_mistakes.py",
            ["common/x.txt"],
            'print("common/x.txt:1: parsed issue")',
        ),
        (
            "mod_encoding",
            "tools/linting/validate_mod_encoding.py",
            ["descriptor.mod"],
            'print("descriptor.mod: Invalid UTF-8 encoding - bad byte")',
        ),
        (
            "loc_encoding",
            "tools/linting/validate_localization_encoding.py",
            ["localisation/english/x_l_english.yml"],
            'print("localisation/english/x_l_english.yml: Missing UTF-8 BOM")',
        ),
    ],
)
def test_lint_unexpected_exit_code_fails_even_with_parsed_issue(
    tmp_path, check, script, files, diagnostic
):
    _seed_all_scripts(tmp_path, {script: f"import sys\n{diagnostic}\nsys.exit(2)\n"})

    out = lint_tool(tmp_path, checks=[check], files=files, validators=[])

    assert out["ok"] is False
    assert out["failed_checks"] == [check]
    assert "unexpected code 2" in out["checks"][0]["error"]


def test_lint_negative_exit_code_is_a_failure(tmp_path, monkeypatch):
    _seed_all_scripts(tmp_path, {})

    def killed(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            -9,
            stdout="common/x.txt:1: parsed issue\n",
            stderr="terminated\n",
        )

    monkeypatch.setattr("md_mcp.tools.linting_tools.run_in_group", killed)
    out = lint_tool(
        tmp_path,
        checks=["common_mistakes"],
        files=["common/x.txt"],
        validators=[],
    )

    assert out["ok"] is False
    assert out["failed_checks"] == ["common_mistakes"]
    assert "unexpected code -9" in out["checks"][0]["error"]


@pytest.mark.parametrize(
    "check,script,files,diagnostic",
    [
        (
            "common_mistakes",
            "tools/linting/check_common_mistakes.py",
            ["common/x.txt"],
            'print("common/x.txt:1: parsed issue")',
        ),
        (
            "mod_encoding",
            "tools/linting/validate_mod_encoding.py",
            ["descriptor.mod"],
            'print("descriptor.mod: Invalid UTF-8 encoding - bad byte")',
        ),
        (
            "loc_encoding",
            "tools/linting/validate_localization_encoding.py",
            ["localisation/english/x_l_english.yml"],
            'print("localisation/english/x_l_english.yml: Missing UTF-8 BOM")',
        ),
    ],
)
def test_lint_traceback_with_parsed_issue_is_a_failure(tmp_path, check, script, files, diagnostic):
    """Regression: exit 1 + a parseable issue used to read as success even when
    the script crashed mid-scan and the issues were partial."""
    _seed_all_scripts(tmp_path, {script: f"import sys\n{diagnostic}\nraise RuntimeError('boom')\n"})

    out = lint_tool(tmp_path, checks=[check], files=files, validators=[])

    assert out["ok"] is False
    assert out["failed_checks"] == [check]
    check_result = out["checks"][0]
    assert check_result["ok"] is False
    assert "crashed mid-run" in check_result["error"]
    assert "boom" in check_result["stderr_tail"]
    assert not any(i["check"] == check for i in out.get("issues", []))


def test_lint_traceback_exit_0_is_a_failure(tmp_path, monkeypatch):
    """A script that prints a traceback but exits 0 still aborted its scan."""
    _seed_all_scripts(tmp_path, {})

    def traced(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="common/x.txt:1: parsed issue\n",
            stderr='Traceback (most recent call last):\n  File "x", line 1\nRuntimeError: boom\n',
        )

    monkeypatch.setattr("md_mcp.tools.linting_tools.run_in_group", traced)
    out = lint_tool(
        tmp_path,
        checks=["common_mistakes"],
        files=["common/x.txt"],
        validators=[],
    )

    assert out["ok"] is False
    assert out["failed_checks"] == ["common_mistakes"]
    assert "crashed mid-run" in out["checks"][0]["error"]


# ---------------------------------------------------------------------------
# mode="changed" (the new default): staged + unstaged + untracked
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout


def _init_repo(repo: Path) -> None:
    """Init a quiet test repo with a baseline commit so subsequent diffs are meaningful."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "baseline.txt").write_text("baseline\n")
    _git(repo, "add", "baseline.txt")
    _git(repo, "commit", "-qm", "baseline")


def test_changed_files_picks_up_staged_unstaged_and_untracked(tmp_path):
    _init_repo(tmp_path)

    # Modify the tracked file and stage it.
    (tmp_path / "baseline.txt").write_text("staged change\n")
    _git(tmp_path, "add", "baseline.txt")

    # Edit it again — that delta is now unstaged.
    (tmp_path / "baseline.txt").write_text("staged + unstaged change\n")

    # Add a brand-new untracked file.
    (tmp_path / "new.txt").write_text("hi\n")

    found = set(_changed_files(tmp_path))
    assert found == {"baseline.txt", "new.txt"}


def test_changed_files_skips_deletions(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "baseline.txt").unlink()
    removed: list[str] = []
    assert _changed_files(tmp_path, removed=removed) == []
    assert removed == ["baseline.txt"]


def test_changed_files_handles_renames(tmp_path):
    _init_repo(tmp_path)
    _git(tmp_path, "mv", "baseline.txt", "renamed.txt")
    removed: list[str] = []
    found = set(_changed_files(tmp_path, removed=removed))
    assert "renamed.txt" in found
    assert "baseline.txt" not in found
    assert removed == ["baseline.txt"]


@pytest.mark.parametrize(
    ("discover", "mode"),
    [(_changed_files, "changed"), (_staged_files, "staged")],
)
def test_git_scope_discovery_non_repo_reports_error(tmp_path, discover, mode):
    with pytest.raises(linting_tools.GitScopeError) as exc_info:
        discover(tmp_path)

    assert exc_info.value.as_dict()["mode"] == mode


def test_changed_files_non_ascii_paths_arrive_verbatim(tmp_path):
    """`-z` output is unquoted; without it git C-quotes `café.txt` into escapes."""
    _init_repo(tmp_path)
    (tmp_path / "café.txt").write_text("x\n", encoding="utf-8")
    assert "café.txt" in _changed_files(tmp_path)


def test_staged_files_returns_index_only(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "staged.txt").write_text("x\n")
    _git(tmp_path, "add", "staged.txt")
    (tmp_path / "unstaged.txt").write_text("y\n")  # untracked, NOT staged
    assert _staged_files(tmp_path) == ["staged.txt"]


def test_staged_files_lists_both_sides_of_renames(tmp_path):
    _init_repo(tmp_path)
    _git(tmp_path, "mv", "baseline.txt", "renamed.txt")
    assert set(_staged_files(tmp_path)) == {"baseline.txt", "renamed.txt"}


def test_staged_files_splits_nul_delimited_output(tmp_path, monkeypatch):
    commands: list[list[str]] = []

    def nul_delimited_git(command, **kwargs):
        commands.append(command)
        stdout = "events/café.txt\0events/a\nb.txt\0\0".encode()
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(linting_tools.subprocess, "run", nul_delimited_git)

    assert _staged_files(tmp_path) == ["events/café.txt", "events/a\nb.txt"]
    assert "-z" in commands[0]
    assert "--no-renames" in commands[0]


def test_staged_files_returns_unquoted_special_paths(tmp_path):
    """Default `core.quotePath` C-quotes non-ASCII and control characters without `-z`."""
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    names = ["events/café.txt", "events/with space.txt", "events/plain.txt"]
    if sys.platform != "win32":
        # Windows rejects control characters in filenames.
        names += ["events/with\ttab.txt", "events/with\nnewline.txt"]
    (tmp_path / "events").mkdir()
    for name in names:
        (tmp_path / name).write_text("x\n", encoding="utf-8")
        _git(tmp_path, "add", "--", name)

    staged = _staged_files(tmp_path)

    assert sorted(staged) == sorted(names)
    assert not any('"' in path or "\\" in path for path in staged)


@pytest.mark.parametrize("discover", [_changed_files, _staged_files])
def test_git_scope_paths_decode_as_utf8_under_legacy_locale(tmp_path, monkeypatch, discover):
    """Windows before Python 3.15 decodes text-mode pipes with a legacy code page."""
    _init_repo(tmp_path)
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "café.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "events/café.txt")
    real_run = subprocess.run

    def cp1252_locale_run(command, **kwargs):
        if kwargs.get("text"):
            kwargs.setdefault("encoding", "cp1252")
        return real_run(command, **kwargs)

    monkeypatch.setattr(linting_tools.subprocess, "run", cp1252_locale_run)

    assert discover(tmp_path) == ["events/café.txt"]


def test_lint_default_mode_is_changed(tmp_path):
    """No `mode` arg → uses `changed`, which surfaces unstaged + untracked."""
    _init_repo(tmp_path)
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": """import sys
# Echo each arg back as a parseable issue line so the dispatcher captures it.
for f in sys.argv[1:]:
    if f.startswith("--"):
        continue
    print(f"{f}:1: saw arg")
sys.exit(0)
""",
        },
    )
    # Untracked .txt — should be picked up by `changed`.
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "new.txt").write_text("focus = { }\n")

    out = lint_tool(tmp_path, checks=["common_mistakes"], validators=[])  # no mode → default
    assert out["mode"] == "changed"
    files_in_issues = {i.get("file") for i in out["issues"]}
    assert "common/new.txt" in files_in_issues


@pytest.mark.parametrize("mode", ["changed", "staged"])
def test_lint_clean_git_scope_is_clean_run(tmp_path, mode):
    """A successful empty Git query is a valid no-op for both scoped modes."""
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    # Commit the stubs so the tree is genuinely clean.
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "stubs")
    out = lint_tool(tmp_path, mode=mode, validators=[])
    assert out["ok"] is True
    assert out["mode"] == mode
    assert out["counts"] == {"error": 0, "warning": 0, "info": 0}
    # Genuinely skipped, not "ran against whatever happened to be staged".
    assert all(c.get("skipped") == "no files in scope" for c in out["checks"])


@pytest.mark.parametrize("mode", ["changed", "staged"])
@pytest.mark.parametrize("failure", ["nonzero", "missing", "timeout"])
def test_lint_git_scope_failures_return_structured_error(tmp_path, monkeypatch, mode, failure):
    def failed_git(command, **kwargs):
        if failure == "nonzero":
            return subprocess.CompletedProcess(
                command,
                128,
                stdout="",
                stderr=("x" * 2_048) + ("é" * 600) + "fatal: café XY",
            )
        if failure == "missing":
            raise FileNotFoundError("git executable unavailable")
        timeout_stderr = ("é" * 600 + "fatal: café XY").encode("utf-8")
        raise subprocess.TimeoutExpired(command, timeout=15, stderr=timeout_stderr)

    monkeypatch.setattr(linting_tools.subprocess, "run", failed_git)

    out = lint_tool(tmp_path, mode=mode, checks=["common_mistakes"], validators=[])

    assert out["ok"] is False
    assert "Git" in out["error"]
    scope_error = out["scope_error"]
    assert scope_error["mode"] == mode
    assert scope_error["command"][0] == "git"
    if failure == "nonzero":
        assert scope_error["exit_code"] == 128
        assert scope_error["stderr_tail"].endswith("fatal: café XY")
        assert not scope_error["stderr_tail"].startswith("\ufffd")
        assert len(scope_error["stderr_tail"].encode("utf-8")) <= 1_000
    elif failure == "timeout":
        assert scope_error["exit_code"] is None
        assert scope_error["stderr_tail"].endswith("fatal: café XY")
        assert not scope_error["stderr_tail"].startswith("\ufffd")
        assert len(scope_error["stderr_tail"].encode("utf-8")) <= 1_000
    else:
        assert scope_error["exit_code"] is None
        assert scope_error["reason"]
    assert "no files in scope" not in repr(out)


def test_git_scope_stderr_tail_keeps_internal_invalid_bytes_visible():
    invalid_utf8 = b"fatal: " + b"\xff" + "café".encode("utf-8")
    tail = linting_tools._scope_text(invalid_utf8, 1_000, tail=True)
    leading_invalid = linting_tools._scope_text(b"\x80oops", 1_000, tail=True)

    assert tail == "fatal: \ufffdcafé"
    assert leading_invalid == "\ufffdoops"


@pytest.mark.parametrize("mode", ["changed", "staged"])
def test_lint_git_scope_undecodable_output_returns_structured_error(tmp_path, monkeypatch, mode):
    def latin1_git(command, **kwargs):
        stdout = b"A  events/caf\xe9.txt\0"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(linting_tools.subprocess, "run", latin1_git)

    out = lint_tool(tmp_path, mode=mode, checks=["common_mistakes"], validators=[])

    assert out["ok"] is False
    assert out["scope_error"]["mode"] == mode
    assert out["scope_error"]["exit_code"] == 0
    assert "UTF-8" in out["scope_error"]["reason"]


@pytest.mark.parametrize("mode", ["changed", "staged"])
@pytest.mark.parametrize("use_overlay", [False, True])
def test_git_scope_discovery_uses_selected_worktree_cwd(tmp_path, monkeypatch, mode, use_overlay):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    expected_cwd = overlay if use_overlay else tmp_path

    def successful_git(command, **kwargs):
        assert kwargs["cwd"] == str(expected_cwd)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(linting_tools.subprocess, "run", successful_git)
    discover = _changed_files if mode == "changed" else _staged_files

    assert discover(tmp_path, overlay if use_overlay else None) == []


def test_lint_staged_mode_passes_non_ascii_path_to_checks(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "café.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "events/café.txt")
    received: list[list[str]] = []

    def record_files(mod_root, *, files, **kwargs):
        received.append(files)
        return {"ok": True, "total": 0, "issues": [], "exit_code": 0}

    monkeypatch.setattr(linting_tools, "lint_common_mistakes_tool", record_files)

    out = lint_tool(tmp_path, mode="staged", checks=["common_mistakes"], validators=[])

    assert out["ok"] is True
    assert received == [["events/café.txt"]]


def test_lint_staged_mode_runs_default_validator_for_non_ascii_path(tmp_path):
    from .test_lint_validators import FakeRunner

    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "café.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "events/café.txt")
    runner = FakeRunner(names=["style"])

    out = lint_tool(tmp_path, mode="staged", checks=["mod_encoding"], validator_runner=runner)

    assert out["validators_run"] == ["style"]
    assert [Path(f).as_posix() for f in runner.scope_calls[0]["files"]] == ["events/café.txt"]


@pytest.mark.parametrize(
    "scope_kwargs",
    [
        pytest.param({"files": ["common/a.txt"]}, id="explicit-files"),
        pytest.param({"mode": "all"}, id="all-files"),
    ],
)
def test_lint_explicit_and_all_modes_skip_git_discovery(tmp_path, monkeypatch, scope_kwargs):
    _seed_all_scripts(tmp_path, {})

    def unexpected_discovery(*args, **kwargs):
        pytest.fail("explicit and all-file scopes must not invoke Git discovery")

    monkeypatch.setattr(linting_tools, "_changed_files", unexpected_discovery)
    monkeypatch.setattr(linting_tools, "_staged_files", unexpected_discovery)

    out = lint_tool(tmp_path, checks=["common_mistakes"], validators=[], **scope_kwargs)

    assert out["ok"] is True


def test_lint_empty_files_scope_skips_every_check(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    out = lint_tool(tmp_path, files=[], validators=[])
    assert out["ok"] is True
    assert out["mode"] == "files"
    assert all(c.get("skipped") == "no files in scope" for c in out["checks"])


@pytest.mark.parametrize("absolute", [False, True])
def test_lint_files_scope_normalises_paths(tmp_path, absolute):
    """Relative and contained absolute paths reach checkers in mod-relative form."""
    _init_repo(tmp_path)
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": """import sys
for f in sys.argv[1:]:
    if not f.startswith("--"):
        print(f"{f}:1: saw arg")
sys.exit(0)
""",
        },
    )
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "new.txt").write_text("x = 1\n")

    file = str(tmp_path / "common" / "new.txt") if absolute else "./common/new.txt"
    out = lint_tool(tmp_path, checks=["common_mistakes"], validators=[], files=[file])
    assert out["mode"] == "files"
    assert {i.get("file") for i in out["issues"]} == {"common/new.txt"}


def test_lint_rejects_absolute_path_outside_mod_root(tmp_path):
    out = lint_tool(tmp_path, files=[str(tmp_path.parent / "outside.txt")], validators=[])

    assert out["ok"] is False
    assert "outside the mod root" in out["error"]


def test_lint_rejects_absolute_symlink_escape(tmp_path):
    source = tmp_path / "common" / "escape.txt"
    source.parent.mkdir()
    source.symlink_to(tmp_path.parent / "outside.txt")

    out = lint_tool(tmp_path, files=[str(source)], validators=[])

    assert out["ok"] is False
    assert "outside the mod root" in out["error"]


def test_lint_common_mistakes_only_gets_script_txt_files(tmp_path):
    """Images, .gfx, loc, and .txt outside the script folders never reach the checker."""
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": """import sys
for f in sys.argv[1:]:
    if not f.startswith("--"):
        print(f"{f}:1: saw arg")
sys.exit(0)
""",
        },
    )

    out = lint_tool(
        tmp_path,
        checks=["common_mistakes"],
        validators=[],
        files=[
            "common/a.txt",
            "events/b.txt",
            "interface/goals_shine.gfx",
            "gfx/flags/ISR.tga",
            "map/buildings.txt",
            "localisation/english/x_l_english.yml",
        ],
    )
    assert {i.get("file") for i in out["issues"]} == {"common/a.txt", "events/b.txt"}


def test_lint_non_script_scope_skips_common_mistakes(tmp_path):
    _seed_all_scripts(tmp_path, {})
    out = lint_tool(
        tmp_path,
        checks=["common_mistakes"],
        validators=[],
        files=["interface/goals_shine.gfx"],
    )
    assert out["checks"][0]["skipped"] == "no files in scope"


def test_lint_invalid_mode_rejected(tmp_path):
    out = lint_tool(tmp_path, mode="bogus")
    assert out["ok"] is False
    assert "Invalid mode" in out["error"]


def test_lint_skips_mod_encoding_without_mod_files(tmp_path):
    """No .mod file in scope -> mod_encoding is skipped rather than auto-discovering."""
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    (tmp_path / "descriptor.mod").write_text('name = "x"\n')
    loc = tmp_path / "localisation" / "english" / "x_l_english.yml"
    loc.parent.mkdir(parents=True)
    loc.write_text('l_english:\n x:0 "y"\n')

    out = lint_tool(tmp_path, files=["localisation/english/x_l_english.yml"], validators=[])
    me = next(c for c in out["checks"] if c["name"] == "mod_encoding")
    assert me["skipped"] == "no files in scope"
    assert me["total"] == 0


def test_lint_all_mode_skips_mod_encoding_without_mod_files(tmp_path):
    """mode=all on a tree with no .mod files -> mod_encoding is skipped, not failed."""
    _seed_all_scripts(tmp_path, {})

    out = lint_tool(tmp_path, mode="all", validators=[], checks=["mod_encoding"])
    assert out["ok"] is True
    assert out["failed_checks"] == []
    me = next(c for c in out["checks"] if c["name"] == "mod_encoding")
    assert me["ok"] is True
    assert me["skipped"] == "no .mod files found"
    assert me["total"] == 0
