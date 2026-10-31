"""Tests for the lint fixer tool (`fix_lint`).

Two layers, matching the validator-wrapper convention:

  * Unit tests plant minimal stand-in upstream modules in a tmp mod_root and
    exercise the wrapper plumbing (scope guards, BOM handling, budget, and the
    no-write guarantee).
  * `@pytest.mark.integration` tests copy the REAL upstream fixer modules out
    of a real Millennium-Dawn checkout (found via MD_MOD_ROOT) into a tmp root
    and round-trip fixtures with known violations. They also copy the
    upstream validation_config.json required by shared_utils imports. The real
    tree is never written to.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from md_mcp.tools import lint_fixers
from md_mcp.tools.lint_fixers import (
    LOG_ID_SCOPES,
    fix_lint_tool,
)
from md_mcp.util.response import BUDGET_BYTES, MAX_TEXT_BYTES

LINTING = "tools/linting"


@pytest.fixture(autouse=True)
def fresh_fixer_imports():
    """Keep fixer stand-ins and helper paths within the test that loaded them."""
    saved_path = sys.path.copy()
    lint_fixers._UPSTREAM.reset()
    lint_fixers._EVENTS_VALIDATION.reset()
    yield
    lint_fixers._UPSTREAM.reset()
    lint_fixers._EVENTS_VALIDATION.reset()
    sys.path[:] = saved_path


# ---------------------------------------------------------------------------
# Stand-in upstream modules (unit layer)
# ---------------------------------------------------------------------------

_STANDIN_SHARED_UTILS = r"""
import os

def strip_inline_comment(line):
    if "#" not in line:
        return line
    in_str = False
    for i, c in enumerate(line):
        if c == '"' and (i == 0 or line[i - 1] != "\\"):
            in_str = not in_str
        elif c == "#" and not in_str:
            return line[:i]
    return line

def is_excluded_path(path, excluded_dirs, repo_root):
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(repo_root))
    return any(part in excluded_dirs for part in rel.split(os.sep))
"""

# OR1 becomes AND1 in the OR pass and AND1 becomes "done" in the AND pass, so
# "OR1" only reaches "done" when the passes run in that order. A "DROP" line
# vanishes in the AND pass, which shows where the wrapper split the lines.
_STANDIN_CLEANUP_OR = r"""
from shared_utils import is_excluded_path

_EXCLUDED_DIRS = {"resources", ".git", ".claude"}

def _is_excluded_path(path, repo_root=None):
    return is_excluded_path(path, _EXCLUDED_DIRS, repo_root)

def simplify_or_block(lines):
    if any("UNCLOSED" in line for line in lines):
        raise ValueError("substring not found")
    return [line.replace("OR1", "AND1") for line in lines]

def simplify_and_block(lines):
    return [line.replace("AND1", "done") for line in lines if line != "DROP\n"]

def find_single_condition_or_blocks(lines):
    return [(n, "or") for n, line in enumerate(lines, 1) if "OR1" in line]

def find_redundant_and_blocks(lines):
    return [(n, "and") for n, line in enumerate(lines, 1) if "AND1" in line]
"""


def _standin_fix_styling(replacement: str) -> str:
    return (
        "def fix_line(line):\n"
        '    n = line.count("XX")\n'
        f'    return line.replace("XX", "{replacement}"), n\n'
    )


_STANDIN_FIX_LOC_YAML = r"""
def check_line(line, line_num):
    if "\t" in line:
        return [(line_num, "tab", "tab character found")]
    return []

def fix_line(line):
    return line.replace("\t", "")
"""

_STANDIN_CHECK_COMMON = r"""
def _find_focus_log_mismatches(lines):
    out = []
    for i, line in enumerate(lines):
        start = line.find("LOGBAD")
        if start >= 0:
            out.append((i, start, start + len("LOGBAD"), "goodid", "badid"))
    return out

def _find_decision_log_mismatches(lines):
    out = []
    for i, line in enumerate(lines):
        start = line.find("DECBAD")
        if start >= 0:
            out.append((i, start, start + len("DECBAD"), "decid", "badid"))
    return out
"""

_STANDIN_CHECK_CHANGELOG = r"""
def check_lines(lines):
    return [f"line {i}: untagged entry must come before [USA] (line 2)"
            for i, line in enumerate(lines, 1) if line.startswith("- untagged-late")]

def order_lines(lines):
    return [line.replace("untagged-late", "untagged-first") for line in lines]
