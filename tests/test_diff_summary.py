"""Tests for `md_mcp.analysis.diff_summary`.

Each test runs against a real, freshly-initialized git repository in a temp
directory. We construct the history deterministically (one `git_commit`
fixture per step) so the diff between an arbitrary `base` revision and `HEAD`
has predictable paths and statuses. This exercises the real git subprocess
path — option parsing, rename handling, file-by-file ID diffing, and timeouts
— rather than just the public-function signature.
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import List

import pytest

from md_mcp.analysis import diff_summary as diff_summary_mod
from md_mcp.analysis.diff_summary import diff_summary

# --- helpers ---------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    """Run `git <args>` in `cwd`. Test-only helper; git is required."""
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_out(cwd: Path, *args: str) -> str:
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


def _commit(cwd: Path, message: str, author: str = "Test <test@example.com>") -> str:
    """Create an empty commit with the given message. Returns the commit SHA."""
    _git_out(
        cwd,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        message,
    )
    return _git_out(cwd, "rev-parse", "HEAD").strip()


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A real, committed-empty git repo rooted in `tmp_path`.

    The repo starts with a single empty commit on `main` (matches a typical
    freshly-cloned mod). Tests then mutate it freely.
    """
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _commit(tmp_path, "initial")
    return tmp_path


def _write(repo: Path, rel_path: str, body: str) -> None:
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(body), encoding="utf-8")


def _add_and_commit(repo: Path, *paths: str, message: str) -> str:
    _git(repo, "add", "--", *paths)
    return _commit(repo, message)


# A focus tree file that's small enough for the parser to chew on reliably.
FOCUS_FILE = "common/national_focus/TST_focus.txt"


# --- signature & non-repo guarantees --------------------------------------


def test_diff_summary_signature_supports_new_params():
    """Guard against accidental signature regression."""
    sig = inspect.signature(diff_summary)
    params = sig.parameters
    for required in ("kinds", "with_ids", "limit"):
        assert required in params, f"diff_summary missing param: {required}"
    assert params["with_ids"].default is True
    assert params["limit"].default == 200


def test_diff_summary_non_repo_returns_error(tmp_path: Path):
    out = diff_summary(tmp_path)
    assert out["ok"] is False
    assert "git diff failed" in out["error"]


def test_diff_summary_rejects_option_like_base(git_repo: Path):
    """A `base` starting with `-` must be refused before being passed to git."""
    out = diff_summary(git_repo, base="--upload-pack=evil")
    assert out["ok"] is False
    assert "invalid base revision" in out["error"]


def test_diff_summary_rejects_empty_base(git_repo: Path):
    out = diff_summary(git_repo, base="")
    assert out["ok"] is False
    assert "invalid base revision" in out["error"]


def test_diff_summary_rejects_base_with_shell_metachars(git_repo: Path):
    """Control characters in `base` are not allowed."""
    out = diff_summary(git_repo, base="main\nrm -rf /")
    assert out["ok"] is False


# --- core diff behaviour against a real repo ------------------------------


def test_diff_summary_added_file_in_branch(git_repo: Path):
    _write(git_repo, FOCUS_FILE, "")
    _add_and_commit(git_repo, FOCUS_FILE, message="add focus tree")

    out = diff_summary(git_repo, base="HEAD~1")
    assert out["ok"] is True
    assert out["total_files"] == 1
    rec = out["files"][0]
    assert rec["status"] == "A"
    assert rec["kind"] == "focus"
    assert rec["path"] == FOCUS_FILE


