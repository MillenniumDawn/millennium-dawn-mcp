"""Tests for `md_mcp.analysis.diff_summary`.

Each test runs against a real, freshly-initialized git repository in a temp
directory. The history is constructed deterministically (the `git_repo`
fixture plus `_add_and_commit`) so the diff between any `base` revision and `HEAD`
has predictable paths and statuses. This exercises the real git subprocess
path — option parsing, rename handling, file-by-file ID diffing, parser
errors, and timeouts — rather than just the public-function signature.

The test coverage here targets the behaviours listed in issue #32:
  * commits, additions, deletions, renames, invalid revisions, path filtering,
    pagination, filenames with spaces.
  * surface (rather than collapse) `git diff` / `git show` / parse failures.
  * bounded git subprocesses.
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
import textwrap
import types
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


def _commit(cwd: Path, message: str) -> str:
    """Create a commit with the given message. Returns the commit SHA."""
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
    """A real, committed-empty git repo rooted in `tmp_path`."""
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


# A focus tree file path used throughout the suite.
FOCUS_FILE = "common/national_focus/TST_focus.txt"


def _focus_body(*ids: str) -> str:
    """Build a valid focus_tree string containing one focus block per id."""
    blocks = "    focus_tree = {\n        id = test_tree\n"
    for i, fid in enumerate(ids):
        blocks += (
            f"        focus = {{\n"
            f"            id = {fid}\n"
            f"            x = {i * 2}\n"
            f"            y = 0\n"
            f"        }}\n"
        )
    blocks += "    }\n"
    return blocks


# --- signature & non-repo guarantees --------------------------------------


def test_diff_summary_signature_supports_new_params():
    """The public signature must keep these parameters with these defaults."""
    params = inspect.signature(diff_summary).parameters
    for required in ("kinds", "with_ids", "limit"):
        assert required in params, f"diff_summary missing param: {required}"
    assert params["with_ids"].default is True
    assert params["limit"].default == 200


def test_non_repo_returns_git_repo_error(tmp_path: Path):
    """A directory that is not a git repo must surface `git diff failed`."""
    out = diff_summary(tmp_path)
    assert out["ok"] is False
    assert "git diff failed" in out["error"]


# --- revision validation --------------------------------------------------


@pytest.mark.parametrize(
    "bad_base",
    [
        "",
        "--upload-pack=evil",
        "-c",
        "main\nrm -rf /",
        "a\x00b",
        "main;echo bad",
        "main && rm -rf /",
    ],
)
def test_invalid_base_revisions_are_rejected(git_repo: Path, bad_base: str):
    """Each of these `base` values would, if passed to git, be interpreted as
    an option, contain a control character, or be otherwise unsafe. None must
    reach the subprocess."""
    out = diff_summary(git_repo, base=bad_base)
    assert out["ok"] is False, f"unexpectedly accepted base={bad_base!r}: {out}"
    assert "invalid base revision" in out["error"]


def test_valid_bases_are_accepted(git_repo: Path):
    """Plain git refs must pass validation and reach `git diff`. HEAD..HEAD
    is a legal no-op (returns an empty file list)."""
    out = diff_summary(git_repo, base="HEAD")
    assert out["ok"] is True
    assert out["total_files"] == 0


# --- core diff behaviour against a real repo ------------------------------


def test_added_file_classified_and_listed(git_repo: Path):
    _write(git_repo, FOCUS_FILE, "")
    _add_and_commit(git_repo, FOCUS_FILE, message="add focus tree")

    out = diff_summary(git_repo, base="HEAD~1")
    assert out["ok"] is True
    assert out["total_files"] == 1
    [rec] = out["files"]
    assert rec["status"] == "A"
    assert rec["kind"] == "focus"
    assert rec["path"] == FOCUS_FILE
    # Pure additions skip the ID diff: no `added_ids`, no `id_diff`.
    assert "added_ids" not in rec
    assert "id_diff" not in rec


def test_modified_focus_file_reports_only_new_ids(git_repo: Path):
    """A modification that adds one focus and removes none must report
    exactly one added ID and zero removed IDs."""
    _write(git_repo, FOCUS_FILE, _focus_body("TST_root", "TST_branch"))
    _add_and_commit(git_repo, FOCUS_FILE, message="base focus tree")
    _write(git_repo, FOCUS_FILE, _focus_body("TST_root", "TST_branch", "TST_new_focus"))
    _add_and_commit(git_repo, FOCUS_FILE, message="add TST_new_focus")

    out = diff_summary(git_repo, base="HEAD~1")
    rec = next(r for r in out["files"] if r["path"] == FOCUS_FILE)
    assert rec["status"] == "M"
    assert rec["added_ids"] == ["TST_new_focus"]
    assert "removed_ids" not in rec


def test_modified_focus_file_reports_removed_ids(git_repo: Path):
    """When the branch removes a focus, `removed_ids` must list it."""
    _write(git_repo, FOCUS_FILE, _focus_body("TST_root", "TST_branch", "TST_drop"))
    _add_and_commit(git_repo, FOCUS_FILE, message="base focus tree")
    _write(git_repo, FOCUS_FILE, _focus_body("TST_root", "TST_branch"))
    _add_and_commit(git_repo, FOCUS_FILE, message="drop TST_drop")

    out = diff_summary(git_repo, base="HEAD~1")
    rec = next(r for r in out["files"] if r["path"] == FOCUS_FILE)
    assert rec["status"] == "M"
    assert rec["removed_ids"] == ["TST_drop"]
    assert "added_ids" not in rec


def test_deleted_file_is_classified_but_skipped_for_id_diff(git_repo: Path):
    _write(git_repo, "events/Test_events.txt", "country_event = { id = TST_evt.1 }")
    _add_and_commit(git_repo, "events/Test_events.txt", message="add events")

    (git_repo / "events" / "Test_events.txt").unlink()
    _git(git_repo, "add", "-A")
    _commit(git_repo, "delete events")

    out = diff_summary(git_repo, base="HEAD~1")
    rec = next(r for r in out["files"] if r["path"] == "events/Test_events.txt")
    assert rec["status"] == "D"
    assert rec["kind"] == "event"
    # Deletions skip ID reads entirely. Comparing against an empty HEAD would
    # otherwise mis-claim every prior ID was removed.
    assert "added_ids" not in rec
    assert "removed_ids" not in rec
    assert "id_diff" not in rec


def test_rename_keeps_both_paths_and_compares_correctly(git_repo: Path):
    """Renames must emit both `old_path` and `path` (= new path), and the
    base-revision ID diff must read the *old* path at base — otherwise every
    ID in the file shows up as `added_ids`, the exact bug this issue fixes."""
    base_body = (
        "add_namespace = TST\ncountry_event = {\n    id = TST_evt.1\n    title = evt_one\n}\n"
    )
    _write(git_repo, "events/Original_events.txt", base_body)
    _add_and_commit(git_repo, "events/Original_events.txt", message="base events")

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
    assert rec["added_ids"] == ["TST_evt.2"]
    assert "removed_ids" not in rec


def test_rename_to_non_id_kind_preserves_correctness(git_repo: Path):
    """Same rename test, but the file is a localisation snippet (no IDs
    expected). The file must be large enough for git's rename detector
    (-M default threshold) to fire — a one-line change in a tiny file is
    treated as add+delete, not rename."""
    base_body = "l_english:\n" + "\n".join(f"  key_{i}: value_{i}" for i in range(20)) + "\n"
    _write(git_repo, "localisation/old_l_english.yml", base_body)
    _add_and_commit(git_repo, "localisation/old_l_english.yml", message="base loc")
    _git(
        git_repo,
        "mv",
        "localisation/old_l_english.yml",
        "localisation/new_l_english.yml",
    )
    # Modify two lines so the rename similarity stays > 50%.
    edited = base_body.replace("value_5", "value_5_v2").replace("value_15", "value_15_v2")
    (git_repo / "localisation" / "new_l_english.yml").write_text(edited, encoding="utf-8")
    _git(git_repo, "add", "localisation/new_l_english.yml")
    _commit(git_repo, "rename loc + edit")

    out = diff_summary(git_repo, base="HEAD~1")
    rec = next(r for r in out["files"] if r["status"] == "R")
    assert rec["kind"] == "loc"
    assert rec["old_path"] == "localisation/old_l_english.yml"
    # Loc files have no ID extraction; nothing should appear under id_diff.
    assert "added_ids" not in rec
    assert "removed_ids" not in rec
    assert "id_diff" not in rec


# --- kinds filter & pagination -------------------------------------------


def test_kinds_filter_drops_other_categories(git_repo: Path):
    _write(git_repo, FOCUS_FILE, "focus = { id = TST_root }")
    _write(git_repo, "events/Other_events.txt", "country_event = { id = TST_evt.1 }")
    _add_and_commit(
        git_repo,
        FOCUS_FILE,
        "events/Other_events.txt",
        message="add focus + event",
    )

    out = diff_summary(git_repo, base="HEAD~1", kinds=["focus"])
    assert [r["path"] for r in out["files"]] == [FOCUS_FILE]
    assert out["counts_by_kind"] == {"focus": 1}

    out_all = diff_summary(git_repo, base="HEAD~1")
    assert out_all["total_files"] == 2
    assert out_all["counts_by_kind"] == {"focus": 1, "event": 1}

    # An explicitly empty filter matches nothing; None means no filter.
    out2 = diff_summary(git_repo, base="HEAD~1", kinds=[])
    assert out2["total_files"] == 0


def test_limit_paginates_without_dropping_total_count(git_repo: Path):
    """`limit` clamps `files_returned` and sets `truncated`, but `total_files`
    and `counts_by_kind` stay accurate — that's the agent's only way to know
    what was held back."""
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
    assert out["counts_by_kind"] == {"event": 3}


def test_nonpositive_limit_returns_no_files(git_repo: Path):
    """Zero and negative limits must clamp to zero rather than using Python's
    negative-slice semantics to return an accidental tail of the list."""
    files = ["events/A.txt", "events/B.txt"]
    for path in files:
        _write(git_repo, path, "")
    _add_and_commit(git_repo, *files, message="two files")

    for limit in (0, -1):
        out = diff_summary(git_repo, base="HEAD~1", limit=limit, with_ids=False)
        assert out["files"] == []
        assert out["files_returned"] == 0
        assert out["total_files"] == 2
        assert out["truncated"] is True


def test_with_ids_false_skips_id_diff_for_modified_files(git_repo: Path):
    _write(git_repo, FOCUS_FILE, _focus_body("TST_root"))
    _add_and_commit(git_repo, FOCUS_FILE, message="base focus")
    _write(git_repo, FOCUS_FILE, _focus_body("TST_root", "TST_new"))
    _add_and_commit(git_repo, FOCUS_FILE, message="modify focus")

    out = diff_summary(git_repo, base="HEAD~1", with_ids=False)
    assert out["ok"] is True
    assert out["with_ids"] is False
    rec = out["files"][0]
    assert rec["status"] == "M"
    assert "added_ids" not in rec
    assert "removed_ids" not in rec
    assert "id_diff" not in rec

    with_ids = diff_summary(git_repo, base="HEAD~1", with_ids=True)
    assert with_ids["files"][0]["added_ids"] == ["TST_new"]


# --- filenames with special characters ------------------------------------


@pytest.mark.parametrize(
    "weird",
    [
        "events/Has Space_in_name.txt",
        "events/has\twith-tabs.txt",
        "events/ümlaut.txt",
    ],
)
def test_filename_with_special_characters(git_repo: Path, weird: str):
    """The `-z` output must correctly parse paths containing spaces,
    tabs, and non-ASCII characters. Falls back to ASCII for portability."""
    body = "country_event = { id = TST_evt.1 }"
    # Tab in path is not portable across filesystem encodings on macOS, so
    # skip if we can't create it.
    try:
        _write(git_repo, weird, body)
    except (OSError, UnicodeEncodeError):
        pytest.skip(f"filesystem cannot encode path {weird!r}")
    _add_and_commit(git_repo, weird, message=f"add {weird}")

    out = diff_summary(git_repo, base="HEAD~1")
    rec = next(r for r in out["files"] if r["path"] == weird)
    assert rec["status"] == "A"
    assert rec["kind"] == "event"


# --- classifier paths ------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path, expected_kind",
    [
        ("common/national_focus/X_focus.txt", "focus"),
        ("events/X.txt", "event"),
        ("common/decisions/X.txt", "decision"),
        ("common/ideas/X.txt", "idea"),
        ("localisation/X.yml", "loc"),
        ("interface/X.gfx", "gfx"),
        ("common/military_industrial_organisation/X.txt", "other"),  # wrong spelling -> other
        ("common/military_industrial_organization/X.txt", "mio"),
        ("history/countries/X.txt", "history"),
        ("tools/X.py", "tools"),
        (".claude/X.md", "claude"),
        ("random/path/unknown.txt", "other"),
    ],
)
def test_classify_routes_paths_correctly(git_repo: Path, rel_path: str, expected_kind: str):
    """Each prefix the classifier handles must map to the right kind.
    A regression here would silently mis-classify every file of that kind."""
    # Create an empty file at the path so git tracks it.
    _write(git_repo, rel_path, "")
    _add_and_commit(git_repo, rel_path, message=f"add {rel_path}")

    out = diff_summary(git_repo, base="HEAD~1")
    rec = next(r for r in out["files"] if r["path"] == rel_path)
    assert rec["kind"] == expected_kind


@pytest.mark.parametrize(
    "kind, rel_path, base_body, new_body, new_id",
    [
        (
            "decision",
            "common/decisions/TST_decisions.txt",
            "TST_category = {\n"
            "    TST_base_decision = {\n"
            "        allowed = { tag = TST }\n"
            "    }\n"
            "}\n",
            "TST_category = {\n"
            "    TST_base_decision = {\n"
            "        allowed = { tag = TST }\n"
            "    }\n"
            "    TST_new_decision = {\n"
            "        allowed = { tag = TST }\n"
            "    }\n"
            "}\n",
            "TST_new_decision",
        ),
        (
            "idea",
            "common/ideas/TST_ideas.txt",
            "ideas = {\n"
            "    country = {\n"
            "        TST_base_idea = {\n"
            "            modifier = { stability_factor = 0.05 }\n"
            "        }\n"
            "    }\n"
            "}\n",
            "ideas = {\n"
            "    country = {\n"
            "        TST_base_idea = {\n"
            "            modifier = { stability_factor = 0.05 }\n"
            "        }\n"
            "        TST_new_idea = {\n"
            "            modifier = { war_support_factor = 0.05 }\n"
            "        }\n"
            "    }\n"
            "}\n",
            "TST_new_idea",
        ),
    ],
)
def test_id_diff_extracts_decision_and_idea_records(
    git_repo: Path,
    kind: str,
    rel_path: str,
    base_body: str,
    new_body: str,
    new_id: str,
):
    """The schema extractors for all four ID-bearing kinds must feed the
    same added/removed contract. Focus/event are covered by the real-repo
    tests above; this covers the decision and idea branches too."""
    _write(git_repo, rel_path, base_body)
    _add_and_commit(git_repo, rel_path, message=f"base {kind}")
    _write(git_repo, rel_path, new_body)
    _add_and_commit(git_repo, rel_path, message=f"add {new_id}")

    out = diff_summary(git_repo, base="HEAD~1")
    rec = out["files"][0]
    assert rec["kind"] == kind
    assert rec["added_ids"] == [new_id]
    assert "removed_ids" not in rec


# --- subprocess failure surfaces ------------------------------------------


def test_parser_failure_surfaces_in_id_diff_without_false_ids(git_repo: Path):
    """A focus file that fails to parse must NOT be reported as a false
    `added_ids=[...]` delta (from whatever partial records the parser
    scraped before failing). It must carry an `id_diff.error` so a
    downstream caller knows the diff is unreliable for this file."""
    _write(git_repo, FOCUS_FILE, _focus_body("TST_root"))
    _add_and_commit(git_repo, FOCUS_FILE, message="base focus")

    # Unbalanced brace: triggers a real ParseError in the paradox parser.
    (git_repo / FOCUS_FILE).write_text(
        "focus_tree = {\n    id = test_tree\n    focus = {\n"
        "        id = TST_bad\n        x = 0\n        y = 0\n",
        encoding="utf-8",
    )
    _git(git_repo, "add", FOCUS_FILE)
    _commit(git_repo, "broken focus file")

    out = diff_summary(git_repo, base="HEAD~1")
    rec = next(r for r in out["files"] if r["path"] == FOCUS_FILE)
    assert (
        "added_ids" not in rec
    ), f"parser failure was reported as added_ids: {rec.get('added_ids')!r}"
    assert "removed_ids" not in rec
    assert "id_diff" in rec
    assert "error" in rec["id_diff"]
    assert "parser failure" in rec["id_diff"]["error"]


def test_git_show_failure_on_real_path_surfaces_as_head_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """A `git show HEAD:<path>` failure must surface as `id_diff.head_error`
    and must not produce false `added_ids`. We simulate the failure without
    faking a parser error."""

    _write(git_repo, FOCUS_FILE, _focus_body("TST_root"))
    _add_and_commit(git_repo, FOCUS_FILE, message="add focus")

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(*args, **kwargs):
            argv = list(args[0]) if args else []
            if argv and argv[0] == "git" and len(argv) >= 2 and argv[1] == "show":
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=128,
                    stderr="fatal: bad object 0000\n",
                )
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=f"A\x00{FOCUS_FILE}\x00",
                stderr="",
            )

    stub = types.SimpleNamespace(
        run=_StubSubprocess.run,
        TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
    )
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    out = diff_summary(git_repo, base="HEAD~1")
    assert out["ok"] is True
    rec = next(r for r in out["files"] if r["path"] == FOCUS_FILE)
    # Stub makes `git show HEAD:<path>` always fail, so head_error must
    # surface and no IDs may be claimed.
    assert "head_error" in rec["id_diff"]
    assert "added_ids" not in rec
    assert "removed_ids" not in rec


def test_git_diff_failure_with_unknown_repo_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A git error other than 'not a git repository' must still surface
    rather than be swallowed — even if the working directory isn't a repo."""

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=list(args[0]) if args else [],
                returncode=1,
                stdout="",
                stderr="fatal: ambiguous argument 'main': unknown revision\n",
            )

    stub = types.SimpleNamespace(
        run=_StubSubprocess.run,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    out = diff_summary(tmp_path, base="main")
    assert out["ok"] is False
    assert out["error"] == "git diff failed"
    # Stderr must surface in `error_msg` (truncated).
    assert "unknown revision" in out["error_msg"]


def test_base_show_failure_surfaces_without_false_ids(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """A non-addition whose base blob cannot be read must carry
    `id_diff.base_error`, not compare against empty text and claim every HEAD
    ID was added."""
    _write(git_repo, FOCUS_FILE, _focus_body("TST_root"))
    _add_and_commit(git_repo, FOCUS_FILE, message="base focus")
    head_body = _focus_body("TST_root", "TST_new")
    _write(git_repo, FOCUS_FILE, head_body)
    _add_and_commit(git_repo, FOCUS_FILE, message="modify focus")

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            argv = list(args[0]) if args else []
            if len(argv) >= 2 and argv[1] == "diff":
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=0,
                    stdout=f"M\x00{FOCUS_FILE}\x00",
                    stderr="",
                )
            if len(argv) >= 2 and argv[1] == "show":
                if argv[-1].startswith("HEAD:"):
                    return subprocess.CompletedProcess(
                        args=argv, returncode=0, stdout=head_body, stderr=""
                    )
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=128,
                    stdout="",
                    stderr=f"fatal: path '{FOCUS_FILE}' does not exist in 'HEAD~1'\n",
                )
            raise AssertionError(f"unexpected git argv: {argv}")

    stub = types.SimpleNamespace(run=_StubSubprocess.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    out = diff_summary(git_repo, base="HEAD~1")
    rec = out["files"][0]
    assert rec["status"] == "M"
    assert rec["id_diff"]["base_error"] == "git show failed"
    assert "added_ids" not in rec
    assert "removed_ids" not in rec


def test_invalid_diff_records_return_error(monkeypatch: pytest.MonkeyPatch):
    """An unexpected internal diff result must fail closed instead of
    iterating arbitrary values and producing a misleading summary."""
    monkeypatch.setattr(diff_summary_mod, "_git_diff_files", lambda *_args: None)

    out = diff_summary(Path("."), base="HEAD")
    assert out == {"ok": False, "error": "git diff returned invalid output"}


def test_git_diff_not_found_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Failure to start git must be distinguishable from an empty diff."""

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            raise FileNotFoundError("git")

    stub = types.SimpleNamespace(run=_StubSubprocess.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    out = diff_summary(tmp_path, base="HEAD")
    assert out == {"ok": False, "error": "git not found on PATH"}


def test_git_diff_failure_output_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Noisy git stderr must be clipped before it reaches the MCP response."""
    from md_mcp.analysis.diff_summary import GIT_ERR_MAX_BYTES

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=list(args[0]) if args else [],
                returncode=1,
                stdout="",
                stderr="fatal: " + ("x" * (GIT_ERR_MAX_BYTES + 500)),
            )

    stub = types.SimpleNamespace(run=_StubSubprocess.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    out = diff_summary(tmp_path, base="HEAD")
    assert out["ok"] is False
    assert out["error"] == "git diff failed"
    assert len(out["error_msg"]) <= GIT_ERR_MAX_BYTES + len("...[truncated]")
    assert out["error_msg"].endswith("...[truncated]")


# --- subprocess timeout (real TimeoutExpired) -----------------------------


def test_subprocess_timeout_via_real_TimeoutExpired(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """Drive the timeout branch by raising the real `subprocess.TimeoutExpired`
    exception, not a generic stub exception. This locks the catch-arms to the
    exact type, not to "anything that raises"."""

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            argv = list(args[0]) if args else []
            raise subprocess.TimeoutExpired(
                cmd=argv[:1] if argv else "git", timeout=kwargs.get("timeout", 0)
            )

    stub = types.SimpleNamespace(run=_StubSubprocess.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    _write(git_repo, FOCUS_FILE, _focus_body("TST_root"))
    _add_and_commit(git_repo, FOCUS_FILE, message="add focus")

    out = diff_summary(git_repo, base="HEAD~1")
    assert out["ok"] is False
    assert "timed out" in out["error"]


def test_git_show_timeout_via_real_TimeoutExpired(git_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """First call (`git diff`) succeeds and reports a modify; second call
    (`git show HEAD:<path>`) raises the real `subprocess.TimeoutExpired`.
    The catch arm on the show path must surface head_error."""
    _write(git_repo, FOCUS_FILE, _focus_body("TST_root"))
    _add_and_commit(git_repo, FOCUS_FILE, message="add focus")

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            argv = list(args[0]) if args else []
            if argv and argv[0] == "git" and len(argv) >= 2 and argv[1] == "show":
                raise subprocess.TimeoutExpired(cmd=argv[:1], timeout=15.0)
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=f"M\x00{FOCUS_FILE}\x00",
                stderr="",
            )

    stub = types.SimpleNamespace(run=_StubSubprocess.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    out = diff_summary(git_repo, base="HEAD~1")
    assert out["ok"] is True
    rec = out["files"][0]
    assert rec["status"] == "M"
    assert "head_error" in rec["id_diff"]
    assert "timed out" in rec["id_diff"]["head_error"]
    assert "added_ids" not in rec
    assert "removed_ids" not in rec


def test__validate_rev_rejects_options_and_control_chars():
    from md_mcp.analysis.diff_summary import _GitRevError, _validate_rev

    for bad in ("", "--upload-pack=evil", "-c", "main\nrm", "a\x00b"):
        with pytest.raises(_GitRevError):
            _validate_rev("rev", bad)

    # Happy path.
    for good in ("HEAD~1", "main", "feature/foo", "release/2026.01", "abc123def"):
        assert _validate_rev("rev", good) == good


def test_read_at_rejects_invalid_revision_without_running_git(
    monkeypatch: pytest.MonkeyPatch,
):
    """`_read_at` must keep its own revision guard even when called directly."""
    from md_mcp.analysis.diff_summary import _GitReadError, _read_at

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            raise AssertionError("git must not run for an invalid revision")

    stub = types.SimpleNamespace(run=_StubSubprocess.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    result = _read_at(Path("."), "--bad", "events/file.txt")
    assert isinstance(result, _GitReadError)
    assert "starts with '-'" in result.error


def test_read_at_reports_git_not_found(monkeypatch: pytest.MonkeyPatch):
    from md_mcp.analysis.diff_summary import _GitReadError, _read_at

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            raise FileNotFoundError("git")

    stub = types.SimpleNamespace(run=_StubSubprocess.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    result = _read_at(Path("."), "HEAD", "events/file.txt")
    assert isinstance(result, _GitReadError)
    assert result.error == "git not found on PATH"


def test_read_at_strips_bom_and_one_trailing_newline(monkeypatch: pytest.MonkeyPatch):
    from md_mcp.analysis.diff_summary import _read_at

    seen: List[List[str]] = []

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            seen.append(list(args[0]))
            return subprocess.CompletedProcess(
                args=list(args[0]) if args else [],
                returncode=0,
                stdout="\ufefffocus_tree = {}\n",
                stderr="",
            )

    stub = types.SimpleNamespace(run=_StubSubprocess.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    assert _read_at(Path("."), "HEAD", "events/file.txt") == "focus_tree = {}"
    assert seen == [["git", "show", "--end-of-options", "HEAD:events/file.txt"]]


def test_git_diff_places_base_after_end_of_options(monkeypatch: pytest.MonkeyPatch):
    """The user-controlled revision must be an operand, never a git option."""
    seen: List[List[str]] = []

    class _StubSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            seen.append(list(args[0]))
            return subprocess.CompletedProcess(
                args=list(args[0]), returncode=0, stdout="", stderr=""
            )

    stub = types.SimpleNamespace(run=_StubSubprocess.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(diff_summary_mod, "subprocess", stub)

    out = diff_summary(Path("."), base="HEAD")
    assert out["ok"] is True
    assert seen == [
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "-M",
            "--end-of-options",
            "HEAD..HEAD",
        ]
    ]


def test__truncate_err_caps_oversize_strings():
    from md_mcp.analysis.diff_summary import GIT_ERR_MAX_BYTES, _truncate_err

    assert _truncate_err("") == ""
    assert _truncate_err("short") == "short"
    big = "x" * (GIT_ERR_MAX_BYTES + 500)
    out = _truncate_err(big)
    assert len(out) <= GIT_ERR_MAX_BYTES + len("...[truncated]")
    assert out.endswith("...[truncated]")


def test__parse_name_status_z_round_trip_with_real_git(git_repo: Path):
    """Spawn a real `git diff` and verify the parser produces the same set of
    status+path pairs as a naive newline-split read of `git diff --name-status`
    without `-z`. Catches format regressions where `git` changes its `-z`
    output (e.g. new status letters in a future version)."""
    # Three files: add / modify / delete.
    base_dir = git_repo
    (base_dir / "events/A_events.txt").parent.mkdir(parents=True, exist_ok=True)
    (base_dir / "events/A_events.txt").write_text(
        "country_event = { id = TST_evt_old }", encoding="utf-8"
    )
    (base_dir / "events/B_events.txt").write_text(
        "country_event = { id = TST_evt_b }", encoding="utf-8"
    )
    (base_dir / "events/C_events.txt").write_text(
        "country_event = { id = TST_evt_c }", encoding="utf-8"
    )
    _git(base_dir, "add", "events/")
    _commit(base_dir, "base three files")

    # Modify A, delete C, leave B alone.
    (base_dir / "events/A_events.txt").write_text(
        "country_event = { id = TST_evt_new }", encoding="utf-8"
    )
    (base_dir / "events/C_events.txt").unlink()
    _git(base_dir, "add", "events/A_events.txt")
    _git(base_dir, "rm", "events/C_events.txt")
    _commit(base_dir, "modify A, delete C")

    # Naive (non-`-z`) baseline: `git diff --name-status HEAD~1..HEAD`.
    naive = _git_out(
        base_dir,
        "diff",
        "--name-status",
        "HEAD~1..HEAD",
    ).splitlines()
    naive_pairs = sorted((line.split("\t")[0][0], line.split("\t")[-1]) for line in naive if line)

    # Parsed via -z.
    z_out = _git_out(base_dir, "diff", "--name-status", "-z", "HEAD~1..HEAD")
    from md_mcp.analysis.diff_summary import _parse_name_status_z

    parsed = sorted((r["status"], r["new_path"]) for r in _parse_name_status_z(z_out))
    assert parsed == naive_pairs


def test__parse_name_status_z_preserves_unknown_status_letter():
    """If git introduces a new one-path status, preserve the record rather
    than silently dropping a file from the summary."""
    from md_mcp.analysis.diff_summary import _parse_name_status_z

    raw = "Z\x00bogus.txt\x00M\x00good.txt\x00"
    records = _parse_name_status_z(raw)
    assert records == [
        {"status": "Z", "old_path": "", "new_path": "bogus.txt"},
        {"status": "M", "old_path": "", "new_path": "good.txt"},
    ]


def test_git_read_error_repr_is_informative():
    """The error record carries message + detail; both should be visible."""
    from md_mcp.analysis.diff_summary import _GitReadError

    err = _GitReadError("git show failed", "fatal: bad object 123abc")
    rep = repr(err)
    assert "git show failed" in rep
    assert "bad object 123abc" in rep
    assert err.error == "git show failed"
    assert err.error_msg == "fatal: bad object 123abc"


def test_extract_ids_returns_empty_for_unknown_kind():
    from md_mcp.analysis.diff_summary import _extract_ids

    assert _extract_ids("id = TST_unknown", "unsupported") == []


# --- empty diff ----------------------------------------------------------


def test_empty_diff_against_same_revision_returns_zero_files(git_repo: Path):
    """Diffing HEAD against HEAD must return `total_files: 0`, not crash."""
    out = diff_summary(git_repo, base="HEAD")
    assert out["ok"] is True
    assert out["total_files"] == 0
    assert out["files"] == []
    assert out["counts_by_kind"] == {}
    assert out["truncated"] is False