"""


def _plant_upstream(root: Path, *, styling_replacement: str = "YY") -> None:
    linting = root / LINTING
    linting.mkdir(parents=True, exist_ok=True)
    (root / "tools" / "shared_utils.py").write_text(_STANDIN_SHARED_UTILS, encoding="utf-8")
    (linting / "fix_styling.py").write_text(
        _standin_fix_styling(styling_replacement), encoding="utf-8"
    )
    (linting / "fix_loc_yaml.py").write_text(_STANDIN_FIX_LOC_YAML, encoding="utf-8")
    (linting / "check_common_mistakes.py").write_text(_STANDIN_CHECK_COMMON, encoding="utf-8")
    (linting / "check_changelog.py").write_text(_STANDIN_CHECK_CHANGELOG, encoding="utf-8")
    (root / "tools" / "cleanup_or.py").write_text(_STANDIN_CLEANUP_OR, encoding="utf-8")


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        pytest.param({"fixer": "nonsense", "content": "x"}, "", id="unknown-fixer"),
        pytest.param(
            {"fixer": "changelog", "path": "other.txt"}, "Changelog.txt", id="changelog-wrong-path"
        ),
        pytest.param({"fixer": "styling"}, "content= or path=", id="no-source"),
        pytest.param(
            {"fixer": "log_ids", "content": "log = x"}, "log_ids", id="log-ids-needs-a-path"
        ),
        pytest.param({"fixer": "loc_yaml", "path": "script.txt"}, ".yml", id="loc-yaml-given-txt"),
        pytest.param({"fixer": "styling", "path": "loc.yml"}, ".txt", id="styling-given-yml"),
        pytest.param({"fixer": "cleanup_or", "path": "loc.yml"}, ".txt", id="cleanup-or-given-yml"),
        pytest.param({"fixer": "styling", "path": "../outside.txt"}, "", id="path-traversal"),
        pytest.param({"fixer": "styling", "path": "common/nope.txt"}, "", id="missing-file"),
    ],
)
def test_fix_lint_rejects_bad_arguments(tmp_path, arguments, error):
    _plant_upstream(tmp_path)
    (tmp_path / "script.txt").write_text("a = b", encoding="utf-8")
    (tmp_path / "loc.yml").write_text("K: v", encoding="utf-8")

    out = fix_lint_tool(tmp_path, **arguments)

    assert out["ok"] is False
    assert error in out["error"]


def test_changelog_fixer_uses_order_lines_without_writing(tmp_path):
    _plant_upstream(tmp_path)
    source = "- untagged-late\n- [USA] tagged\n"

    out = fix_lint_tool(tmp_path, fixer="changelog", content=source)

    assert out["ok"] is True
    assert out["changed"] is True
    assert out["txt"] == "- untagged-first\n- [USA] tagged\n"
    assert not (tmp_path / "Changelog.txt").exists()


def test_changelog_helpers_are_reloaded_when_mod_root_changes(tmp_path):
    from md_mcp.tools.linting_tools import lint_changelog_tool

    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    for root, label in ((root_a, "A"), (root_b, "B")):
        checker = root / LINTING / "check_changelog.py"
        checker.parent.mkdir(parents=True, exist_ok=True)
        checker.write_text(
            "def check_lines(lines):\n    return []\n"
            f"def order_lines(lines):\n    return [{label!r} + line for line in lines]\n",
            encoding="utf-8",
        )
    (root_b / "Changelog.txt").write_text("entry\n", encoding="utf-8")

    first = fix_lint_tool(root_a, fixer="changelog", content="entry\n")
    lint_changelog_tool(root_b)
    second = fix_lint_tool(root_a, fixer="changelog", content="entry\n")

    assert first["txt"] == "Aentry\n"
    assert second["txt"] == "Aentry\n"


# ---------------------------------------------------------------------------
# content / path sources, BOM, no-write guarantee
# ---------------------------------------------------------------------------


def test_content_fix_styling(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", content="a XX b\n")
    assert out["ok"] is True
    assert out["source"] == "content"
    assert out["changed"] is True
    assert out["fixes"] == 1
    assert out["summary"]["line_fixes"] == 1
    assert out["txt"] == "a YY b\n"


def test_styling_normalizes_trailing_newline(tmp_path):
    """Upstream fix_file always ends the file with exactly one newline."""
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", content="a XX b")
    assert out["txt"] == "a YY b\n"
    out = fix_lint_tool(tmp_path, fixer="styling", content="a XX b\n\n\n")
    assert out["txt"] == "a YY b\n"


def test_path_as_metadata_only_with_content(tmp_path):
    """content= wins and the path is never touched (may not even exist)."""
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="styling",
        path="common/national_focus/not_on_disk.txt",
        content="a XX b\n",
    )
    assert out["ok"] is True
    assert out["source"] == "content"
    assert out["file"] == "common/national_focus/not_on_disk.txt"
    assert out["txt"] == "a YY b\n"


def test_path_fix_leaves_file_unchanged(tmp_path):
    _plant_upstream(tmp_path)
    f = tmp_path / "common" / "national_focus" / "x.txt"
    f.parent.mkdir(parents=True)
    original = b"a XX b\n"
    f.write_bytes(original)

    out = fix_lint_tool(tmp_path, fixer="styling", path="common/national_focus/x.txt")

    assert out["ok"] is True
    assert out["changed"] is True
    assert out["txt"] == "a YY b\n"
    assert f.read_bytes() == original  # server never writes


def test_unchanged_content_omits_txt(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", content="a b c\n")
    assert out["ok"] is True
    assert out["changed"] is False
    assert "txt" not in out


def test_bom_from_path_reported_and_stripped(tmp_path):
    _plant_upstream(tmp_path)
    f = tmp_path / "localisation" / "english" / "x.yml"
    f.parent.mkdir(parents=True)
    f.write_bytes("\ufeffK:\tvalue\n".encode("utf-8"))

    out = fix_lint_tool(tmp_path, fixer="loc_yaml", path="localisation/english/x.yml")

    assert out["ok"] is True
    assert out["had_bom"] is True
    assert out["txt"].startswith("K:")
    assert out["summary"]["tab"] == 1


def test_bom_from_content_reported_and_stripped(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="loc_yaml", content="\ufeffK:\tvalue\n")
    assert out["had_bom"] is True
    assert out["txt"] == "K:value\n"


def test_invalid_utf8_rejected(tmp_path):
    _plant_upstream(tmp_path)
    f = tmp_path / "bad.txt"
    f.write_bytes(b"id = \xff\xfe\n")
    out = fix_lint_tool(tmp_path, fixer="styling", path="bad.txt")
    assert out["ok"] is False
    assert "Invalid UTF-8" in out["error"]


def test_line_endings_fixes_crlf(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="line_endings", content="a\r\nb\r\nc\n")
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["fixes"] == 2
    assert out["summary"] == {"crlf_to_lf": 2}
    assert out["txt"] == "a\nb\nc\n"


def test_line_endings_unchanged_omits_txt(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="line_endings", content="a\nb\n")
    assert out["ok"] is True
    assert out["changed"] is False
    assert out["fixes"] == 0
    assert "txt" not in out


def test_line_endings_needs_no_upstream_modules(tmp_path):
    """line_endings is byte-level; it must work on a bare mod root."""
    out = fix_lint_tool(tmp_path, fixer="line_endings", content="a\r\nb\n")
    assert out["ok"] is True
    assert out["txt"] == "a\nb\n"


def test_line_endings_bom_from_path(tmp_path):
    f = tmp_path / "x.yml"
    f.write_bytes(b"\xef\xbb\xbfK: v\r\n")
    out = fix_lint_tool(tmp_path, fixer="line_endings", path="x.yml")
    assert out["ok"] is True
    assert out["had_bom"] is True
    assert out["txt"] == "K: v\n"


def test_line_endings_non_utf8_reports_manual_path(tmp_path):
    """The fix applies but can't be returned as text — say so instead of crashing."""
    f = tmp_path / "bad.bin"
    f.write_bytes(b"a\xff\r\nb\r\n")
    out = fix_lint_tool(tmp_path, fixer="line_endings", path="bad.bin")
    assert out["ok"] is False
    assert "fix_line_endings.py manually" in out["error"]