def test_diff_summary_modified_focus_ids(git_repo: Path):
    # Base: two focuses. Branch: add a third, remove none.
    base_body = (
        "focus_tree = {\n"
        "    id = test_tree\n"
        "    focus = {\n"
        "        id = TST_root\n"
        "        x = 0\n"
        "        y = 0\n"
        "    }\n"
        "    focus = {\n"
        "        id = TST_branch\n"
        "        x = 2\n"
        "        y = 0\n"
        "        prerequisite = { focus = TST_root }\n"
        "    }\n"
        "}\n"
    )
    _write(git_repo, FOCUS_FILE, base_body)
    _add_and_commit(git_repo, FOCUS_FILE, message="base focus tree")

    new_body = (
        "focus_tree = {\n"
        "    id = test_tree\n"
        "    focus = {\n"
        "        id = TST_root\n"
        "        x = 0\n"
        "        y = 0\n"
        "    }\n"
        "    focus = {\n"
        "        id = TST_branch\n"
        "        x = 2\n"
        "        y = 0\n"
        "        prerequisite = { focus = TST_root }\n"
        "    }\n"
        "    focus = {\n"
        "        id = TST_new_focus\n"
        "        x = 4\n"
        "        y = 0\n"
        "        prerequisite = { focus = TST_branch }\n"
        "    }\n"
        "}\n"
    )
    _write(git_repo, FOCUS_FILE, new_body)
    _add_and_commit(git_repo, FOCUS_FILE, message="add TST_new_focus")

    out = diff_summary(git_repo, base="HEAD~1")
    assert out["ok"] is True
    rec = next(r for r in out["files"] if r["path"] == FOCUS_FILE)
    assert rec["status"] == "M"
    assert "id_diff" not in rec or "error" not in rec["id_diff"], rec.get("id_diff")
    assert rec["added_ids"] == ["TST_new_focus"]
    assert "removed_ids" not in rec


def test_diff_summary_deleted_file(git_repo: Path):
    _write(git_repo, "events/Test_events.txt", "country_event = { id = TST_evt.1 }")
    _add_and_commit(git_repo, "events/Test_events.txt", message="add events")

    # Delete the file in a follow-up commit.
    (git_repo / "events" / "Test_events.txt").unlink()
    _git(git_repo, "add", "-A")
    _commit(git_repo, "delete events")

    out = diff_summary(git_repo, base="HEAD~1")
    assert out["ok"] is True
    rec = next(r for r in out["files"] if r["path"] == "events/Test_events.txt")
    assert rec["status"] == "D"
    assert rec["kind"] == "event"
    # Pure deletions skip the ID diff entirely.
    assert "added_ids" not in rec
    assert "removed_ids" not in rec


def test_diff_summary_rename_keeps_both_paths_and_compares_correctly(git_repo: Path):
    """Renames must surface old_path AND new_path, and ID diff must use the
    OLD path at base."""
    base_body = (
        "add_namespace = TST\ncountry_event = {\n    id = TST_evt.1\n    title = evt_one\n}\n"
    )
    _write(git_repo, "events/Original_events.txt", base_body)
    _add_and_commit(git_repo, "events/Original_events.txt", message="base events")

    # Rename via git, then add a new event in the renamed file.
    _git(
        git_repo,
        "mv",
        "events/Original_events.txt",
        "events/Renamed_events.txt",
    )
    body = (
        "add_namespace = TST\n"
        "country_event = {\n"
        "    id = TST_evt.1\n"
        "    title = evt_one\n"
        "}\n"
        "country_event = {\n"
        "    id = TST_evt.2\n"
        "    title = evt_two\n"
        "}\n"
    )
    (git_repo / "events" / "Renamed_events.txt").write_text(body, encoding="utf-8")
    _git(git_repo, "add", "events/Renamed_events.txt")
    _commit(git_repo, "rename + add evt_two")

    out = diff_summary(git_repo, base="HEAD~1")
    rec = next(r for r in out["files"] if r["status"] == "R")
    assert rec["path"] == "events/Renamed_events.txt"
    assert rec["old_path"] == "events/Original_events.txt"
    # The ID diff must read the OLD path at base — so the only NEW ID is
    # TST_evt.2. If the implementation incorrectly used the new path at base,
    # every ID in the file (evt.1, evt.2) would appear as `added_ids`.
    assert rec["added_ids"] == ["TST_evt.2"]
    assert "removed_ids" not in rec


def test_diff_summary_path_filter(git_repo: Path):
    _write(git_repo, FOCUS_FILE, "focus = { id = TST_root }")
    _write(git_repo, "events/Other_events.txt", "country_event = { id = TST_evt.1 }")
    _add_and_commit(
        git_repo,
        FOCUS_FILE,
        "events/Other_events.txt",
        message="add focus + event",
    )

    out = diff_summary(git_repo, base="HEAD~1", kinds=["focus"])
    paths = [r["path"] for r in out["files"]]
    assert paths == [FOCUS_FILE]
    assert out["counts_by_kind"] == {"focus": 1}


