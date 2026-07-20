"""Tests for the unified `lint` dispatcher + the secondary lint wrappers.

Strategy: write fake stand-in scripts at the expected paths and assert the
dispatcher routes correctly, parses each script's output flavour, aggregates
issues, and respects the `checks=[...]` / `severity_min=` / `limit=` /
`counts_only=` knobs.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from typing import Callable

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


def test_lint_runs_every_check_by_default(tmp_path):
    _seed_all_scripts(tmp_path, {})
    (tmp_path / "descriptor.mod").write_text('name = "x"\n')
    out = lint_tool(tmp_path, mode="all")
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
    out = lint_tool(tmp_path, checks=["common_mistakes", "loc_encoding"], mode="all")
    assert out["ok"] is True
    ran = {c["name"] for c in out["checks"]}
    assert ran == {"common_mistakes", "loc_encoding"}


def test_lint_aggregates_issues_from_multiple_checks(tmp_path):
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": """import sys
print("sub/a.txt:1: is_in_faction = TAG is not valid")
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
        files=["sub/a.txt", "descriptor.mod"],
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
print("sub/a.txt:1: foo")
print("sub/a.txt:2: bar")
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
        files=["sub/a.txt", "descriptor.mod"],
        severity_min="error",
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
print("sub/a.txt:1: foo")
sys.exit(1)
""",
        },
    )
    out = lint_tool(tmp_path, checks=["common_mistakes"], files=["sub/a.txt"], counts_only=True)
    assert out["ok"] is True
    assert "issues" not in out
    assert out["counts"]["warning"] == 1


def test_lint_limit_truncates(tmp_path):
    body = "\n".join(f'print("sub/a.txt:{i}: msg{i}")' for i in range(1, 21))
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_common_mistakes.py": f"import sys\n{body}\nsys.exit(1)\n",
        },
    )
    out = lint_tool(
        tmp_path,
        checks=["common_mistakes"],
        files=["sub/a.txt"],
        limit=5,
    )
    assert out["issues_total_after_filter"] == 20
    assert len(out["issues"]) == 5
    assert out["truncated"] is True


def test_lint_per_check_failure_isolated(tmp_path):
    """One missing script doesn't bring down the rest of the run.

    In `mode=all` both checks always invoke (script-side auto-discovery).
    Delete one script and confirm the other still completes ok.
    """
    _seed_all_scripts(tmp_path, {})
    (tmp_path / "tools" / "linting" / "check_common_mistakes.py").unlink()
    out = lint_tool(tmp_path, checks=["common_mistakes", "loc_encoding"], mode="all")
    assert out["ok"] is True  # overall pass
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["common_mistakes"]["ok"] is False
    assert by_name["loc_encoding"]["ok"] is True


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
    assert _changed_files(tmp_path) == []


def test_changed_files_handles_renames(tmp_path):
    _init_repo(tmp_path)
    _git(tmp_path, "mv", "baseline.txt", "renamed.txt")
    found = set(_changed_files(tmp_path))
    assert "renamed.txt" in found
    assert "baseline.txt" not in found


def test_changed_files_non_git_dir_returns_empty(tmp_path):
    assert _changed_files(tmp_path) == []


def test_staged_files_returns_index_only(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "staged.txt").write_text("x\n")
    _git(tmp_path, "add", "staged.txt")
    (tmp_path / "unstaged.txt").write_text("y\n")  # untracked, NOT staged
    assert _staged_files(tmp_path) == ["staged.txt"]


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
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.txt").write_text("focus = { }\n")

    out = lint_tool(tmp_path, checks=["common_mistakes"])  # no mode → default
    assert out["mode"] == "changed"
    files_in_issues = {i.get("file") for i in out["issues"]}
    assert "sub/new.txt" in files_in_issues


def test_lint_changed_mode_no_changes_is_clean_run(tmp_path):
    """When git is clean, all checks no-op (skipped) with overall ok."""
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    out = lint_tool(tmp_path)
    assert out["ok"] is True
    assert out["mode"] == "changed"
    # Every check reported, none with errors.
    assert out["counts"] == {"error": 0, "warning": 0, "info": 0}


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

    out = lint_tool(tmp_path, files=["localisation/english/x_l_english.yml"])
    me = next(c for c in out["checks"] if c["name"] == "mod_encoding")
    assert me["skipped"] == "no files in scope"
    assert me["total"] == 0