# ---------------------------------------------------------------------------
# log_ids scoping and span rewrite
# ---------------------------------------------------------------------------


def test_log_ids_outside_scope_is_a_note(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="log_ids",
        path="events/my_events.txt",
        content="LOGBAD",
    )
    assert out["ok"] is True
    assert out["changed"] is False
    assert "outside fixer scope" in out["note"]
    assert set(LOG_ID_SCOPES) == {"common/national_focus/", "common/decisions/"}


def test_log_ids_focus_span_rewrite(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="log_ids",
        path="common/national_focus/x.txt",
        content="log = 'x LOGBAD y'\n",
    )
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["summary"] == {"focus_log_ids": 1}
    assert out["txt"] == "log = 'x goodid y'\n"


def test_log_ids_decision_span_rewrite(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="log_ids",
        path="common/decisions/x.txt",
        content="DECBAD\n",
    )
    assert out["ok"] is True
    assert out["summary"] == {"decision_log_ids": 1}
    assert out["txt"] == "decid\n"


def test_log_ids_no_mismatch_omits_txt(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="log_ids",
        path="common/national_focus/x.txt",
        content="clean\n",
    )
    assert out["ok"] is True
    assert out["changed"] is False
    assert out["summary"] == {"focus_log_ids": 0}
    assert "txt" not in out


def test_log_ids_rewrites_multiple_spans_on_one_line(tmp_path):
    """Spans apply rightmost-first, so a short-then-long rewrite pair on one
    line must not shift the later span (leftmost-first corrupts the line)."""
    _plant_upstream(tmp_path)
    (tmp_path / LINTING / "check_common_mistakes.py").write_text(
        "def _find_focus_log_mismatches(lines):\n"
        "    line = lines[0]\n"
        "    a = line.find('AA')\n"
        "    b = line.find('BB')\n"
        "    return [(0, a, a + 2, 'AAA', 'AA'), (0, b, b + 2, 'Z', 'BB')]\n"
        "\n"
        "def _find_decision_log_mismatches(lines):\n"
        "    return []\n",
        encoding="utf-8",
    )
    out = fix_lint_tool(
        tmp_path,
        fixer="log_ids",
        path="common/national_focus/x.txt",
        content="AA and BB\n",
    )
    assert out["ok"] is True
    assert out["fixes"] == 2
    assert out["txt"] == "AAA and Z\n"


def test_missing_upstream_modules_reports_gracefully(tmp_path):
    """A mod root without tools/linting degrades to ok:false, not an exception."""
    out = fix_lint_tool(tmp_path, fixer="styling", content="a b\n")
    assert out["ok"] is False
    assert "tools" in out["error"]
    assert str(tmp_path) in out["error"]


# ---------------------------------------------------------------------------
# Module cache: a new mod_root must get a fresh import
# ---------------------------------------------------------------------------