def test_diff_summary_pagination(git_repo: Path):
    # Add three event files in one commit so we have a multi-file diff.
    files = [
        "events/A_events.txt",
        "events/B_events.txt",
        "events/C_events.txt",
    ]
    for f in files:
        _write(git_repo, f, f"country_event = {{ id = TST_{f[7]}.evt }}")
    _add_and_commit(git_repo, *files, message="three events")

    out = diff_summary(git_repo, base="HEAD~1", limit=2)
    assert out["ok"] is True
    assert out["total_files"] == 3
    assert out["files_returned"] == 2
    assert out["truncated"] is True
    assert len(out["files"]) == 2


def test_diff_summary_filename_with_space(git_repo: Path):
    """`-z` parsing must handle paths containing spaces."""
    weird = "events/Has Space_in_name.txt"
    _write(git_repo, weird, "country_event = { id = TST_evt.1 }")
    _add_and_commit(git_repo, weird, message="add weird-name file")

    out = diff_summary(git_repo, base="HEAD~1")
    assert out["ok"] is True
    rec = next(r for r in out["files"] if r["path"] == weird)
    assert rec["status"] == "A"
    assert rec["kind"] == "event"


def test_diff_summary_with_ids_false_skips_id_diff(git_repo: Path):
    _write(git_repo, FOCUS_FILE, "focus = { id = TST_root }")
    _add_and_commit(git_repo, FOCUS_FILE, message="add focus")

    out = diff_summary(git_repo, base="HEAD~1", with_ids=False)
    assert out["ok"] is True
    assert out["with_ids"] is False
    rec = out["files"][0]
    assert "added_ids" not in rec
    assert "removed_ids" not in rec
    assert "id_diff" not in rec


def test_diff_summary_subprocess_timeouts_are_bounded(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """A hung git must not wedge the call. We set a tiny timeout on the
    wrapped `subprocess.run` and raise `TimeoutExpired` from a substitute,
    then verify the call surfaces a timeout error rather than hanging.

    We patch by binding a name to a substitute `run` *only* in the diff_summary
    module — done by temporarily replacing the module's `subprocess` attribute
    with a tiny stub. That way the test setup (which still uses the real
    `subprocess.run` via `_git`) keeps working.
    """
    import types

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        def run(self, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0] if args else "git", timeout=0.5)

    stub = types.SimpleNamespace(
        run=_StubSubprocess.run,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    # Build a non-empty diff first so the parser path actually exercises
    # both `git diff` and `git show`.
    _write(git_repo, FOCUS_FILE, "focus = { id = TST_root }")
    _add_and_commit(git_repo, FOCUS_FILE, message="add focus")

    out = diff_summary(git_repo, base="HEAD~1")
    assert out["ok"] is False
    # The first subprocess call is `git diff` (50% threshold); we only care
    # that the error message reports a timeout rather than hanging.
    assert "timed out" in out["error"]


def test__validate_rev_rejects_options():
    """Direct unit test for the option-injection guard."""
    from md_mcp.analysis.diff_summary import _GitRevError, _validate_rev

    for bad in ("", "--upload-pack=evil", "-c", "main\nrm", "a\x00b"):
        with pytest.raises(_GitRevError):
            _validate_rev("rev", bad)

    # Happy path.
    assert _validate_rev("rev", "HEAD~1") == "HEAD~1"
    assert _validate_rev("rev", "main") == "main"
    assert _validate_rev("rev", "feature/foo") == "feature/foo"


def test__parse_name_status_z_handles_renames_and_spaces():
    """Unit test for the `-z` parser, including spaces in paths."""
    from md_mcp.analysis.diff_summary import _parse_name_status_z

    # `git diff --name-status -z` output:
    #   non-rename entry: `STATUS\0PATH\0`
    #   rename entry:     `STATUS\0OLD\0NEW\0`
    raw = (
        "M\x00events/Plain.txt\x00"
        "A\x00path with space.txt\x00"
        "R\x00old name.txt\x00new name.txt\x00"
        "D\x00deleted.txt\x00"
    )
    records: List[dict] = _parse_name_status_z(raw)
    by_path = {(r["status"], r["new_path"]): r for r in records}
    assert ("M", "events/Plain.txt") in by_path
    assert ("A", "path with space.txt") in by_path
    rename = by_path[("R", "new name.txt")]
    assert rename["old_path"] == "old name.txt"
    assert ("D", "deleted.txt") in by_path