def test_fresh_import_per_mod_root(tmp_path):
    root_a = tmp_path / "mod_a"
    root_b = tmp_path / "mod_b"
    _plant_upstream(root_a, styling_replacement="YY")
    _plant_upstream(root_b, styling_replacement="ZZ")

    out_a = fix_lint_tool(root_a, fixer="styling", content="XX\n")
    out_b = fix_lint_tool(root_b, fixer="styling", content="XX\n")

    assert out_a["txt"] == "YY\n"
    assert out_b["txt"] == "ZZ\n"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_truncation_clips_txt_and_warns(tmp_path):
    _plant_upstream(tmp_path)
    big = "XX " + "a" * 120_000
    out = fix_lint_tool(tmp_path, fixer="styling", content=big)

    assert out["ok"] is True
    assert out["txt_truncated"] is True
    assert out["txt_bytes"] > out["txt_returned_bytes"]
    assert len(out["txt"].encode("utf-8")) == out["txt_returned_bytes"]
    assert "do NOT write clipped content back" in out["note"]
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("\t" * 60_000, id="tabs"),
        pytest.param("\x01" * 20_000, id="control-characters"),
        pytest.param('\\"' * 30_000, id="quotes-and-backslashes"),
        pytest.param("é" * 60_000, id="utf8-boundary"),
    ],
)
def test_fixed_text_keeps_a_prefix_when_json_expands(tmp_path, body):
    out = fix_lint_tool(tmp_path, fixer="line_endings", content=body + "\r\n")
    fixed = body + "\n"

    assert out["ok"] is True
    assert out["changed"] is True
    assert out["fixes"] == 1
    assert out["summary"] == {"crlf_to_lf": 1}
    assert out["txt_truncated"] is True
    assert 0 < len(out["txt"]) < len(fixed)
    assert fixed.startswith(out["txt"])
    assert out["txt_bytes"] == len(fixed.encode("utf-8"))
    assert out["txt_returned_bytes"] == len(out["txt"].encode("utf-8"))
    assert "txt_dropped" not in out
    assert "do NOT write clipped content back" in out["note"]
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_oversized_unchanged_content_is_budget_safe(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", content="a" * 120_000 + "\n")
    assert out["ok"] is True
    assert out["changed"] is False
    assert "txt" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_styling_warnings_capped_like_upstream(tmp_path):
    """Upstream displays at most 50 unfixable issues plus an 'and N more' line;
    an unbounded warnings list could crowd the fixed text out of the budget."""
    _plant_upstream(tmp_path)
    content = "".join(f'a{i} = "odd b{i}\n' for i in range(80))
    out = fix_lint_tool(tmp_path, fixer="styling", content=content)
    assert out["ok"] is True
    assert len(out["warnings"]) == 51
    assert out["warnings"][49].startswith("line 50:")
    assert out["warnings"][50] == "... and 30 more"


# ---------------------------------------------------------------------------
# cleanup_or: pass order, line splitting, line endings, excluded paths
# ---------------------------------------------------------------------------


def test_cleanup_or_runs_or_pass_then_and_pass(tmp_path):
    _plant_upstream(tmp_path)

    out = fix_lint_tool(tmp_path, fixer="cleanup_or", content="OR1\n")

    assert out["ok"] is True
    assert out["changed"] is True
    assert out["txt"] == "done\n"
    # The AND detector sees the OR pass output, where OR1 became AND1.
    assert out["summary"] == {"or_blocks": 1, "and_blocks": 1}
    assert out["fixes"] == 2


def test_cleanup_or_reports_upstream_value_error(tmp_path):
    """Upstream raises ValueError on an unclosed `OR = {` at end of input."""
    _plant_upstream(tmp_path)

    out = fix_lint_tool(tmp_path, fixer="cleanup_or", content="UNCLOSED\n")

    assert out == {
        "ok": False,
        "fixer": "cleanup_or",
        "error": "upstream cleanup_or could not process the input: substring not found",
    }


def test_cleanup_or_unchanged_omits_txt(tmp_path):
    _plant_upstream(tmp_path)

    out = fix_lint_tool(tmp_path, fixer="cleanup_or", content="plain\n")

    assert out["ok"] is True
    assert out["changed"] is False
    assert out["fixes"] == 0
    assert "txt" not in out


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("a\x0cDROP\nb\n", None, id="form-feed-does-not-split"),
        pytest.param("a\x1cDROP\nb\n", None, id="file-separator-does-not-split"),
        pytest.param("a DROP\nb\n", None, id="line-separator-does-not-split"),
        pytest.param("a\r\nDROP\r\nb\r\n", "a\r\nb\r\n", id="crlf-is-kept"),
        pytest.param("a\r\nDROP\nb\rc\n", "a\r\nb\r\nc\r\n", id="mixed-with-crlf-becomes-crlf"),
        pytest.param("a\rDROP\rb\r", "a\nb\n", id="lone-cr-becomes-lf"),
        pytest.param("a\r\nb\nc\r", None, id="mixed-endings-nothing-to-do"),
        pytest.param("a\rb\r", None, id="lone-cr-nothing-to-do"),
    ],
)
def test_cleanup_or_matches_upstream_read_and_write(tmp_path, source, expected):
    """Upstream reads with universal newlines and writes only when the lines change,
    keeping CRLF when the file already had any (atomic_write_text)."""
    _plant_upstream(tmp_path)

    out = fix_lint_tool(tmp_path, fixer="cleanup_or", content=source)

    assert out["ok"] is True
    assert out["changed"] is (expected is not None)
    assert out.get("txt") == expected


def test_cleanup_or_path_source_keeps_crlf_and_never_writes(tmp_path):
    _plant_upstream(tmp_path)
    f = tmp_path / "common" / "x.txt"
    f.parent.mkdir()
    original = b"OR1\r\nDROP\r\n"
    f.write_bytes(original)

    out = fix_lint_tool(tmp_path, fixer="cleanup_or", path="common/x.txt")

    assert out["ok"] is True
    assert out["source"] == "path"
    assert out["txt"] == "done\r\n"
    assert f.read_bytes() == original


@pytest.mark.parametrize(
    "rel",
    ["resources/x.txt", ".git/x.txt", ".claude/x.txt", "common/resources/x.txt"],
)
def test_cleanup_or_refuses_excluded_directories(tmp_path, rel):
    _plant_upstream(tmp_path)
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("OR1\n", encoding="utf-8")

    from_path = fix_lint_tool(tmp_path, fixer="cleanup_or", path=rel)
    from_content = fix_lint_tool(tmp_path, fixer="cleanup_or", path=rel, content="OR1\n")

    for out in (from_path, from_content):
        assert out["ok"] is False
        assert "excluded directory" in out["error"]
        assert "txt" not in out


def test_cleanup_or_exclusion_is_relative_to_mod_root_not_cwd(tmp_path, monkeypatch):
    """A mod under an ancestor named `resources`, and a cwd inside the mod's own
    `resources/`, must not change which mod-relative paths are excluded."""
    root = tmp_path / "resources" / "mod"
    _plant_upstream(root)
    (root / "resources").mkdir()
    monkeypatch.chdir(root / "resources")

    kept = fix_lint_tool(root, fixer="cleanup_or", path="common/x.txt", content="OR1\n")
    refused = fix_lint_tool(root, fixer="cleanup_or", path="resources/x.txt", content="OR1\n")

    assert kept["ok"] is True
    assert kept["txt"] == "done\n"
    assert refused["ok"] is False


@pytest.mark.parametrize(
    "module_source",
    [None, "import md_missing_dependency\n"],
    ids=["missing", "broken-import"],
)
def test_unusable_cleanup_or_does_not_break_other_fixers(tmp_path, module_source):
    _plant_upstream(tmp_path)
    module = tmp_path / "tools" / "cleanup_or.py"
    if module_source is None:
        module.unlink()
    else:
        module.write_text(module_source, encoding="utf-8")

    refused = fix_lint_tool(tmp_path, fixer="cleanup_or", content="OR1\n")
    styled = fix_lint_tool(tmp_path, fixer="styling", content="a XX b\n")

    assert refused["ok"] is False
    assert "not found" in refused["error"]
    assert styled["ok"] is True
    assert styled["txt"] == "a YY b\n"


def test_cleanup_or_truncation_clips_txt(tmp_path):
    _plant_upstream(tmp_path)

    out = fix_lint_tool(tmp_path, fixer="cleanup_or", content="OR1\n" + "a" * 120_000 + "\n")

    assert out["ok"] is True
    assert out["txt_truncated"] is True
    assert out["txt_bytes"] > MAX_TEXT_BYTES >= out["txt_returned_bytes"]
    assert "do NOT write clipped content back" in out["note"]
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


# ---------------------------------------------------------------------------
# Integration: real upstream fixer cores, fixtures with known violations
# ---------------------------------------------------------------------------

_UPSTREAM_FILES = [
    # shared_utils loads this file while importing validate_style.
    Path("validation_config.json"),
    Path("tools") / "shared_utils.py",
    Path("tools") / "cleanup_or.py",
    Path("tools") / "linting" / "fix_styling.py",
    Path("tools") / "linting" / "fix_loc_yaml.py",
    Path("tools") / "linting" / "check_common_mistakes.py",
    Path("tools") / "linting" / "check_changelog.py",
    # fix_styling imports validate_style, which imports validator_common.
    Path("tools") / "validation" / "validate_style.py",
    Path("tools") / "validation" / "validator_common.py",
    Path("tools") / "validation" / "disk_cache.py",
]

_STYLING_VIOLATIONS = 'focus = {\n\tid = "TEST"\n    # === bad comment ===\n\ta = b   c =  d\n}\n'
_STYLING_FIXED = 'focus = {\n\tid = "TEST"\n    # === bad comment ===\n\ta = b c = d\n}\n'

_LOC_BODY = (
    "l_english:\n"
    ' TEST_KEY:0 "Version one"\n'
    '\tOTHER: "Tabbed"\n'
    ' QUOTE:"No space"\n'
    '  INDENT:  "Two spaces"\n'
    "# smart “quote” comment\n"
)
_LOC_FIXED = (
    "l_english:\n"
    ' TEST_KEY: "Version one"\n'
    ' OTHER: "Tabbed"\n'
    ' QUOTE: "No space"\n'
    ' INDENT:  "Two spaces"\n'
    '# smart "quote" comment\n'
)

_FOCUS_LOG_VIOLATIONS = 'focus = {\n\tid = REAL_ID\n\tlog = "...Focus WRONG_ID"\n}\n'
_FOCUS_LOG_FIXED = 'focus = {\n\tid = REAL_ID\n\tlog = "...Focus REAL_ID"\n}\n'

_DECISION_LOG_VIOLATIONS = (
    'my_category = {\n\tmy_decision = {\n\t\tlog = "...Decision WRONG_ID"\n\t}\n}\n'
)
_DECISION_LOG_FIXED = (
    'my_category = {\n\tmy_decision = {\n\t\tlog = "...Decision my_decision"\n\t}\n}\n'
)


@pytest.fixture
def upstream_root(tmp_path) -> Path:
    """Copy real fixer modules and their config dependency into a tmp mod root.

    A copy, not the real checkout: fixtures with known violations need to be
    written under mod_root, and newer shared_utils loads validation_config.json
    during imports. The real tree must never be touched. Skipped (like the
    other integration fixtures) without MD_MOD_ROOT.
    """
    src_root = os.environ.get("MD_MOD_ROOT")
    if src_root is None or not (Path(src_root) / "descriptor.mod").exists():
        pytest.skip("MD_MOD_ROOT not set to a mod checkout")
    for rel in _UPSTREAM_FILES:
        src = Path(src_root) / rel
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    return tmp_path


def _write(root: Path, rel: str, content: str | bytes) -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        f.write_bytes(content)
    else:
        f.write_text(content, encoding="utf-8")


@pytest.mark.integration
def test_integration_styling_roundtrip(upstream_root):
    rel = "common/national_focus/styling_fixture.txt"
    _write(upstream_root, rel, _STYLING_VIOLATIONS)

    out = fix_lint_tool(upstream_root, fixer="styling", path=rel)
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["fixes"] == 1
    assert out["txt"] == _STYLING_FIXED

    # The server never writes, so the fixpoint check feeds txt back in.
    again = fix_lint_tool(upstream_root, fixer="styling", content=out["txt"])
    assert again["changed"] is False
    assert again["fixes"] == 0
    assert "txt" not in again

    assert (upstream_root / rel).read_text(encoding="utf-8") == _STYLING_VIOLATIONS


@pytest.mark.integration
def test_integration_changelog_lint_and_fix_roundtrip(upstream_root):
    from md_mcp.tools.linting_tools import lint_changelog_tool

    source = "v1.0.0\n\nFeatures:\n" "- [USA] tagged entry\n" "- untagged entry\n"
    _write(upstream_root, "Changelog.txt", source)

    lint = lint_changelog_tool(upstream_root)
    fixed = fix_lint_tool(upstream_root, fixer="changelog", path="Changelog.txt")

    assert lint["ok"] is True
    assert lint["total"] >= 1
    assert fixed["ok"] is True
    assert fixed["changed"] is True
    assert fixed["fixes"] == 1
    assert (upstream_root / "Changelog.txt").read_text(encoding="utf-8") == source
    assert lint_changelog_tool(upstream_root)["total"] == lint["total"]


@pytest.mark.integration
def test_integration_loc_yaml_roundtrip(upstream_root):
    rel = "localisation/english/loc_fixture.yml"
    _write(upstream_root, rel, "\ufeff" + _LOC_BODY)

    out = fix_lint_tool(upstream_root, fixer="loc_yaml", path=rel)
    assert out["ok"] is True
    assert out["had_bom"] is True
    assert out["changed"] is True
    assert out["fixes"] == 5
    assert out["summary"] == {
        "version_key": 1,
        "tab": 1,
        "colon_space": 1,
        "indent": 1,
        "smart_quote": 1,
    }
    assert out["txt"] == _LOC_FIXED

    # Fixpoint check via content=: content carries no BOM, so had_bom is absent.
    again = fix_lint_tool(upstream_root, fixer="loc_yaml", content=out["txt"])
    assert again["changed"] is False
    assert again["fixes"] == 0


@pytest.mark.integration
def test_integration_line_endings_roundtrip(upstream_root):
    rel = "common/national_focus/crlf_fixture.txt"
    _write(upstream_root, rel, b"focus = {\r\n\tid = TEST\r\n}\r\n")

    out = fix_lint_tool(upstream_root, fixer="line_endings", path=rel)
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["fixes"] == 3
    assert out["txt"] == "focus = {\n\tid = TEST\n}\n"

    again = fix_lint_tool(upstream_root, fixer="line_endings", content=out["txt"])
    assert again["changed"] is False
    assert again["fixes"] == 0


@pytest.mark.integration
def test_integration_focus_log_id_roundtrip(upstream_root):
    rel = "common/national_focus/log_fixture.txt"
    _write(upstream_root, rel, _FOCUS_LOG_VIOLATIONS)

    out = fix_lint_tool(upstream_root, fixer="log_ids", path=rel)
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["summary"] == {"focus_log_ids": 1}
    assert out["txt"] == _FOCUS_LOG_FIXED

    # log_ids needs the path for scope detection even in content mode.
    again = fix_lint_tool(upstream_root, fixer="log_ids", path=rel, content=out["txt"])
    assert again["changed"] is False
    assert again["summary"] == {"focus_log_ids": 0}


@pytest.mark.integration
def test_integration_decision_log_id_roundtrip(upstream_root):
    rel = "common/decisions/log_fixture.txt"
    _write(upstream_root, rel, _DECISION_LOG_VIOLATIONS)

    out = fix_lint_tool(upstream_root, fixer="log_ids", path=rel)
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["summary"] == {"decision_log_ids": 1}
    assert out["txt"] == _DECISION_LOG_FIXED

    again = fix_lint_tool(upstream_root, fixer="log_ids", path=rel, content=out["txt"])
    assert again["changed"] is False
    assert again["summary"] == {"decision_log_ids": 0}


@pytest.mark.integration
def test_integration_all_fixers_at_max_txt_bytes_are_clipped(upstream_root):
    """A fixer output larger than the clip cap truncates txt and stays budgeted."""
    rel = "common/national_focus/big_styling_fixture.txt"
    # Upstream v3.0 only fixes spacing around '=' and braces, so the bulk
    # must be a genuine violation of that shape to change and clip.
    _write(upstream_root, rel, "a = b\n" + "\ta  = b\n" * 20_000)

    out = fix_lint_tool(upstream_root, fixer="styling", path=rel)
    assert out["ok"] is True
    assert out["txt_truncated"] is True
    assert out["txt_bytes"] > MAX_TEXT_BYTES >= out["txt_returned_bytes"]
    assert "do NOT write clipped content back" in out["note"]
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


_CLEANUP_OR_SIMPLIFIABLE = (
    "trigger = {\n"
    "\tOR = {\n"
    "\t\thas_idea = single_a\n"
    "\t}\n"
    "\tOR = { has_idea = inline_b }\n"
    "\tNOT = { OR = { has_idea = inline_c } }\n"
    "\tAND = {\n"
    "\t\thas_idea = promoted_d\n"
    "\t\thas_idea = promoted_e\n"
    "\t}\n"
    "\tOR = {\n"
    "\t\thas_idea = multi_f\n"
    "\t\thas_idea = multi_g\n"
    "\t}\n"
    "\tOR = {\n"
    "\t\tAND = {\n"
    "\t\t\thas_idea = kept_h\n"
    "\t\t\thas_idea = kept_i\n"
    "\t\t}\n"
    "\t\thas_idea = kept_j\n"
    "\t}\n"
    "\tNOT = {\n"
    "\t\tAND = {\n"
    "\t\t\thas_idea = kept_k\n"
    "\t\t\thas_idea = kept_l\n"
    "\t\t}\n"
    "\t}\n"
    "\tOR = {\n"
    "\t\tAND = {\n"
    "\t\t\thas_idea = chain_m\n"
    "\t\t\thas_idea = chain_n\n"
    "\t\t}\n"
    "\t}\n"
    "}\n"
)
_CLEANUP_OR_SIMPLIFIED = (
    "trigger = {\n"
    "\thas_idea = single_a\n"
    "\thas_idea = inline_b\n"
    "\tNOT = { has_idea = inline_c }\n"
    "\thas_idea = promoted_d\n"
    "\thas_idea = promoted_e\n"
    "\tOR = {\n"
    "\t\thas_idea = multi_f\n"
    "\t\thas_idea = multi_g\n"
    "\t}\n"
    "\tOR = {\n"
    "\t\tAND = {\n"
    "\t\t\thas_idea = kept_h\n"
    "\t\t\thas_idea = kept_i\n"
    "\t\t}\n"
    "\t\thas_idea = kept_j\n"
    "\t}\n"
    "\tNOT = {\n"
    "\t\tAND = {\n"
    "\t\t\thas_idea = kept_k\n"
    "\t\t\thas_idea = kept_l\n"
    "\t\t}\n"
    "\t}\n"
    "\thas_idea = chain_m\n"
    "\thas_idea = chain_n\n"
    "}\n"
)
_CLEANUP_OR_CLEAN = (
    "trigger = {\n"
    "\tOR = {\n"
    "\t\thas_idea = multi_f\n"
    "\t\thas_idea = multi_g\n"
    "\t}\n"
    "\tNOT = {\n"
    "\t\tAND = {\n"
    "\t\t\thas_idea = kept_k\n"
    "\t\t\thas_idea = kept_l\n"
    "\t\t}\n"
    "\t}\n"
    "}\n"
)


def _with_newlines(text: str, style: str) -> bytes:
    endings = {"lf": ["\n"], "crlf": ["\r\n"], "cr": ["\r"], "mixed": ["\r\n", "\n", "\r"]}[style]
    lines = text.split("\n")[:-1]
    return "".join(line + endings[i % len(endings)] for i, line in enumerate(lines)).encode()


@pytest.mark.integration
def test_integration_cleanup_or_simplifies_each_wrapper_kind(upstream_root):
    out = fix_lint_tool(upstream_root, fixer="cleanup_or", content=_CLEANUP_OR_SIMPLIFIABLE)

    assert out["ok"] is True
    assert out["changed"] is True
    assert out["txt"] == _CLEANUP_OR_SIMPLIFIED
    assert out["summary"] == {"or_blocks": 4, "and_blocks": 2}
    assert out["fixes"] == 6

    again = fix_lint_tool(upstream_root, fixer="cleanup_or", content=out["txt"])
    assert again["changed"] is False
    assert "txt" not in again


@pytest.mark.integration
@pytest.mark.parametrize("style", ["lf", "crlf", "cr", "mixed"])
@pytest.mark.parametrize(
    ("source", "expect_changed"),
    [(_CLEANUP_OR_SIMPLIFIABLE, True), (_CLEANUP_OR_CLEAN, False)],
    ids=["simplifiable", "clean"],
)
def test_integration_cleanup_or_matches_upstream_process_file(
    upstream_root, source, expect_changed, style
):
    raw = _with_newlines(source, style)
    _write(upstream_root, "common/mine.txt", raw)
    _write(upstream_root, "common/upstream.txt", raw)

    out = fix_lint_tool(upstream_root, fixer="cleanup_or", path="common/mine.txt")
    core = lint_fixers._UPSTREAM.load(upstream_root, ("cleanup_or",))["cleanup_or"]
    upstream_changed = core.process_file(str(upstream_root / "common" / "upstream.txt"))
    upstream_bytes = (upstream_root / "common" / "upstream.txt").read_bytes()

    assert upstream_changed is expect_changed
    assert out["ok"] is True
    assert out["changed"] is expect_changed
    if expect_changed:
        assert out["txt"].encode("utf-8") == upstream_bytes
    else:
        assert "txt" not in out
        assert upstream_bytes == raw
    assert (upstream_root / "common" / "mine.txt").read_bytes() == raw


@pytest.mark.integration
def test_integration_cleanup_or_refuses_excluded_paths_relative_to_mod_root(
    upstream_root, monkeypatch
):
    (upstream_root / "resources").mkdir()
    monkeypatch.chdir(upstream_root / "resources")

    refused = fix_lint_tool(
        upstream_root,
        fixer="cleanup_or",
        path="resources/x.txt",
        content=_CLEANUP_OR_SIMPLIFIABLE,
    )
    kept = fix_lint_tool(
        upstream_root,
        fixer="cleanup_or",
        path="common/x.txt",
        content=_CLEANUP_OR_SIMPLIFIABLE,
    )

    assert refused["ok"] is False
    assert "excluded directory" in refused["error"]
    assert kept["ok"] is True
    assert kept["txt"] == _CLEANUP_OR_SIMPLIFIED


def test_path_prefers_submod_copy_over_shadowed_base(tmp_path):
    base = tmp_path / "base"
    submod = tmp_path / "submod"
    _plant_upstream(base)
    rel = "common/national_focus/x.txt"
    for root, text in ((base, "a XX base\n"), (submod, "a XX overlay\n")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")

    out = fix_lint_tool(base, fixer="styling", path=rel, submod_root=submod)

    assert out["txt"] == "a YY overlay\n"


def test_path_reads_overlay_only_and_base_only_files(tmp_path):
    base = tmp_path / "base"
    submod = tmp_path / "submod"
    _plant_upstream(base)
    submod.mkdir()
    (base / "base_only.txt").write_text("XX base\n", encoding="utf-8")
    (submod / "overlay_only.txt").write_text("XX overlay\n", encoding="utf-8")

    overlay = fix_lint_tool(base, fixer="styling", path="overlay_only.txt", submod_root=submod)
    fallback = fix_lint_tool(base, fixer="styling", path="base_only.txt", submod_root=submod)

    assert overlay["txt"] == "YY overlay\n"
    assert fallback["txt"] == "YY base\n"


def test_content_ignores_submod_root(tmp_path):
    _plant_upstream(tmp_path)
    submod = tmp_path / "submod"
    submod.mkdir()

    out = fix_lint_tool(tmp_path, fixer="styling", content="a XX b\n", submod_root=submod)

    assert out["txt"] == "a YY b\n"


def _plant_event_log_detector(root: Path, findings: list[tuple[str, int]] | None = None) -> None:
    validation = root / "tools" / "validation"
    validation.mkdir(parents=True, exist_ok=True)
    (validation / "validate_events.py").write_text(
        "def find_option_logs_without_effects(text):\n" f"    return {(findings or [])!r}\n",
        encoding="utf-8",
    )


def test_event_option_logs_uses_detector_and_returns_in_memory_text(tmp_path):
    _plant_event_log_detector(tmp_path, [("unnamed option", 2)])
    source = 'option = {\n\tlog = "nothing happened"\n\tadd_political_power = 1\n}\n'
    path = tmp_path / "events" / "test.txt"
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8")

    result = fix_lint_tool(
        tmp_path, fixer="event_option_logs", path="events/test.txt", content=source
    )

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["fixes"] == 1
    assert result["summary"] == {"removed_log_lines": 1}
    assert result["txt"] == "option = {\n\tadd_political_power = 1\n}\n"
    assert path.read_text(encoding="utf-8") == source


def test_event_option_logs_refuses_out_of_scope_and_shared_source_line(tmp_path):
    _plant_event_log_detector(tmp_path, [("option", 1)])
    out_of_scope = fix_lint_tool(
        tmp_path, fixer="event_option_logs", path="common/test.txt", content='log = "x"\n'
    )
    unsafe = fix_lint_tool(
        tmp_path,
        fixer="event_option_logs",
        path="events/test.txt",
        content='log = "x" add_political_power = 1\n',
    )

    assert out_of_scope["ok"] is False
    assert unsafe["ok"] is False
    assert "shares a line" in unsafe["error"]


def test_event_option_logs_refuses_unquoted_value_with_closing_brace(tmp_path):
    _plant_event_log_detector(tmp_path, [("Test.1.a", 5)])
    source = (
        "country_event = {\n"
        "\tid = Test.1\n"
        "\toption = {\n"
        "\t\tname = Test.1.a\n"
        "\t\tlog = nothing}\n"
        "\t}\n"
        "}\n"
    )

    result = fix_lint_tool(
        tmp_path, fixer="event_option_logs", path="events/test.txt", content=source
    )

    assert result["ok"] is False
    assert "shares a line" in result["error"]
    assert "txt" not in result


@pytest.mark.parametrize("log_line", ['log = "nothing happened"\n', "log = nothing_happened\n"])
def test_event_option_logs_accepts_standalone_quoted_and_unquoted_values(tmp_path, log_line):
    _plant_event_log_detector(tmp_path, [("option", 1)])

    result = fix_lint_tool(
        tmp_path, fixer="event_option_logs", path="events/test.txt", content=log_line
    )

    assert result["ok"] is True
    assert result["txt"] == ""


@pytest.mark.integration
def test_event_option_logs_uses_real_upstream_detector(real_mod_root):
    source = (
        "country_event = {\n"
        "\tid = MCPFixture.1\n"
        "\toption = {\n"
        "\t\tname = MCPFixture.1.a\n"
        '\t\tlog = "no effect"\n'
        "\t}\n"
        "}\n"
    )
    result = fix_lint_tool(
        real_mod_root, fixer="event_option_logs", path="events/mcp_fixture.txt", content=source
    )

    assert result["ok"] is True
    assert result["changed"] is True
    assert "log =" not in result["txt"]
